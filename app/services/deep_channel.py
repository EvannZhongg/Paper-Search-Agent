from __future__ import annotations

import asyncio
import time
from typing import Any

from app.domain.schemas import CriterionJudgment, PaperResult, SearchCriterion, SearchIntent, SearchRequest, SearchResponse
from app.llm import LLMClient
from app.prompts import DEEP_JUDGE_SYSTEM_PROMPT, DEEP_JUDGE_USER_PROMPT, render_prompt
from app.services.search_common import (
    add_timing_ms,
    assess_criteria_match,
    build_query_bundle,
    clamp_score,
    dedup_results,
    elapsed_ms,
    finalize_timings_ms,
    get_channel_settings,
    get_retrieval_settings,
    max_timing_ms,
    plan_search_intent,
    recall_results_by_source,
    result_lane_keys,
    resolve_criterion_supported_threshold,
    unique_preserve_order,
)


def _hard_filter_reason(intent: SearchIntent, result: PaperResult) -> str | None:
    filters = intent.filters if isinstance(intent.filters, dict) else {}
    year_from = filters.get("year_from")
    year_to = filters.get("year_to")
    require_oa = filters.get("is_oa")

    if isinstance(year_from, int) and result.year is not None and result.year < year_from:
        return f"hard filter failed: year {result.year} < {year_from}"
    if isinstance(year_to, int) and result.year is not None and result.year > year_to:
        return f"hard filter failed: year {result.year} > {year_to}"
    if require_oa is True and result.is_oa is False:
        return "hard filter failed: open access required"
    return None


def _render_criteria_prompt(criteria: list[SearchCriterion]) -> str:
    if not criteria:
        return "- id=topic_match; required=true; description=The paper matches the user query."

    lines: list[str] = []
    for criterion in criteria:
        terms = ", ".join(criterion.terms) if criterion.terms else "-"
        query_hints = ", ".join(criterion.query_hints) if criterion.query_hints else "-"
        lines.append(
            f"- id={criterion.id}; required={str(criterion.required).lower()}; "
            f"description={criterion.description}; terms={terms}; query_hints={query_hints}"
        )
    return "\n".join(lines)


def _coerce_bool(value: object, default: bool = False) -> bool:
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


def _parse_llm_criterion_judgments(
    raw_criteria: object,
    criteria: list[SearchCriterion],
    channel_settings: dict[str, Any] | None = None,
) -> list[CriterionJudgment]:
    items_by_id: dict[str, dict[str, Any]] = {}
    if isinstance(raw_criteria, list):
        for item in raw_criteria:
            if not isinstance(item, dict):
                continue
            criterion_id = str(item.get("criterion_id") or item.get("id") or "").strip()
            if criterion_id:
                items_by_id[criterion_id] = item

    parsed: list[CriterionJudgment] = []
    for criterion in criteria:
        raw_item = items_by_id.get(criterion.id, {})
        threshold = resolve_criterion_supported_threshold(criterion, channel_settings or {})
        try:
            score = clamp_score(float(raw_item.get("score", 1.0 if _coerce_bool(raw_item.get("supported")) else 0.0)))
        except (TypeError, ValueError):
            score = 0.0
        try:
            confidence = clamp_score(float(raw_item.get("confidence", score)))
        except (TypeError, ValueError):
            confidence = score

        evidence = unique_preserve_order(_coerce_string_list(raw_item.get("evidence", [])))
        parsed.append(
            CriterionJudgment(
                criterion_id=criterion.id,
                description=criterion.description,
                required=criterion.required,
                supported=_coerce_bool(raw_item.get("supported"), default=score >= threshold) and score >= threshold,
                score=score,
                confidence=confidence,
                evidence=evidence[:4],
                reason=str(raw_item.get("reason", "")).strip() or None,
            )
        )

    return parsed


def _required_judgments(judgments: list[CriterionJudgment]) -> list[CriterionJudgment]:
    required = [judgment for judgment in judgments if judgment.required]
    return required or judgments


def _active_criteria(intent: SearchIntent) -> list[SearchCriterion]:
    required = [criterion for criterion in intent.criteria if criterion.required]
    return required or intent.criteria


def _required_coverage(judgments: list[CriterionJudgment]) -> float:
    required = _required_judgments(judgments)
    if not required:
        return 0.0
    supported = sum(1 for judgment in required if judgment.supported)
    return supported / len(required)


