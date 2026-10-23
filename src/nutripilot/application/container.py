"""Dependency composition with a credential-free default runtime."""

from __future__ import annotations

from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from apps.nutrition_mcp.service import NutritionMCPService

from nutripilot.agent import AgentGraph, create_agent_graph
from nutripilot.application.chat_service import ChatService
from nutripilot.application.evaluation_service import EvaluationService
from nutripilot.config import Settings, get_settings
from nutripilot.evaluation import EvalRunner
from nutripilot.infrastructure import InMemoryDataStore, StaticWeatherProvider, load_seed_store
from nutripilot.infrastructure.history import InMemoryConversationStore, SQLiteConversationStore
from nutripilot.infrastructure.run_store import SQLiteRunStore
from nutripilot.model_gateway import (
    FakeModelGateway,
    ModelGateway,
    OpenAICompatibleModelGateway,
)
from nutripilot.paths import project_root
from nutripilot.retrieval import (
    BGEEmbedder,
    BGEReranker,
    HashingEmbedder,
    LexicalReranker,
    RetrievalService,
)
from nutripilot.retrieval.ports import Embedder, MealReranker
from nutripilot.tools import ToolExecutor, ToolRegistry
from nutripilot.tools.local import register_local_tools
from nutripilot.tools.mcp import (
    InProcessMCPTransport,
    MCPToolAdapter,
    StreamableHTTPMCPTransport,
)
from nutripilot.tools.mcp.transport import MCPTransport
from nutripilot.tracing import InMemoryTraceRecorder, TraceRecorder
from nutripilot.tracing.recorder import SQLiteTraceRecorder


@dataclass(slots=True)
class ApplicationContainer:
    settings: Settings
    gateway: ModelGateway
    recorder: TraceRecorder
    data_store: Any
    tool_executor: ToolExecutor
    graph: AgentGraph
    chat_service: ChatService
    evaluation_service: EvaluationService
    resource_stack: AsyncExitStack | None = None
    runtime_mode: str = "demo"
    run_store: SQLiteRunStore | None = None


def build_container(
    *,
    gateway: ModelGateway | None = None,
    recorder: TraceRecorder | None = None,
    tool_executor: ToolExecutor | None = None,
    data_store: InMemoryDataStore | None = None,
    settings: Settings | None = None,
) -> ApplicationContainer:
    selected_settings = settings or get_settings()
    selected_gateway = gateway or _build_gateway(selected_settings)
    run_path = selected_settings.run_store_path
    if not run_path and selected_settings.uses_external_infrastructure:
        run_path = str(project_root() / ".runtime" / "observability.sqlite3")
    run_store = SQLiteRunStore(run_path) if run_path else None
    if run_store:
        run_store.interrupt_running_reports()
    selected_recorder = recorder or (
        SQLiteTraceRecorder(run_store) if run_store else InMemoryTraceRecorder()
    )
    selected_store = data_store or load_seed_store(project_root() / "data")
    selected_executor = tool_executor or _build_tool_executor(selected_store, selected_settings)
    graph = create_agent_graph(
        gateway=selected_gateway,
        tool_executor=selected_executor,
        recorder=selected_recorder,
        max_tool_steps=selected_settings.agent_max_steps,
        deadline_seconds=selected_settings.agent_deadline_seconds,
        token_budget=selected_settings.agent_token_budget,
    )
    chat_service = ChatService(
        graph, SQLiteConversationStore(run_store) if run_store else InMemoryConversationStore()
    )
    return ApplicationContainer(
        settings=selected_settings,
        gateway=selected_gateway,
        recorder=selected_recorder,
        data_store=selected_store,
        tool_executor=selected_executor,
        graph=graph,
        chat_service=chat_service,
        evaluation_service=EvaluationService(EvalRunner(chat_service), run_store),
        runtime_mode=selected_settings.app_mode,
        run_store=run_store,
    )


def _build_gateway(settings: Settings) -> ModelGateway:
    if settings.uses_fake_model:
        return FakeModelGateway()
    assert settings.model_api_key is not None
    return OpenAICompatibleModelGateway(
        api_key=settings.model_api_key,
        base_url=settings.model_base_url,
        model=settings.model_name,
        fallback_model=settings.model_fallback_name,
        timeout_seconds=settings.model_timeout_seconds,
        max_retries=settings.model_max_retries,
    )


def _build_tool_executor(store: InMemoryDataStore, settings: Settings) -> ToolExecutor:
    registry = register_local_tools(ToolRegistry(), store, StaticWeatherProvider())
    if settings.mcp_enabled:
        transport: MCPTransport
        if settings.uses_external_infrastructure:
            transport = StreamableHTTPMCPTransport(
                settings.mcp_server_url,
                token=settings.mcp_internal_token,
                timeout_seconds=settings.mcp_timeout_seconds,
            )
        else:
            nutrition_service = NutritionMCPService(
                store,
                RetrievalService(
                    store,
                    embedder=_build_embedder(settings),
                    reranker=_build_reranker(settings),
                ),
            )
            transport = InProcessMCPTransport(nutrition_service)
        adapter = MCPToolAdapter(
            transport,
            call_timeout_seconds=settings.mcp_timeout_seconds,
        )
        adapter.register_contracts(registry)
    return ToolExecutor(registry, max_steps=settings.agent_max_steps)


def _build_embedder(settings: Settings) -> Embedder:
    if settings.embedding_provider == "bge":
        return BGEEmbedder(settings.embedding_model, device=settings.embedding_device)
    return HashingEmbedder()


def _build_reranker(settings: Settings) -> MealReranker:
    if settings.rerank_provider == "bge":
        return BGEReranker(settings.rerank_model, device=settings.rerank_device)
    return LexicalReranker()
