import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from openai import APIStatusError
from pydantic import ValidationError

from nutripilot.agent.contracts import AnswerReview, IntentOutput, PlannerOutput
from nutripilot.config import Settings
from nutripilot.domain.enums import Intent
from nutripilot.model_gateway import ModelGatewayError, OpenAICompatibleModelGateway
from nutripilot.model_gateway.budget import (
    ExecutionLimitExceeded,
    execution_budget,
    model_allocation,
)


class FakeCompletions:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.models: list[str] = []
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.models.append(kwargs["model"])
        self.requests.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=response))],
            usage=SimpleNamespace(prompt_tokens=12, completion_tokens=8),
        )


def fake_client(responses: list[Any]) -> Any:
    completions = FakeCompletions(responses)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


def intent_json() -> str:
    return json.dumps(
        {
            "intent": Intent.MEAL_RECOMMENDATION.value,
            "confidence": 0.9,
            "extracted_slots": {"meal_type": "dinner"},
            "missing_slots": [],
            "notes": "test",
        }
    )


def test_budget_calibration_preserves_actual_usage_and_never_increases_the_cap():
    request = {"payload": "字段" * 1000}
    with execution_budget(seconds=5, tokens=5000) as budget:
        conservative = budget.estimate_input(request)
        budget.observe_input(request, 1000)
        assert budget.estimate_input(request) == 1200
        assert budget.estimate_input(request) < conservative
        budget.add_usage(1000, 200)
        assert budget.remaining_tokens == 3800
        assert budget.completion_limit(request) == 2600
        budget.observe_input(request, conservative * 2)
        assert budget.estimate_input(request) == conservative
        budget.add_usage(3800, 0)
        with pytest.raises(ExecutionLimitExceeded, match="TOKEN_BUDGET_EXCEEDED"):
            budget.completion_limit(request)


def test_generation_allocation_caps_output_and_resets_after_failure():
    with execution_budget(seconds=5, tokens=10000) as budget:
        usual = budget.completion_limit({})
        with pytest.raises(RuntimeError), model_allocation(output_tokens=1200):
            assert budget.completion_limit({}) == 1200
            raise RuntimeError("cancel generation")
        assert budget.completion_limit({}) == usual
        with (
            model_allocation(output_tokens=2400),
            pytest.raises(ExecutionLimitExceeded),
        ):
            budget.add_usage(9900, 0)
            budget.completion_limit({})


@pytest.mark.asyncio
async def test_invalid_reviewer_reference_retries_review_without_rewriting_answer():
    check = {
        "unit_id": "message:0", "kind": "conversation", "has_object_count": False,
        "object_counts": [], "conditions": [], "conditions_preserved": True,
        "supported": True, "explanation": "对象未明确，合理澄清。",
    }
    invalid = {"coherent": True, "supported": True, "checks": [{
        **check, "evidence": [{"record_id": "invented", "quote": "not in evidence"}],
    }]}
    valid = {**invalid, "checks": [{**check, "evidence": []}]}
    client = fake_client([json.dumps(invalid), json.dumps(valid)])
    gateway = OpenAICompatibleModelGateway(
        api_key="test", model="primary", max_retries=1, client=client,
    )
    payload = {"review_units": {"message:0": "你指的是哪道菜？"}, "evidence_records": {}}
    result = await gateway.generate_structured(
        worker="answer_review", system_prompt="review", payload=payload,
        response_model=AnswerReview, prompt_version="test",
    )
    assert result.attempts == 2
    requests = client.chat.completions.requests
    assert all(json.loads(r["messages"][1]["content"])["input"] == payload for r in requests)
    feedback = json.loads(requests[1]["messages"][1]["content"])["schema_feedback"]
    assert "引用不存在" in feedback[0]["message"]


@pytest.mark.parametrize("invalid_name", ["search_meals", "delete_user"])
@pytest.mark.asyncio
async def test_planner_repairs_unknown_arguments_or_tools_with_existing_retry(invalid_name):
    valid = {
        "complete": False, "plan_summary": "搜索候选",
        "tool_calls": [{
            "call_id": "search", "name": "search_meals", "arguments": {"query": "午餐"},
        }],
    }
    invalid = {
        **valid, "tool_calls": [{
            "call_id": "search", "name": invalid_name,
            "arguments": {"query": "午餐", "max_fat_g": 10},
        }],
    }
    payload = {"available_tools": [{"function": {
        "name": "search_meals", "parameters": {
            "type": "object", "properties": {"query": {"type": "string"}},
            "additionalProperties": False,
        },
    }}]}
    client = fake_client([json.dumps(invalid), json.dumps(valid)])
    gateway = OpenAICompatibleModelGateway(
        api_key="test", model="primary", max_retries=1, client=client,
    )
    result = await gateway.generate_structured(
        worker="planner", system_prompt="plan", payload=payload,
        response_model=PlannerOutput, prompt_version="test",
    )
    assert result.attempts == 2
    assert result.output.tool_calls[0].arguments == {"query": "午餐"}
    feedback = json.loads(
        client.chat.completions.requests[1]["messages"][1]["content"],
    )["schema_feedback"]
    assert "允许" in feedback[0]["message"]
    assert ("不支持参数" if invalid_name == "search_meals" else "不在当前白名单") in (
        feedback[0]["message"]
    )


