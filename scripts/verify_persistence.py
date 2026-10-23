"""Capture or compare user-visible records across an API process restart."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import httpx


def snapshot(client: httpx.Client, user_id: int) -> dict[str, Any]:
    response = client.get("/api/v1/threads", params={"limit": 100})
    response.raise_for_status()
    threads = response.json()["items"]
    assert threads, "No conversations to verify"
    messages = {}
    states = {}
    for thread in threads:
        thread_id = thread["thread_id"]
        response = client.get(f"/api/v1/threads/{thread_id}/messages")
        response.raise_for_status()
        messages[thread_id] = response.json()
        response = client.get(f"/api/v1/threads/{thread_id}", params={"user_id": user_id})
        response.raise_for_status()
        states[thread_id] = response.json()
    response = client.get("/api/v1/meal-plans")
    response.raise_for_status()
    plans = response.json()
    return {"threads": threads, "messages": messages, "states": states, "plans": plans}


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--user-id", type=int, default=910024)
    parser.add_argument("--record", type=Path, default=Path(".runtime/persistence.json"))
    parser.add_argument("--capture", action="store_true")
    args = parser.parse_args()
    headers = {"X-User-ID": str(args.user_id)}
    if token := os.getenv("NUTRIPILOT_ACCESS_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    with httpx.Client(base_url=args.base_url, headers=headers, timeout=30) as client:
        value = snapshot(client, args.user_id)
    if args.capture:
        args.record.parent.mkdir(parents=True, exist_ok=True)
        args.record.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        status = "captured"
    else:
        before = json.loads(args.record.read_text(encoding="utf-8"))
        assert digest(before) == digest(value), "User-visible records changed across restart"
        status = "verified"
    print(
        json.dumps(
            {
                "status": status,
                "threads": len(value["threads"]),
                "plans": len(value["plans"]),
                "sha256": digest(value),
            }
        )
    )


if __name__ == "__main__":
    main()
