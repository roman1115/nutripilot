"""Application lifecycle wiring for external infrastructure mode."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from apps.nutrition_mcp.service import NutritionMCPService

from nutripilot.agent import create_agent_graph
from nutripilot.application.chat_runs import ChatRunService
from nutripilot.application.chat_service import ChatService
from nutripilot.application.evaluation_service import EvaluationService
from nutripilot.evaluation import EvalRunner
from nutripilot.infrastructure import PostgresDataStore, RedisTTLStore, StaticWeatherProvider
from nutripilot.infrastructure.chat_tasks import ChatTaskStore
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

    if container.chat_runs is not None:
        return
    if not container.settings.uses_external_infrastructure:
        tasks = ChatTaskStore.create(queue=container.settings.chat_queue)
        await asyncio.to_thread(tasks.initialize)
        if container.run_store:
            await asyncio.to_thread(tasks.import_history, container.run_store)
        container.chat_service.history = tasks
        container.chat_runs = ChatRunService(
            container.chat_service, tasks, container.settings.chat_max_queued
        )
        container.evaluation_service.runner = EvalRunner(
            container.chat_service, container.chat_runs, meal_repository=container.data_store
        )
        from nutripilot.application.chat_worker import ChatWorker

        container.chat_worker = ChatWorker(container.chat_service, tasks, container.settings)
        container.chat_worker_task = asyncio.create_task(container.chat_worker.run())
        await container.chat_worker.ready.wait()
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
            context_window=container.settings.model_context_window,
        )
        tasks = ChatTaskStore.create(container.settings.database_url, container.settings.chat_queue)
        await asyncio.to_thread(tasks.initialize)
        stack.callback(tasks.close)
        if container.run_store:
            await asyncio.to_thread(tasks.import_history, container.run_store)
        chat_service = ChatService(
            graph,
            tasks,
            timezone_name=container.settings.user_timezone,
        )

        container.data_store = store
        container.tool_executor = executor
        container.graph = graph
        container.chat_service = chat_service
        container.chat_runs = ChatRunService(
            chat_service, tasks, container.settings.chat_max_queued
        )
        container.evaluation_service = EvaluationService(
            EvalRunner(chat_service, container.chat_runs, meal_repository=store),
            container.run_store,
        )
        container.resource_stack = stack
    except BaseException:
        await stack.aclose()
        raise


async def stop_container(container: ApplicationContainer) -> None:
    if container.chat_worker is not None:
        await container.chat_worker.close()
    if container.chat_worker_task is not None:
        await container.chat_worker_task
        container.chat_worker_task = None
    if container.chat_runs is not None and not container.settings.uses_external_infrastructure:
        container.chat_runs.store.close()
    container.chat_runs = None
    await container.evaluation_service.close()
    stack = container.resource_stack
    container.resource_stack = None
    if stack is not None:
        await stack.aclose()


async def _seed_postgres(store: PostgresDataStore, data_directory: Path) -> None:
    seed = load_seed_store(data_directory)
    await store.seed(await seed.all_meals(), await seed.list_guidelines())
    profile = await seed.get_user_profile(1)
    await store.seed_user_profile(profile)


@asynccontextmanager
async def _postgres_checkpointer(container: ApplicationContainer) -> AsyncIterator[Any]:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg import AsyncConnection
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    async with AsyncConnectionPool[AsyncConnection[dict[str, Any]]](
        _psycopg_url(container.settings.database_url),
        min_size=1,
        max_size=4,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        check=AsyncConnectionPool.check_connection,
    ) as pool:
        await pool.wait()
        yield AsyncPostgresSaver(pool)


def _psycopg_url(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)
