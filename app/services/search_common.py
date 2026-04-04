from __future__ import annotations

import asyncio
import math
import re
import time
import unicodedata
from datetime import datetime
from typing import Any, Iterable

from app.domain.schemas import (
    CriterionJudgment,
    PaperResult,
    QueryBundleItem,
    RetrievalTrace,
    SearchCriterion,
    SearchIntent,
    SearchRequest,
)
from app.llm import LLMClient
from app.prompts import INTENT_PLANNER_SYSTEM_PROMPT, INTENT_PLANNER_USER_PROMPT, render_prompt
from app.services.provider_registry import get_clients_for_mode
from config import get_settings


TOKEN_PATTERN = re.compile(r"[A-Za-z0-9]+|[\u3400-\u4DBF\u4E00-\u9FFF]+")
CONJUNCTION_PATTERN = re.compile(
    r"\b(?:and|plus|with|along with|together with|combined with|hybrid|fusion|joint)\b|和|与|及|以及|结合|联合|融合|混合",
    re.IGNORECASE,
)
DISJUNCTION_PATTERN = re.compile(
    r"\b(?:or|either|any of|one of)\b|或者|或|抑或|其一|任一|任选",
    re.IGNORECASE,
)
COMBINATION_HINT_PATTERN = re.compile(
    r"\b(?:combine|combined|combining|hybrid|fusion|mixed|integrated|joint)\b|结合|联合|融合|混合",
    re.IGNORECASE,
)
INSTRUCTIONAL_HINT_PREFIX_PATTERN = re.compile(
    r"^(?:look\s+for|search\s+for|include|prioritize|prefer|focus\s+on|find|identify|select|return)\s+",
    re.IGNORECASE,
)
TRAILING_HINT_CLAUSE_PATTERN = re.compile(
    r"\b(?:rather\s+than|instead\s+of|not\s+just|not\s+only|without)\b.*$",
    re.IGNORECASE,
)
QUERY_HINT_NOISE_PATTERN = re.compile(
    r"\b(?:also\s+try|related\s+term(?:s)?|search\s+for|look\s+for|find(?:\s+papers?\s+about)?|"
    r"include|prioritize|prefer|focus\s+on|identify|select|return|show\s+me|"
    r"papers?|research(?:\s+papers?)?|literature)\b",
    re.IGNORECASE,
)
GENERIC_FRAGMENT_PREFIX_PATTERN = re.compile(
    r"^(?:papers?|research(?:\s+papers?)?|find(?:\s+some)?|looking\s+for|show\s+me|find\s+papers\s+about)\s+",
    re.IGNORECASE,
)
GENERIC_FRAGMENT_SUFFIX_PATTERN = re.compile(
    r"\s+(?:papers?|research(?:\s+papers?)?)$",
    re.IGNORECASE,
)
GENERIC_CHINESE_PREFIXES = ("找一些", "找一下", "找找", "找", "有关", "相关", "关于", "帮我找")
GENERIC_CHINESE_SUFFIXES = ("的论文", "论文", "文献", "相关文章")
COMBINATION_TERMS = [
    "combine",
    "combined",
    "combining",
    "hybrid",
    "fusion",
    "mixed",
    "integrated",
    "joint",
]
QUERY_HINT_STOP_TOKENS = {
    "a",
    "about",
    "also",
    "an",
    "and",
    "find",
    "for",
    "identify",
    "include",
    "literature",
    "look",
    "not",
    "of",
    "or",
    "paper",
    "papers",
    "prefer",
    "prioritize",
    "related",
    "research",
    "return",
    "search",
    "select",
    "show",
    "term",
    "terms",
    "the",
    "to",
    "try",
}


def get_retrieval_settings() -> dict[str, Any]:
    settings = get_settings().get("retrieval", {})
    return settings if isinstance(settings, dict) else {}


def get_channel_settings(channel: str) -> dict[str, Any]:
    retrieval_settings = get_retrieval_settings()
    channel_settings = retrieval_settings.get(channel, {})
    return channel_settings if isinstance(channel_settings, dict) else {}


def elapsed_ms(started_at: float) -> float:
    return max(0.0, (time.perf_counter() - started_at) * 1000.0)


def add_timing_ms(timings: dict[str, float], key: str, value: float) -> None:
    timings[key] = timings.get(key, 0.0) + max(0.0, value)


def max_timing_ms(timings: dict[str, float], key: str, value: float) -> None:
    timings[key] = max(timings.get(key, 0.0), max(0.0, value))


def finalize_timings_ms(timings: dict[str, float]) -> dict[str, float]:
    return {key: round(value, 2) for key, value in timings.items()}


def resolve_limit_per_source(mode: str, request: SearchRequest) -> int:
    if request.limit_per_source is not None:
        return max(1, int(request.limit_per_source))

    channel_settings = get_channel_settings(mode)
    configured = channel_settings.get("limit_per_source_default")
    if configured is not None:
        try:
            return max(1, int(configured))
        except (TypeError, ValueError):
            pass

    return 5


def unique_preserve_order(values: Iterable[str]) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    for raw_value in values:
        value = str(raw_value or "").strip()
        if not value:
            continue
        if value in seen:
            continue
        seen.add(value)
        items.append(value)
    return items


def normalize_phrase(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text or "").lower()
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def _expand_cjk_token(token: str) -> list[str]:
    cleaned = token.strip()
    if not cleaned:
        return []
    pieces = [cleaned]
    if len(cleaned) > 1:
        pieces.extend(cleaned[index : index + 2] for index in range(len(cleaned) - 1))
    return pieces


