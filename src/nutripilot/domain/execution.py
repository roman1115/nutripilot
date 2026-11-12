"""Ownership guards shared by durable graph execution and write tools."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractContextManager, asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from anyio import CancelScope, CapacityLimiter, to_thread


class ExecutionOwnershipLost(RuntimeError):
    pass


@dataclass
class RunExecution:
    fence: Callable[[], AbstractContextManager[None]]
    reserve: Callable[[int], Awaitable[None]] | None = None
    settle: Callable[[int, int], Awaitable[None]] | None = None
    suspend: bool = False
    finishing: bool = False


current_execution: ContextVar[RunExecution | None] = ContextVar("run_execution", default=None)


@asynccontextmanager
async def execution_fence() -> AsyncIterator[None]:
    execution = current_execution.get()
    if execution is None:
        yield
        return
    if execution.suspend:
        raise ExecutionOwnershipLost("The worker no longer owns this execution.")
    guard = execution.fence()
    # Lock waiters can exhaust asyncio's executor; unlocking needs independent capacity.
    exit_limiter = CapacityLimiter(1)
    # Ownership must remain locked until an in-flight checkpoint/write has finished.
    with CancelScope(shield=True):
        entering = asyncio.create_task(asyncio.to_thread(guard.__enter__))
        try:
            await asyncio.shield(entering)
        except asyncio.CancelledError:
            await entering
            await to_thread.run_sync(guard.__exit__, None, None, None, limiter=exit_limiter)
            raise
        try:
            yield
        finally:
            await to_thread.run_sync(guard.__exit__, None, None, None, limiter=exit_limiter)