def test_settings_default_to_credential_free_fake_mode() -> None:
    settings = Settings(_env_file=None)

    assert settings.app_mode == "demo"
    assert settings.model_provider == "fake"
    assert settings.uses_fake_model is True
    assert settings.model_timeout_seconds == 45
    assert settings.agent_deadline_seconds == 90
    assert settings.agent_token_budget == 24000


def test_settings_require_key_for_real_provider() -> None:
    with pytest.raises(ValidationError, match="MODEL_API_KEY"):
        Settings(model_provider="openai", model_api_key=None, _env_file=None)


@pytest.mark.asyncio
async def test_openai_gateway_repairs_invalid_structured_output_by_retrying() -> None:
    client = fake_client(["not-json", intent_json()])
    gateway = OpenAICompatibleModelGateway(
        api_key="test",
        model="primary",
        max_retries=1,
        client=client,
    )

    result = await gateway.generate_structured(
        worker="intent",
        system_prompt="test",
        payload={"message": "晚餐"},
        response_model=IntentOutput,
        prompt_version="test-v1",
    )

    assert result.output.intent is Intent.MEAL_RECOMMENDATION
    assert result.attempts == 2
    assert result.usage.total_tokens == 40
    repaired = json.loads(client.chat.completions.requests[1]["messages"][1]["content"])
    assert repaired["schema_feedback"] == [{"type": "invalid_json"}]


@pytest.mark.asyncio
async def test_schema_retry_explains_allowed_values_without_echoing_input() -> None:
    invalid = json.dumps({"intent": "secret-value", "confidence": 0.9})
    client = fake_client([invalid, intent_json()])
    gateway = OpenAICompatibleModelGateway(
        api_key="test", model="primary", max_retries=1, client=client,
    )
    result = await gateway.generate_structured(
        worker="intent", system_prompt="test", payload={}, response_model=IntentOutput,
        prompt_version="test-v1",
    )
    assert result.attempts == 2
    repaired = json.loads(client.chat.completions.requests[1]["messages"][1]["content"])
    feedback, = repaired["schema_feedback"]
    assert feedback["field"] == ["intent"]
    assert "MEAL_RECOMMENDATION" in feedback["message"]
    assert "secret-value" not in json.dumps(feedback)


@pytest.mark.asyncio
async def test_openai_gateway_switches_to_fallback_after_retryable_failure() -> None:
    client = fake_client([TimeoutError("slow"), intent_json()])
    gateway = OpenAICompatibleModelGateway(
        api_key="test",
        model="primary",
        fallback_model="backup",
        max_retries=0,
        client=client,
    )

    result = await gateway.generate_structured(
        worker="intent",
        system_prompt="test",
        payload={"message": "晚餐"},
        response_model=IntentOutput,
        prompt_version="test-v1",
    )

    assert result.model == "backup"
    assert result.used_fallback is True
    assert client.chat.completions.models == ["primary", "backup"]


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (403, {"code": "AllocationQuota.FreeTierOnly"}, "MODEL_QUOTA_EXHAUSTED"),
        (403, {"error": {"type": "AllocationQuota.FreeTierOnly"}}, "MODEL_QUOTA_EXHAUSTED"),
        (401, {"error": {"code": "invalid_api_key"}}, "MODEL_AUTHENTICATION_FAILED"),
        (403, {"code": "AccessDenied"}, "MODEL_PERMISSION_DENIED"),
        (400, "invalid request", "MODEL_REQUEST_REJECTED"),
    ],
)
@pytest.mark.asyncio
async def test_provider_rejections_are_safe_terminal_errors(status, body, expected) -> None:
    error = APIStatusError(
        "raw provider detail containing secret-value",
        response=httpx.Response(status, request=httpx.Request("POST", "https://provider.invalid")),
        body=body,
    )
    client = fake_client([error])
    gateway = OpenAICompatibleModelGateway(
        api_key="test", model="primary", fallback_model="backup", max_retries=2, client=client
    )
    with pytest.raises(ModelGatewayError) as caught:
        await gateway.generate_structured(
            worker="intent",
            system_prompt="test",
            payload={},
            response_model=IntentOutput,
            prompt_version="test",
        )
    assert caught.value.code == expected
    assert not caught.value.retryable
    assert client.chat.completions.models == ["primary"]
    assert "secret-value" not in str(caught.value)
