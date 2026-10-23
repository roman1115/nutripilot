"""Run the small real-model regression dataset through a warmed API."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

import httpx


async def verify(args: argparse.Namespace) -> dict[str, object]:
    token = os.getenv("NUTRIPILOT_ACCESS_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(base_url=args.base_url, headers=headers, timeout=150) as client:
        health = await client.get("/health/ready")
        health.raise_for_status()
        runtime = health.json()
        assert runtime["model_provider"] == "openai"
        assert runtime["embedding_provider"] == runtime["rerank_provider"] == "bge"
        dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
        response = await client.post(
            "/api/v1/evaluations/run",
            json={
                "dataset_version": dataset["version"],
                "cases": dataset["cases"],
            },
        )
        response.raise_for_status()
        run_id = response.json()["run_id"]
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            response = await client.get(f"/api/v1/evaluations/{run_id}")
            response.raise_for_status()
            report = response.json()
            if report["status"] != "running":
                break
            await asyncio.sleep(1)
        else:
            raise TimeoutError(f"Evaluation {run_id} is still running")
        evidence = []
        for result in report["results"]:
            events = []
            for trace_id in result.get("trace_ids") or [result["trace_id"]]:
                trace = await client.get(f"/api/v1/traces/{trace_id}")
                trace.raise_for_status()
                events.extend(trace.json()["events"])
            evidence.append(
                {
                    "case_id": result["case_id"],
                    "requires_model": next(
                        case.get("metadata", {}).get("requires_model", True)
                        for case in dataset["cases"]
                        if case["id"] == result["case_id"]
                    ),
                    "deterministic_validation": any(
                        event["event_type"] == "intent.scope_validated" for event in events
                    ),
                    "model_calls": sum(
                        event["event_type"] == "model.completed" for event in events
                    ),
                    "fallbacks": sum(
                        "fallback" in event["event_type"]
                        or bool(event.get("payload", {}).get("used_fallback_model"))
                        for event in events
                    ),
                }
            )
        return {"runtime": runtime, "report": report, "evidence": evidence}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("evals/datasets/real-regressions.json"),
    )
    parser.add_argument("--output", type=Path, default=Path(".runtime/real-regressions.json"))
    parser.add_argument("--timeout", type=float, default=420)
    args = parser.parse_args()
    result = asyncio.run(verify(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    report = result["report"]
    assert isinstance(report, dict)
    print(
        json.dumps(
            {
                "status": report["status"],
                "run_id": report["run_id"],
                "passed": report["passed_cases"],
                "total": report["total_cases"],
                "failures": report["failures"],
                "metrics": report["metrics"],
                "evidence": result["evidence"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    evidence = result["evidence"]
    assert isinstance(evidence, list)
    return (
        0
        if (
            report["status"] == "completed"
            and report["passed_cases"] == report["total_cases"]
            and all(
                item["fallbacks"] == 0
                and (
                    item["model_calls"] > 0
                    or (not item["requires_model"] and item["deterministic_validation"])
                )
                for item in evidence
            )
        )
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
