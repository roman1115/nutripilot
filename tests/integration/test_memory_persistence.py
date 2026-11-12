"""Memory checkpoint integration against an explicit local PostgreSQL endpoint."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from langgraph.graph import END, START, StateGraph

from nutripilot.agent.state import AgentState, new_agent_state
from nutripilot.application import build_container
from nutripilot.application.history import ConversationMessage
from nutripilot.application.runtime import _postgres_checkpointer
from nutripilot.config import Settings
from nutripilot.infrastructure.chat_tasks import ChatTaskStore
from nutripilot.infrastructure.checkpointer import FencedCheckpointer
from nutripilot.memory.nodes import MemoryNodes
from nutripilot.model_gateway import FakeModelGateway
from nutripilot.model_gateway.budget import BudgetedModelGateway, execution_budget
from nutripilot.tracing import InMemoryTraceRecorder

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("NUTRIPILOT_TEST_EXTERNAL") != "1", reason="Local PostgreSQL required",
    ),
]


@pytest.mark.asyncio
async def test_postgres_summary_removal_and_archive_survive_pool_recreation():
    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://nutripilot:nutripilot@127.0.0.1:5432/nutripilot",
    )
    container = build_container(settings=settings)
    store = ChatTaskStore.create(settings.database_url, f"memory-{uuid4().hex}")
    store.initialize()
    thread, current = uuid4().hex, uuid4().hex
    user_id = 7_000_000_000 + uuid4().int % 1_000_000_000
    ids = []
    for index in range(9):
        turn = uuid4().hex
        for role, suffix in [("user", "user"), ("assistant", "response")]:
            message_id = f"{turn}:{suffix}"
            ids.append(message_id)
            store.append(user_id, thread, ConversationMessage(
                id=message_id, role=role, content=f"{index}:{role}",
                response={"status": "completed"} if role == "assistant" else None,
            ))
    store.append(user_id, thread, ConversationMessage(
        id=f"{current}:user", role="user", content="继续",
    ))
    memory = MemoryNodes(
        BudgetedModelGateway(FakeModelGateway()), InMemoryTraceRecorder(),
    )
    memory.history = store

    def build(saver):
        graph = StateGraph(AgentState)
        graph.add_node("load", memory.load)
        graph.add_node("summarize", memory.summarize)
        graph.add_node("freeze", memory.freeze)
        graph.add_edge(START, "load")
        graph.add_edge("load", "summarize")
        graph.add_edge("summarize", "freeze")
        graph.add_edge("freeze", END)
        return graph.compile(checkpointer=FencedCheckpointer(saver))

    config = {"configurable": {"thread_id": thread}}
    try:
        async with _postgres_checkpointer(container) as saver:
            await saver.setup()
            compiled = build(saver)
            state = new_agent_state(
                user_id=user_id, thread_id=thread, turn_id=current, message="继续",
            )
            state["compression_candidate_ids"] = ids[:6]
            with execution_budget(seconds=15, tokens=24000):
                await compiled.ainvoke(
                    state, config, interrupt_after=["summarize"], durability="sync",
                )
            checkpoint = await compiled.aget_state(config)
            assert checkpoint.next == ("freeze",)
            assert checkpoint.values["memory_summary"]["covered_through_position"] == 6
        async with _postgres_checkpointer(container) as saver:
            compiled = build(saver)
            result = await compiled.ainvoke(None, config, durability="sync")
            assert not (await compiled.aget_state(config)).next
            assert len(result["messages"]) == 13
            assert len(result["context_snapshot"]["recent_messages"]) == 12
            assert result["memory_summary"]["version"] == 1
            assert len(store.messages(user_id, thread, 100, 0)) == 19
            # Removing current state messages does not erase older checkpoint content.
            assert len([item async for item in compiled.aget_state_history(config)]) >= 4
    finally:
        async with _postgres_checkpointer(container) as saver:
            await saver.adelete_thread(thread)
        store.delete_thread(user_id, thread)
        store.purge_thread(thread)
        store.close()
