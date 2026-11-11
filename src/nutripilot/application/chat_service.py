"""Use-case service for starting, continuing, and confirming conversations."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

from anyio import CancelScope
from langchain_core.messages import HumanMessage

from nutripilot.agent.agents.base import compact_tool_results
from nutripilot.agent.graph import AgentGraph
from nutripilot.agent.slots import today_in_timezone
from nutripilot.agent.state import AgentState, new_agent_state
from nutripilot.application.history import (
    ConversationMessage,
    ConversationStore,
    ConversationSummary,
)
from nutripilot.domain.enums import Intent, Phase


class ThreadNotFoundError(LookupError):
    pass


class InvalidConfirmationStateError(ValueError):
    pass


class ThreadBusyError(RuntimeError):
    pass


class ChatService:
    def __init__(
        self,
        graph: AgentGraph,
        history: ConversationStore | None = None,
        *,
        timezone_name: str = "America/Los_Angeles",
    ) -> None:
        self.graph = graph
        self.history = history
        self.timezone_name = timezone_name
        ZoneInfo(timezone_name)
        self._active: dict[str, tuple[int, asyncio.Task[Any]]] = {}

    @property
    def history(self) -> ConversationStore | None:
        return self._history

    @history.setter
    def history(self, value: ConversationStore | None) -> None:
        self._history = value
        self.graph.memory.history = value

    @contextmanager
    def _claim(self, user_id: int, thread_id: str) -> Iterator[None]:
        key = thread_id
        if key in self._active:
            raise ThreadBusyError("该会话已有请求正在执行。")
        task = asyncio.current_task()
        assert task is not None
        self._active[key] = (user_id, task)
        try:
            yield
        finally:
            self._active.pop(key, None)

    def cancel(self, *, thread_id: str, user_id: int) -> bool:
        entry = self._active.get(thread_id)
        if entry is None or entry[0] != user_id:
            return False
        entry[1].cancel()
        return True

    @staticmethod
    def _config(thread_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": thread_id}}

    async def chat(self, *, user_id: int, message: str, thread_id: str | None = None) -> AgentState:
        thread_id = thread_id or str(uuid4())
        with self._claim(user_id, thread_id):
            state, config = await self._prepare_turn(
                user_id=user_id,
                message=message,
                thread_id=thread_id,
            )
            try:
                result = await self.graph.ainvoke(state, config)
            except asyncio.CancelledError:
                with CancelScope(shield=True):
                    await self._save_checkpoint(config)
                raise
            await self._save_response(result)
            return result

    async def stream(
        self,
        *,
        user_id: int,
        message: str,
        thread_id: str | None = None,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        thread_id = thread_id or str(uuid4())
        with self._claim(user_id, thread_id):
            try:
                async for event in self._stream_turn(
                    user_id=user_id,
                    message=message,
                    thread_id=thread_id,
                ):
                    yield event
            except asyncio.CancelledError:
                with CancelScope(shield=True):
                    await self._save_checkpoint(self._config(thread_id))
                raise

    async def _stream_turn(
        self, *, user_id: int, message: str, thread_id: str
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        state, config = await self._prepare_turn(
            user_id=user_id,
            message=message,
            thread_id=thread_id,
        )
        yield (
            "started",
            {
                "phase": "RECEIVED",
                "thread_id": state["thread_id"],
                "turn_id": state["turn_id"],
                "trace_id": state["trace_id"],
            },
        )
        async for node, update in self.graph.astream_updates(state, config):
            yield (
                "progress",
                {
                    "component": node,
                    "node": node,
                    "status": "completed",
                    "phase": update.get("phase"),
                    "thread_id": state["thread_id"],
                    "turn_id": state["turn_id"],
                    "trace_id": state["trace_id"],
                },
            )
        final = await self.graph.aget_state(config)
        if final is None:
            raise RuntimeError("The Agent graph completed without a checkpointed state.")
        await self._save_response(final)
        yield "completed", cast("dict[str, Any]", final)

    async def _prepare_turn(
        self,
        *,
        user_id: int,
        message: str,
        thread_id: str | None,
        turn_id: str | None = None,
        trace_id: str | None = None,
        persist: bool = True,
    ) -> tuple[AgentState, dict[str, Any]]:
        thread_id = thread_id or str(uuid4())
        config = self._config(thread_id)
        previous = await self.graph.aget_state(config)
        if previous is not None and previous.get("user_id") != user_id:
            raise ThreadNotFoundError(thread_id)
        state = new_agent_state(
            message=message,
            user_id=user_id,
            thread_id=thread_id,
            turn_id=turn_id,
            trace_id=trace_id,
            current_date=today_in_timezone(self.timezone_name),
        )
        # Existing thread memory is updated by the graph, never overwritten by turn defaults.
        for field in (
            "memory_summary", "messages", "history_loaded_through",
            "recent_messages", "conversation_summary",
        ):
            cast("dict[str, Any]", state).pop(field, None)
        state["messages"] = [HumanMessage(
            id=f"{state['turn_id']}:user", content=message.strip(),
            additional_kwargs={"turn_id": state["turn_id"]},
        )]
        state["response_created_at"] = 0
        if self.history and persist:
            await asyncio.to_thread(
                self.history.append,
                user_id,
                thread_id,
                ConversationMessage(id=f"{state['turn_id']}:user", role="user", content=message),
            )
        if previous:
            state["slots"] = dict(previous.get("slots", {}))
            response = previous.get("final_response") or {}
            state["discussed_meals"] = [
                {"name": item["name"], "meal_id": item["meal_id"],
                 "reason": str(item.get("reason", ""))[:300]}
                for item in response.get("recommendations", [])
            ] or list(previous.get("discussed_meals", []))
            for result in reversed(previous.get("tool_results", [])):
                if (
                    result.get("tool_name") in {"get_recipe", "get_nutrition"}
                    and result.get("status") == "success"
                    and previous.get("intent") in {
                        Intent.RECIPE_QUERY.value, Intent.NUTRITION_QUERY.value,
                    }
                ):
                    data = result.get("data") or {}
                    state["discussed_meals"] = [
                        {"name": data["name"], "meal_id": data["meal_id"]}
                    ]
                    break
            # Historical evidence explains displayed dishes; it never becomes a new search.
            carried_evidence = (
                previous.get("conversation_evidence", [])
                if previous.get("intent") in {
                    Intent.FOOD_QUESTION.value, Intent.CONVERSATION.value,
                } else []
            )
            evidence = [
                result for result in compact_tool_results([
                    *carried_evidence, *previous.get("tool_results", []),
                ])
                if result.get("status") == "success" and result.get("tool_name") in {
                    "search_meals", "get_recipe", "get_nutrition", "get_user_profile",
                    "get_weather_context", "query_dietary_guidelines",
                }
            ]
            if evidence:
                displayed = {item["meal_id"] for item in state["discussed_meals"]}
                for result in evidence:
                    if result.get("tool_name") == "search_meals":
                        result["data"]["hits"] = [
                            meal for meal in result["data"]["hits"]
                            if (meal.get("meal_id") or meal.get("id")) in displayed
                        ]
                state["conversation_evidence"] = evidence[-6:]
            else:
                state["conversation_evidence"] = list(previous.get("conversation_evidence", []))
            bounded: list[dict[str, Any]] = []
            size = 0
            for result in reversed(state["conversation_evidence"]):
                result_size = len(json.dumps(result, ensure_ascii=False))
                if size + result_size <= 16000:
                    bounded.insert(0, result)
                    size += result_size
            state["conversation_evidence"] = bounded
            state["previous_candidate_ids"] = list(previous.get("candidate_ids", []))
            if previous.get("intent") == Intent.MEAL_PLAN.value and any(
                term in message for term in ("换一批", "换菜", "换一下", "重新安排", "重新制定")
            ):
                state["replacing_plan"] = True
                plan_calls = [
                    call
                    for call in previous.get("pending_tool_calls", [])
                    if call.get("name") == "create_meal_plan"
                ]
                plan_data = (
                    plan_calls[0].get("arguments", {})
                    if plan_calls
                    else next(
                        (
                            result.get("data") or {}
                            for result in reversed(previous.get("tool_results", []))
                            if result.get("tool_name") == "create_meal_plan"
                            and result.get("status") == "success"
                        ),
                        {},
                    )
                )
                if plan_data:
                    state["slots"]["start_date"] = plan_data["start_date"]
                    state["previous_candidate_ids"] = list(
                        {item["meal_id"] for item in plan_data.get("items", [])}
                    )
            if previous.get("phase") == Phase.CLARIFY.value:
                state["clarification_intent"] = previous.get("intent")
        return state, config

    async def confirm(
        self,
        *,
        thread_id: str,
        user_id: int,
        approved: bool,
        confirmation_id: str | None = None,
    ) -> AgentState:
        with self._claim(user_id, thread_id):
            return await self._confirm(
                thread_id=thread_id,
                user_id=user_id,
                approved=approved,
                confirmation_id=confirmation_id,
            )

    async def _confirm(
        self,
        *,
        thread_id: str,
        user_id: int,
        approved: bool,
        confirmation_id: str | None,
    ) -> AgentState:
        config = self._config(thread_id)
        previous = await self.graph.aget_state(config)
        if previous is None or previous.get("user_id") != user_id:
            raise ThreadNotFoundError(thread_id)
        if previous.get("phase") != Phase.AWAITING_CONFIRMATION.value:
            raise InvalidConfirmationStateError("The thread is not waiting for confirmation.")
        pending_calls = previous.get("pending_tool_calls", [])
        pending_ids = {str(call.get("call_id")) for call in pending_calls}
        if confirmation_id is not None and confirmation_id not in pending_ids:
            raise InvalidConfirmationStateError(
                "The confirmation does not match the pending action."
            )
        update = AgentState(
            thread_id=thread_id,
            trace_id=previous["trace_id"],
            turn_id=previous["turn_id"],
            user_id=user_id,
            phase=Phase.AWAITING_CONFIRMATION.value,
            confirmation_decision=approved,
            response_kind="confirmation",
            response_created_at=0,
            memory_finalized_run_id=None,
        )
        if self.history:
            await asyncio.to_thread(
                self.history.append,
                user_id,
                thread_id,
                ConversationMessage(
                    id=f"{previous['turn_id']}:decision",
                    role="system",
                    content="已确认操作。" if approved else "已拒绝本次写入操作。",
                ),
            )
        try:
            result = await self.graph.ainvoke(update, config)
        except asyncio.CancelledError:
            with CancelScope(shield=True):
                await self._save_checkpoint(config, suffix="confirmation")
            raise
        await self._save_response(result, suffix="confirmation")
        return result

    async def get_thread(self, *, thread_id: str, user_id: int) -> AgentState:
        state = await self.graph.aget_state(self._config(thread_id))
        if state is None or state.get("user_id") != user_id:
            raise ThreadNotFoundError(thread_id)
        return state

    async def list_threads(
        self, *, user_id: int, query: str = "", limit: int = 30, offset: int = 0
    ) -> list[ConversationSummary]:
        if self.history is None:
            return []
        return await asyncio.to_thread(self.history.list_threads, user_id, query, limit, offset)

    async def messages(
        self, *, user_id: int, thread_id: str, limit: int = 100, offset: int = 0
    ) -> list[ConversationMessage]:
        await self.get_thread(thread_id=thread_id, user_id=user_id)
        if self.history is None:
            return []
        return await asyncio.to_thread(self.history.messages, user_id, thread_id, limit, offset)

    async def _save_checkpoint(self, config: dict[str, Any], suffix: str = "response") -> None:
        state = await self.graph.aget_state(config)
        if state is not None:
            await self._save_response(state, suffix=suffix)

    async def _save_response(self, state: AgentState, suffix: str = "response") -> None:
        if self.history is None:
            return
        response = state.get("final_response") or {}
        content = response.get("message") or (
            "计划已准备，请核对后确认。"
            if state.get("requires_confirmation")
            else "本轮请求已结束。"
        )
        await asyncio.to_thread(
            self.history.append,
            state["user_id"],
            state["thread_id"],
            ConversationMessage(
                id=f"{state['turn_id']}:{suffix}",
                role="assistant",
                content=str(content),
                trace_id=state["trace_id"],
                response=response,
                created_at=datetime.fromtimestamp(state["response_created_at"], UTC)
                if state.get("response_created_at") else datetime.now(UTC),
            ),
        )
