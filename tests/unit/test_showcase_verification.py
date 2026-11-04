from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from scripts import verify_real_regressions, verify_showcase
from scripts.verify_showcase import ModelProbeOutput, _model_evidence

from nutripilot.config import Settings
from nutripilot.model_gateway import GatewayResult, ModelGatewayError, ModelUsage


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


@pytest.mark.parametrize("failure", [None, "quota", "missing_usage"])
@pytest.mark.asyncio
async def test_model_probe_never_retries_falls_back_or_leaves_client_open(monkeypatch, failure):
    result = GatewayResult(
        output=ModelProbeOutput(ok=True),
        model="qwen-plus",
        usage=ModelUsage(input_tokens=0 if failure == "missing_usage" else 100, output_tokens=5),
    )
    generate = AsyncMock(return_value=result)
    if failure == "quota":
        generate.side_effect = ModelGatewayError("MODEL_QUOTA_EXHAUSTED", "额度耗尽")
    gateway = SimpleNamespace(
        generate_structured=generate, client=SimpleNamespace(close=AsyncMock())
    )
    factory = Mock(return_value=gateway)
    monkeypatch.setattr(verify_showcase, "OpenAICompatibleModelGateway", factory)
    monkeypatch.setenv("MODEL_API_KEY", "private-test-key")
    settings = Settings(_env_file=None, model_provider="openai")
    evidence = await verify_showcase.probe_model(settings)

    assert evidence["status"] == ("blocked" if failure else "passed")
    if failure:
        assert evidence["error_code"] == (
            "MODEL_QUOTA_EXHAUSTED" if failure == "quota" else "MODEL_USAGE_MISSING"
        )
    if failure == "quota":
        assert evidence["usage"] is None
    else:
        assert evidence["usage"]["output_tokens"] == 5
        assert "estimated_cost" not in evidence["usage"]
    assert factory.call_args.kwargs["max_retries"] == 0
    assert not factory.call_args.kwargs.get("fallback_model")
    assert generate.call_count == 1
    gateway.client.close.assert_awaited_once()
    assert "private-test-key" not in str(evidence)


@pytest.mark.parametrize("failure", ["wrong_model", "no_tokens", "fallback"])
def test_regression_evidence_rejects_invalid_model_execution(failure) -> None:
    events = _trace()["events"]
    if failure == "wrong_model":
        events[0]["payload"]["model"] = "fake-nutripilot-v1"
    elif failure == "no_tokens":
        events[0]["input_tokens"] = 0
    else:
        events.append(
            {"event_type": "model.fallback", "payload": {"error_code": "MODEL_QUOTA_EXHAUSTED"}}
        )
    evidence = verify_real_regressions._model_evidence(events, expected_model="qwen-plus")
    assert evidence["invalid_model_calls"] or evidence["fallbacks"]
    if failure == "fallback":
        assert evidence["model_errors"] == ["MODEL_QUOTA_EXHAUSTED"]


def test_failed_probe_prevents_batch_evaluation(monkeypatch) -> None:
    monkeypatch.setattr(verify_real_regressions.sys, "argv", ["verify_real_regressions.py"])
    monkeypatch.setattr(
        verify_real_regressions.subprocess, "run", Mock(return_value=SimpleNamespace(returncode=1))
    )
    evaluate = Mock()
    monkeypatch.setattr(verify_real_regressions.asyncio, "run", evaluate)
    assert verify_real_regressions.main() == 1
    evaluate.assert_not_called()
