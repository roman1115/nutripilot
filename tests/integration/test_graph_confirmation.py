from datetime import date, timedelta

import pytest

from nutripilot.application import build_container
from nutripilot.application.chat_service import InvalidConfirmationStateError
from nutripilot.domain.enums import Phase
from nutripilot.model_gateway import FakeModelGateway
from nutripilot.tools.local.models import UpdateUserProfileArgs


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_tool_waits_for_confirmation_and_resumes_once() -> None:
    container = build_container()

    waiting = await container.chat_service.chat(
        user_id=1,
        message="帮我制定一周晚餐饮食计划",
        thread_id="plan-flow",
    )
    assert waiting["phase"] == Phase.AWAITING_CONFIRMATION.value
    assert waiting["requires_confirmation"] is True
    assert waiting["pending_tool_calls"][0]["name"] == "create_meal_plan"
    pending_items = waiting["pending_tool_calls"][0]["arguments"]["items"]
    assert len(pending_items) == 7
    assert {item["meal_type"] for item in pending_items} == {"dinner"}
    assert len({item["meal_date"] for item in pending_items}) == 7

    completed = await container.chat_service.confirm(
        thread_id="plan-flow", user_id=1, approved=True
    )
    assert completed["phase"] == Phase.COMPLETED.value
    plan_results = [
        result for result in completed["tool_results"] if result["tool_name"] == "create_meal_plan"
    ]
    assert len(plan_results) == 1
    assert plan_results[0]["status"] == "success"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rejected_write_does_not_execute() -> None:
    container = build_container()
    await container.chat_service.chat(
        user_id=1,
        message="帮我制定一周晚餐饮食计划",
        thread_id="rejected-plan",
    )

    state = await container.chat_service.confirm(
        thread_id="rejected-plan", user_id=1, approved=False
    )

    assert state["phase"] == Phase.COMPLETED.value
    assert all(result["tool_name"] != "create_meal_plan" for result in state["tool_results"])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_default_week_plan_contains_seven_days_of_three_meals() -> None:
    container = build_container()

    state = await container.chat_service.chat(
        user_id=1,
        message="帮我制定一周饮食计划",
        thread_id="full-week-plan",
    )

    items = state["pending_tool_calls"][0]["arguments"]["items"]
    assert len(items) == 21
    assert len({item["meal_date"] for item in items}) == 7
    assert {item["meal_type"] for item in items} == {"breakfast", "lunch", "dinner"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stale_confirmation_id_cannot_approve_a_different_action() -> None:
    container = build_container()
    await container.chat_service.chat(
        user_id=1,
        message="帮我制定一周晚餐饮食计划",
        thread_id="stale-confirmation",
    )

    with pytest.raises(InvalidConfirmationStateError):
        await container.chat_service.confirm(
            thread_id="stale-confirmation",
            user_id=1,
            approved=True,
            confirmation_id="another-turn:write",
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_plan_write_preflight_blocks_meal_outside_safe_candidates() -> None:
    gateway = FakeModelGateway()
    gateway.queue_response(
        "planner",
        {
            "complete": False,
            "tool_calls": [{"call_id": "profile", "name": "get_user_profile", "arguments": {}}],
            "plan_summary": "读取画像",
        },
    )
    gateway.queue_response(
        "planner",
        {
            "complete": False,
            "tool_calls": [
                {
                    "call_id": "weather",
                    "name": "get_weather_context",
                    "arguments": {"location": "上海"},
                }
            ],
            "plan_summary": "读取天气",
        },
    )
    gateway.queue_response(
        "planner",
        {
            "complete": False,
            "tool_calls": [
                {
                    "call_id": "search",
                    "name": "search_meals",
                    "arguments": {"query": "一周晚餐", "meal_type": "dinner", "limit": 10},
                }
            ],
            "plan_summary": "检索安全候选",
        },
    )
    start_date = date.today()
    gateway.queue_response(
        "planner",
        {
            "complete": False,
            "tool_calls": [
                {
                    "call_id": "unsafe-plan",
                    "name": "create_meal_plan",
                    "arguments": {
                        "title": "不安全计划",
                        "start_date": start_date.isoformat(),
                        "items": [
                            {
                                "meal_date": (start_date + timedelta(days=index)).isoformat(),
                                "meal_type": "dinner",
                                "meal_id": 5,
                            }
                            for index in range(7)
                        ],
                    },
                }
            ],
            "plan_summary": "尝试写入非安全候选",
        },
    )
    container = build_container(gateway=gateway)
    await container.data_store.update_user_profile(
        1,
        UpdateUserProfileArgs(allergens=["香菇"], dislikes=["香菇"]),
    )

    state = await container.chat_service.chat(
        user_id=1,
        message="帮我制定一周晚餐饮食计划",
        thread_id="unsafe-plan-preflight",
    )

    failed = next(
        result for result in state["tool_results"] if result["tool_name"] == "create_meal_plan"
    )
    assert failed["status"] == "error"
    assert failed["error"]["code"] == "PLAN_CANDIDATE_NOT_ALLOWED"
    assert "UNSAFE_PLAN_BLOCKED" in state["risk_flags"]
    assert await container.data_store.list_plans(1) == []
