import pytest
from apps.nutrition_mcp.service import (
    ExternalNutritionMCPService,
    _build_embedder,
    _build_reranker,
    build_default_service,
)
from pydantic import ValidationError

from nutripilot.application import build_container
from nutripilot.application.runtime import _psycopg_url, start_container, stop_container
from nutripilot.config import Settings
from nutripilot.retrieval.embedding import BGEEmbedder
from nutripilot.retrieval.rerank import BGEReranker


@pytest.mark.asyncio
async def test_demo_runtime_does_not_open_external_resources() -> None:
    container = build_container(settings=Settings(app_mode="demo", model_provider="fake"))

    await start_container(container)
    await stop_container(container)

    assert container.resource_stack is None
    assert container.runtime_mode == "demo"


@pytest.mark.asyncio
async def test_external_mcp_service_exposes_contracts_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_MODE", "docker")
    service = build_default_service()

    assert isinstance(service, ExternalNutritionMCPService)
    assert {tool.name for tool in await service.list_tools()} == {
        "get_nutrition",
        "query_dietary_guidelines",
        "search_meals",
    }
    assert service._delegate is None


def test_asyncpg_url_is_converted_for_langgraph_psycopg_checkpointer() -> None:
    assert _psycopg_url("postgresql+asyncpg://user:pass@postgres/db") == (
        "postgresql://user:pass@postgres/db"
    )
    assert _psycopg_url("postgresql://user:pass@postgres/db") == (
        "postgresql://user:pass@postgres/db"
    )


def test_settings_reject_unknown_user_timezone() -> None:
    with pytest.raises(ValidationError, match="USER_TIMEZONE"):
        Settings(_env_file=None, user_timezone="Mars/Olympus")


def test_bge_provider_wiring_is_lazy_and_uses_configured_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "bge")
    monkeypatch.setenv("EMBEDDING_MODEL", "test/bge-embedding")
    monkeypatch.setenv("RERANK_PROVIDER", "bge")
    monkeypatch.setenv("RERANK_MODEL", "test/bge-reranker")

    embedder = _build_embedder()
    reranker = _build_reranker()

    assert isinstance(embedder, BGEEmbedder)
    assert embedder.model_name == "test/bge-embedding"
    assert embedder._model is None
    assert isinstance(reranker, BGEReranker)
    assert reranker.model_name == "test/bge-reranker"
    assert reranker._model is None
