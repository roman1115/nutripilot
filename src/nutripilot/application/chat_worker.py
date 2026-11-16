"""A single queue owner running bounded, independently cancellable conversations."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from functools import partial
from typing import Any, cast

from anyio import CancelScope
from psycopg import InterfaceError as CheckpointInterfaceError
from psycopg import OperationalError as CheckpointOperationalError
from sqlalchemy import text
from sqlalchemy.exc import InterfaceError, OperationalError

from nutripilot.agent.state import AgentState
from nutripilot.api.schemas import ChatResponse
from nutripilot.application.chat_runs import ChatRunService, response_message
from nutripilot.application.chat_service import ChatService, ThreadNotFoundError
from nutripilot.application.diet_privacy import DietPrivacyService
from nutripilot.config import Settings
from nutripilot.domain.diet import DietError
from nutripilot.domain.diet_jobs import BackgroundJob, PurgePayload
from nutripilot.domain.execution import (
    ExecutionOwnershipLost,
    RunExecution,
    current_execution,
)
from nutripilot.infrastructure.chat_tasks import ChatTaskStore
from nutripilot.memory.nodes import finalized_update
from nutripilot.model_gateway.base import ModelGatewayError
from nutripilot.model_gateway.budget import ExecutionLimitExceeded

logger = logging.getLogger(__name__)


class ChatWorker:
    def __init__(self, chat: ChatService, store: ChatTaskStore, settings: Settings) -> None:
        self.chat, self.store, self.settings = chat, store, settings
        self.active: dict[str, tuple[asyncio.Task[None], RunExecution, int]] = {}
        self.stopping = asyncio.Event()
        self.ready = asyncio.Event()
        self.parsing = chat.diet_parsing
        self.privacy = DietPrivacyService(self.parsing) if self.parsing else None
        self.jobs_active: dict[str, tuple[asyncio.Task[None], RunExecution, BackgroundJob]] = {}

    async def run(self) -> None:
        lock = await asyncio.to_thread(self.store.worker_lock)
        if lock is None:
            raise RuntimeError("该队列已有 Worker，不能启动第二个执行进程。")
        try:
            await asyncio.to_thread(self.store.recover)
            if self.parsing:
                await asyncio.to_thread(self.parsing.jobs.recover, startup=True)
            await asyncio.to_thread(self.store.heartbeat)
            self.ready.set()
            heartbeat_at = time.monotonic()
            cleanup_at = time.monotonic() - 60
            while not self.stopping.is_set():
                if self.store.postgres:
                    await asyncio.to_thread(lock.execute, text("SELECT 1"))
                    await asyncio.to_thread(lock.commit)
                if time.monotonic() - heartbeat_at >= 1:
                    await asyncio.to_thread(self.store.heartbeat)
                    if self.parsing:
                        for task, execution, job in self.jobs_active.values():
                            if not task.done():
                                try:
                                    await asyncio.to_thread(self.parsing.jobs.heartbeat, job)
                                except ExecutionOwnershipLost:
                                    execution.suspend = True
                                    task.cancel()
                    heartbeat_at = time.monotonic()
                if time.monotonic() - cleanup_at >= 60:
                    try:
                        await ChatRunService(self.chat, self.store).purge_expired_threads()
                        if self.privacy:
                            await self.privacy.cleanup()
                    except Exception:
                        logger.exception("Conversation trash cleanup failed; will retry")
                    cleanup_at = time.monotonic()
                for run_id, (task, execution, user_id) in list(self.active.items()):
                    if task.done():
                        task.result()
                        del self.active[run_id]
                        continue
                    try:
                        state = await asyncio.to_thread(self.store.get, run_id, user_id)
                    except ThreadNotFoundError:
                        # A completed run can be trashed before its coroutine returns.
                        continue
                    if (
                        state["status"] == "cancelling"
                        and not task.cancelling()
                        and not execution.finishing
                    ):
                        task.cancel()
                for job_id, (task, _, _) in list(self.jobs_active.items()):
                    if task.done():
                        task.result()
                        del self.jobs_active[job_id]
                if self.parsing:
                    maintenance = await asyncio.to_thread(self.parsing.jobs.claim_maintenance)
                    if maintenance is not None:
                        await self._start_job(maintenance)
                while (
                    len(self.active)
                    + sum(
                        job.kind in ("parse_text", "parse_image")
                        for _, _, job in self.jobs_active.values()
                    )
                    < self.settings.chat_concurrency
                ):
                    row = await asyncio.to_thread(
                        self.store.claim,
                        self.settings.chat_concurrency,
                        self.settings.chat_user_concurrency,
                        self.settings.agent_deadline_seconds,
                        tuple(self.active),
                    )
                    if row is None:
                        if self.parsing:
                            claimed_job = await asyncio.to_thread(
                                self.parsing.jobs.claim,
                                self.settings.chat_concurrency,
                                self.settings.chat_user_concurrency,
                            )
                            if claimed_job is not None:
                                await self._start_job(claimed_job)
                                continue
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
        for task, execution, _ in self.jobs_active.values():
            execution.suspend = True
            task.cancel()
        if self.active:
            await asyncio.gather(
                *(entry[0] for entry in self.active.values()), return_exceptions=True
            )
        self.active.clear()
        if self.jobs_active:
            await asyncio.gather(
                *(entry[0] for entry in self.jobs_active.values()), return_exceptions=True
            )
        self.jobs_active.clear()

    async def _start_job(self, job: BackgroundJob) -> None:
        assert self.parsing is not None
        jobs = self.parsing.jobs

        async def reserve(tokens: int) -> None:
            await asyncio.to_thread(jobs.account, job, tokens)

        async def settle(reserved: int, used: int) -> None:
            await asyncio.to_thread(jobs.account, job, reserved, used)

        execution = RunExecution(fence=partial(jobs.fence, job), reserve=reserve, settle=settle)
        task = asyncio.create_task(self._execute_job(job, execution), name=f"diet:{job.id}")
        self.jobs_active[str(job.id)] = task, execution, job

    async def _execute_job(self, job: BackgroundJob, execution: RunExecution) -> None:
        assert self.parsing is not None
        handle = current_execution.set(execution)
        try:
            if job.kind == "export":
                assert self.privacy is not None
                artifact = await self.privacy.execute_export(job)
                await self._finish_job(job, artifact=artifact)
            elif job.kind == "purge":
                assert self.privacy is not None
                assert isinstance(job.payload, PurgePayload)
                await self.privacy.execute_purge(job)
                await self._finish_job(job, refs=job.payload.refs)
            else:
                result, refs = await self.parsing.execute(job)
                await self._finish_job(job, result=result, refs=refs)
        except asyncio.CancelledError:
            if not execution.suspend:
                await asyncio.to_thread(self.parsing.jobs.cancel, job.id, job.user_id)
        except ExecutionOwnershipLost:
            pass
        except (DietError, ModelGatewayError, ExecutionLimitExceeded, TimeoutError) as exc:
            code = exc.code if hasattr(exc, "code") else "MODEL_TIMEOUT"
            with suppress(ExecutionOwnershipLost):
                if isinstance(exc, (ModelGatewayError, TimeoutError)) and (
                    isinstance(exc, TimeoutError) or exc.retryable
                ):
                    await asyncio.to_thread(self.parsing.jobs.retry, job, code)
                else:
                    await self._finish_job(job, error=code)
        except Exception:
            logger.error("Diet task failed: %s", job.id)
            with suppress(ExecutionOwnershipLost):
                await self._finish_job(job, error="EXECUTION_FAILED")
        finally:
            current_execution.reset(handle)

    async def _finish_job(self, job: BackgroundJob, **values: Any) -> None:
        assert self.parsing is not None
        delay = 0.1
        while True:
            try:
                await asyncio.to_thread(self.parsing.jobs.finish, job, **values)
                return
            except (OperationalError, InterfaceError):
                logger.warning("Diet task finalization unavailable; retrying: %s", job.id)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 5)

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
                if state.get("diet_submission_anchor"):
                    prepared["diet_submission_anchor"] = state["diet_submission_anchor"]
                state = prepared
            if completed_checkpoint:
                if previous.get("memory_finalized_run_id") != row["id"]:
                    previous["response_kind"] = (
                        "confirmation" if row["kind"] == "confirmation" else "response"
                    )
                    await self.chat.graph.compiled.aupdate_state(
                        config,
                        {
                            **finalized_update(cast("AgentState", previous)),
                            "response_kind": previous["response_kind"],
                        },
                        as_node="memory_finalize",
                    )
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
        except Exception as exc:
            if isinstance(exc, ModelGatewayError):
                logger.error("Chat model failed: %s (%s)", row["id"], exc.code)
            else:
                logger.exception("Chat execution failed: %s", row["id"])
            if not execution.suspend and not execution.finishing:
                with CancelScope(shield=True):
                    try:
                        await self.chat.graph._terminate(state, config, "EXECUTION_FAILED")
                        await self._finish(row)
                    except ExecutionOwnershipLost:
                        pass
        finally:
            current_execution.reset(handle)

    async def _finish(self, row: dict[str, Any]) -> None:
        delay = 0.1
        while True:
            try:
                await self._finish_once(row)
                return
            except (
                OperationalError,
                InterfaceError,
                CheckpointOperationalError,
                CheckpointInterfaceError,
            ) as exc:
                logger.warning(
                    "Final result persistence failed; retrying: %s (%s)",
                    row["id"],
                    type(exc).__name__,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 5)

    async def _finish_once(self, row: dict[str, Any]) -> None:
        checkpoint = await self.chat.graph.compiled.aget_state(
            self.chat._config(row["thread_id"]),
        )
        state = checkpoint.values
        if (
            not state
            or state.get("run_id") != row["id"]
            or checkpoint.next
            or state.get("memory_finalized_run_id") != row["id"]
        ):
            raise RuntimeError("Missing completed execution checkpoint or memory finalization.")
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
