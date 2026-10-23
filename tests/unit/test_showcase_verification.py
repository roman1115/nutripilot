from copy import deepcopy
from typing import Any

import pytest
from scripts.verify_showcase import _model_evidence


def _trace() -> dict[str, Any]:
    return {
        "events": [
            {
                "event_type": "model.completed",
                "component": worker,
                "payload": {"model": "qwen-plus", "used_fallback_model": False},
                "input_tokens": 100,
                "output_tokens": 20,
            }
            for worker in ("IntentAgent", "PlannerAgent", "RecommendationAgent")
        ]
    }


def test_showcase_requires_actual_execution_from_all_workers() -> None:
    evidence = _model_evidence(_trace(), expected_model="qwen-plus")

    assert evidence["model_calls"] == 3
    assert evidence["input_tokens"] == 300
    assert evidence["output_tokens"] == 60
    assert evidence["fallback_count"] == 0


@pytest.mark.parametrize(
    "failure",
    [
        "missing_worker",
        "fallback_event",
        "tool_failure",
        "fake_model",
        "zero_tokens",
        "fallback_model",
    ],
)
def test_showcase_rejects_incomplete_or_degraded_model_evidence(failure: str) -> None:
    trace = deepcopy(_trace())
    events = trace["events"]
    if failure == "missing_worker":
        events.pop()
    elif failure == "fallback_event":
        events.append({"event_type": "model.fallback", "component": "PlannerAgent"})
    elif failure == "tool_failure":
        events.append({"event_type": "tool.failed", "component": "search_meals"})
    elif failure == "fake_model":
        events[0]["payload"]["model"] = "fake-nutripilot-v1"
    elif failure == "zero_tokens":
        events[0]["input_tokens"] = 0
    elif failure == "fallback_model":
        events[0]["payload"]["used_fallback_model"] = True

    with pytest.raises(AssertionError):
        _model_evidence(trace, expected_model="qwen-plus")
