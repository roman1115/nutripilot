import pytest
from pydantic import ValidationError

from nutripilot.agent.agents.intent import _resolved_slots
from nutripilot.agent.contracts import IntentOutput
from nutripilot.agent.nodes.tool import _apply_profile_constraints
from nutripilot.agent.state import new_agent_state
from nutripilot.model_gateway import FakeModelGateway
from nutripilot.tools import ToolCall

LIMITS = {
    "max_calories_kcal": 600,
    "max_price_cny": 20,
    "max_prep_minutes": 15,
    "min_protein_g": 30,
}


@pytest.mark.parametrize("field", LIMITS)
def test_explicit_clear_only_removes_requested_temporary_constraint(field):
    state = new_agent_state(message="取消这项限制，换一批", user_id=1)
    state["slots"] = {
        **LIMITS, "allergens": ["花生"], "dislikes": ["香菜"],
        "required_ingredients": ["鸡胸肉"],
    }
    output = IntentOutput(
        intent="MEAL_ADJUSTMENT", confidence=1,
        extracted_slots={"clear_constraints": [field]},
    )

    slots, missing = _resolved_slots(state, output)

    assert slots[field] is None
    assert {key: slots[key] for key in LIMITS if key != field} == {
        key: value for key, value in LIMITS.items() if key != field
    }
    assert slots["allergens"] == ["花生"]
    assert slots["dislikes"] == ["香菜"]
    assert slots["required_ingredients"] == ["鸡胸肉"]
    assert "clear_constraints" not in slots
    assert not missing
    assert state["slots"][field] == LIMITS[field]


def test_missing_or_null_output_preserves_limits_and_plain_adjustment_keeps_clears():
    state = new_agent_state(message="换一批", user_id=1)
    state["slots"] = {**LIMITS, "max_price_cny": None}
    slots, _ = _resolved_slots(state, IntentOutput(
        intent="MEAL_ADJUSTMENT", confidence=1,
        extracted_slots={"max_calories_kcal": None, "max_price_cny": None},
    ))

    assert {key: slots[key] for key in LIMITS} == state["slots"]


@pytest.mark.parametrize("field", LIMITS)
def test_cleared_constraint_can_be_set_again(field):
    state = new_agent_state(message="重新设置条件", user_id=1)
    state["slots"] = {field: None, "allergens": ["花生"]}

    slots, _ = _resolved_slots(state, IntentOutput(
        intent="MEAL_ADJUSTMENT", confidence=1, extracted_slots={field: LIMITS[field]},
    ))

    assert slots[field] == LIMITS[field]
    assert slots["allergens"] == ["花生"]


@pytest.mark.parametrize("field", ["allergens", "dislikes", "daily_calorie_target", "unknown"])
def test_clear_contract_only_accepts_supported_temporary_numeric_constraints(field):
    with pytest.raises(ValidationError):
        IntentOutput(
            intent="MEAL_ADJUSTMENT", confidence=1,
            extracted_slots={"clear_constraints": [field]},
        )


def test_tool_boundary_drops_cleared_limits_even_if_planner_reintroduces_them():
    call = ToolCall(call_id="search", name="search_meals", arguments={
        "query": "菜", **LIMITS,
    })
    profile = [{
        "tool_name": "get_user_profile", "status": "success",
        "data": {"allergens": ["花生"], "dislikes": ["香菜"]},
    }]

    result = _apply_profile_constraints(call, profile, {
        **dict.fromkeys(LIMITS), "allergens": ["鸡蛋"],
    })

    assert all(field not in result.arguments for field in LIMITS)
    assert set(result.arguments["excluded_allergens"]) == {"花生", "鸡蛋"}
    assert {"花生", "鸡蛋", "香菜"} <= set(result.arguments["excluded_ingredients"])
    assert call.arguments["max_price_cny"] == 20


def test_tool_boundary_uses_replacement_values_instead_of_old_stricter_limits():
    call = ToolCall(call_id="search", name="search_meals", arguments={
        "query": "菜", **LIMITS,
    })
    updated = {
        "max_calories_kcal": 900, "max_price_cny": 50,
        "max_prep_minutes": 30, "min_protein_g": 10,
    }

    result = _apply_profile_constraints(call, [], updated)

    assert {key: result.arguments[key] for key in LIMITS} == updated


def test_clear_wins_over_echoed_values_and_previous_explicit_calorie_text():
    state = new_agent_state(message="之前不超过600千卡，现在取消热量限制", user_id=1)
    state["slots"] = dict(LIMITS)

    slots, _ = _resolved_slots(state, IntentOutput(
        intent="MEAL_ADJUSTMENT", confidence=1,
        extracted_slots={"max_calories_kcal": 600, "clear_constraints": ["max_calories_kcal"]},
    ))

    assert slots["max_calories_kcal"] is None


def test_plan_keeps_cleared_limits_distinct_from_unspecified_daily_targets():
    state = new_agent_state(message="取消热量和蛋白质限制，重新安排一天餐单", user_id=1)
    state["slots"] = dict(LIMITS)

    slots, _ = _resolved_slots(state, IntentOutput(
        intent="MEAL_PLAN", confidence=1,
        extracted_slots={"clear_constraints": ["max_calories_kcal", "min_protein_g"]},
    ))

    assert "max_calories_kcal" in slots and slots["max_calories_kcal"] is None
    assert "min_protein_g" in slots and slots["min_protein_g"] is None
    assert slots["max_price_cny"] == 20


@pytest.mark.parametrize(
    ("message", "cleared"),
    [
        ("不限热量了", ["max_calories_kcal"]),
        ("取消预算限制", ["max_price_cny"]),
        ("时间不限", ["max_prep_minutes"]),
        ("不再限制蛋白质下限", ["min_protein_g"]),
        ("取消热量和预算限制", ["max_calories_kcal", "max_price_cny"]),
        ("取消准备时间限制，热量不限", ["max_calories_kcal", "max_prep_minutes"]),
        ("不要取消热量限制", []),
        ("别取消预算限制", []),
        ("怎么取消热量限制？", []),
        ("如果取消热量限制会怎么样？", []),
        ("不用取消时间限制", []),
    ],
)
def test_fake_cancellation_phrases_and_negations(message, cleared):
    output = IntentOutput.model_validate(FakeModelGateway._intent({
        "message": message, "slots": LIMITS,
    }))
    assert sorted(output.extracted_slots.get("clear_constraints", [])) == cleared
    if cleared:
        assert output.intent == "MEAL_ADJUSTMENT"
