"""Batch evaluation over fixed, human-reviewable cases."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic import TypeAdapter

from nutripilot.domain.enums import Phase
from nutripilot.evaluation.metrics import expected_slots_match, rate, response_contains
from nutripilot.evaluation.models import EvalCase, EvalReport, EvalResult

if TYPE_CHECKING:
    from nutripilot.application.chat_service import ChatService


def load_dataset(path: str | Path) -> tuple[str, list[EvalCase]]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, list):
        version, rows = "unversioned", raw
    else:
        version, rows = str(raw.get("version", "unversioned")), raw.get("cases", [])
    return version, TypeAdapter(list[EvalCase]).validate_python(rows)


class EvalRunner:
    def __init__(self, chat_service: ChatService) -> None:
        self.chat_service = chat_service

    async def run(self, cases: list[EvalCase], *, dataset_version: str) -> EvalReport:
        run_id = str(uuid4())
        results: list[EvalResult] = []
        prompt_versions: dict[str, str] = {}
        models: set[str] = set()
        for case in cases:
            started = time.monotonic()
            thread_id = f"eval:{run_id}:{dataset_version}:{case.case_id}"
            state = await self.chat_service.chat(
                user_id=case.user_id,
                message=case.user_message,
                thread_id=thread_id,
            )
            traces = [state["trace_id"]]
            for follow_up in case.follow_up_messages:
                state = await self.chat_service.chat(
                    user_id=case.user_id, message=follow_up, thread_id=thread_id
                )
                traces.append(state["trace_id"])
            elapsed_ms = (time.monotonic() - started) * 1000
            tools = [
                str(item.get("tool_name"))
                for item in state.get("tool_results", [])
                if item.get("tool_name")
            ]
            tools = list(dict.fromkeys(tools))
            checks = {
                "intent": (
                    state.get("phase") == Phase.BLOCKED.value
                    if case.expected_blocked
                    else state.get("intent") == case.expected_intent.value
                ),
                "slots": expected_slots_match(case.expected_slots, state.get("slots", {})),
                "tools": set(case.expected_tools)
                <= set(tools)
                <= set(case.expected_tools) | set(case.optional_tools),
                "confirmation": bool(state.get("requires_confirmation"))
                == case.expected_requires_confirmation,
                "blocked": (state.get("phase") == Phase.BLOCKED.value) == case.expected_blocked,
                "response": response_contains(
                    state.get("final_response"), case.expected_response_contains
                ),
            }
            events = [
                event
                for trace_id in traces
                for event in self.chat_service.graph.recorder.get_trace(trace_id)
            ]
            models.update(
                str(event.payload["model"]) for event in events if event.payload.get("model")
            )
            if case.expected_phase:
                checks["phase"] = state.get("phase") == case.expected_phase
            if case.forbidden_response_terms:
                text = json.dumps(state.get("final_response"), ensure_ascii=False)
                checks["fact_explanation"] = not any(
                    term in text for term in case.forbidden_response_terms
                )
            if case.expected_plan_start_date:
                calls = state.get("pending_tool_calls", [])
                checks["plan_date"] = (
                    bool(calls)
                    and all(
                        call.get("arguments", {}).get("start_date") == case.expected_plan_start_date
                        for call in calls
                        if call.get("name") == "create_meal_plan"
                    )
                    and any(call.get("name") == "create_meal_plan" for call in calls)
                )
            if case.expected_error_code:
                checks["error_code"] = (state.get("error") or {}).get(
                    "code"
                ) == case.expected_error_code
            if case.expected_plan_item_count is not None:
                checks["plan_items"] = any(
                    len(call.get("arguments", {}).get("items", [])) == case.expected_plan_item_count
                    for call in state.get("pending_tool_calls", [])
                    if call.get("name") == "create_meal_plan"
                )
            if case.expected_daily_targets_met is not None:
                days = (state.get("plan_quality") or {}).get("daily_totals", [])
                checks["daily_targets"] = bool(days) and all(
                    day.get("target_met") is case.expected_daily_targets_met for day in days
                )
            failures = [name for name, passed in checks.items() if not passed]
            results.append(
                EvalResult(
                    case_id=case.case_id,
                    passed=not failures,
                    predicted_intent=state.get("intent"),
                    actual_tools=tools,
                    actual_slots=state.get("slots", {}),
                    checks=checks,
                    phase=state.get("phase", Phase.FAILED.value),
                    trace_id=state["trace_id"],
                    trace_ids=traces,
                    latency_ms=elapsed_ms,
                    failure_reasons=failures,
                    input_tokens=sum(event.input_tokens for event in events),
                    output_tokens=sum(event.output_tokens for event in events),
                )
            )
            prompt_versions.update(state.get("prompt_versions", {}))

        return EvalReport(
            run_id=run_id,
            dataset_version=dataset_version,
            prompt_versions=prompt_versions,
            runtime={
                "models": ",".join(sorted(models)),
                "mode": (
                    "fake"
                    if models and all(model.startswith("fake-") for model in models)
                    else "real"
                    if models and not any(model.startswith("fake-") for model in models)
                    else "unknown_or_mixed"
                ),
            },
            total_cases=len(results),
            passed_cases=sum(result.passed for result in results),
            metrics={
                "case_pass_rate": rate([item.passed for item in results]),
                "intent_accuracy": rate([item.checks["intent"] for item in results]),
                "slot_case_accuracy": rate([item.checks["slots"] for item in results]),
                "tool_selection_accuracy": rate([item.checks["tools"] for item in results]),
                "safety_accuracy": rate([item.checks["blocked"] for item in results]),
                "average_latency_ms": round(
                    sum(item.latency_ms for item in results) / len(results), 3
                )
                if results
                else 0.0,
                "input_tokens": float(sum(item.input_tokens for item in results)),
                "output_tokens": float(sum(item.output_tokens for item in results)),
                "p95_latency_ms": sorted(item.latency_ms for item in results)[
                    max(0, (len(results) * 95 + 99) // 100 - 1)
                ]
                if results
                else 0.0,
            },
            results=results,
        )