def _required_average_score(judgments: list[CriterionJudgment]) -> float:
    required = _required_judgments(judgments)
    if not required:
        return 0.0
    return sum(judgment.score or 0.0 for judgment in required) / len(required)


def _required_max_score(judgments: list[CriterionJudgment]) -> float:
    required = _required_judgments(judgments)
    if not required:
        return 0.0
    return max(judgment.score or 0.0 for judgment in required)


def _criteria_signal(judgments: list[CriterionJudgment], intent: SearchIntent) -> float:
    coverage = _required_coverage(judgments)
    average = _required_average_score(judgments)
    if intent.logic == "OR":
        peak = _required_max_score(judgments)
        return clamp_score(0.7 * peak + 0.3 * coverage)
    return clamp_score(0.6 * coverage + 0.4 * average)


def _blend_llm_criterion_judgments(
    heuristic_judgments: list[CriterionJudgment],
    llm_judgments: list[CriterionJudgment],
    channel_settings: dict[str, Any],
) -> list[CriterionJudgment]:
    heuristic_by_id = {judgment.criterion_id: judgment for judgment in heuristic_judgments}
    llm_by_id = {judgment.criterion_id: judgment for judgment in llm_judgments}
    ordered_ids = unique_preserve_order([*heuristic_by_id.keys(), *llm_by_id.keys()])

    blended: list[CriterionJudgment] = []
    for criterion_id in ordered_ids:
        heuristic = heuristic_by_id.get(criterion_id)
        llm = llm_by_id.get(criterion_id)
        anchor = llm or heuristic
        if anchor is None:
            continue

        threshold = resolve_criterion_supported_threshold(
            SearchCriterion(
                id=anchor.criterion_id,
                description=anchor.description,
                required=anchor.required,
            ),
            channel_settings,
        )

        if heuristic is not None and llm is not None:
            combined_score = llm.score if llm.score is not None else heuristic.score
            combined_confidence = max(heuristic.confidence or 0.0, llm.confidence or 0.0)
            combined_evidence = unique_preserve_order([*heuristic.evidence, *llm.evidence])
            combined_reason = llm.reason or heuristic.reason
            combined_supported = bool(llm.supported) and (combined_score or 0.0) >= threshold
        else:
            selected = llm or heuristic
            combined_score = selected.score
            combined_confidence = selected.confidence
            combined_evidence = list(selected.evidence)
            combined_reason = selected.reason
            combined_supported = bool(selected.supported) and (combined_score or 0.0) >= threshold

        blended.append(
            CriterionJudgment(
                criterion_id=anchor.criterion_id,
                description=anchor.description,
                required=anchor.required,
                supported=combined_supported,
                score=combined_score,
                confidence=combined_confidence,
                evidence=combined_evidence[:6],
                reason=combined_reason,
            )
        )

    return blended


def _resolve_judge_limit(
    request: SearchRequest,
    channel_settings: dict[str, Any],
    intent: SearchIntent,
) -> int:
    base_limit = request.llm_top_n if request.llm_top_n is not None else int(channel_settings.get("llm_top_n_per_source", 4))
    required_count = max(1, len(_active_criteria(intent)))
    bonus_per_extra = max(0, int(channel_settings.get("llm_top_n_per_source_complex_bonus", 2) or 2))
    dynamic_limit = base_limit + max(0, required_count - 1) * bonus_per_extra
    max_dynamic = max(base_limit, int(channel_settings.get("max_dynamic_llm_top_n_per_source", max(base_limit, 8)) or max(base_limit, 8)))
    return max(1, min(dynamic_limit, max_dynamic))


def _resolve_prefilter_floor(channel_settings: dict[str, Any], intent: SearchIntent) -> float:
    base_floor = float(channel_settings.get("llm_prefilter_min_score", 0.15))
    required_count = max(1, len(_active_criteria(intent)))
    return max(0.05, base_floor - 0.03 * max(0, required_count - 1))


