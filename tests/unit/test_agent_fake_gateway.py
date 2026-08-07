import pytest

from nutripilot.agent.contracts import IntentOutput, PlannerOutput
from nutripilot.domain.enums import Intent
from nutripilot.model_gateway import FakeModelGateway


@pytest.mark.asyncio
async def test_fake_gateway_returns_validated_intent_without_api_key() -> None:
    gateway = FakeModelGateway()

    result = await gateway.generate_structured(
        worker="intent",
        system_prompt="test",
        payload={"message": "晚餐想吃清淡的减脂餐", "slots": {}},
        response_model=IntentOutput,
        prompt_version="test-v1",
    )

    assert result.output.intent is Intent.MEAL_RECOMMENDATION
    assert result.output.extracted_slots["meal_type"] == "dinner"
    assert result.usage.total_tokens > 0


@pytest.mark.asyncio
async def test_fake_gateway_extracts_arbitrary_inline_allergy() -> None:
    result = await FakeModelGateway().generate_structured(
        worker="intent",
        system_prompt="test",
        payload={"message": "我对香菇过敏，推荐晚餐", "slots": {}},
        response_model=IntentOutput,
        prompt_version="test-v1",
    )

    assert result.output.extracted_slots["allergens"] == ["香菇"]


@pytest.mark.asyncio
async def test_fake_gateway_supports_queued_failures_and_responses() -> None:
    gateway = FakeModelGateway()
    gateway.queue_response("planner", RuntimeError("offline"))
    gateway.queue_response(
        "planner",
        {"complete": True, "tool_calls": [], "plan_summary": "done"},
    )

    with pytest.raises(RuntimeError, match="offline"):
        await gateway.generate_structured(
            worker="planner",
            system_prompt="test",
            payload={},
            response_model=PlannerOutput,
            prompt_version="test-v1",
        )

    result = await gateway.generate_structured(
        worker="planner",
        system_prompt="test",
        payload={},
        response_model=PlannerOutput,
        prompt_version="test-v1",
    )
    assert result.output.complete is True
