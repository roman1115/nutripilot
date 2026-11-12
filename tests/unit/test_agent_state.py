from datetime import UTC, date, datetime

import pytest

from nutripilot.agent.agents.intent import _resolved_slots
from nutripilot.agent.contracts import IntentOutput
from nutripilot.agent.routing import route_after_tool
from nutripilot.agent.slots import extract_calorie_limit, today_in_timezone
from nutripilot.agent.state import new_agent_state
from nutripilot.domain.enums import Phase
from nutripilot.memory.context import merge_slots


def test_new_agent_state_has_explicit_serializable_defaults() -> None:
    state = new_agent_state(message="  推荐晚餐  ", user_id=7, thread_id="thread-1")

    assert state["thread_id"] == "thread-1"
    assert state["user_message"] == "推荐晚餐"
    assert state["phase"] == Phase.RECEIVED.value
    assert state["slots"] == {}
    assert state["tool_results"] == []
    assert state["requires_confirmation"] is False


def test_today_in_user_timezone_handles_utc_date_boundary() -> None:
    instant = datetime(2026, 10, 27, 2, 0, tzinfo=UTC)

    assert today_in_timezone("Asia/Shanghai", now=instant) == date(2026, 10, 27)
    assert today_in_timezone("America/Los_Angeles", now=instant) == date(2026, 10, 26)


def test_merge_slots_preserves_existing_and_deduplicates_lists() -> None:
    merged = merge_slots(
        {"meal_type": "dinner", "allergens": ["花生"]},
        {"allergens": ["花生", "牛奶"], "tastes": ["清淡"], "empty": None},
    )

    assert merged == {
        "meal_type": "dinner",
        "allergens": ["花生", "牛奶"],
        "tastes": ["清淡"],
    }


@pytest.mark.parametrize("unit", ["千卡", "大卡", "kcal", "KCAL"])
def test_explicit_calorie_limit_overrides_model_unit_conversion(unit) -> None:
    state = new_agent_state(message=f"午餐推荐一道菜，不超过1{unit}", user_id=7)
    output = IntentOutput(
        intent="MEAL_RECOMMENDATION",
        confidence=1,
        extracted_slots={"meal_type": "lunch", "max_calories_kcal": 1000},
    )

    slots, missing = _resolved_slots(state, output)

    assert slots["max_calories_kcal"] == 1
    assert not missing
    assert extract_calorie_limit(f"午餐不超过0.5{unit}") == 0.5
    assert extract_calorie_limit("每日1500千卡") is None


@pytest.mark.parametrize("intent", ["MEAL_RECOMMENDATION", "MEAL_ADJUSTMENT"])
@pytest.mark.parametrize("hits", [[], [{"meal_id": 1001}]])
def test_successful_search_finishes_retrieval_even_without_candidates(intent, hits) -> None:
    state = new_agent_state(message="推荐一道菜", user_id=7)
    state["intent"] = intent
    state["step_count"] = 8
    state["tool_results"] = [
        {"tool_name": "search_meals", "status": "success", "data": {"hits": hits}}
    ]

    assert route_after_tool(state) == "recommendation"

    state["intent"] = "MEAL_PLAN"
    state["step_count"] = 3
    assert route_after_tool(state) == "planner"
    state["tool_results"][-1].update(status="error", error={"retryable": False})
    assert route_after_tool(state) == "tool_failure"
