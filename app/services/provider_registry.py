from __future__ import annotations

from app.connectors import (
    ArxivClient,
    CoreClient,
    CrossrefClient,
    IEEEClient,
    OpenAlexClient,
    SemanticScholarClient,
    UnpaywallClient,
)
from app.connectors.base import BaseSourceClient
from app.domain.schemas import ProviderConfigSummary
from config import get_settings


CLIENT_CLASSES: dict[str, type[BaseSourceClient]] = {
    "openalex": OpenAlexClient,
    "semanticscholar": SemanticScholarClient,
    "core": CoreClient,
    "crossref": CrossrefClient,
    "unpaywall": UnpaywallClient,
    "arxiv": ArxivClient,
    "ieee": IEEEClient,
}


def build_clients() -> dict[str, BaseSourceClient]:
    settings = get_settings()
    sources = settings.get("sources", {})
    clients: dict[str, BaseSourceClient] = {}
    for name, client_cls in CLIENT_CLASSES.items():
        if name in sources:
            clients[name] = client_cls(name=name, settings=sources[name])
    return clients


def list_provider_summaries() -> list[ProviderConfigSummary]:
    clients = build_clients()
    summaries: list[ProviderConfigSummary] = []
    for name, client in clients.items():
        summaries.append(
            ProviderConfigSummary(
                name=name,
                enabled=client.enabled,
                public_enabled=client.public_enabled,
                supports_quick=client.supports_mode("quick"),
                supports_deep=client.supports_mode("deep"),
                supports_fusion=client.supports_mode("fusion"),
                has_credentials=client.has_credentials(),
            )
        )
    return summaries


def get_clients_for_mode(mode: str, sources: list[str] | None = None, public_only: bool = True) -> list[BaseSourceClient]:
    clients = build_clients()
    selected = []
    for name, client in clients.items():
        if sources and name not in sources:
            continue
        if not client.enabled:
            continue
        if public_only and not client.public_enabled:
            continue
        if not client.supports_mode(mode):
            continue
        selected.append(client)
    return selected
