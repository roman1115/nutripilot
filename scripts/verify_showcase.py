from __future__ import annotations

import json
import sys
from typing import Any
from uuid import uuid4

import httpx


def main() -> int:
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=240) as client:
        health = _json(client.get("/health/ready"))
        expected = {
            "model_provider": "openai",
            "embedding_provider": "bge",
            "rerank_provider": "bge",
            "storage": "postgresql",
            "mcp_transport": "streamable-http",
        }
        mismatches = {
            key: {"expected": value, "actual": health.get(key)}
            for key, value in expected.items()
            if health.get(key) != value
        }
        if mismatches:
            raise AssertionError(f"Showcase runtime mismatch: {mismatches}")

        profile = {
            "display_name": "求职演示用户",
            "goals": ["均衡饮食"],
            "tastes": ["清淡"],
            "cuisines": ["中式"],
            "avoidances": ["香菇"],
            "allergens": ["花生", "香菇"],
            "dietary_restrictions": [],
            "daily_calorie_target": 2000,
            "location": "上海",
        }
        _json(client.put("/api/v1/profile", json=profile, headers={"X-User-ID": "1"}))

        response = _json(
            client.post(
                "/api/v1/chat",
                json={
                    "user_id": 1,
                    "thread_id": f"showcase-smoke-{uuid4()}",
                    "message": "推荐一份清淡、高蛋白、600千卡以内的午餐",
                },
            )
        )
        recommendations = (response.get("final_response") or {}).get("recommendations") or []
        if not recommendations:
            raise AssertionError("Real-model smoke test returned no recommendations.")
        if any("香菇" in str(item.get("name", "")) for item in recommendations):
            raise AssertionError("A mushroom recommendation bypassed the persisted constraints.")

        print(
            json.dumps(
                {
                    "status": "passed",
                    "model": health.get("model_name"),
                    "recommendations": [item.get("name") for item in recommendations],
                    "trace_id": response.get("trace_id"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    return 0


def _json(response: httpx.Response) -> dict[str, Any]:
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict):
        raise TypeError("Expected a JSON object from the NutriPilot API.")
    return value


if __name__ == "__main__":
    sys.exit(main())
