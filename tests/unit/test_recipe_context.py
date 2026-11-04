import pytest

from nutripilot.agent.agents.intent import _resolved_slots
from nutripilot.agent.contracts import IntentOutput
from nutripilot.agent.slots import resolve_recipe_name
from nutripilot.agent.state import new_agent_state


@pytest.mark.parametrize(
    ("message", "name"),
    [
        ("给我西葫芦炒鸡蛋的做法", "西葫芦炒鸡蛋"),
        ("西葫芦炒鸡蛋怎么做", "西葫芦炒鸡蛋"),
        ("怎么做西葫芦炒鸡蛋", "西葫芦炒鸡蛋"),
        ("请告诉我西葫芦炒鸡蛋做法", "西葫芦炒鸡蛋"),
        ("这个怎么做", None),
        ("怎么做", None),
        ("第二道怎么做", None),
    ],
)
def test_recipe_name_without_context_never_uses_stale_nutrition_slot(message, name):
    state = new_agent_state(message=message, user_id=1)
    state["slots"] = {"food_name": "糖拌西红柿", "meal_type": "lunch"}
    output = IntentOutput(
        intent="RECIPE_QUERY", confidence=1, extracted_slots={"food_name": "糖拌西红柿"}
    )
    slots, missing = _resolved_slots(state, output)
    assert slots.get("food_name") == name
    assert missing == ([] if name else ["food_name"])
    assert slots["meal_type"] == "lunch"


def test_explicit_model_extraction_supports_polite_phrases_without_forcing_bad_prefix():
    assert resolve_recipe_name(
        "请给我详细介绍西葫芦炒鸡蛋的做法", "西葫芦炒鸡蛋", []
    ) == "西葫芦炒鸡蛋"


def test_a_prior_dish_substring_does_not_override_the_new_explicit_dish():
    assert resolve_recipe_name(
        "给我西葫芦炒鸡蛋的做法",
        "给我西葫芦炒鸡蛋的做法",
        [{"name": "炒鸡蛋", "meal_id": 99}],
    ) == "西葫芦炒鸡蛋"


@pytest.mark.parametrize("message", ["这个怎么做", "这道菜的做法", "怎么做"])
def test_single_dish_reference_requires_exactly_one_displayed_dish(message):
    one = [{"meal_id": 1008, "name": "西葫芦炒鸡蛋"}]
    two = [*one, {"meal_id": 1004, "name": "糖拌西红柿"}]
    assert resolve_recipe_name(message, "糖拌西红柿", one) == "西葫芦炒鸡蛋"
    assert resolve_recipe_name(message, "糖拌西红柿", two) is None