def normalize_text(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text or "").lower()
    tokens: list[str] = []
    for match in TOKEN_PATTERN.finditer(normalized):
        token = match.group(0).strip()
        if not token:
            continue
        if re.fullmatch(r"[\u3400-\u4DBF\u4E00-\u9FFF]+", token):
            tokens.extend(_expand_cjk_token(token))
            continue
        if len(token) > 1:
            tokens.append(token)
    return unique_preserve_order(tokens)


def normalize_doi(doi: str | None) -> str | None:
    if not doi:
        return None

    normalized = doi.strip().lower()
    normalized = re.sub(r"^https?://(dx\.)?doi\.org/", "", normalized)
    normalized = re.sub(r"^doi:\s*", "", normalized)
    normalized = normalized.strip().strip("/")
    return normalized or None


def build_document_text(result: PaperResult) -> str:
    parts = [
        result.title or "",
        result.abstract or "",
        " ".join(result.authors[:8]),
        result.doi or "",
    ]
    return "\n".join(part.strip() for part in parts if part and part.strip())


def current_year() -> int:
    return datetime.now().year


def clamp_score(value: float) -> float:
    return max(0.0, min(1.0, value))


def compute_recency_score(year: int | None, window_years: int = 10) -> float:
    if not year:
        return 0.0

    max_year = current_year()
    min_year = max_year - max(1, window_years)
    if year <= min_year:
        return 0.0
    if year >= max_year:
        return 1.0
    return clamp_score((year - min_year) / max(max_year - min_year, 1))


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0

    numerator = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return clamp_score((numerator / (left_norm * right_norm) + 1.0) / 2.0)


def _slugify_identifier(text: str, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", normalize_phrase(text))
    slug = slug.strip("_")
    return slug or fallback


def _extract_initialism(text: str) -> str:
    tokens = [token for token in normalize_text(text) if token.isalpha()]
    if len(tokens) < 2:
        return ""
    return "".join(token[0] for token in tokens if token)


def _clean_fragment(fragment: str) -> str:
    cleaned = normalize_phrase(fragment)
    cleaned = GENERIC_FRAGMENT_PREFIX_PATTERN.sub("", cleaned)
    cleaned = GENERIC_FRAGMENT_SUFFIX_PATTERN.sub("", cleaned)

    for prefix in GENERIC_CHINESE_PREFIXES:
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :].strip()
    for suffix in GENERIC_CHINESE_SUFFIXES:
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)].strip()

    cleaned = re.sub(r"^[的相与和及并]+", "", cleaned)
    cleaned = re.sub(r"[的相与和及并]+$", "", cleaned)
    return cleaned.strip(" ,.:;!?\"'()[]{}")


def extract_planning_terms(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text or "").lower()
    terms: list[str] = []
    for match in TOKEN_PATTERN.finditer(normalized):
        token = match.group(0).strip()
        if not token:
            continue
        if re.fullmatch(r"[\u3400-\u4DBF\u4E00-\u9FFF]+", token):
            cleaned = _clean_fragment(token)
            if len(cleaned) > 1:
                terms.append(cleaned)
            continue
        if len(token) > 1:
            terms.append(token)
    return unique_preserve_order(terms)


def _infer_logic_from_query(query: str) -> str:
    if DISJUNCTION_PATTERN.search(query or ""):
        return "OR"
    return "AND"


def _split_query_fragments(query: str, logic: str = "AND") -> list[str]:
    splitter = DISJUNCTION_PATTERN if logic == "OR" else CONJUNCTION_PATTERN
    fragments = [_clean_fragment(part) for part in splitter.split(query or "")]
    return [fragment for fragment in fragments if fragment and len(fragment) > 1]


def _is_combination_criterion(criterion: SearchCriterion) -> bool:
    haystack = " ".join([criterion.description, *criterion.terms, *criterion.query_hints])
    return bool(COMBINATION_HINT_PATTERN.search(haystack))


