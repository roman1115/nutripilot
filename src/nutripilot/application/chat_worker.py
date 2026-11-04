"""A single queue owner running bounded, independently cancellable conversations."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from functools import partial
from typing import Any, cast

from anyio import CancelScope
from sqlalchemy import text

from nutripilot.agent.state import AgentState
from nutripilot.api.schemas import ChatResponse
from nutripilot.application.chat_runs import response_message
from nutripilot.application.chat_service import ChatService
from nutripilot.config import Settings
from nutripilot.domain.execution import (
    ExecutionOwnershipLost,
    RunExecution,
    current_execution,
)
from nutripilot.infrastructure.chat_tasks import ChatTaskStore

logger = logging.getLogger(__name__)


class ChatWorker:
    def __init__(self, chat: ChatService, store: ChatTaskStore, settings: Settings) -> None:
        self.chat, self.store, self.settings = chat, store, settings
        self.active: dict[str, tuple[asyncio.Task[None], RunExecution, int]] = {}
        self.stopping = asyncio.Event()
        self.ready = asyncio.Event()

    async def run(self) -> None:
        lock = await asyncio.to_thread(self.store.worker_lock)
        if lock is None:
            raise RuntimeError("该队列已有 Worker，不能启动第二个执行进程。")
        try:
            await asyncio.to_thread(self.store.recover)
            await asyncio.to_thread(self.store.heartbeat)
            self.ready.set()
            heartbeat_at = time.monotonic()
            while not self.stopping.is_set():
                if self.store.postgres:
                    await asyncio.to_thread(lock.execute, text("SELECT 1"))
                    await asyncio.to_thread(lock.commit)
                if time.monotonic() - heartbeat_at >= 1:
                    await asyncio.to_thread(self.store.heartbeat)
                    heartbeat_at = time.monotonic()
                for run_id, (task, execution, user_id) in list(self.active.items()):
                    if task.done():
                        task.result()
                        del self.active[run_id]
                        continue
                    state = await asyncio.to_thread(self.store.get, run_id, user_id)
                    if (
                        state["status"] == "cancelling"
                        and not task.cancelling()
                        and not execution.finishing
                    ):
                        task.cancel()
                while len(self.active) < self.settings.chat_concurrency:
                    row = await asyncio.to_thread(
                        self.store.claim,
                        self.settings.chat_concurrency,
                        self.settings.chat_user_concurrency,
                        self.settings.agent_deadline_seconds,
                        tuple(self.active),
                    )
                    if row is None:
                        break
                    run_id, version = row["id"], row["version"]

                    async def reserve(tokens: int, rid: str = run_id, rev: int = version) -> None:
                        await asyncio.to_thread(
                            self.store.reserve, rid, rev, tokens, self.settings.agent_token_budget
                        )

                    async def settle(
                        reserved: int, used: int, rid: str = run_id, rev: int = version
                    ) -> None:
                        await asyncio.to_thread(self.store.settle, rid, rev, reserved, used)

                    execution = RunExecution(
                        fence=partial(self.store.fence, run_id, version),
                        reserve=reserve,
                        settle=settle,
                    )
                    task = asyncio.create_task(self._execute(row, execution), name=f"chat:{run_id}")
                    self.active[run_id] = (task, execution, row["user_id"])
                with suppress(TimeoutError):
                    await asyncio.wait_for(self.stopping.wait(), timeout=0.2)
        finally:
            await self.close()
            try:
                await asyncio.to_thread(self.store.heartbeat, False)
            finally:
                await asyncio.to_thread(self.store.release_worker, lock)

    async def close(self) -> None:
        self.stopping.set()
        for task, execution, _ in self.active.values():
            execution.suspend = True
            task.cancel()
        if self.active:
            await asyncio.gather(
                *(entry[0] for entry in self.active.values()), return_exceptions=True
            )
        self.active.clear()

    async def _execute(self, row: dict[str, Any], execution: RunExecution) -> None:
        state = cast("AgentState", row["state"])
        config = self.chat._config(state["thread_id"])
        handle = current_execution.set(execution)
        try:
            checkpoint = await self.chat.graph.compiled.aget_state(config)
            previous = checkpoint.values or {}
            completed_checkpoint = previous.get("run_id") == row["id"] and not checkpoint.next
            if (
                row["kind"] == "chat"
                and (not previous or previous.get("run_id") != row["id"])
                and row["status"] != "cancelling"
            ):
                prepared, _ = await self.chat._prepare_turn(
                    user_id=state["user_id"],
                    thread_id=state["thread_id"],
                    message=state["user_message"],
                    turn_id=state["turn_id"],
                    trace_id=state["trace_id"],
                    persist=False,
                )
                prepared["run_id"] = row["id"]
                prepared["current_date"] = state["current_date"]
                state = prepared
            if completed_checkpoint:
                pass
            elif row["status"] == "cancelling":
                await self.chat.graph._terminate(state, config, "CANCELLED")
            elif row["attempts"] > self.settings.chat_max_attempts:
                await self.chat.graph._terminate(state, config, "RECOVERY_LIMIT_EXCEEDED")
            else:
                await asyncio.to_thread(
                    self.store.progress,
                    row["id"],
                    row["version"],
                    "status",
                    {
                        "phase": "RECEIVED",
                        "status": "running",
                        "run_id": row["id"],
                        "thread_id": state["thread_id"],
                        "turn_id": state["turn_id"],
                        "trace_id": state["trace_id"],
                    },
                )
                async for node, update in self.chat.graph.astream_durable(
                    state,
                    config,
                    seconds=max(0.001, row["deadline_at"] - time.time()),
                    tokens=max(0, self.settings.agent_token_budget - row["reserved_tokens"]),
                ):
                    await asyncio.to_thread(
                        self.store.progress,
                        row["id"],
                        row["version"],
                        "progress",
                        {
                            "node": node,
                            "component": node,
                            "phase": update.get("phase"),
                            "status": "completed",
                            "thread_id": state["thread_id"],
                            "trace_id": state["trace_id"],
                        },
                    )
            execution.finishing = True
            await self._finish(row)
        except asyncio.CancelledError:
            if not execution.suspend:
                with CancelScope(shield=True):
                    current = await self.chat.graph.aget_state(config)
                    if (
                        not current
                        or current.get("run_id") != row["id"]
                        or (current.get("error") or {}).get("code") != "CANCELLED"
                    ):
                        await self.chat.graph._terminate(state, config, "CANCELLED")
                    await self._finish(row)
        except ExecutionOwnershipLost:
            pass
        except Exception:
            logger.exception("Chat execution failed: %s", row["id"])
            if not execution.suspend:
                with CancelScope(shield=True):
                    try:
                        await self.chat.graph._terminate(state, config, "EXECUTION_FAILED")
                        await self._finish(row)
                    except ExecutionOwnershipLost:
                        pass
        finally:
            current_execution.reset(handle)

    async def _finish(self, row: dict[str, Any]) -> None:
        state = await self.chat.graph.aget_state(self.chat._config(row["thread_id"]))
        if state is None:
            raise RuntimeError("Missing execution checkpoint.")
        status = (
            "cancelled"
            if (state.get("error") or {}).get("code") == "CANCELLED"
            else "awaiting_confirmation"
            if state.get("requires_confirmation")
            else "failed"
            if state.get("phase") == "FAILED"
            else "completed"
        )
        result = ChatResponse.from_state(state).model_dump(mode="json")
        await asyncio.to_thread(
            self.store.finish,
            row["id"],
            row["version"],
            status,
            result,
            response_message(state, row["kind"], time.time()),
            (state.get("error") or {}).get("code"),
            dict(state),
        )
