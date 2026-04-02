from __future__ import annotations

import re
import time
from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable

import httpx

from app.domain.schemas import PaperResult, ProbeResult, QueryBundleItem, RetrievalTrace, SearchMode
from app.services.provider_runtime import ProviderRuntime
from config import get_settings


class BaseSourceClient(ABC):
    def __init__(self, name: str, settings: dict[str, Any]):
        self.name = name
        self.settings = settings
        app_settings = get_settings()
        http_settings = app_settings.get("http", {})
        self.http_settings = http_settings if isinstance(http_settings, dict) else {}
        self.timeout = float(settings.get("request_timeout_seconds", self.http_settings.get("request_timeout_seconds", 30)))
        self.connect_timeout = float(
            settings.get("connect_timeout_seconds", self.http_settings.get("connect_timeout_seconds", 10))
        )
        self.user_agent = str(self.http_settings.get("user_agent", "paper-search-agent/0.1"))
        self.runtime = ProviderRuntime(provider_name=name, settings=settings, http_settings=self.http_settings)

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("enabled", False))

    @property
    def public_enabled(self) -> bool:
        return bool(self.settings.get("public_enabled", False))

    def supports_mode(self, mode: str) -> bool:
        return bool(self.settings.get(f"supports_{mode}", False))

    def has_credentials(self) -> bool:
        for key in ("api_key", "email", "mailto", "plus_api_token", "app_key", "app_secret", "app_code", "session_cookie"):
            if self.settings.get(key):
                return True
        return False

    def build_client(self) -> httpx.AsyncClient:
        timeout = httpx.Timeout(self.timeout, connect=self.connect_timeout)
        headers = {"User-Agent": self.user_agent}
        return httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers)

    async def batch_search(
        self,
        mode: SearchMode,
        query_bundle: list[QueryBundleItem],
        limit: int = 5,
    ) -> list[PaperResult]:
        if mode == "deep":
            return await self.batch_deep_search(query_bundle, limit=limit)
        return await self.runtime.batch_results(query_bundle, limit, self.quick_search_item)

    async def batch_quick_search(self, queries: list[str], limit: int = 5) -> list[PaperResult]:
        query_bundle = [
            QueryBundleItem(label=f"quick-{index}", query=query, purpose="Quick search query")
            for index, query in enumerate(queries, start=1)
        ]
        return await self.runtime.batch_results(query_bundle, limit, self.quick_search_item)

    async def batch_deep_search(self, query_bundle: list[QueryBundleItem], limit: int = 5) -> list[PaperResult]:
        return await self.runtime.batch_results(query_bundle, limit, self.deep_search)

    def normalize_query(self, query: str) -> str:
        return " ".join((query or "").split()).strip()

    def render_query_for_mode(self, mode: SearchMode, query_item: QueryBundleItem) -> str:
        rendered = self.normalize_query(query_item.query)
        if mode != "deep":
            return rendered
        rendered = re.sub(r"[()]", " ", rendered)
        rendered = rendered.replace('"', " ")
        rendered = re.sub(r"\b(?:AND|OR|NOT)\b", " ", rendered, flags=re.IGNORECASE)
        rendered = re.sub(r"\s+", " ", rendered)
        return rendered.strip()

    def _attach_retrieval_trace(
        self,
        results: list[PaperResult],
        mode: SearchMode,
        query_item: QueryBundleItem,
        rendered_query: str,
    ) -> list[PaperResult]:
        trace = RetrievalTrace(
            mode=mode,
            query_label=query_item.label,
            query=query_item.query,
            rendered_query=rendered_query,
            purpose=query_item.purpose,
        )
        annotated: list[PaperResult] = []
        for result in results:
            enriched = result.model_copy(deep=True)
            enriched.retrieval_traces.append(trace)
            annotated.append(enriched)
        return annotated

    async def execute_mode_search(
        self,
        mode: SearchMode,
        query_item: QueryBundleItem,
        limit: int,
        fetcher: Callable[[str, int], Awaitable[list[PaperResult]]],
    ) -> list[PaperResult]:
        rendered_query = self.render_query_for_mode(mode, query_item)
        normalized_limit = max(1, int(limit))
        results = await self.runtime.run_results_operation(
            operation=f"{mode}_search",
            cache_payload={
                "query_label": query_item.label,
                "query": query_item.query,
                "rendered_query": rendered_query,
                "limit": normalized_limit,
            },
            producer=lambda: fetcher(rendered_query, normalized_limit),
        )
        return self._attach_retrieval_trace(results, mode, query_item, rendered_query)

    async def execute_quick_search(
        self,
        query: str,
        limit: int,
        fetcher: Callable[[str, int], Awaitable[list[PaperResult]]],
    ) -> list[PaperResult]:
        return await self.execute_mode_search(
            "quick",
            QueryBundleItem(label="quick-query", query=query, purpose="Quick search query"),
            limit,
            fetcher,
        )

    async def execute_deep_search(
        self,
        query_item: QueryBundleItem,
        limit: int,
        fetcher: Callable[[str, int], Awaitable[list[PaperResult]]],
    ) -> list[PaperResult]:
        return await self.execute_mode_search("deep", query_item, limit, fetcher)

    async def quick_search_item(self, query_item: QueryBundleItem, limit: int = 5) -> list[PaperResult]:
        return await self.quick_search(query_item.query, limit=limit)

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        async with self.build_client() as client:
            return await self.runtime.request(client, method, url, params=params, headers=headers)

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        response = await self.request("GET", url, params=params, headers=headers)
        response.raise_for_status()
        return response.json()

    async def get_text(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> str:
        response = await self.request("GET", url, params=params, headers=headers)
        response.raise_for_status()
        return response.text

    async def probe(self) -> ProbeResult:
        if not self.enabled:
            return ProbeResult(
                name=self.name,
                status="disabled",
                message="provider disabled in config",
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

    @abstractmethod
    async def quick_search(self, query: str, limit: int = 5) -> list[PaperResult]:
        raise NotImplementedError

    @abstractmethod
    async def deep_search(self, query_item: QueryBundleItem, limit: int = 5) -> list[PaperResult]:
        raise NotImplementedError
