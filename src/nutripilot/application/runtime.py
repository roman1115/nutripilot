"""Application lifecycle wiring for external infrastructure mode."""

from __future__ import annotations

from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any

from apps.nutrition_mcp.service import NutritionMCPService

from nutripilot.agent import create_agent_graph
from nutripilot.application.chat_service import ChatService
from nutripilot.application.evaluation_service import EvaluationService
from nutripilot.evaluation import EvalRunner
from nutripilot.infrastructure import PostgresDataStore, RedisTTLStore, StaticWeatherProvider
from nutripilot.infrastructure.memory import load_seed_store
from nutripilot.paths import project_root
from nutripilot.retrieval import RetrievalService
from nutripilot.tools import ToolExecutor, ToolRegistry
from nutripilot.tools.local import register_local_tools
from nutripilot.tools.mcp import (
    InProcessMCPTransport,
    MCPToolAdapter,
    StreamableHTTPMCPTransport,
)
from nutripilot.tools.mcp.transport import MCPTransport

if TYPE_CHECKING:
    from nutripilot.application.container import ApplicationContainer


async def start_container(container: ApplicationContainer) -> None:
    """Replace demo adapters with deployed adapters before accepting traffic."""

    if not container.settings.uses_external_infrastructure or container.resource_stack:
        return

    stack = AsyncExitStack()
    try:
        store = PostgresDataStore.from_url(container.settings.database_url)
        await store.initialize()
        stack.push_async_callback(store.close)
        await _seed_postgres(store, project_root() / "data")

        redis_store = RedisTTLStore.from_url(container.settings.redis_url)
        await redis_store.client.ping()
        stack.push_async_callback(redis_store.close)

        registry = register_local_tools(ToolRegistry(), store, StaticWeatherProvider())
        transport: MCPTransport
        if container.settings.mcp_enabled:
            transport = StreamableHTTPMCPTransport(
                container.settings.mcp_server_url,
                token=container.settings.mcp_internal_token,
                timeout_seconds=container.settings.mcp_timeout_seconds,
            )
        else:
            service = NutritionMCPService(store, RetrievalService(store))
            transport = InProcessMCPTransport(service)
        adapter = MCPToolAdapter(
            transport,
            call_timeout_seconds=container.settings.mcp_timeout_seconds,
        )
        adapter.register_contracts(registry)

        executor = ToolExecutor(
            registry,
            idempotency_store=redis_store,
            trace_sink=container.recorder,
            max_steps=container.settings.agent_max_steps,
        )
        checkpointer = await stack.enter_async_context(_postgres_checkpointer(container))
        await checkpointer.setup()
        graph = create_agent_graph(
            gateway=container.gateway,
            tool_executor=executor,
            recorder=container.recorder,
            checkpointer=checkpointer,
            max_tool_steps=container.settings.agent_max_steps,
            deadline_seconds=container.settings.agent_deadline_seconds,
            token_budget=container.settings.agent_token_budget,
        )
        chat_service = ChatService(
            graph,
            container.chat_service.history,
            timezone_name=container.settings.user_timezone,
        )

        container.data_store = store
        container.tool_executor = executor
        container.graph = graph
        container.chat_service = chat_service
        container.evaluation_service = EvaluationService(
            EvalRunner(chat_service), container.run_store
        )
        container.resource_stack = stack
    except BaseException:
        await stack.aclose()
        raise


async def stop_container(container: ApplicationContainer) -> None:
    await container.evaluation_service.close()
    stack = container.resource_stack
    container.resource_stack = None
    if stack is not None:
        await stack.aclose()


async def _seed_postgres(store: PostgresDataStore, data_directory: Path) -> None:
    seed = load_seed_store(data_directory)
    await store.seed(await seed.list_meals(), await seed.list_guidelines())
    profile = await seed.get_user_profile(1)
    await store.seed_user_profile(profile)


def _postgres_checkpointer(container: ApplicationContainer) -> Any:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    return AsyncPostgresSaver.from_conn_string(_psycopg_url(container.settings.database_url))


def _psycopg_url(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)
