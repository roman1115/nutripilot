from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from nutripilot.agent.state import AgentState, new_agent_state
from nutripilot.application import build_container
from nutripilot.application.history import ConversationMessage
from nutripilot.config import Settings
from nutripilot.infrastructure.chat_tasks import ChatTaskStore
from nutripilot.infrastructure.history import InMemoryConversationStore, SQLiteConversationStore
from nutripilot.infrastructure.run_store import SQLiteRunStore
from nutripilot.memory.context import conversation_context, ordered_messages
from nutripilot.memory.nodes import MemoryNodes, archived_message
from nutripilot.model_gateway import FakeModelGateway
from nutripilot.model_gateway.budget import (
    BudgetedModelGateway,
    ExecutionLimitExceeded,
    execution_budget,
)
from nutripilot.tracing import InMemoryTraceRecorder


def memory_state(rounds: int = 9) -> AgentState:
    state = new_agent_state(message="谢谢", user_id=1, thread_id="memory", turn_id="current")
    state["messages"] = [
        archived_message(ConversationMessage(
            id=f"turn-{index}:{suffix}", role=role,
            content=f"第{index}轮的{role}正文", position=index * 2 + offset,
            response={"status": "completed"} if role == "assistant" else None,
        ))
        for index in range(rounds)
        for offset, suffix, role in [(1, "user", "user"), (2, "response", "assistant")]
    ]
    state["messages"].append(HumanMessage(id="current:user", content="谢谢"))
    return state


def nodes(gateway: FakeModelGateway | None = None) -> MemoryNodes:
    return MemoryNodes(
        BudgetedModelGateway(gateway or FakeModelGateway()), InMemoryTraceRecorder(),
    )


def test_candidates_are_complete_prefix_and_keep_six_rounds():
    state = memory_state()
    candidates = nodes().candidates(state)
    assert [message.id for message in candidates] == [
        f"turn-{index}:{suffix}" for index in range(3) for suffix in ("user", "response")
    ]
    state["messages"][1].additional_kwargs["status"] = "confirmation_required"
    assert not nodes().candidates(state)
    state["messages"].insert(2, HumanMessage(
        id="turn-0:decision", content="已拒绝本次写入操作。",
        additional_kwargs={"turn_id": "turn-0", "position": 2},
    ))
    state["messages"].insert(3, AIMessage(
        id="turn-0:confirmation", content="已取消",
        additional_kwargs={"turn_id": "turn-0", "position": 2, "status": "cancelled"},
    ))
    assert len(nodes().candidates(state)) == 8


@pytest.mark.asyncio
async def test_summary_and_removal_resume_as_one_graph_step():
    memory = nodes()
    graph = StateGraph(AgentState)
    graph.add_node("summarize", memory.summarize)
    graph.add_node("freeze", memory.freeze)
    graph.add_edge(START, "summarize")
    graph.add_edge("summarize", "freeze")
    graph.add_edge("freeze", END)
    saver = InMemorySaver()
    compiled = graph.compile(checkpointer=saver)
    state = memory_state()
    state["compression_candidate_ids"] = [
        str(message.id) for message in memory.candidates(state)
    ]
    config = {"configurable": {"thread_id": "memory"}}
    with execution_budget(seconds=5, tokens=24000):
        await compiled.ainvoke(state, config, interrupt_after=["summarize"], durability="sync")
    checkpoint = await compiled.aget_state(config)
    assert checkpoint.next == ("freeze",)
    assert checkpoint.values["memory_summary"]["covered_through_position"] == 6
    assert checkpoint.values["memory_summary"]["version"] == 1
    assert len(checkpoint.values["messages"]) == 13
    restored = graph.compile(checkpointer=saver)
    result = await restored.ainvoke(None, config, durability="sync")
    assert not (await restored.aget_state(config)).next
    assert result["context_snapshot"]["summary_version"] == 1
    assert len(result["context_snapshot"]["recent_messages"]) == 12
    assert all(item["content"] != "谢谢" for item in
               result["context_snapshot"]["recent_messages"])


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [False, True])
async def test_failed_summary_keeps_originals_and_coverage(invalid):
    gateway = FakeModelGateway()
    gateway.queue_response("memory_summary", {
        "entries": [{"kind": "user", "text": "invalid",
                     "source_message_ids": ["does-not-exist"]}],
    } if invalid else RuntimeError("offline"))
    memory = nodes(gateway)
    state = memory_state()
    state["compression_candidate_ids"] = [str(m.id) for m in memory.candidates(state)]
    with execution_budget(seconds=5, tokens=24000):
        update = await memory.summarize(state)
    assert update == {"compression_attempted": True, "compression_requested": False}
    assert len(state["messages"]) == 19
    assert not state["memory_summary"]


