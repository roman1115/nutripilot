"""Run one durable chat worker separately from the HTTP server."""

from __future__ import annotations

import asyncio
import signal

from nutripilot.application import build_container
from nutripilot.application.chat_worker import ChatWorker
from nutripilot.application.runtime import start_container, stop_container


async def serve() -> None:
    container = build_container(interrupt_evaluations=False)
    if not container.settings.uses_external_infrastructure:
        raise RuntimeError("独立 Worker 需要 PostgreSQL 持久化模式。")
    await start_container(container)
    assert container.chat_runs is not None
    worker = ChatWorker(container.chat_service, container.chat_runs.store, container.settings)
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, worker.stopping.set)
        except NotImplementedError:
            signal.signal(signum, lambda *_: loop.call_soon_threadsafe(worker.stopping.set))
    try:
        await worker.run()
    finally:
        await stop_container(container)


if __name__ == "__main__":
    asyncio.run(serve())
