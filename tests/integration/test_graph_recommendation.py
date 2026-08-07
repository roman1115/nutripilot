import pytest

from nutripilot.application import build_container
from nutripilot.domain.enums import Phase
from nutripilot.model_gateway import FakeModelGateway
from nutripilot.tools.local.models import RecordFeedbackArgs, UpdateUserProfileArgs


@pytest.mark.integration
@pytest.mark.asyncio
async def test_graph_runs_three_workers_and_real_tool_loop() -> None:
    gateway = FakeModelGateway()
    container = build_container(gateway=gateway)

    state = await container.chat_service.chat(
        user_id=1,
        message="晚餐想吃清淡的减脂餐, 我对花生过敏",
        thread_id="recommendation-flow",
    )

    assert state["phase"] == Phase.COMPLETED.value
    assert state["slots"]["meal_type"] == "dinner"
    assert state["slots"]["allergens"] == ["花生"]
    assert state["candidate_ids"]
    assert state["final_response"]["recommendations"]
    assert {item["tool_name"] for item in state["tool_results"]} == {
        "get_user_profile",
        "get_user_feedback",
        "get_weather_context",
        "search_meals",
    }
    workers = [item["worker"] for item in gateway.call_history]
    assert workers[0] == "intent"
    assert "planner" in workers
    assert workers[-1] == "recommendation"
    events = container.recorder.get_trace(state["trace_id"])
    assert any(event.component == "ToolExecutor" for event in events)
    assert any(event.component == "OutputGuard" for event in events)
    first_planner_call = next(item for item in gateway.call_history if item["worker"] == "planner")
    available_names = {
        schema["function"]["name"] for schema in first_planner_call["payload"]["available_tools"]
    }
    assert available_names == {
        "get_user_profile",
        "get_user_feedback",
        "get_weather_context",
        "search_meals",
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_planner_rejects_tool_outside_the_intent_allowlist() -> None:
    gateway = FakeModelGateway()
    gateway.queue_response(
        "planner",
        {
            "complete": False,
            "tool_calls": [
                {
                    "call_id": "off-intent",
                    "name": "update_user_profile",
                    "arguments": {"preferences": ["清淡"]},
                }
            ],
            "plan_summary": "越权修改画像",
        },
    )
    container = build_container(gateway=gateway)

    state = await container.chat_service.chat(
        user_id=1,
        message="推荐清淡晚餐",
        thread_id="planner-allowlist",
    )

    assert state["phase"] == Phase.COMPLETED.value
    assert "update_user_profile" not in {
        result["tool_name"] for result in state["tool_results"]
    }
    assert any(
        item["component"] == "PlannerAgent" for item in state["fallback_events"]
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_worker_failure_uses_controlled_fallback() -> None:
    gateway = FakeModelGateway()
    gateway.queue_response("intent", RuntimeError("model timeout"))
    container = build_container(gateway=gateway)

    state = await container.chat_service.chat(
        user_id=1,
        message="晚餐推荐一个清淡的",
        thread_id="fallback-flow",
    )

    assert state["phase"] == Phase.COMPLETED.value
    assert state["fallback_events"][0]["component"] == "IntentAgent"
    events = container.recorder.get_trace(state["trace_id"])
    assert any(event.event_type == "model.fallback" for event in events)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_profile_constraints_are_enforced_when_planner_omits_them() -> None:
    gateway = FakeModelGateway()
    gateway.queue_response(
        "planner",
        {
            "complete": False,
            "tool_calls": [
                {"call_id": "profile", "name": "get_user_profile", "arguments": {}}
            ],
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
                    "arguments": {"query": "午餐吃什么", "meal_type": "lunch", "limit": 10},
                }
            ],
            "plan_summary": "检索餐食",
        },
    )
    gateway.queue_response(
        "planner",
        {"complete": True, "tool_calls": [], "plan_summary": "完成"},
    )
    container = build_container(gateway=gateway)
    await container.data_store.update_user_profile(
        1,
        UpdateUserProfileArgs(dislikes=["香菇"], allergens=["香菇"]),
    )

    state = await container.chat_service.chat(
        user_id=1,
        message="午餐吃什么",
        thread_id="profile-constraint-flow",
    )

    assert state["phase"] == Phase.COMPLETED.value
    assert "香菇" in state["dietary_constraints"]["allergens"]
    assert "香菇" in state["dietary_constraints"]["excluded_ingredients"]
    search_result = next(
        result for result in state["tool_results"] if result["tool_name"] == "search_meals"
    )
    hits = search_result["data"]["hits"]
    assert hits
    assert all("香菇" not in ingredient for hit in hits for ingredient in hit["ingredients"])
    assert all(item["meal_id"] != 5 for item in state["final_response"]["recommendations"])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_meal_search_fails_closed_when_profile_was_not_loaded() -> None:
    gateway = FakeModelGateway()
    gateway.queue_response(
        "planner",
        {
            "complete": False,
            "tool_calls": [
                {
                    "call_id": "unsafe-direct-search",
                    "name": "search_meals",
                    "arguments": {"query": "午餐吃什么", "meal_type": "lunch"},
                }
            ],
            "plan_summary": "直接检索",
        },
    )
    gateway.queue_response(
        "planner",
        {"complete": True, "tool_calls": [], "plan_summary": "完成"},
    )
    container = build_container(gateway=gateway)

    state = await container.chat_service.chat(
        user_id=1,
        message="午餐吃什么",
        thread_id="profile-required-flow",
    )

    search_result = next(
        result for result in state["tool_results"] if result["tool_name"] == "search_meals"
    )
    assert search_result["status"] == "error"
    assert search_result["error"]["code"] == "PROFILE_REQUIRED"
    assert state["candidate_ids"] == []
    assert state["final_response"]["recommendations"] == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dislike_feedback_excludes_meal_from_next_recommendation() -> None:
    container = build_container(gateway=FakeModelGateway())

    first = await container.chat_service.chat(
        user_id=1,
        message="推荐一个清淡的晚餐",
        thread_id="feedback-first",
    )
    disliked_meal_id = first["final_response"]["recommendations"][0]["meal_id"]
    await container.data_store.record_feedback(
        1,
        RecordFeedbackArgs(meal_id=disliked_meal_id, feedback_type="dislike"),
        "feedback-exclusion-test",
    )

    second = await container.chat_service.chat(
        user_id=1,
        message="再推荐一个清淡的晚餐",
        thread_id="feedback-second",
    )

    feedback_result = next(
        result
        for result in second["tool_results"]
        if result["tool_name"] == "get_user_feedback"
    )
    assert disliked_meal_id in feedback_result["data"]["excluded_meal_ids"]
    assert disliked_meal_id not in {
        item["meal_id"] for item in second["final_response"]["recommendations"]
    }