@pytest.mark.asyncio
async def test_summary_budget_failure_does_not_start_a_call():
    gateway = FakeModelGateway()
    memory = nodes(gateway)
    state = memory_state()
    state["compression_candidate_ids"] = [str(m.id) for m in memory.candidates(state)]
    with execution_budget(seconds=5, tokens=100):
        update = await memory.summarize(state)
    assert not gateway.call_history
    assert "messages" not in update


@pytest.mark.asyncio
async def test_rolling_summary_uses_old_sources_and_advances_only_the_selected_prefix():
    gateway = FakeModelGateway()
    state = memory_state(10)
    state["memory_summary"] = {
        "version": 2, "covered_through_position": 2,
        "entries": [{"kind": "correction", "text": "已取消旧预算",
                     "source_message_ids": ["older:user"]}],
    }
    state["messages"] = state["messages"][2:]
    memory = nodes(gateway)
    state["compression_candidate_ids"] = [str(m.id) for m in memory.candidates(state)]
    with execution_budget(seconds=5, tokens=24000):
        update = await memory.summarize(state)
    assert update["memory_summary"]["version"] == 3
    assert update["memory_summary"]["covered_through_position"] == 8
    assert update["memory_summary"]["entries"][0]["kind"] == "correction"
    assert len(update["messages"]) == 6


@pytest.mark.asyncio
async def test_new_turn_keeps_summary_and_both_roles_without_duplicating_current_input():
    container = build_container(settings=Settings(_env_file=None))
    first = await container.chat_service.chat(user_id=1, message="你好", thread_id="carry")
    await container.graph.compiled.aupdate_state(
        container.chat_service._config("carry"),
        {"memory_summary": {"version": 1, "entries": [
            {"kind": "user", "text": "曾讨论晚餐", "source_message_ids": ["older:user"]},
        ]}},
        as_node="memory_finalize",
    )
    second = await container.chat_service.chat(user_id=1, message="谢谢", thread_id="carry")
    assert second["memory_summary"]["version"] == 1
    assert len(second["messages"]) == 4
    payloads = [
        call["payload"] for call in container.gateway.call_history  # type: ignore[attr-defined]
        if call["payload"].get("message") == "谢谢"
        or call["payload"].get("user_message") == "谢谢"
    ]
    assert len(payloads) == 2
    for payload in payloads:
        assert payload["conversation_summary"]
        assert payload["recent_messages"] == [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": first["final_response"]["message"]},
        ]
    messages = await container.chat_service.messages(user_id=1, thread_id="carry")
    assert len(messages) == 4
    assert messages[-1].content == second["messages"][-1].content


@pytest.mark.asyncio
async def test_capacity_preflight_counts_schema_and_never_calls_an_oversized_request():
    from nutripilot.agent.contracts import IntentOutput

    gateway = FakeModelGateway()
    with (
        execution_budget(seconds=5, tokens=24000, context_window=1024),
        pytest.raises(ExecutionLimitExceeded, match="CONTEXT_WINDOW_EXCEEDED"),
    ):
        await BudgetedModelGateway(gateway).generate_structured(
            worker="intent", system_prompt="rules", payload={},
            response_model=IntentOutput, prompt_version="test",
        )
    assert not gateway.call_history


@pytest.mark.parametrize("adapter", ["memory", "sqlite", "tasks"])
def test_incremental_history_has_stable_positions_and_owner_isolation(tmp_path, adapter):
    store: Any
    if adapter == "memory":
        store = InMemoryConversationStore()
    elif adapter == "sqlite":
        store = SQLiteConversationStore(SQLiteRunStore(tmp_path / "history.sqlite"))
    else:
        store = ChatTaskStore.create()
        store.initialize()
    for index in range(4):
        store.append(1, "thread", ConversationMessage(
            id=f"id-{index}", role="user", content=str(index),
        ))
    first = store.after_position(1, "thread", 0, 2)
    rest = store.after_position(1, "thread", first[-1].position, 2)
    assert [message.id for message in first + rest] == [f"id-{i}" for i in range(4)]
    assert [message.position for message in first + rest] == [1, 2, 3, 4]
    assert [match.message.content for match in store.search_messages(
        1, "thread", "2", 3,
    )] == ["2"]
    assert store.search_messages(2, None, "2", 3) == []
    store.append(1, "thread", first[0])
    with pytest.raises(ValueError):
        store.append(1, "thread", first[0].model_copy(update={"content": "conflict"}))
    if adapter == "tasks":
        from nutripilot.application.chat_service import ThreadNotFoundError

        with pytest.raises(ThreadNotFoundError):
            store.after_position(2, "thread", 0, 10)
        store.delete_thread(1, "thread")
        with pytest.raises(ThreadNotFoundError):
            store.after_position(1, "thread", 0, 10)
        assert store.search_messages(1, None, "2", 3) == []
        store.close()
    else:
        assert store.after_position(2, "thread", 0, 10) == []


