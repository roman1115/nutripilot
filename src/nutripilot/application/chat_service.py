"""Use-case service for starting, continuing, and confirming conversations."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from typing import Any, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

from nutripilot.agent.graph import AgentGraph
from nutripilot.agent.slots import today_in_timezone
from nutripilot.agent.state import AgentState, new_agent_state
from nutripilot.application.history import (
    ConversationMessage,
    ConversationStore,
    ConversationSummary,
)
from nutripilot.domain.enums import Phase


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
            current_date=today_in_timezone(self.timezone_name),
        )
        if self.history:
            await asyncio.to_thread(
                self.history.append,
                user_id,
                thread_id,
                ConversationMessage(id=f"{state['turn_id']}:user", role="user", content=message),
            )
        if previous:
            state["slots"] = dict(previous.get("slots", {}))
            state["conversation_summary"] = previous.get("conversation_summary")
            state["candidate_ids"] = list(previous.get("candidate_ids", []))
            if previous.get("phase") == Phase.CLARIFY.value:
                state["clarification_intent"] = previous.get("intent")
                state["conversation_summary"] = (
                    f"上一轮意图：{previous.get('intent')}。"
                    f"上一轮追问：{(previous.get('final_response') or {}).get('message', '')}"
                )
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
            ),
        )