def _heuristic_decision(score: float, coverage: float, intent: SearchIntent, channel_settings: dict[str, Any]) -> str:
    keep_threshold = float(channel_settings.get("keep_threshold", 0.6))
    maybe_threshold = float(channel_settings.get("maybe_threshold", 0.35))
    required_count = len(_active_criteria(intent))

    if intent.logic == "OR" and required_count > 1:
        if coverage > 0.0 and score >= keep_threshold:
            return "keep"
        if coverage > 0.0 and score >= maybe_threshold:
            return "maybe"
        return "drop"

    if intent.logic == "AND" and required_count > 1:
        if coverage >= 1.0 and score >= keep_threshold:
            return "keep"
        if coverage >= max(1.0 / required_count, 0.5) and score >= maybe_threshold:
            return "maybe"
        return "drop"

    if score >= keep_threshold:
        return "keep"
    if score >= maybe_threshold:
        return "maybe"
    return "drop"


def _apply_coverage_guard(decision: str, coverage: float, intent: SearchIntent) -> str:
    required_count = len(_active_criteria(intent))
    if required_count <= 1:
        return decision
    if intent.logic == "OR":
        return decision if coverage > 0.0 else "drop"
    if intent.logic != "AND":
        return decision
    if coverage >= 1.0:
        return decision
    if coverage >= max(1.0 / required_count, 0.5):
        return "maybe" if decision == "keep" else decision
    return "drop"


def _candidate_sort_key(result: PaperResult, intent: SearchIntent) -> tuple[float, float, float, float]:
    if intent.logic == "OR":
        return (
            result.scores.get("deep_logic_signal", 0.0),
            result.scores.get("deep_required_peak", 0.0),
            result.scores.get("deep_heuristic", 0.0),
            result.scores.get("deep_required_coverage", 0.0),
        )
    return (
        result.scores.get("deep_required_coverage", 0.0),
        result.scores.get("deep_heuristic", 0.0),
        result.scores.get("deep_criteria_score", 0.0),
        result.scores.get("deep_required_peak", 0.0),
    )


def _resolve_full_coverage_target(intent: SearchIntent, candidates: list[PaperResult]) -> float:
    coverages = sorted({result.criteria_coverage or 0.0 for result in candidates if (result.criteria_coverage or 0.0) > 0.0}, reverse=True)
    if not coverages:
        return 0.0
    if intent.logic == "AND" and coverages[0] >= 1.0:
        return 1.0
    return coverages[0]


def _resolve_round_robin_floor(intent: SearchIntent, channel_settings: dict[str, Any]) -> float:
    configured = channel_settings.get("judge_round_robin_min_coverage")
    if configured is not None:
        try:
            return clamp_score(float(configured))
        except (TypeError, ValueError):
            pass

    required_count = len(_active_criteria(intent))
    if intent.logic == "AND" and required_count > 1:
        return clamp_score(max(0.5, 1.0 - 1.0 / required_count))
    return 0.0


def _resolve_lane_negative_streak_limit(channel_settings: dict[str, Any]) -> int:
    return max(1, int(channel_settings.get("judge_lane_negative_streak_limit", 2) or 2))


def _resolve_lane_positive_maybe_score(channel_settings: dict[str, Any]) -> float:
    keep_threshold = float(channel_settings.get("keep_threshold", 0.6))
    return clamp_score(float(channel_settings.get("judge_lane_positive_maybe_score", max(keep_threshold - 0.05, 0.7)) or max(keep_threshold - 0.05, 0.7)))


def _is_positive_lane_outcome(result: PaperResult, channel_settings: dict[str, Any]) -> bool:
    if result.decision == "keep":
        return True
    if result.decision != "maybe":
        return False
    return (result.scores.get("deep", result.score or 0.0)) >= _resolve_lane_positive_maybe_score(channel_settings)