def _trim_instructional_hint(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text or "")
    previous = None
    while normalized != previous:
        previous = normalized
        normalized = QUERY_HINT_NOISE_PATTERN.sub(" ", normalized)
        normalized = INSTRUCTIONAL_HINT_PREFIX_PATTERN.sub("", normalized)
        normalized = TRAILING_HINT_CLAUSE_PATTERN.sub("", normalized)
        normalized = re.sub(r"\b(?:and|or|not)\b", " ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"[(){}\[\],;:]+", " ", normalized)
        normalized = re.sub(r"\s+", " ", normalized)
        normalized = normalized.strip(" .,:;!?\"'")
    return normalize_phrase(normalized)


def _sanitize_query_hint(text: str, *, max_words: int = 4) -> str | None:
    trimmed = _trim_instructional_hint(text)
    if not trimmed:
        return None

    filtered_tokens = [token for token in normalize_text(trimmed) if token not in QUERY_HINT_STOP_TOKENS]
    filtered_tokens = unique_preserve_order(filtered_tokens)
    if not filtered_tokens or len(filtered_tokens) > max_words:
        return None
    return " ".join(filtered_tokens)


def _compose_query_hint(parts: Iterable[str], *, max_words: int = 4) -> str | None:
    merged_tokens: list[str] = []
    for part in parts:
        sanitized = _sanitize_query_hint(part, max_words=max_words)
        if not sanitized:
            continue
        part_tokens = normalize_text(sanitized)
        if not part_tokens:
            continue
        if len(merged_tokens) + len(part_tokens) > max_words:
            break
        merged_tokens.extend(part_tokens)

    merged_tokens = unique_preserve_order(merged_tokens)
    if not merged_tokens:
        return None
    return " ".join(merged_tokens)


def _is_provider_friendly_hint(text: str) -> bool:
    return _sanitize_query_hint(text) is not None


def _extract_description_search_phrases(description: str) -> list[str]:
    lowered = normalize_phrase(description)
    phrases: list[str] = []

    if "retrieval-augmented generation" in lowered or re.search(r"\brag\b", lowered):
        phrases.append("retrieval-augmented generation")
    if "large language model" in lowered or re.search(r"\bllm\b", lowered):
        phrases.append("LLM")
    if "document" in lowered:
        phrases.append("document retrieval")
    if "passage" in lowered:
        phrases.append("passage retrieval")
    if "text" in lowered or "unstructured" in lowered:
        phrases.append("text retrieval")
    if "dense" in lowered:
        phrases.append("dense retrieval")
    if "bm25" in lowered:
        phrases.append("BM25")
    if "graph" in lowered:
        phrases.append("graph retrieval")
    if "knowledge graph" in lowered or re.search(r"\bkg\b", lowered):
        phrases.append("knowledge graph")
    if "graphrag" in lowered:
        phrases.append("GraphRAG")
    if re.search(r"\b(?:hybrid|fusion|combine|combined|joint|integrat|coordinated)\b", lowered):
        phrases.append("hybrid retrieval")

    return unique_preserve_order(phrases)


def _build_provider_friendly_query_hints(criterion: SearchCriterion) -> list[str]:
    terms = unique_preserve_order(term.strip().strip(".") for term in criterion.terms if term.strip())
    cleaned_existing = unique_preserve_order(
        sanitized
        for hint in criterion.query_hints
        if (sanitized := _sanitize_query_hint(hint))
    )
    description_phrases = unique_preserve_order(
        sanitized
        for phrase in _extract_description_search_phrases(criterion.description)
        if (sanitized := _sanitize_query_hint(phrase))
    )

    candidates: list[str] = []
    if _is_combination_criterion(criterion):
        combination_parts: list[str] = []
        haystack = normalize_phrase(" ".join([criterion.description, *terms, *criterion.query_hints]))
        if any(keyword in haystack for keyword in ("text", "document", "passage")):
            combination_parts.append("text")
        if "graph" in haystack or "knowledge graph" in haystack:
            combination_parts.append("graph")
        if any(keyword in haystack for keyword in ("hybrid", "fusion", "combine", "combined", "joint", "integrat", "mixed")):
            combination_parts.append("hybrid")
        if any(keyword in haystack for keyword in ("retrieval", "rag", "graphrag", "bm25", "dense")):
            combination_parts.append("retrieval")

        primary_combination = _compose_query_hint(combination_parts)
        if primary_combination:
            candidates.append(primary_combination)
    else:
        primary_terms_hint = _compose_query_hint(terms)
        if primary_terms_hint:
            candidates.append(primary_terms_hint)

    candidates.extend(cleaned_existing)
    candidates.extend(
        sanitized
        for term in terms
        if (sanitized := _sanitize_query_hint(term))
    )
    candidates.extend(description_phrases)

    fallback_description_hint = _sanitize_query_hint(criterion.description)
    if fallback_description_hint:
        candidates.append(fallback_description_hint)

    return unique_preserve_order(candidate for candidate in candidates if candidate)[:3]


def finalize_criteria_for_search(criteria: list[SearchCriterion]) -> list[SearchCriterion]:
    finalized: list[SearchCriterion] = []
    for criterion in criteria:
        finalized.append(
            criterion.model_copy(
                update={
                    "query_hints": _build_provider_friendly_query_hints(criterion),
                }
            )
        )
    return finalized


def _active_criteria(criteria: list[SearchCriterion]) -> list[SearchCriterion]:
    required = [criterion for criterion in criteria if criterion.required]
    return required or criteria


def resolve_criterion_supported_threshold(criterion: SearchCriterion, settings: dict[str, Any] | None = None) -> float:
    settings = settings or {}
    if criterion.required:
        default_threshold = float(settings.get("criterion_supported_threshold", 0.45))
        if _is_combination_criterion(criterion):
            return float(settings.get("criterion_combination_supported_threshold", max(default_threshold, 0.65)))
        return default_threshold
    return float(settings.get("criterion_optional_supported_threshold", 0.3))


def _merge_related_terms(terms: list[str]) -> list[list[str]]:
    groups: list[list[str]] = []
    for term in unique_preserve_order(terms):
        term_initialism = _extract_initialism(term)
        matched_group: list[str] | None = None
        for group in groups:
            for existing in group:
                existing_initialism = _extract_initialism(existing)
                if term.lower() == existing.lower():
                    matched_group = group
                    break
                if term.isascii() and term.isalpha():
                    if term.lower() == existing_initialism or term.lower() == _extract_initialism(existing):
                        matched_group = group
                        break
                if existing.isascii() and existing.isalpha():
                    if existing.lower() == term_initialism:
                        matched_group = group
                        break
            if matched_group is not None:
                break

        if matched_group is None:
            groups.append([term])
        else:
            matched_group.append(term)

    return [unique_preserve_order(group) for group in groups]


def build_default_criteria(
    query: str,
    rewritten_query: str,
    must_terms: list[str],
    should_terms: list[str],
    logic: str = "AND",
) -> list[SearchCriterion]:
    criteria: list[SearchCriterion] = []
    fragments = _split_query_fragments(rewritten_query or query, logic=logic)

    if len(fragments) >= 2:
        for index, fragment in enumerate(fragments[:4], start=1):
            criteria.append(
                SearchCriterion(
                    id=_slugify_identifier(fragment, f"criterion_{index}"),
                    description=f"The paper discusses {fragment}.",
                    required=True,
                    terms=[fragment],
                    query_hints=[fragment],
                )
            )

        if logic == "AND" and COMBINATION_HINT_PATTERN.search(rewritten_query or query):
            fragment_text = " and ".join(fragments[:2])
            criteria.append(
                SearchCriterion(
                    id="combination",
                    description=f"The paper explicitly combines {fragment_text}.",
                    required=True,
                    terms=COMBINATION_TERMS,
                    query_hints=[f"{fragment_text} hybrid", f"{fragment_text} combined retrieval"],
                )
            )
        return criteria

    if must_terms:
        term_groups = _merge_related_terms(must_terms)
        for index, group in enumerate(term_groups, start=1):
            anchor = group[0]
            criteria.append(
                SearchCriterion(
                    id=_slugify_identifier(anchor, f"criterion_{index}"),
                    description=f"The paper discusses {anchor}.",
                    required=True,
                    terms=group,
                    query_hints=group,
                )
            )
        if criteria:
            return criteria

    fallback_query = rewritten_query.strip() or query.strip()
    return [
        SearchCriterion(
            id="topic_match",
            description=f"The paper matches the search topic: {fallback_query}.",
            required=True,
            terms=must_terms[:4] or normalize_text(fallback_query)[:6],
            query_hints=[fallback_query],
        )
    ]


def _coerce_bool(value: object, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "1"}:
            return True
        if lowered in {"false", "no", "0"}:
            return False
    return default


def _coerce_string_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _sanitize_logic(value: object) -> str:
    logic = str(value or "AND").strip().upper()
    return logic if logic in {"AND", "OR", "NOT"} else "AND"


def _normalize_criteria(
    raw_criteria: object,
    query: str,
    rewritten_query: str,
    must_terms: list[str],
    should_terms: list[str],
    logic: str,
) -> list[SearchCriterion]:
    normalized: list[SearchCriterion] = []
    if isinstance(raw_criteria, list):
        for index, item in enumerate(raw_criteria, start=1):
            if not isinstance(item, dict):
                continue
            description = str(item.get("description") or "").strip()
            terms = unique_preserve_order(_coerce_string_list(item.get("terms", [])))
            query_hints = unique_preserve_order(_coerce_string_list(item.get("query_hints", [])))
            anchor = description or (query_hints[0] if query_hints else terms[0] if terms else "")
            if not anchor:
                continue
            normalized.append(
                SearchCriterion(
                    id=_slugify_identifier(str(item.get("id") or anchor), f"criterion_{index}"),
                    description=description or f"The paper discusses {anchor}.",
                    required=_coerce_bool(item.get("required", True), default=True),
                    terms=terms,
                    query_hints=query_hints,
                )
            )

    if normalized:
        return normalized

    return build_default_criteria(query, rewritten_query, must_terms, should_terms, logic=logic)


def _ensure_unique_criterion_ids(criteria: list[SearchCriterion]) -> list[SearchCriterion]:
    seen: dict[str, int] = {}
    normalized: list[SearchCriterion] = []
    for criterion in criteria:
        base_id = criterion.id.strip() or "criterion"
        count = seen.get(base_id, 0)
        seen[base_id] = count + 1
        criterion_id = base_id if count == 0 else f"{base_id}_{count + 1}"
        normalized.append(criterion.model_copy(update={"id": criterion_id}))
    return normalized


def heuristic_plan_intent(query: str) -> SearchIntent:
    planning_terms = extract_planning_terms(query)
    cleaned_query = _clean_fragment(query) or query.strip()
    contains_cjk = bool(re.search(r"[\u3400-\u4DBF\u4E00-\u9FFF]", query or ""))
    rewritten_query = cleaned_query if contains_cjk else " ".join(planning_terms[:12]).strip() or cleaned_query
    must_terms = planning_terms[:4]
    should_terms = planning_terms[4:8]
    logic = _infer_logic_from_query(query)
    criteria = finalize_criteria_for_search(
        _ensure_unique_criterion_ids(
            build_default_criteria(query, rewritten_query, must_terms, should_terms, logic=logic)
        )
    )
    return SearchIntent(
        original_query=query,
        rewritten_query=rewritten_query,
        must_terms=must_terms,
        should_terms=should_terms,
        exclude_terms=[],
        filters={},
        logic=logic,
        criteria=criteria,
        planner="heuristic",
        reasoning="fallback heuristic planner used",
    )


async def plan_search_intent(query: str, request: SearchRequest) -> SearchIntent:
    if not request.enable_intent_planner:
        return heuristic_plan_intent(query)

    llm_client = LLMClient()
    if not (request.enable_llm and llm_client.is_configured()):
        return heuristic_plan_intent(query)

    user_prompt = render_prompt(INTENT_PLANNER_USER_PROMPT, query=query)
    try:
        payload = await llm_client.complete_json(
            system_prompt=INTENT_PLANNER_SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )
        rewritten_query = str(payload.get("rewritten_query") or query).strip() or query
        must_terms = unique_preserve_order(_coerce_string_list(payload.get("must_terms", [])))
        should_terms = unique_preserve_order(_coerce_string_list(payload.get("should_terms", [])))
        exclude_terms = unique_preserve_order(_coerce_string_list(payload.get("exclude_terms", [])))
        filters = payload.get("filters", {})
        if not isinstance(filters, dict):
            filters = {}
        inferred_logic = _infer_logic_from_query(query)
        logic = _sanitize_logic(payload.get("logic") or inferred_logic)
        criteria = finalize_criteria_for_search(
            _ensure_unique_criterion_ids(
                _normalize_criteria(
                    payload.get("criteria"),
                    query,
                    rewritten_query,
                    must_terms,
                    should_terms,
                    logic,
                )
            )
        )
        return SearchIntent(
            original_query=query,
            rewritten_query=rewritten_query,
            must_terms=must_terms,
            should_terms=should_terms,
            exclude_terms=exclude_terms,
            filters=filters,
            logic=logic,
            criteria=criteria,
            planner="llm",
            reasoning=str(payload.get("reasoning", "")).strip() or None,
        )
    except Exception:
        return heuristic_plan_intent(query)


def _criterion_representative_phrase(criterion: SearchCriterion) -> str:
    candidates = unique_preserve_order(
        [
            *(_sanitize_query_hint(hint) for hint in criterion.query_hints),
            *(_sanitize_query_hint(term) for term in criterion.terms),
        ]
    )
    if not candidates and criterion.description:
        fallback_hint = _sanitize_query_hint(criterion.description)
        candidates = [fallback_hint] if fallback_hint else [criterion.description]
    if not candidates:
        return criterion.id

    def rank(candidate: str) -> tuple[int, int, int]:
        normalized = candidate.strip()
        lowered = normalized.lower()
        semantic_bonus = 1 if any(term in lowered for term in ("rag", "graph", "knowledge", "text", "retrieval")) else 0
        operator_penalty = 2 if any(token in normalized.upper() for token in (" AND ", " OR ")) else 0
        operator_penalty += 1 if any(symbol in normalized for symbol in ('"', "(", ")")) else 0
        return (
            semantic_bonus,
            len(normalize_text(normalized)) - operator_penalty,
            len(normalized),
        )

    return max(
        candidates,
        key=rank,
    )


def _quote_fragment(fragment: str) -> str:
    normalized = re.sub(r"\s+", " ", fragment.strip())
    normalized = normalized.strip('"')
    if not normalized:
        return ""
    if len(normalize_text(normalized)) > 3:
        return normalized
    if any(token in normalized.upper() for token in (" AND ", " OR ")) or any(symbol in normalized for symbol in ("(", ")")):
        return normalized
    return f"\"{normalized}\"" if " " in normalized else normalized


def _build_compact_criterion_query(criteria: list[SearchCriterion], intent: SearchIntent) -> str:
    token_source: list[str] = []
    for criterion in criteria:
        token_source.extend(normalize_text(_criterion_representative_phrase(criterion)))
    token_source.extend(normalize_text(" ".join(intent.must_terms[:4])))
    compact_tokens = unique_preserve_order(token_source)[:8]
    return " ".join(compact_tokens).strip()


def _build_disjunctive_criterion_query(criteria: list[SearchCriterion]) -> str:
    fragments = [_quote_fragment(_criterion_representative_phrase(criterion)) for criterion in criteria]
    return " OR ".join(fragment for fragment in fragments if fragment).strip()


def _build_bundle_limit(mode: str, channel_settings: dict[str, Any], intent: SearchIntent) -> int:
    base_limit = max(1, int(channel_settings.get("max_query_variants", 1) or 1))
    if mode != "deep":
        return base_limit

    required_count = max(1, len(_active_criteria(intent.criteria)))
    complexity_bonus_cap = max(0, int(channel_settings.get("max_query_variants_complexity_bonus", 3) or 3))
    dynamic_bonus = min(complexity_bonus_cap, max(0, required_count - 1) * 2)
    return base_limit + dynamic_bonus


def build_query_bundle(mode: str, request: SearchRequest, intent: SearchIntent) -> list[QueryBundleItem]:
    channel_settings = get_channel_settings(mode)
    max_variants = _build_bundle_limit(mode, channel_settings, intent)
    bundle: list[QueryBundleItem] = []
    seen_queries: set[str] = set()

    def add_item(label: str, query: str, purpose: str) -> None:
        normalized_query = normalize_phrase(query)
        if not normalized_query or normalized_query in seen_queries:
            return
        seen_queries.add(normalized_query)
        bundle.append(QueryBundleItem(label=label, query=query.strip(), purpose=purpose))

    add_item("rewritten-main", intent.rewritten_query, "Main academic-English query")

    if mode == "quick":
        add_item("original-query", request.query, "Original user wording fallback")
        return bundle[:max_variants]

    active_criteria = _active_criteria(intent.criteria)
    prioritized_required_criteria = sorted(
        active_criteria,
        key=lambda criterion: (not _is_combination_criterion(criterion), criterion.id),
    )
    representative_phrases = [_criterion_representative_phrase(criterion) for criterion in active_criteria[:4]]
    quoted_conjunction = " AND ".join(_quote_fragment(fragment) for fragment in representative_phrases if fragment).strip()
    quoted_disjunction = _build_disjunctive_criterion_query(active_criteria[:4])
    compact_query = _build_compact_criterion_query(active_criteria, intent)
    must_terms_query = " ".join(intent.must_terms[:6]).strip()

    if intent.logic == "OR":
        add_item("original-query", request.query, "Original user wording fallback")

        for criterion in prioritized_required_criteria:
            add_item(
                f"criterion-{criterion.id}",
                _criterion_representative_phrase(criterion),
                f"Alternative-focused query for criterion {criterion.id}",
            )
            if len(bundle) >= max_variants:
                break

        if len(bundle) < max_variants:
            add_item("criteria-or", quoted_disjunction, "Alternative query across required criteria")
        if len(bundle) < max_variants and channel_settings.get("include_must_terms_query", True) and must_terms_query:
            add_item("must-terms", must_terms_query, "Broad alternative fallback")
        if len(bundle) < max_variants:
            add_item("criteria-compact", compact_query, "Compact alternative fallback")
        return bundle[:max_variants]

    add_item("criteria-and", quoted_conjunction, "Strict conjunction across required criteria")
    add_item("original-query", request.query, "Original user wording fallback")

    if channel_settings.get("include_must_terms_query", True) and must_terms_query:
        add_item("must-terms", must_terms_query, "Focused must-term fallback")

    for criterion in prioritized_required_criteria:
        add_item(
            f"criterion-{criterion.id}",
            _criterion_representative_phrase(criterion),
            f"Focused query for criterion {criterion.id}",
        )
        if len(bundle) >= max_variants:
            break

    if len(bundle) < max_variants:
        add_item("criteria-compact", compact_query, "Compact multi-criterion fallback")

    return bundle[:max_variants]


def build_query_variants(mode: str, request: SearchRequest, intent: SearchIntent) -> list[str]:
    return [item.query for item in build_query_bundle(mode, request, intent)]


def _result_identity_key(result: PaperResult) -> str:
    doi = normalize_doi(result.doi)
    if doi:
        return f"doi:{doi}"
    title = re.sub(r"\s+", " ", normalize_phrase(result.title))
    first_author = normalize_phrase(result.authors[0] if result.authors else "")
    return f"title:{title}|year:{result.year or ''}|author:{first_author}"


def merge_criterion_judgments(
    existing: list[CriterionJudgment],
    incoming: list[CriterionJudgment],
) -> list[CriterionJudgment]:
    merged: dict[str, CriterionJudgment] = {}
    order: list[str] = []

    for judgment in [*existing, *incoming]:
        key = judgment.criterion_id
        if key not in merged:
            merged[key] = judgment.model_copy(deep=True)
            order.append(key)
            continue

        current = merged[key]
        current.supported = current.supported or judgment.supported
        current.required = current.required or judgment.required
        if judgment.description and not current.description:
            current.description = judgment.description
        if judgment.score is not None:
            current.score = max(current.score or 0.0, judgment.score)
        if judgment.confidence is not None:
            current.confidence = max(current.confidence or 0.0, judgment.confidence)
        current.evidence = unique_preserve_order([*current.evidence, *judgment.evidence])

        current_score = current.score or 0.0
        incoming_score = judgment.score or 0.0
        if incoming_score >= current_score and judgment.reason:
            current.reason = judgment.reason

    return [merged[key] for key in order]


def merge_retrieval_traces(
    existing: list[RetrievalTrace],
    incoming: list[RetrievalTrace],
) -> list[RetrievalTrace]:
    merged: dict[str, RetrievalTrace] = {}
    order: list[str] = []

    for trace in [*existing, *incoming]:
        key = "|".join(
            [
                str(trace.mode),
                trace.query_label,
                trace.query,
                trace.rendered_query or "",
            ]
        )
        if key in merged:
            continue
        merged[key] = trace.model_copy(deep=True)
        order.append(key)

    return [merged[key] for key in order]


def result_lane_keys(result: PaperResult, mode: str) -> list[str]:
    lane_keys = [
        f"{result.source}|{trace.query_label}"
        for trace in result.retrieval_traces
        if str(trace.mode) == mode and trace.query_label
    ]
    return unique_preserve_order(lane_keys) or [f"{result.source}|default"]


def merge_paper_results(existing: PaperResult, incoming: PaperResult) -> PaperResult:
    existing_scores = {**existing.scores}
    for key, value in incoming.scores.items():
        existing_scores[key] = max(existing_scores.get(key, value), value)

    merged = existing.model_copy(deep=True)
    merged.scores = existing_scores
    merged.matched_fields = sorted(set(existing.matched_fields) | set(incoming.matched_fields))
    merged.criterion_judgments = merge_criterion_judgments(existing.criterion_judgments, incoming.criterion_judgments)
    merged.retrieval_traces = merge_retrieval_traces(existing.retrieval_traces, incoming.retrieval_traces)
    merged.criteria_coverage = max(existing.criteria_coverage or 0.0, incoming.criteria_coverage or 0.0)
    if len(incoming.authors) > len(existing.authors):
        merged.authors = incoming.authors
    merged.abstract = incoming.abstract if len(incoming.abstract or "") > len(existing.abstract or "") else existing.abstract
    merged.title = incoming.title if len(incoming.title or "") > len(existing.title or "") else existing.title
    merged.doi = normalize_doi(existing.doi) or normalize_doi(incoming.doi)
    merged.url = existing.url or incoming.url
    merged.pdf_url = existing.pdf_url or incoming.pdf_url
    merged.is_oa = existing.is_oa or incoming.is_oa
    merged.year = existing.year or incoming.year

    existing_score = existing.score or 0.0
    incoming_score = incoming.score or 0.0
    if incoming_score >= existing_score:
        merged.score = incoming.score
        merged.decision = incoming.decision or existing.decision
        merged.confidence = incoming.confidence if incoming.confidence is not None else existing.confidence
        merged.reason = incoming.reason or existing.reason
        merged.source = incoming.source or existing.source
        merged.source_id = incoming.source_id or existing.source_id
        merged.raw = incoming.raw or existing.raw
    else:
        merged.score = existing.score
        merged.decision = existing.decision or incoming.decision
        merged.confidence = existing.confidence if existing.confidence is not None else incoming.confidence
        merged.reason = existing.reason or incoming.reason
        merged.raw = existing.raw or incoming.raw

    return merged


def dedup_results(results: list[PaperResult]) -> list[PaperResult]:
    deduped_by_key: dict[str, PaperResult] = {}
    order: list[str] = []

    for result in results:
        key = _result_identity_key(result)
        if key not in deduped_by_key:
            deduped_by_key[key] = result.model_copy(deep=True)
            order.append(key)
            continue
        deduped_by_key[key] = merge_paper_results(deduped_by_key[key], result)

    return [deduped_by_key[key] for key in order]


def assess_relevance(query: str, result: PaperResult, intent: SearchIntent | None = None) -> tuple[float, list[str], str]:
    query_tokens = set(normalize_text(query))
    title_tokens = set(normalize_text(result.title))
    abstract_tokens = set(normalize_text(result.abstract or ""))
    merged_tokens = title_tokens | abstract_tokens
    if not query_tokens or not merged_tokens:
        return 0.0, [], "insufficient lexical evidence"

    overlap = query_tokens & merged_tokens
    overlap_ratio = len(overlap) / max(len(query_tokens), 1)
    title_ratio = len(query_tokens & title_tokens) / max(len(query_tokens), 1)

    must_terms = intent.must_terms if intent else []
    should_terms = intent.should_terms if intent else []
    must_hits = 0
    should_hits = 0

    for term in must_terms:
        term_tokens = set(normalize_text(term))
        if term_tokens and term_tokens <= merged_tokens:
            must_hits += 1
    for term in should_terms:
        term_tokens = set(normalize_text(term))
        if term_tokens and term_tokens <= merged_tokens:
            should_hits += 1

    must_ratio = must_hits / max(len(must_terms), 1) if must_terms else 0.0
    should_ratio = should_hits / max(len(should_terms), 1) if should_terms else 0.0
    oa_bonus = 0.05 if result.is_oa else 0.0

    score = clamp_score(0.45 * overlap_ratio + 0.25 * title_ratio + 0.2 * must_ratio + 0.05 * should_ratio + oa_bonus)

    matched_fields: list[str] = []
    if query_tokens & title_tokens:
        matched_fields.append("title")
    if query_tokens & abstract_tokens:
        matched_fields.append("abstract")

    reason_parts: list[str] = []
    if overlap:
        reason_parts.append(f"matched query tokens: {', '.join(sorted(list(overlap))[:6])}")
    if must_hits:
        reason_parts.append(f"matched {must_hits}/{len(must_terms)} must terms")
    if should_hits:
        reason_parts.append(f"matched {should_hits}/{len(should_terms)} should terms")
    if result.is_oa:
        reason_parts.append("open-access bonus applied")

    if not reason_parts:
        reason_parts.append("no meaningful lexical overlap found")

    return score, matched_fields, "; ".join(reason_parts)


def _criterion_search_terms(criterion: SearchCriterion) -> list[str]:
    return unique_preserve_order([*criterion.terms, *criterion.query_hints, criterion.description])


def assess_criterion_support(
    result: PaperResult,
    criterion: SearchCriterion,
    channel_settings: dict[str, Any] | None = None,
) -> CriterionJudgment:
    settings = channel_settings or {}
    title_text = normalize_phrase(result.title)
    abstract_text = normalize_phrase(result.abstract or "")
    title_tokens = set(normalize_text(result.title))
    abstract_tokens = set(normalize_text(result.abstract or ""))
    merged_tokens = title_tokens | abstract_tokens

    evidence: list[str] = []
    best_score = 0.0
    best_reason = "no direct lexical support found"

    for term in _criterion_search_terms(criterion):
        phrase = normalize_phrase(term)
        term_tokens = set(normalize_text(term))
        phrase_in_title = bool(phrase and phrase in title_text)
        phrase_in_abstract = bool(phrase and phrase in abstract_text)
        token_overlap = len(term_tokens & merged_tokens) / max(len(term_tokens), 1) if term_tokens else 0.0
        title_overlap = len(term_tokens & title_tokens) / max(len(term_tokens), 1) if term_tokens else 0.0
        term_score = clamp_score(
            (0.75 if phrase_in_title else 0.0)
            + (0.55 if phrase_in_abstract else 0.0)
            + 0.35 * token_overlap
            + 0.15 * title_overlap
        )
        if term_score > best_score:
            best_score = term_score
            best_reason = f"best matched term '{term}' with score {term_score:.3f}"
        if phrase_in_title or phrase_in_abstract or token_overlap >= 0.5:
            evidence.append(term)

    description_tokens = set(normalize_text(criterion.description))
    description_overlap = (
        len(description_tokens & merged_tokens) / max(len(description_tokens), 1) if description_tokens else 0.0
    )
    score = clamp_score(max(best_score, 0.8 * best_score + 0.2 * description_overlap))

    threshold = resolve_criterion_supported_threshold(criterion, settings)
    supported = score >= threshold

    if supported:
        reason = f"criterion supported; {best_reason}"
    else:
        reason = f"criterion not fully supported; {best_reason}"

    return CriterionJudgment(
        criterion_id=criterion.id,
        description=criterion.description,
        required=criterion.required,
        supported=supported,
        score=score,
        confidence=clamp_score(0.35 + 0.55 * score),
        evidence=unique_preserve_order(evidence)[:4],
        reason=reason,
    )


def assess_criteria_match(
    query: str,
    result: PaperResult,
    intent: SearchIntent,
    channel_settings: dict[str, Any] | None = None,
) -> tuple[float, list[str], str, list[CriterionJudgment], float, float]:
    settings = channel_settings or {}
    lexical_score, matched_fields, lexical_reason = assess_relevance(query, result, intent)
    criterion_judgments = [assess_criterion_support(result, criterion, settings) for criterion in intent.criteria]

    required_judgments = [judgment for judgment in criterion_judgments if judgment.required]
    active_judgments = required_judgments or criterion_judgments
    optional_judgments = [judgment for judgment in criterion_judgments if not judgment.required] if required_judgments else []

    required_supported = sum(1 for judgment in active_judgments if judgment.supported)
    required_coverage = (
        required_supported / max(len(active_judgments), 1) if active_judgments else (1.0 if lexical_score > 0.0 else 0.0)
    )
    required_average = (
        sum(judgment.score or 0.0 for judgment in active_judgments) / len(active_judgments)
        if active_judgments
        else lexical_score
    )
    required_best = max((judgment.score or 0.0) for judgment in active_judgments) if active_judgments else lexical_score
    optional_average = (
        sum(judgment.score or 0.0 for judgment in optional_judgments) / len(optional_judgments)
        if optional_judgments
        else 0.0
    )

    coverage_weight = float(settings.get("coverage_weight", 0.55))
    criterion_weight = float(settings.get("criterion_score_weight", 0.25))
    overall_weight = float(settings.get("overall_query_score_weight", 0.2))
    if active_judgments and intent.logic == "OR":
        composite_score = clamp_score(
            0.45 * required_best + 0.2 * required_average + 0.2 * lexical_score + 0.15 * required_coverage
        )
        if any(judgment.supported for judgment in active_judgments):
            composite_score = clamp_score(composite_score + 0.05)
    else:
        composite_score = clamp_score(
            coverage_weight * required_coverage + criterion_weight * required_average + overall_weight * lexical_score
        )
    if optional_judgments:
        composite_score = clamp_score(composite_score + 0.05 * optional_average)
    if active_judgments and intent.logic == "AND" and required_coverage < 1.0:
        composite_score = clamp_score(composite_score * (0.65 + 0.35 * required_coverage))

    supported_ids = [judgment.criterion_id for judgment in active_judgments if judgment.supported]
    missing_ids = [judgment.criterion_id for judgment in active_judgments if not judgment.supported]
    coverage_label = "alternative coverage" if intent.logic == "OR" else "required coverage"
    supported_label = "supported alternatives" if intent.logic == "OR" else "supported criteria"
    missing_label = "unsupported alternatives" if intent.logic == "OR" else "missing criteria"
    reason_parts = [f"{coverage_label} {required_supported}/{len(active_judgments) or 1}"]
    if supported_ids:
        reason_parts.append(f"{supported_label}: {', '.join(supported_ids[:4])}")
    if missing_ids:
        reason_parts.append(f"{missing_label}: {', '.join(missing_ids[:4])}")
    reason_parts.append(lexical_reason)

    return (
        composite_score,
        matched_fields,
        "; ".join(reason_parts),
        criterion_judgments,
        required_coverage,
        required_average,
    )


async def recall_results_by_source(
    mode: str,
    query_bundle: list[QueryBundleItem],
    request: SearchRequest,
) -> tuple[dict[str, list[PaperResult]], list[str], int, dict[str, float]]:
    async def timed_batch_search(client: Any) -> tuple[str, list[PaperResult] | Exception, float]:
        started_at = time.perf_counter()
        try:
            payload = await client.batch_search(mode, query_bundle, limit=limit_per_source)
        except Exception as exc:
            return client.name, exc, elapsed_ms(started_at)
        return client.name, payload, elapsed_ms(started_at)

    limit_per_source = resolve_limit_per_source(mode, request)
    clients = get_clients_for_mode(mode, sources=request.sources, public_only=request.public_only)
    gathered = await asyncio.gather(*(timed_batch_search(client) for client in clients))

    results_by_source: dict[str, list[PaperResult]] = {}
    used_sources: list[str] = []
    raw_recall_count = 0
    source_timings_ms: dict[str, float] = {}

    for client_name, client_payload, client_elapsed_ms in gathered:
        source_timings_ms[client_name] = client_elapsed_ms
        if isinstance(client_payload, Exception):
            continue

        source_results = client_payload
        raw_recall_count += len(source_results)

        if source_results:
            used_sources.append(client_name)
            results_by_source[client_name] = dedup_results(source_results)

    return results_by_source, used_sources, raw_recall_count, source_timings_ms
