"""Frozen synthetic text acceptance using the actual configured text deployment."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from nutripilot.application.diet_parsing import (
    PARSER_PROMPT,
    PARSER_PROMPT_VERSION,
)
from nutripilot.config import Settings
from nutripilot.domain.diet import SubmissionAnchor
from nutripilot.domain.diet_jobs import ParsedIntake
from nutripilot.model_gateway import OpenAICompatibleModelGateway
from nutripilot.model_gateway.budget import execution_budget


def assess(case: dict[str, Any], output: ParsedIntake) -> dict[str, Any]:
    entries = output.entries
    if case.get("reject_actual"):
        unsafe = any(entry.actuality == "actual" and entry.subject == "self" for entry in entries)
        return {"correct": not unsafe, "unsafe_actual": unsafe, "tp": 0, "fp": 0, "fn": 0}
    accepted = {case["food"], *case.get("aliases", [])}
    names = [
        item.food.name if item.food.kind == "unknown" else ""
        for entry in entries
        for item in entry.items
    ]
    tp = min(1, sum(name in accepted for name in names))
    fields_match = len(entries) == 1 and (
        entries[0].actuality == case["actuality"]
        and entries[0].subject == case["subject"]
        and entries[0].temporal.meal_type == case["meal"]
        and str(entries[0].temporal.local_date) == case["date"]
    )
    quantity_match = len(entries) == 1 and len(entries[0].items) == 1
    if quantity_match:
        quantity = entries[0].items[0].quantity
        quantity_match = (
            quantity.kind == "mass" and quantity.grams == Decimal(case["grams"])
            if "grams" in case
            else quantity.kind == "unknown"
        )
    return {
        "correct": tp == 1 and len(names) == 1 and fields_match and quantity_match,
        "fields_match": fields_match,
        "quantity_match": quantity_match,
        "unsafe_actual": any(
            entry.actuality == "actual" and entry.subject == "self" for entry in entries
        )
        and (case["actuality"] != "actual" or case["subject"] != "self"),
        "tp": tp,
        "fp": len(names) - tp,
        "fn": 1 - tp,
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    frozen = args.dataset.read_bytes()
    dataset = json.loads(frozen)
    assert dataset["cases"], "Acceptance requires a nonempty, frozen dataset."
    assert len({case["id"] for case in dataset["cases"]}) == len(dataset["cases"])
    settings = Settings()
    if not settings.model_api_key:
        raise ValueError("A configured text deployment key is required.")
    gateway = OpenAICompatibleModelGateway(
        api_key=settings.model_api_key,
        model=settings.model_name,
        base_url=settings.model_base_url,
        timeout_seconds=45,
        max_retries=0,
    )
    anchor = SubmissionAnchor.capture(
        datetime.fromisoformat(dataset["anchor"]), dataset["timezone"]
    )
    results: list[dict[str, Any]] = []
    semaphore = asyncio.Semaphore(2)

    async def sample(case: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            started = time.monotonic()
            try:
                with execution_budget(seconds=50, tokens=6000):
                    response = await gateway.generate_structured(
                        worker="diet_parser",
                        system_prompt=PARSER_PROMPT,
                        payload={
                            "text": case["text"],
                            "anchor": anchor.model_dump(mode="json"),
                            "calendar_hint": anchor.calendar_hint(),
                        },
                        response_model=ParsedIntake,
                        prompt_version=PARSER_PROMPT_VERSION,
                    )
                if response.model != settings.model_name or response.usage.total_tokens <= 0:
                    raise ValueError("Actual model identity or usage evidence is missing.")
                return {
                    "id": case["id"],
                    "model": response.model,
                    "usage": response.usage.model_dump(),
                    "attempts": response.attempts,
                    "used_fallback": response.used_fallback,
                    "seconds": round(time.monotonic() - started, 3),
                    "output": response.output.model_dump(mode="json"),
                    **assess(case, response.output),
                }
            except Exception as exc:
                usage = getattr(exc, "usage", None)
                return {
                    "id": case["id"],
                    "correct": False,
                    "error": type(exc).__name__,
                    "code": getattr(exc, "code", None),
                    "validation_errors": (
                        exc.__cause__.errors(
                            include_input=False, include_context=False, include_url=False
                        )
                        if isinstance(exc.__cause__, ValidationError)
                        else []
                    ),
                    "usage": usage.model_dump() if usage is not None else None,
                    "seconds": round(time.monotonic() - started, 3),
                    "tp": 0,
                    "fp": 0,
                    "fn": 0 if case.get("reject_actual") else 1,
                }

    try:
        first = await sample(dataset["cases"][0])
        results.append(first)
        if first.get("code") not in (
            "MODEL_QUOTA_EXHAUSTED",
            "MODEL_AUTHENTICATION_FAILED",
            "MODEL_PERMISSION_DENIED",
            "MODEL_REQUEST_REJECTED",
            "MODEL_CONNECTION_ERROR",
        ):
            tasks = [asyncio.create_task(sample(case)) for case in dataset["cases"][1:]]
            for task in asyncio.as_completed(tasks):
                row = await task
                results.append(row)
                print(f"{row['id']}: {'pass' if row['correct'] else 'fail'}", flush=True)
        tp = sum(row["tp"] for row in results)
        fp = sum(row["fp"] for row in results)
        fn = sum(row["fn"] for row in results)
        precision, recall = tp / (tp + fp) if tp + fp else 0, tp / (tp + fn) if tp + fn else 0
        report = {
            "dataset_sha256": hashlib.sha256(frozen).hexdigest(),
            "prompt_version": PARSER_PROMPT_VERSION,
            "prompt_sha256": hashlib.sha256(PARSER_PROMPT.encode()).hexdigest(),
            "scope": dataset["note"],
            "model": settings.model_name,
            "total": len(dataset["cases"]),
            "executed": len(results),
            "correct": sum(row["correct"] for row in results),
            "precision": precision,
            "recall": recall,
            "unsafe_actual": sum(row.get("unsafe_actual", False) for row in results),
            "formal_writes": 0,
            "passed": len(results) == len(dataset["cases"])
            and all(row["correct"] for row in results)
            and precision >= 0.9
            and recall >= 0.9,
            "results": sorted(results, key=lambda row: row["id"]),
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        return {key: value for key, value in report.items() if key != "results"}
    finally:
        await gateway.client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("data/eval_diet_text_20261112.json"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".runtime/diet-acceptance-20261112/current/text-report.json"),
    )
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
