from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify real Qwen and BGE showcase execution.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--user-id", type=int, default=910021)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.user_id <= 0:
        parser.error("--user-id must be positive")
    token = os.getenv("NUTRIPILOT_ACCESS_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with httpx.Client(base_url=args.base_url, timeout=300, headers=headers) as client:
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
        model_name = str(health.get("model_name", ""))
        if not model_name.startswith("qwen"):
            raise AssertionError(f"Expected a Qwen model, got {model_name!r}")

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
        _json(client.put("/api/v1/profile", json=profile, headers={"X-User-ID": str(args.user_id)}))

        response = _json(
            client.post(
                "/api/v1/chat",
                json={
                    "user_id": args.user_id,
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
        if response.get("phase") != "COMPLETED":
            raise AssertionError("The real-model request did not complete.")
        if (response.get("final_response") or {}).get("status") != "completed":
            raise AssertionError("The real-model request returned a degraded response.")
        trace = _json(client.get(f"/api/v1/traces/{response['trace_id']}"))
        evidence = _model_evidence(trace, expected_model=model_name)

        result = {
            "status": "passed",
            "runtime": health,
            "model": model_name,
            "user_id": args.user_id,
            "recommendations": [item.get("name") for item in recommendations],
            "trace_id": response.get("trace_id"),
            **evidence,
        }
        serialized = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(serialized + "\n", encoding="utf-8")
        print(serialized)
    return 0


def _model_evidence(trace: dict[str, Any], *, expected_model: str) -> dict[str, Any]:
    events = trace.get("events") or []
    if any("fallback" in str(event.get("event_type", "")).lower() for event in events):
        raise AssertionError("The request used a fallback; real-model validation failed.")
    if any(event.get("event_type") == "tool.failed" for event in events):
        raise AssertionError("A tool failed during real-model validation.")
    calls = [event for event in events if event.get("event_type") == "model.completed"]
    required = {"IntentAgent", "PlannerAgent", "RecommendationAgent"}
    if not required <= {event.get("component") for event in calls}:
        raise AssertionError("Missing real-model execution evidence for one or more workers.")
    for event in calls:
        payload = event.get("payload") or {}
        if payload.get("model") != expected_model or payload.get("used_fallback_model"):
            raise AssertionError("A worker used a different model or a fallback model.")
        if int(event.get("input_tokens", 0)) <= 0 or int(event.get("output_tokens", 0)) <= 0:
            raise AssertionError("A model call has no actual token usage evidence.")
    return {
        "model_calls": len(calls),
        "input_tokens": sum(int(event["input_tokens"]) for event in calls),
        "output_tokens": sum(int(event["output_tokens"]) for event in calls),
        "workers": sorted({str(event["component"]) for event in calls}),
        "fallback_count": 0,
    }


def _json(response: httpx.Response) -> dict[str, Any]:
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict):
        raise TypeError("Expected a JSON object from the NutriPilot API.")
    return value


if __name__ == "__main__":
    sys.exit(main())
