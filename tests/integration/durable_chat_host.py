"""Subprocess fixture for destructive restart tests; never uses a real model."""

from __future__ import annotations

import argparse
import asyncio
import signal
from pathlib import Path
from typing import Any

import uvicorn

from nutripilot.api.app import create_app
from nutripilot.application import build_container
from nutripilot.application.chat_worker import ChatWorker
from nutripilot.application.runtime import start_container, stop_container
from nutripilot.config import Settings
from nutripilot.model_gateway import FakeModelGateway


class GatedGateway(FakeModelGateway):
    def __init__(self, directory: Path) -> None:
        super().__init__()
        self.directory = directory

    async def generate_structured(self, **kwargs: Any) -> Any:
        worker = kwargs["worker"]
        with (self.directory / "calls.log").open("a") as output:
            output.write(worker + "\n")
        if worker == "planner":
            while not (self.directory / "release").exists():  # noqa: ASYNC110 - cross-process gate
                await asyncio.sleep(0.05)
        return await super().generate_structured(**kwargs)


async def run_worker(container: Any, directory: Path) -> None:
    await start_container(container)
    executor = container.tool_executor
    execute = executor.execute

    async def pause_after_write(call: Any, *args: Any, **kwargs: Any) -> Any:
        result = await execute(call, *args, **kwargs)
        if call.name == "create_meal_plan" and result.ok:
            (directory / "write-finished").touch()
            while (directory / "pause-write").exists():  # noqa: ASYNC110 - cross-process gate
                await asyncio.sleep(0.05)
        return result

    executor.execute = pause_after_write
    worker = ChatWorker(container.chat_service, container.chat_runs.store, container.settings)
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, worker.stopping.set)
    try:
        await worker.run()
    finally:
        await stop_container(container)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["api", "worker"])
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--queue", required=True)
    parser.add_argument("--port", type=int, default=8007)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        _env_file=None,
        app_mode="docker",
        model_provider="fake",
        embedding_provider="hashing",
        rerank_provider="lexical",
        mcp_enabled=False,
        database_url="postgresql+asyncpg://nutripilot:nutripilot@127.0.0.1:5432/nutripilot",
        redis_url="redis://127.0.0.1:6379/0",
        chat_queue=args.queue,
        run_store_path=str(args.directory / "traces.sqlite3"),
        agent_deadline_seconds=180,
        api_rate_limit_per_minute=10000,
        web_origin="http://127.0.0.1:5177,http://127.0.0.1:5178",
    )
    container = build_container(
        settings=settings, gateway=GatedGateway(args.directory), interrupt_evaluations=False
    )
    if args.role == "api":
        uvicorn.run(create_app(container), host="127.0.0.1", port=args.port)
    else:
        asyncio.run(run_worker(container, args.directory))


if __name__ == "__main__":
    main()
