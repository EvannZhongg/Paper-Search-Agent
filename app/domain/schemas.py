from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


SearchMode = Literal["quick", "deep", "fusion"]
ProviderStatusState = Literal["ok", "error", "disabled", "skipped"]


class SearchCriterion(BaseModel):
    id: str
    description: str
    required: bool = True
    terms: list[str] = Field(default_factory=list)
    query_hints: list[str] = Field(default_factory=list)


class CriterionJudgment(BaseModel):
    criterion_id: str
    description: str
    required: bool = True
    supported: bool = False
    score: float | None = None
    confidence: float | None = None
    evidence: list[str] = Field(default_factory=list)
    reason: str | None = None


class QueryBundleItem(BaseModel):
    label: str
    query: str
    purpose: str | None = None


class RetrievalTrace(BaseModel):
    mode: SearchMode
    query_label: str
    query: str
    rendered_query: str | None = None
    purpose: str | None = None


class PaperResult(BaseModel):
    source: str
    source_id: str | None = None
    title: str
    abstract: str | None = None
    year: int | None = None
    doi: str | None = None
    url: str | None = None
    pdf_url: str | None = None
    is_oa: bool | None = None
    authors: list[str] = Field(default_factory=list)
    score: float | None = None
    scores: dict[str, float] = Field(default_factory=dict)
    decision: str | None = None
    confidence: float | None = None
    reason: str | None = None
    matched_fields: list[str] = Field(default_factory=list)
    criteria_coverage: float | None = None
    criterion_judgments: list[CriterionJudgment] = Field(default_factory=list)
    retrieval_traces: list[RetrievalTrace] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    sources: list[str] | None = None
    limit_per_source: int | None = Field(default=None, ge=1, le=25)
    public_only: bool = True
    llm_top_n: int | None = Field(default=None, ge=1, le=25)
    enable_llm: bool = True
    enable_intent_planner: bool = True


class SearchIntent(BaseModel):
    original_query: str
    rewritten_query: str
    must_terms: list[str] = Field(default_factory=list)
    should_terms: list[str] = Field(default_factory=list)
    exclude_terms: list[str] = Field(default_factory=list)
    filters: dict[str, Any] = Field(default_factory=dict)
    logic: str = "AND"
    criteria: list[SearchCriterion] = Field(default_factory=list)
    planner: str = "heuristic"
    reasoning: str | None = None


class SearchResponse(BaseModel):
    query: str
    rewritten_query: str
    mode: SearchMode
    used_sources: list[str]
    total_results: int
    raw_recall_count: int = Field(default=0, ge=0)
    deduped_count: int = Field(default=0, ge=0)
    finalized_count: int = Field(default=0, ge=0)
    timings_ms: dict[str, float] = Field(default_factory=dict)
    intent: SearchIntent
    query_bundle: list[QueryBundleItem] = Field(default_factory=list)
    results: list[PaperResult]


class ProviderConfigSummary(BaseModel):
    name: str
    enabled: bool
    public_enabled: bool
    supports_quick: bool
    supports_deep: bool
    supports_fusion: bool
    has_credentials: bool


class ProbeResult(BaseModel):
    name: str
    status: ProviderStatusState
    message: str
    http_status: int | None = None
    latency_ms: int | None = None
    used_credentials: bool = False
    sample_title: str | None = None


class ProvidersStatusResponse(BaseModel):
    mode: SearchMode = "quick"
    providers: list[ProbeResult]
