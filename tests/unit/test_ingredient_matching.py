from __future__ import annotations

import pytest
from pydantic import ValidationError

from nutripilot.agent.agents.intent import _resolved_slots
from nutripilot.agent.answers import _find_constraint_violations
from nutripilot.agent.contracts import IntentOutput
from nutripilot.agent.grounding import ground_recommendation_facts
from nutripilot.agent.nodes.tool import _extract_grounding
from nutripilot.agent.state import new_agent_state
from nutripilot.domain.models import FinalResponse
from nutripilot.retrieval.models import MealSearchFilters
from nutripilot.safety.dietary import has_dietary_conflict, match_ingredient
from nutripilot.tools.mcp.models import SearchMealsMCPArgs


def test_identity_aliases_separate_fresh_ingredients_and_contact_only_items():
    records = [
        {"ingredient_id": "tomato", "name": "鲜番茄", "aliases": ["西红柿", "番茄"]},
        {"ingredient_id": "peanut", "name": "花生油", "aliases": ["花生"], "consumed": False},
    ]
    assert match_ingredient("西红柿", [], records)[0]["ingredient_id"] == "tomato"
    assert not match_ingredient("花生", ["花生油"], records)
    assert has_dietary_conflict(
        meal_allergens=[], meal_ingredients=[], declared_allergens=["花生"],
        ingredient_records=records,
    )
    sauce = [{"ingredient_id": "sauce", "name": "番茄调味酱", "aliases": ["番茄酱"]}]
    assert not match_ingredient("番茄", ["番茄调味酱"], sauce)
    assert match_ingredient("番茄", [], sauce, exclusion=True)
    assert match_ingredient("西红柿", [], sauce, exclusion=True)
    assert not match_ingredient("西红柿", [], sauce)
    assert match_ingredient("鸡胸肉", ["去皮鸡胸肉"])  # Legacy records remain readable.


@pytest.mark.parametrize("model", [MealSearchFilters, SearchMealsMCPArgs])
@pytest.mark.parametrize("groups", [[[]], [[" "]], [["鸡蛋"] * 11]])
def test_any_groups_are_validated_at_each_boundary(model, groups):
    with pytest.raises(ValidationError):
        model.model_validate({"query": "菜", "ingredient_any_groups": groups}
                             if model is SearchMealsMCPArgs else {"ingredient_any_groups": groups})


@pytest.mark.parametrize(
    ("message", "extracted", "required", "groups", "excluded"),
    [
        ("换一批", {}, ["鸡胸肉"], [], ["香菜"]),
        ("改成豆腐", {"required_ingredients": ["豆腐"]}, ["豆腐"], [], ["香菜"]),
        ("再加番茄", {"required_ingredients": ["番茄"]}, ["鸡胸肉", "番茄"], [], ["香菜"]),
        ("不限食材", {}, [], [], []),
        ("取消食材限制", {"ingredient_action": "clear"}, [], [], []),
        ("换一批，不要鸡胸肉", {"excluded_ingredients": ["鸡胸肉"]}, [], [], ["香菜", "鸡胸肉"]),
        (
            "鸡胸肉或豆腐都可以",
            {"ingredient_any_groups": [["鸡胸肉", "豆腐"]]},
            [], [["鸡胸肉", "豆腐"]], ["香菜"],
        ),
    ],
)
def test_explicit_ingredient_lifecycle_preserves_allergy(
    message, extracted, required, groups, excluded
):
    state = new_agent_state(message=message, user_id=1)
    state["slots"] = {
        "required_ingredients": ["鸡胸肉"], "excluded_ingredients": ["香菜"],
        "allergens": ["花生"],
    }
    slots, _ = _resolved_slots(
        state, IntentOutput(intent="MEAL_ADJUSTMENT", confidence=1, extracted_slots=extracted)
    )
    assert slots["required_ingredients"] == required
    assert slots["ingredient_any_groups"] == groups
    assert slots["excluded_ingredients"] == excluded
    assert slots["allergens"] == ["花生"]
    assert "ingredient_action" not in slots


def test_or_reasons_only_claim_the_matched_option_and_guards_recheck_candidates():
    meals = [
        {"meal_id": 1, "name": "鸡丝", "ingredients": ["鸡胸肉"], "allergens": []},
        {"meal_id": 2, "name": "鸡翅", "ingredients": ["鸡翅"], "allergens": []},
    ]
    results = [{"tool_name": "search_meals", "status": "success", "data": {"hits": meals}}]
    constraints = {"ingredient_any_groups": [["鸡胸肉", "豆腐"]]}
    ids, _, removed = _extract_grounding(results, constraints)
    assert ids == [1]
    assert removed == {2}
    state = new_agent_state(message="鸡胸肉或豆腐都可以", user_id=1)
    state.update(slots=constraints, tool_results=results)
    assert _find_constraint_violations(state) == {2}
    response = ground_recommendation_facts(FinalResponse(message=""), results, constraints)
    assert [card.meal_id for card in response.recommendations] == [1]
    assert "鸡胸肉" in response.recommendations[0].reason
    assert "豆腐" not in response.recommendations[0].reason
