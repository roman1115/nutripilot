"""Use-case service for starting, continuing, and confirming conversations."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, cast
from uuid import uuid4

from nutripilot.agent.graph import AgentGraph
from nutripilot.agent.state import AgentState, new_agent_state
from nutripilot.domain.enums import Phase


class ThreadNotFoundError(LookupError):
    pass


class InvalidConfirmationStateError(ValueError):
    pass


class ChatService:
    def __init__(self, graph: AgentGraph) -> None:
        self.graph = graph

    @staticmethod
    def _config(thread_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": thread_id}}

    async def chat(self, *, user_id: int, message: str, thread_id: str | None = None) -> AgentState:
        state, config = await self._prepare_turn(
            user_id=user_id,
            message=message,
            thread_id=thread_id,
        )
        return await self.graph.ainvoke(state, config)

    async def stream(
        self,
        *,
        user_id: int,
        message: str,
        thread_id: str | None = None,
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
        state = new_agent_state(message=message, user_id=user_id, thread_id=thread_id)
        if previous:
            state["slots"] = dict(previous.get("slots", {}))
            state["conversation_summary"] = previous.get("conversation_summary")
            state["candidate_ids"] = list(previous.get("candidate_ids", []))
        return state, config

    async def confirm(
        self,
        *,
        thread_id: str,
        user_id: int,
        approved: bool,
        confirmation_id: str | None = None,
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
        return await self.graph.ainvoke(update, config)

    async def get_thread(self, *, thread_id: str, user_id: int) -> AgentState:
        state = await self.graph.aget_state(self._config(thread_id))
        if state is None or state.get("user_id") != user_id:
            raise ThreadNotFoundError(thread_id)
        return state
