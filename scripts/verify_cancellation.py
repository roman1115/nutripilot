"""Check cancellation of an actual HTTP stream without leaving a request running."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import httpx


async def verify(args: argparse.Namespace) -> dict[str, object]:
    ready = asyncio.Event()
    state: dict[str, str] = {}
    thread_id = f"cancel-check-{uuid4()}"
    token = os.getenv("NUTRIPILOT_ACCESS_TOKEN")
    headers = {"X-User-ID": str(args.user_id)}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(base_url=args.base_url, headers=headers, timeout=60) as client:

        async def read() -> None:
            async with client.stream(
                "POST",
                "/api/v1/chat/stream",
                json={
                    "user_id": args.user_id,
                    "thread_id": thread_id,
                    "message": "推荐清淡午餐",
                },
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data = json.loads(line[6:])
                        if data.get("trace_id"):
                            state["trace_id"] = data["trace_id"]
                            ready.set()

        task = asyncio.create_task(read())
        try:
            await asyncio.wait_for(ready.wait(), timeout=10)
            await asyncio.sleep(0.5)
            response = await client.post(f"/api/v1/chat/{thread_id}/cancel")
            response.raise_for_status()
            assert response.json()["cancelled"] is True
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=10)
            trace = await client.get(f"/api/v1/traces/{state['trace_id']}")
            trace.raise_for_status()
            events = trace.json()["events"]
            assert any(event["event_type"] == "execution.cancelled" for event in events)
            assert not any(event["event_type"] == "tools.batch_completed" for event in events)
            saved = await client.get(
                f"/api/v1/threads/{thread_id}", params={"user_id": args.user_id}
            )
            saved.raise_for_status()
            assert saved.json()["phase"] == "FAILED"
            assert "已停止" in saved.json()["final_response"]["message"]
            history = await client.get(f"/api/v1/threads/{thread_id}/messages")
            history.raise_for_status()
            assert len(history.json()["items"]) == 2
            return {
                "status": "passed",
                "thread_id": thread_id,
                "trace_id": state["trace_id"],
                "events": [event["event_type"] for event in events],
                "terminal_checkpoint_and_history": True,
            }
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--user-id", type=int, default=910023)
    parser.add_argument("--output", type=Path, default=Path(".runtime/cancellation.json"))
    args = parser.parse_args()
    result = asyncio.run(verify(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