async def _apply_llm_batch(
    query: str,
    intent: SearchIntent,
    candidates: list[PaperResult],
    llm_client: LLMClient,
    channel_settings: dict[str, Any],
    heuristic_weight: float,
    llm_weight: float,
) -> None:
    if not candidates:
        return

    judgments = await asyncio.gather(
        *(_llm_judge(query, intent, result, llm_client, channel_settings) for result in candidates),
        return_exceptions=True,
    )
    for result, payload in zip(candidates, judgments):
        heuristic_score = result.scores.get("deep_heuristic", 0.0)
        heuristic_judgments = result.criterion_judgments
        if isinstance(payload, Exception):
            logic_signal = _criteria_signal(heuristic_judgments, intent)
            deep_score = clamp_score(0.5 * logic_signal + 0.5 * heuristic_score)
            result.scores["deep_llm"] = heuristic_score
            result.scores["deep_logic_signal"] = logic_signal
            result.scores["deep"] = deep_score
            result.score = deep_score
            result.decision = _heuristic_decision(deep_score, result.criteria_coverage or 0.0, intent, channel_settings)
            result.reason = f"{result.reason}; llm judge fallback used"
            continue

        llm_relevance, decision, confidence, reason, llm_judgments = payload
        merged_judgments = _blend_llm_criterion_judgments(heuristic_judgments, llm_judgments, channel_settings)
        required_coverage = _required_coverage(merged_judgments)
        criterion_average = _required_average_score(merged_judgments)
        required_peak = _required_max_score(merged_judgments)
        logic_signal = _criteria_signal(merged_judgments, intent)
        blended_relevance = clamp_score(heuristic_weight * heuristic_score + llm_weight * llm_relevance)
        deep_score = clamp_score(0.55 * logic_signal + 0.45 * blended_relevance)

        result.criterion_judgments = merged_judgments
        result.criteria_coverage = required_coverage
        result.scores["deep_llm"] = llm_relevance
        result.scores["deep"] = deep_score
        result.scores["deep_required_coverage"] = required_coverage
        result.scores["deep_criteria_score"] = criterion_average
        result.scores["deep_required_peak"] = required_peak
        result.scores["deep_logic_signal"] = logic_signal
        result.score = deep_score
        result.decision = _apply_coverage_guard(decision, required_coverage, intent)
        result.confidence = max(result.confidence or 0.0, confidence)
        result.reason = (
            f"llm judge on source={result.source} => relevance={llm_relevance:.3f}, "
            f"heuristic={heuristic_score:.3f}, coverage={required_coverage:.3f}; {reason or result.reason}"
        )


async def _run_dynamic_llm_window(
    query: str,
    intent: SearchIntent,
    candidates: list[PaperResult],
    llm_client: LLMClient,
    channel_settings: dict[str, Any],
    judge_limit: int,
    heuristic_weight: float,
    llm_weight: float,
) -> set[int]:
    judged_ids: set[int] = set()
    full_coverage_target = _resolve_full_coverage_target(intent, candidates)
    guaranteed_candidates = [
        result
        for result in candidates
        if full_coverage_target > 0.0 and (result.criteria_coverage or 0.0) >= full_coverage_target
    ]
    if guaranteed_candidates:
        await _apply_llm_batch(query, intent, guaranteed_candidates, llm_client, channel_settings, heuristic_weight, llm_weight)
        judged_ids.update(id(result) for result in guaranteed_candidates)

    budget_remaining = max(0, max(judge_limit, len(guaranteed_candidates)) - len(guaranteed_candidates))
    if budget_remaining <= 0:
        return judged_ids

    round_robin_floor = _resolve_round_robin_floor(intent, channel_settings)
    available_coverages = sorted(
        {
            result.criteria_coverage or 0.0
            for result in candidates
            if id(result) not in judged_ids and (result.criteria_coverage or 0.0) > 0.0
        },
        reverse=True,
    )
    band_values = [
        coverage
        for coverage in available_coverages
        if coverage < full_coverage_target and coverage >= round_robin_floor
    ]
    if not band_values:
        fallback_band = next((coverage for coverage in available_coverages if coverage < full_coverage_target), None)
        band_values = [fallback_band] if fallback_band is not None else []

    negative_streak_limit = _resolve_lane_negative_streak_limit(channel_settings)
    lane_negative_streaks: dict[str, int] = {}

    for band_value in band_values:
        if budget_remaining <= 0:
            break

        band_candidates = [
            result
            for result in candidates
            if id(result) not in judged_ids and abs((result.criteria_coverage or 0.0) - band_value) < 1e-9
        ]
        if not band_candidates:
            continue

        lane_queues: dict[str, list[PaperResult]] = {}
        lane_order: list[str] = []
        for result in band_candidates:
            for lane_key in result_lane_keys(result, "deep"):
                if lane_key not in lane_queues:
                    lane_queues[lane_key] = []
                    lane_order.append(lane_key)
                lane_queues[lane_key].append(result)

        for lane_key in lane_order:
            lane_queues[lane_key].sort(key=lambda item: _candidate_sort_key(item, intent), reverse=True)

        while budget_remaining > 0 and lane_order:
            round_batch: list[PaperResult] = []
            selected_lanes: dict[int, str] = {}
            next_lane_order: list[str] = []

            for lane_key in lane_order:
                if lane_negative_streaks.get(lane_key, 0) >= negative_streak_limit:
                    continue

                queue = lane_queues.get(lane_key, [])
                candidate: PaperResult | None = None
                while queue:
                    probe = queue.pop(0)
                    if id(probe) in judged_ids or id(probe) in selected_lanes:
                        continue
                    candidate = probe
                    break

                if queue:
                    next_lane_order.append(lane_key)

                if candidate is None:
                    continue

                round_batch.append(candidate)
                selected_lanes[id(candidate)] = lane_key
                if budget_remaining - len(round_batch) <= 0:
                    break

            if not round_batch:
                break

            await _apply_llm_batch(query, intent, round_batch, llm_client, channel_settings, heuristic_weight, llm_weight)
            for result in round_batch:
                judged_ids.add(id(result))
                lane_key = selected_lanes.get(id(result))
                if not lane_key:
                    continue
                if _is_positive_lane_outcome(result, channel_settings):
                    lane_negative_streaks[lane_key] = 0
                else:
                    lane_negative_streaks[lane_key] = lane_negative_streaks.get(lane_key, 0) + 1

            budget_remaining -= len(round_batch)
            lane_order = [lane_key for lane_key in next_lane_order if lane_negative_streaks.get(lane_key, 0) < negative_streak_limit]

    return judged_ids