def test_prompt_projection_sorts_archive_positions_and_does_not_mutate_state():
    state = memory_state(2)
    state["messages"] = list(reversed(state["messages"]))
    original = list(state["messages"])
    assert ordered_messages(state)[0].id == "turn-0:user"
    assert len(conversation_context(state)["recent_messages"]) == 4
    assert state["messages"] == original


@pytest.mark.asyncio
async def test_pressure_uses_capacity_ratio_and_rounds_alone_do_not_trigger():
    state = memory_state(10)
    memory = nodes()
    with execution_budget(seconds=5, tokens=1_000_000):
        assert not (await memory.prepare(state))["compression_requested"]
    for message in state["messages"]:
        message.content = str(message.content) * 40
    memory.context_window = 4000
    with execution_budget(seconds=5, tokens=1_000_000):
        assert (await memory.prepare(state))["compression_requested"]
    state["compression_attempted"] = True
    with execution_budget(seconds=5, tokens=1_000_000):
        assert not (await memory.prepare(state))["compression_requested"]
    with execution_budget(seconds=5, tokens=1):
        assert not (await memory.prepare(memory_state()))["compression_requested"]


@pytest.mark.asyncio
async def test_history_recall_is_scoped_and_intent_recheck_is_bounded():
    gateway = FakeModelGateway()
    container = build_container(gateway=gateway, settings=Settings(_env_file=None))
    await container.chat_service.chat(user_id=1, thread_id="first", message="你好")
    await container.chat_service.chat(user_id=1, thread_id="other", message="你好")
    await container.chat_service.chat(user_id=2, thread_id="private", message="你好")
    for _ in range(2):
        gateway.queue_response("intent", {
            "intent": "CONVERSATION", "confidence": 1, "history_query": "你好",
        })
    second = await container.chat_service.chat(
        user_id=1, thread_id="first", message="之前说过什么",
    )
    assert second["history_recalled"]
    assert {snippet["thread_id"] for snippet in
            second["context_snapshot"]["history_recall"]} == {"first"}
    before = len(gateway.call_history)
    for _ in range(2):
        gateway.queue_response("intent", {
            "intent": "CONVERSATION", "confidence": 1, "history_query": "你好",
        })
    cross = await container.chat_service.chat(
        user_id=1, thread_id="first", message="其他会话里之前说过什么",
    )
    assert {snippet["thread_id"] for snippet in
            cross["context_snapshot"]["history_recall"]} == {"first", "other"}
    assert [call["worker"] for call in gateway.call_history[before:]] == [
        "intent", "intent", "recommendation",
    ]


def test_tool_projection_deduplicates_only_identical_facts():
    from nutripilot.agent.agents.base import compact_tool_results

    first = {"tool_name": "get_recipe", "status": "success",
             "data": {"meal_id": 1, "raw_quantities": "豆腐1盒", "version": "1"}}
    second = {"tool_name": "get_recipe", "status": "success",
              "data": {"meal_id": 1, "raw_quantities": "豆腐300克", "version": "2"}}
    assert compact_tool_results([first, first, second]) == [first, second]


@pytest.mark.asyncio
async def test_summary_preserves_source_object_order_and_decisions_are_unprivileged():
    state = memory_state()
    state["messages"][1].additional_kwargs["displayed_meals"] = [
        {"meal_id": 20, "name": "第二十号菜"}, {"meal_id": 10, "name": "第十号菜"},
    ]
    memory = nodes()
    state["compression_candidate_ids"] = [str(m.id) for m in memory.candidates(state)]
    with execution_budget(seconds=5, tokens=24000):
        update = await memory.summarize(state)
    assert update["memory_summary"]["objects"] == [{
        "source_message_id": "turn-0:response",
        "displayed_meals": [
            {"meal_id": 20, "name": "第二十号菜"}, {"meal_id": 10, "name": "第十号菜"},
        ],
    }]
    decision = archived_message(ConversationMessage(
        id="turn:decision", role="system", content="已确认操作", position=1,
    ))
    assert decision.type == "human"
    assert decision.additional_kwargs["history_event"]


@pytest.mark.asyncio
async def test_summary_cost_is_charged_and_freeze_checks_the_remaining_budget_again():
    memory = nodes()
    state = memory_state()
    for message in state["messages"][:6]:
        message.content = str(message.content) * 50
    state["compression_candidate_ids"] = [str(m.id) for m in memory.candidates(state)]
    with execution_budget(seconds=5, tokens=24000) as budget:
        before = memory.pressure(state)
        update = await memory.summarize(state)
        assert budget.remaining_tokens < 24000
        state["memory_summary"] = update["memory_summary"]
        state["messages"] = add_messages(state["messages"], update["messages"])
        after = memory.pressure(state)
        assert after[0] < before[0]
        budget.add_usage(budget.remaining_tokens - 100, 0)
        with pytest.raises(ExecutionLimitExceeded, match="TOKEN_BUDGET_EXCEEDED"):
            await memory.freeze(state)
