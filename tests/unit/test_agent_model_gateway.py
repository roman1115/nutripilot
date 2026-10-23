import json
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from nutripilot.agent.contracts import IntentOutput
from nutripilot.config import Settings
from nutripilot.domain.enums import Intent
from nutripilot.model_gateway import OpenAICompatibleModelGateway


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


def test_settings_default_to_credential_free_fake_mode() -> None:
    settings = Settings(_env_file=None)

    assert settings.app_mode == "demo"
    assert settings.model_provider == "fake"
    assert settings.uses_fake_model is True


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