def _resolve_return_limit(channel_settings: dict[str, Any]) -> int:
    retrieval_settings = get_retrieval_settings()
    configured = channel_settings.get("return_limit", retrieval_settings.get("default_top_k_return", 20))
    return max(1, int(configured or 20))


def _resolve_final_maybe_min_score(channel_settings: dict[str, Any]) -> float:
    keep_threshold = float(channel_settings.get("keep_threshold", 0.6))
    maybe_threshold = float(channel_settings.get("maybe_threshold", 0.35))
    return clamp_score(float(channel_settings.get("final_high_score_maybe_threshold", max(keep_threshold - 0.05, maybe_threshold + 0.2)) or max(keep_threshold - 0.05, maybe_threshold + 0.2)))


def _resolve_final_maybe_min_coverage(intent: SearchIntent, channel_settings: dict[str, Any]) -> float:
    configured = channel_settings.get("final_high_score_maybe_min_coverage")
    if configured is not None:
        try:
            return clamp_score(float(configured))
        except (TypeError, ValueError):
            pass

    required_count = len(_active_criteria(intent))
    if intent.logic == "AND" and required_count > 1:
        return clamp_score(max(0.5, 1.0 - 1.0 / required_count))
    if intent.logic == "OR":
        return 0.0
    return 0.5


def _finalize_deep_results(
    results: list[PaperResult],
    intent: SearchIntent,
    channel_settings: dict[str, Any],
) -> list[PaperResult]:
    if not results:
        return []

    return_limit = _resolve_return_limit(channel_settings)
    maybe_min_score = _resolve_final_maybe_min_score(channel_settings)
    maybe_min_coverage = _resolve_final_maybe_min_coverage(intent, channel_settings)

    finalized = [
        result
        for result in results
        if result.decision == "keep"
        or (
            result.decision == "maybe"
            and (result.scores.get("deep", result.score or 0.0)) >= maybe_min_score
            and (result.criteria_coverage or 0.0) >= maybe_min_coverage
        )
    ]
    if not finalized:
        finalized = [result for result in results if result.decision in {"keep", "maybe"}] or results

    return finalized[:return_limit]


async def _llm_judge(
    query: str,
    intent: SearchIntent,
    result: PaperResult,
    llm_client: LLMClient,
    channel_settings: dict[str, Any],
) -> tuple[float, str, float, str, list[CriterionJudgment]]:
    user_prompt = render_prompt(
        DEEP_JUDGE_USER_PROMPT,
        query=query,
        logic=intent.logic,
        criteria=_render_criteria_prompt(intent.criteria),
        title=result.title,
        abstract=result.abstract or "",
        year=result.year,
        source=result.source,
        authors=", ".join(result.authors[:8]),
    )
    judgment = await llm_client.complete_json(
        system_prompt=DEEP_JUDGE_SYSTEM_PROMPT.strip(),
        user_prompt=user_prompt,
    )
    relevance = clamp_score(float(judgment.get("relevance", 0.0)))
    confidence = clamp_score(float(judgment.get("confidence", 0.0)))
    decision = str(judgment.get("decision", "maybe")).strip().lower() or "maybe"
    reason = str(judgment.get("reason", "")).strip()
    if decision not in {"keep", "maybe", "drop"}:
        decision = "maybe"
    criterion_judgments = _parse_llm_criterion_judgments(judgment.get("criteria"), intent.criteria, channel_settings)
    return relevance, decision, confidence, reason, criterion_judgments


