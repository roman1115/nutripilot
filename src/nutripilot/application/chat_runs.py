"""Submission and reconnect semantics independent of an HTTP connection."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from nutripilot.agent.state import AgentState
from nutripilot.application.chat_service import (
    ChatService,
    InvalidConfirmationStateError,
    ThreadBusyError,
)
from nutripilot.application.history import ConversationMessage
from nutripilot.infrastructure.chat_tasks import TERMINAL, ChatTaskStore


class ChatRunService:
    def __init__(self, chat: ChatService, store: ChatTaskStore, max_queued: int = 20) -> None:
        self.chat = chat
        self.store = store
        self.max_queued = max_queued

    async def delete_thread(self, user_id: int, thread_id: str) -> None:
        bundle = await asyncio.to_thread(self.store.bundle, thread_id, user_id)
        run = bundle["run"]
        if run and run["status"] in ("queued", "running", "cancelling"):
            raise ThreadBusyError("会话仍有任务正在执行，完成或停止后才能删除。")
        await self.chat.graph.compiled.checkpointer.adelete_thread(thread_id)
        await asyncio.to_thread(self.store.delete_thread, user_id, thread_id)

    async def submit(
        self,
        *,
        user_id: int,
        thread_id: str,
        message: str = "",
        request_key: str,
        approved: bool | None = None,
        confirmation_id: str | None = None,
    ) -> dict[str, Any]:
        kind = "chat" if approved is None else "confirmation"
        request_hash = self.store.request_hash(
            {
                "thread": thread_id,
                "message": message.strip(),
                "kind": kind,
                "approved": approved,
                "confirmation_id": confirmation_id,
            }
        )
        existing = await asyncio.to_thread(
            self.store.find_request, user_id, request_key, request_hash
        )
        if existing:
            return existing
        run_id = str(uuid4())
        if approved is None:
            state, _ = await self.chat._prepare_turn(
                user_id=user_id, thread_id=thread_id, message=message, persist=False
            )
            history = ConversationMessage(
                id=f"{state['turn_id']}:user", role="user", content=message.strip()
            )
        else:
            previous = await self.chat.get_thread(user_id=user_id, thread_id=thread_id)
            calls = previous.get("pending_tool_calls", [])
            if (
                previous.get("phase") != "AWAITING_CONFIRMATION"
                or not confirmation_id
                or confirmation_id not in {str(call.get("call_id")) for call in calls}
            ):
                raise InvalidConfirmationStateError("确认已失效，请重新读取当前预览。")
            state = AgentState(
                thread_id=thread_id,
                turn_id=previous["turn_id"],
                trace_id=previous["trace_id"],
                user_id=user_id,
                phase="AWAITING_CONFIRMATION",
                confirmation_decision=approved,
            )
            history = ConversationMessage(
                id=f"{previous['turn_id']}:decision",
                role="system",
                content="已确认操作。" if approved else "已拒绝本次写入操作。",
            )
        state["run_id"] = run_id
        return await asyncio.to_thread(
            self.store.submit,
            user_id=user_id,
            thread_id=thread_id,
            request_key=request_key,
            request_hash=request_hash,
            state=cast("dict[str, Any]", state),
            kind=kind,
            message=history,
            max_queued=self.max_queued,
        )

    async def subscribe(
        self, run_id: str, user_id: int, after: int = 0
    ) -> AsyncIterator[dict[str, Any]]:
        while True:
            events = await asyncio.to_thread(self.store.events, run_id, user_id, after)
            for event in events:
                after = event["id"]
                yield event
            status = await asyncio.to_thread(self.store.get, run_id, user_id)
            if status["status"] in TERMINAL and after >= status["last_event"]:
                return
            if not events:
                yield {"type": "heartbeat", "data": {}, "id": after}
                await asyncio.sleep(0.5)

    async def wait(self, run: dict[str, Any], user_id: int) -> AgentState:
        async for _ in self.subscribe(run["id"], user_id):
            pass
        state = await asyncio.to_thread(self.store.final_state, run["id"], user_id)
        return cast("AgentState", state)


def response_message(state: AgentState, kind: str, created_at: float) -> ConversationMessage:
    response = state.get("final_response") or {}
    return ConversationMessage(
        id=f"{state['turn_id']}:{'confirmation' if kind == 'confirmation' else 'response'}",
        role="assistant",
        content=str(
            response.get("message")
            or (
                "计划已准备，请核对后确认。"
                if state.get("requires_confirmation")
                else "本轮请求已结束。"
            )
        ),
        created_at=datetime.fromtimestamp(created_at, UTC),
        trace_id=state["trace_id"],
        response=response,
    )
