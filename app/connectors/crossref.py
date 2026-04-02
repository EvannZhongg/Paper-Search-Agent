from __future__ import annotations

import html
import re
import time
from typing import Any

import httpx

from app.connectors.base import BaseSourceClient
from app.domain.schemas import PaperResult, ProbeResult, QueryBundleItem, SearchMode


_TAG_PATTERN = re.compile(r"<[^>]+>")


def _extract_year(*date_candidates: object) -> int | None:
    for candidate in date_candidates:
        if not isinstance(candidate, dict):
            continue
        date_parts = candidate.get("date-parts")
        if not isinstance(date_parts, list) or not date_parts:
            continue
        first = date_parts[0]
        if not isinstance(first, list) or not first:
            continue
        year = first[0]
        if str(year).isdigit():
            return int(year)
    return None


def _flatten_title(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        for item in value:
            text = str(item or "").strip()
            if text:
                return text
    return ""


def _strip_jats(text: str | None) -> str | None:
    if not text:
        return None
    cleaned = html.unescape(_TAG_PATTERN.sub(" ", text))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or None


def _build_author_name(author: dict[str, Any]) -> str | None:
    given = str(author.get("given") or "").strip()
    family = str(author.get("family") or "").strip()
    name = " ".join(part for part in [given, family] if part)
    if name:
        return name
    literal = str(author.get("name") or "").strip()
    return literal or None


def _string_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


class CrossrefClient(BaseSourceClient):
    def render_query_for_mode(self, mode: SearchMode, query_item: QueryBundleItem) -> str:
        rendered = super().render_query_for_mode(mode, query_item)
        if mode != "deep":
            return rendered
        if query_item.label.startswith("criterion-"):
            return rendered
        return " ".join(rendered.split()[:12]).strip()

    async def probe(self) -> ProbeResult:
        if not self.enabled:
            return ProbeResult(
                name=self.name,
                status="disabled",
                message="provider disabled in config",
                used_credentials=self.has_credentials(),
            )

        try:
            self._require_mailto()
        except RuntimeError as exc:
            return ProbeResult(
                name=self.name,
                status="error",
                message=str(exc),
                used_credentials=self.has_credentials(),
            )

        start = time.perf_counter()
        try:
            sample = await self.quick_search("transformer", limit=1)
            latency_ms = int((time.perf_counter() - start) * 1000)
            return ProbeResult(
                name=self.name,
                status="ok",
                message="probe succeeded",
                latency_ms=latency_ms,
                used_credentials=self.has_credentials(),
                sample_title=sample[0].title if sample else None,
            )
        except httpx.HTTPStatusError as exc:
            latency_ms = int((time.perf_counter() - start) * 1000)
            return ProbeResult(
                name=self.name,
                status="error",
                message=str(exc),
                http_status=exc.response.status_code,
                latency_ms=latency_ms,
                used_credentials=self.has_credentials(),
            )
        except Exception as exc:
            latency_ms = int((time.perf_counter() - start) * 1000)
            return ProbeResult(
                name=self.name,
                status="error",
                message=str(exc),
                latency_ms=latency_ms,
                used_credentials=self.has_credentials(),
            )

    async def quick_search(self, query: str, limit: int = 5) -> list[PaperResult]:
        async def fetch(normalized_query: str, normalized_limit: int) -> list[PaperResult]:
            payload = await self.get_json(
                self._works_url(),
                params=self._build_search_params(normalized_query, normalized_limit),
                headers=self._build_headers(),
            )
            return self._parse_results(payload, normalized_limit)

        return await self.execute_quick_search(query, limit, fetch)

    async def deep_search(self, query_item: QueryBundleItem, limit: int = 5) -> list[PaperResult]:
        async def fetch(rendered_query: str, normalized_limit: int) -> list[PaperResult]:
            payload = await self.get_json(
                self._works_url(),
                params=self._build_search_params(rendered_query, normalized_limit),
                headers=self._build_headers(),
            )
            return self._parse_results(payload, normalized_limit)

        return await self.execute_deep_search(query_item, limit, fetch)

    def _works_url(self) -> str:
        return f"{self.settings['base_url']}{self.settings['works_path']}"

    def _require_mailto(self) -> str | None:
        mailto = str(self.settings.get("mailto") or "").strip()
        if mailto:
            return mailto
        if self.settings.get("require_mailto", True):
            raise RuntimeError("CROSSREF_MAILTO not configured")
        return None

    def _build_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        mailto = str(self.settings.get("mailto") or "").strip()
        if mailto:
            headers["User-Agent"] = f"{self.user_agent} (mailto:{mailto})"
        plus_api_token = str(self.settings.get("plus_api_token") or "").strip()
        if plus_api_token:
            headers["Crossref-Plus-API-Token"] = f"Bearer {plus_api_token}"
        return headers

    def _build_search_params(self, query: str, limit: int) -> dict[str, Any]:
        query_parameter = str(self.settings.get("query_parameter", "query.bibliographic")).strip() or "query.bibliographic"
        normalized_limit = max(1, int(limit))
        oversample_factor = max(1, int(self.settings.get("oversample_factor", 3) or 3))
        max_rows = max(1, int(self.settings.get("max_rows", 100) or 100))
        rows = min(max_rows, normalized_limit * oversample_factor)

        params: dict[str, Any] = {
            query_parameter: query,
            "rows": rows,
            "sort": str(self.settings.get("default_sort", "relevance")),
            "order": str(self.settings.get("default_order", "desc")),
        }
        mailto = self._require_mailto()
        if mailto:
            params["mailto"] = mailto

        filters = self.settings.get("default_filters", [])
        if isinstance(filters, str):
            filters = [filters]
        if isinstance(filters, list):
            filter_value = ",".join(str(item).strip() for item in filters if str(item).strip())
            if filter_value:
                params["filter"] = filter_value

        return params

    def _parse_results(self, payload: Any, limit: int) -> list[PaperResult]:
        message = payload.get("message", {}) if isinstance(payload, dict) else {}
        items = message.get("items", []) if isinstance(message, dict) else []
        if not isinstance(items, list):
            return []

        allowed_types = {
            item.lower()
            for item in _string_list(self.settings.get("allowed_types"))
        }

        results: list[PaperResult] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "").strip().lower()
            if allowed_types and item_type not in allowed_types:
                continue

            parsed = self._parse_item(item)
            if parsed is None:
                continue
            results.append(parsed)
            if len(results) >= limit:
                break

        return results

    def _parse_item(self, item: dict[str, Any]) -> PaperResult | None:
        title = _flatten_title(item.get("title"))
        doi = str(item.get("DOI") or "").strip() or None
        if not title and not doi:
            return None

        resource = item.get("resource") if isinstance(item.get("resource"), dict) else {}
        primary_resource = resource.get("primary") if isinstance(resource.get("primary"), dict) else {}
        secondary_resources = resource.get("secondary") if isinstance(resource.get("secondary"), list) else []

        url = (
            str(primary_resource.get("URL") or "").strip()
            or str(item.get("URL") or "").strip()
            or None
        )
        pdf_url = self._extract_pdf_url(item.get("link"), secondary_resources, url)
        authors = [
            author_name
            for author_name in (
                _build_author_name(author)
                for author in (item.get("author") or [])
                if isinstance(author, dict)
            )
            if author_name
        ]
        year = _extract_year(
            item.get("published"),
            item.get("published-print"),
            item.get("published-online"),
            item.get("issued"),
            item.get("created"),
        )

        return PaperResult(
            source=self.name,
            source_id=doi,
            title=title,
            abstract=_strip_jats(item.get("abstract")),
            year=year,
            doi=doi,
            url=url,
            pdf_url=pdf_url,
            is_oa=None,
            authors=authors,
            raw=item,
        )

    def _extract_pdf_url(self, links: object, secondary_resources: list[Any], fallback_url: str | None) -> str | None:
        if isinstance(links, list):
            for link in links:
                if not isinstance(link, dict):
                    continue
                candidate = str(link.get("URL") or "").strip()
                content_type = str(link.get("content-type") or "").strip().lower()
                if candidate and (content_type == "application/pdf" or candidate.lower().endswith(".pdf")):
                    return candidate

        for item in secondary_resources:
            if not isinstance(item, dict):
                continue
            candidate = str(item.get("URL") or "").strip()
            if candidate.lower().endswith(".pdf"):
                return candidate

        if fallback_url and fallback_url.lower().endswith(".pdf"):
            return fallback_url
        return None