async def _judge_source_results(
    query: str,
    intent: SearchIntent,
    source_name: str,
    source_results: list[PaperResult],
    request: SearchRequest,
    channel_settings: dict[str, Any],
) -> tuple[str, list[PaperResult], dict[str, float]]:
    source_timings_ms: dict[str, float] = {}
    total_started_at = time.perf_counter()
    llm_client = LLMClient()
    llm_enabled = request.enable_llm and llm_client.is_configured()
    llm_weight = float(channel_settings.get("llm_weight", 0.7))
    heuristic_weight = float(channel_settings.get("heuristic_weight", 0.3))
    judge_limit = _resolve_judge_limit(request, channel_settings, intent)
    heuristic_floor = _resolve_prefilter_floor(channel_settings, intent)
    scoring_query = intent.rewritten_query or query

    candidates: list[PaperResult] = []
    dropped: list[PaperResult] = []
    heuristic_prefilter_started_at = time.perf_counter()
    for result in source_results:
        (
            heuristic_score,
            matched_fields,
            heuristic_reason,
            criterion_judgments,
            required_coverage,
            criterion_average,
        ) = assess_criteria_match(scoring_query, result, intent, channel_settings)
        required_peak = _required_max_score(criterion_judgments)
        logic_signal = _criteria_signal(criterion_judgments, intent)
        result.scores["deep_heuristic"] = heuristic_score
        result.scores["deep_required_coverage"] = required_coverage
        result.scores["deep_criteria_score"] = criterion_average
        result.scores["deep_required_peak"] = required_peak
        result.scores["deep_logic_signal"] = logic_signal
        result.criteria_coverage = required_coverage
        result.criterion_judgments = criterion_judgments
        result.matched_fields = matched_fields
        result.reason = heuristic_reason
        result.score = heuristic_score
        result.confidence = clamp_score(0.35 + 0.25 * heuristic_score + 0.25 * logic_signal + 0.15 * required_coverage)

        filter_reason = _hard_filter_reason(intent, result)
        if filter_reason:
            result.scores["deep"] = 0.0
            result.score = 0.0
            result.decision = "drop"
            result.confidence = 0.95
            result.reason = filter_reason
            dropped.append(result)
            continue
        candidates.append(result)

    candidates.sort(key=lambda item: _candidate_sort_key(item, intent), reverse=True)
    source_timings_ms["heuristic_prefilter"] = elapsed_ms(heuristic_prefilter_started_at)

    eligible_candidates = [
        result
        for result in candidates
        if result.scores.get("deep_heuristic", 0.0) >= heuristic_floor
        or result.scores.get("deep_logic_signal", 0.0) >= heuristic_floor
        or result.scores.get("deep_required_peak", 0.0) >= max(heuristic_floor, 0.5)
        or (result.criteria_coverage or 0.0) > 0.0
    ]
    judged_ids: set[int] = set()

    llm_window_started_at = time.perf_counter()
    if llm_enabled and eligible_candidates:
        judged_ids = await _run_dynamic_llm_window(
            scoring_query,
            intent,
            eligible_candidates,
            llm_client,
            channel_settings,
            judge_limit,
            heuristic_weight,
            llm_weight,
        )
    source_timings_ms["llm_window"] = elapsed_ms(llm_window_started_at)

    finalize_unjudged_started_at = time.perf_counter()
    for result in candidates:
        if id(result) in judged_ids and llm_enabled:
            continue
        heuristic_score = result.scores.get("deep_heuristic", 0.0)
        logic_signal = _criteria_signal(result.criterion_judgments, intent)
        result.scores["deep_logic_signal"] = logic_signal
        deep_score = clamp_score(0.5 * logic_signal + 0.5 * heuristic_score)
        result.scores["deep"] = deep_score
        result.score = deep_score
        result.decision = _heuristic_decision(deep_score, result.criteria_coverage or 0.0, intent, channel_settings)
        if llm_enabled:
            result.reason = f"{result.reason}; not sent to llm judge for this source"
        else:
            result.reason = f"{result.reason}; llm judge disabled or unavailable"

    source_timings_ms["finalize_unjudged"] = elapsed_ms(finalize_unjudged_started_at)
    source_timings_ms["total"] = elapsed_ms(total_started_at)
    return source_name, candidates + dropped, source_timings_ms


