from __future__ import annotations

import time
from typing import Any

from app.domain.schemas import PaperResult, SearchRequest, SearchResponse
from app.llm import EmbeddingClient
from app.services.search_common import (
    assess_relevance,
    elapsed_ms,
    finalize_timings_ms,
    build_document_text,
    build_query_bundle,
    clamp_score,
    compute_recency_score,
    cosine_similarity,
    dedup_results,
    get_channel_settings,
    plan_search_intent,
    recall_results_by_source,
)


def _resolve_hybrid_weights(channel_settings: dict[str, Any], semantic_available: bool) -> dict[str, float]:
    configured = channel_settings.get("hybrid_weights", {})
    if not isinstance(configured, dict):
        configured = {}

    weights = {
        "lexical": float(configured.get("lexical", 0.45)),
        "semantic": float(configured.get("semantic", 0.35)),
        "source_prior": float(configured.get("source_prior", 0.1)),
        "recency": float(configured.get("recency", 0.05)),
        "open_access": float(configured.get("open_access", 0.05)),
    }

    if not semantic_available:
        weights["semantic"] = 0.0

    return weights


async def _compute_semantic_scores(query: str, results: list[PaperResult]) -> list[float]:
    embed_client = EmbeddingClient()
    if not embed_client.is_configured() or not results:
        return [0.0 for _ in results]

    try:
        vectors = await embed_client.embed_texts([query, *[build_document_text(result) for result in results]])
    except Exception:
        return [0.0 for _ in results]

    if len(vectors) != len(results) + 1:
        return [0.0 for _ in results]

    query_vector = vectors[0]
    return [cosine_similarity(query_vector, vector) for vector in vectors[1:]]


async def run_quick_channel(request: SearchRequest) -> SearchResponse:
    timings_ms: dict[str, float] = {}
    total_started_at = time.perf_counter()

    step_started_at = time.perf_counter()
    intent = await plan_search_intent(request.query, request)
    timings_ms["plan_intent"] = elapsed_ms(step_started_at)
    channel_settings = get_channel_settings("quick")

    step_started_at = time.perf_counter()
    query_bundle = build_query_bundle("quick", request, intent)
    timings_ms["build_query_bundle"] = elapsed_ms(step_started_at)

    step_started_at = time.perf_counter()
    results_by_source, used_sources, raw_recall_count, recall_source_timings_ms = await recall_results_by_source(
        "quick",
        query_bundle,
        request,
    )
    timings_ms["recall"] = elapsed_ms(step_started_at)
    for source_name, source_elapsed_ms in recall_source_timings_ms.items():
        timings_ms[f"recall_source_{source_name}"] = source_elapsed_ms

    all_results = [result for source_results in results_by_source.values() for result in source_results]
    step_started_at = time.perf_counter()
    deduped = dedup_results(all_results)
    timings_ms["dedup"] = elapsed_ms(step_started_at)

    step_started_at = time.perf_counter()
    if channel_settings.get("enable_embedding_similarity", True):
        semantic_scores = await _compute_semantic_scores(intent.rewritten_query or request.query, deduped)
    else:
        semantic_scores = [0.0 for _ in deduped]
    timings_ms["compute_semantic_scores"] = elapsed_ms(step_started_at)

    source_priors = channel_settings.get("source_priors", {})
    if not isinstance(source_priors, dict):
        source_priors = {}

    hybrid_weights = _resolve_hybrid_weights(channel_settings, semantic_available=any(score > 0.0 for score in semantic_scores))
    weight_sum = sum(hybrid_weights.values()) or 1.0
    recency_window = int(channel_settings.get("recency_window_years", 10) or 10)

    ranked: list[PaperResult] = []
    step_started_at = time.perf_counter()
    for result, semantic_score in zip(deduped, semantic_scores):
        lexical_score, matched_fields, lexical_reason = assess_relevance(intent.rewritten_query or request.query, result, intent)
        source_prior = clamp_score(float(source_priors.get(result.source, channel_settings.get("default_source_prior", 0.6))))
        recency_score = compute_recency_score(result.year, window_years=recency_window)
        oa_score = 1.0 if result.is_oa else 0.0

        quick_score = (
            hybrid_weights["lexical"] * lexical_score
            + hybrid_weights["semantic"] * semantic_score
            + hybrid_weights["source_prior"] * source_prior
            + hybrid_weights["recency"] * recency_score
            + hybrid_weights["open_access"] * oa_score
        ) / weight_sum
        quick_score = clamp_score(quick_score)

        result.score = quick_score
        result.scores["quick"] = quick_score
        result.scores["quick_lexical"] = lexical_score
        result.scores["quick_semantic"] = semantic_score
        result.scores["quick_source_prior"] = source_prior
        result.scores["quick_recency"] = recency_score
        result.scores["quick_open_access"] = oa_score
        result.matched_fields = matched_fields
        result.reason = (
            f"hybrid rerank => lexical={lexical_score:.3f}, semantic={semantic_score:.3f}, "
            f"source_prior={source_prior:.3f}, recency={recency_score:.3f}, oa={oa_score:.3f}; {lexical_reason}"
        )
        result.decision = "keep"
        result.confidence = clamp_score(0.45 + 0.5 * quick_score)
        ranked.append(result)
    timings_ms["rank"] = elapsed_ms(step_started_at)

    step_started_at = time.perf_counter()
    ranked.sort(key=lambda item: (item.scores.get("quick", 0.0), item.confidence or 0.0), reverse=True)
    timings_ms["sort"] = elapsed_ms(step_started_at)
    timings_ms["total"] = elapsed_ms(total_started_at)
    timings_ms = finalize_timings_ms(timings_ms)

    return SearchResponse(
        query=request.query,
        rewritten_query=intent.rewritten_query,
        mode="quick",
        used_sources=used_sources,
        total_results=len(ranked),
        raw_recall_count=raw_recall_count,
        deduped_count=len(deduped),
        finalized_count=len(ranked),
        timings_ms=timings_ms,
        intent=intent,
        query_bundle=query_bundle,
        results=ranked,
    )
