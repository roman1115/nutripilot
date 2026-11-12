"""Real API/Worker process loss with PostgreSQL checkpoints and persisted writes."""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("NUTRIPILOT_TEST_EXTERNAL") != "1", reason="Local PostgreSQL/Redis required"
    ),
]


@pytest.mark.asyncio
async def test_sigkill_api_worker_and_write_before_checkpoint(tmp_path: Path) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    queue = "process-test-" + uuid4().hex
    user = 4_000_000_000 + uuid4().int % 1_000_000_000
    processes: list[subprocess.Popen[bytes]] = []
    logs = (tmp_path / "processes.log").open("wb")

    def start(role: str) -> subprocess.Popen[bytes]:
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).with_name("durable_chat_host.py")),
                role,
                "--directory",
                str(tmp_path),
                "--queue",
                queue,
                "--port",
                str(port),
            ],
            stdout=logs,
            stderr=logs,
        )
        processes.append(process)
        return process

    async def wait_for(predicate):  # type: ignore[no-untyped-def]
        async with asyncio.timeout(35):
            while True:
                try:
                    value = await predicate()
                    if value:
                        return value
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.1)

    try:
        api, worker = start("api"), start("worker")
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            headers={"X-User-ID": str(user)},
            trust_env=False,
        ) as client:

            async def ready():  # type: ignore[no-untyped-def]
                response = await client.get("/health/ready")
                return response.status_code == 200 and response.json()["chat_worker_ready"]

            await wait_for(ready)
            thread = uuid4().hex
            response = await client.post(
                f"/api/v1/threads/{thread}/runs",
                json={"message": "推荐午餐", "request_id": uuid4().hex},
            )
            assert response.status_code == 202, response.text
            run = response.json()

            async def planner_started():  # type: ignore[no-untyped-def]
                path = tmp_path / "calls.log"
                return path.exists() and "planner" in path.read_text()

            await wait_for(planner_started)
            api.kill()
            await asyncio.to_thread(api.wait, 10)
            api = start("api")
            await wait_for(ready)
            assert (await client.get(f"/api/v1/runs/{run['id']}")).json()["status"] == "running"
            worker.kill()
            await asyncio.to_thread(worker.wait, 10)
            worker = start("worker")

            async def resumed():  # type: ignore[no-untyped-def]
                current = (await client.get(f"/api/v1/runs/{run['id']}")).json()
                return current["status"] == "running" and current["attempts"] == 2

            await wait_for(resumed)
            assert (tmp_path / "calls.log").read_text().splitlines().count("intent") == 1
            (tmp_path / "release").touch()

            async def done(run_id: str):  # type: ignore[no-untyped-def]
                value = (await client.get(f"/api/v1/runs/{run_id}")).json()
                return value if value["status"] not in ("queued", "running", "cancelling") else None

            assert (await wait_for(lambda: done(run["id"])))["status"] == "completed"
            snapshot = (await client.get(f"/api/v1/threads/{thread}/snapshot")).json()
            assert len(snapshot["messages"]) == 2
            plan_thread = uuid4().hex
            response = await client.post(
                f"/api/v1/threads/{plan_thread}/runs",
                json={"message": "制定明天的一日三餐餐单", "request_id": uuid4().hex},
            )
            assert response.status_code == 202, response.text
            pending = await wait_for(lambda: done(response.json()["id"]))
            assert pending["status"] == "awaiting_confirmation"
            (tmp_path / "pause-write").touch()
            confirmation = await client.post(
                f"/api/v1/threads/{plan_thread}/confirm",
                json={
                    "approved": True,
                    "request_id": uuid4().hex,
                    "confirmation_id": pending["result"]["pending_action"]["id"],
                },
            )
            assert confirmation.status_code == 202, confirmation.text

            async def write_finished():  # type: ignore[no-untyped-def]
                return (tmp_path / "write-finished").exists()

            await wait_for(write_finished)
            worker.kill()
            await asyncio.to_thread(worker.wait, 10)
            (tmp_path / "pause-write").unlink()
            worker = start("worker")
            confirmed = await wait_for(lambda: done(confirmation.json()["id"]))
            assert confirmed["status"] == "completed"
            assert confirmed["attempts"] == 2
            assert len((await client.get("/api/v1/meal-plans")).json()) == 1
            replay = await client.get(f"/api/v1/runs/{confirmation.json()['id']}/events")
            assert replay.text.count("event: completed") == 1
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                await asyncio.to_thread(process.wait, 10)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait, 10)
        logs.close()