async def run_deep_channel(request: SearchRequest) -> SearchResponse:
    timings_ms: dict[str, float] = {}
    total_started_at = time.perf_counter()

    step_started_at = time.perf_counter()
    intent = await plan_search_intent(request.query, request)
    timings_ms["plan_intent"] = elapsed_ms(step_started_at)
    channel_settings = get_channel_settings("deep")

    step_started_at = time.perf_counter()
    query_bundle = build_query_bundle("deep", request, intent)
    timings_ms["build_query_bundle"] = elapsed_ms(step_started_at)

    step_started_at = time.perf_counter()
    results_by_source, used_sources, raw_recall_count, recall_source_timings_ms = await recall_results_by_source(
        "deep",
        query_bundle,
        request,
    )
    timings_ms["recall"] = elapsed_ms(step_started_at)
    for source_name, source_elapsed_ms in recall_source_timings_ms.items():
        timings_ms[f"recall_source_{source_name}"] = source_elapsed_ms

    step_started_at = time.perf_counter()
    judged_groups = await asyncio.gather(
        *(
            _judge_source_results(request.query, intent, source_name, source_results, request, channel_settings)
            for source_name, source_results in results_by_source.items()
        )
    )
    timings_ms["judge_and_score"] = elapsed_ms(step_started_at)

    judge_timing_sums: dict[str, float] = {}
    judge_timing_max: dict[str, float] = {}
    merged_results: list[PaperResult] = []
    for source_name, source_results, source_timings_ms in judged_groups:
        merged_results.extend(source_results)
        for timing_key, timing_value in source_timings_ms.items():
            add_timing_ms(judge_timing_sums, timing_key, timing_value)
            max_timing_ms(judge_timing_max, timing_key, timing_value)
            timings_ms[f"judge_source_{source_name}_{timing_key}"] = timing_value

    for timing_key in ("heuristic_prefilter", "llm_window", "finalize_unjudged", "total"):
        sum_value = judge_timing_sums.get(timing_key)
        max_value = judge_timing_max.get(timing_key)
        if sum_value is not None:
            summary_key = f"judge_{timing_key}_sum" if timing_key != "total" else "judge_source_total_sum"
            timings_ms[summary_key] = sum_value
        if max_value is not None:
            summary_key = f"judge_{timing_key}_max" if timing_key != "total" else "judge_source_total_max"
            timings_ms[summary_key] = max_value

    step_started_at = time.perf_counter()
    deduped = dedup_results(merged_results)
    timings_ms["dedup"] = elapsed_ms(step_started_at)

    decision_priority = {"keep": 2, "maybe": 1, "drop": 0}
    step_started_at = time.perf_counter()
    deduped.sort(
        key=lambda item: (
            decision_priority.get(item.decision or "drop", 0),
            item.criteria_coverage or 0.0,
            item.scores.get("deep", 0.0),
            item.confidence or 0.0,
        ),
        reverse=True,
    )
    timings_ms["sort"] = elapsed_ms(step_started_at)

    step_started_at = time.perf_counter()
    finalized_results = _finalize_deep_results(deduped, intent, channel_settings)
    timings_ms["finalize"] = elapsed_ms(step_started_at)
    timings_ms["total"] = elapsed_ms(total_started_at)
    timings_ms = finalize_timings_ms(timings_ms)

    return SearchResponse(
        query=request.query,
        rewritten_query=intent.rewritten_query,
        mode="deep",
        used_sources=used_sources,
        total_results=len(finalized_results),
        raw_recall_count=raw_recall_count,
        deduped_count=len(deduped),
        finalized_count=len(finalized_results),
        timings_ms=timings_ms,
        intent=intent,
        query_bundle=query_bundle,
        results=finalized_results,
    )
