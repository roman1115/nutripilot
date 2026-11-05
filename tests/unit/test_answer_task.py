from __future__ import annotations

import copy
import json

import pytest

from nutripilot.agent.agents.intent import IntentAgent
from nutripilot.agent.agents.recommendation import RecommendationAgent
from nutripilot.agent.answers import (
    answer_comparisons,
    answer_evidence,
    answer_nutrition_calculations,
    answer_requested_calculations,
    answer_review_records,
    prose_issues,
)
from nutripilot.agent.contracts import AnswerTask, IntentOutput, RecommendationDraft
from nutripilot.agent.grounding import ground_recommendation_facts
from nutripilot.agent.state import new_agent_state
from nutripilot.domain.models import FinalResponse, MealRecommendation
from nutripilot.model_gateway import FakeModelGateway
from nutripilot.tracing import InMemoryTraceRecorder


def meal(meal_id: int, fat: float) -> dict:
    return {
        "meal_id": meal_id, "name": f"菜{meal_id}", "ingredients": ["鸡蛋"],
        "allergens": ["鸡蛋"], "meal_role": "dish",
        "nutrition": {
            "protein_g": 30, "fat_g": fat, "source_status": "calculated_estimate",
            "serving_size": "整道菜", "weight_basis": "ingredient_input",
            "nutrient_ranges": {"fat_g": {"minimum": fat, "maximum": fat}},
        },
    }


def search(*meals: dict) -> dict:
    return {"tool_name": "search_meals", "status": "success", "data": {"hits": list(meals)}}


def test_null_reference_means_no_selected_historical_object():
    assert AnswerTask.model_validate({"meal_ids": None}).meal_ids == []


def test_misplaced_turn_metadata_is_validated_without_becoming_dietary_constraints():
    raw = {
        "intent": "MEAL_ADJUSTMENT", "confidence": 1,
        "extracted_slots": {"meal_ids": [1], "nutrient_preference": "lower_fat"},
    }
    original = copy.deepcopy(raw)
    output = IntentOutput.model_validate(raw)
    assert output.extracted_slots == {}
    assert output.answer_task.meal_ids == [1]
    assert output.answer_task.nutrient_preference == "lower_fat"
    assert raw == original
    with pytest.raises(ValueError, match="Conflicting"):
        IntentOutput.model_validate({**raw, "answer_task": {"meal_ids": [2]}})


@pytest.mark.asyncio
async def test_intent_keeps_relative_goal_and_count_out_of_persistent_slots():
    gateway = FakeModelGateway()
    state = new_agent_state(user_id=1, message="菜1脂肪太高了，换一个")
    state["slots"] = {"meal_type": "lunch", "health_goals": ["high_protein"]}
    state["discussed_meals"] = [{"meal_id": 1, "name": "菜1"}]
    state["recent_messages"] = [{"role": "user", "content": "推荐一份高蛋白午餐"}]
    gateway.queue_response("intent", {
        "intent": "MEAL_ADJUSTMENT", "confidence": 1,
        "extracted_slots": {"excluded_ingredients": ["菜1", "香菜"]},
        "answer_task": {
            "objective": "保留高蛋白午餐需求，替换菜1为脂肪更少的一个选项",
            "context": "meals", "meal_ids": [1, 999], "recommendation_count": 1,
            "nutrient_preference": "lower_fat", "needs_nutrition": True,
        },
    })
    update = await IntentAgent(gateway, InMemoryTraceRecorder()).run(state)
    assert update["answer_task"]["meal_ids"] == [1]
    assert update["answer_task"]["recommendation_count"] == 1
    assert update["slots"]["health_goals"] == ["high_protein"]
    assert update["slots"]["excluded_ingredients"] == ["香菜"]
    assert "nutrient_preference" not in update["slots"]
    assert gateway.call_history[0]["payload"]["recent_messages"] == state["recent_messages"]
    assert new_agent_state(user_id=1, message="谢谢")["answer_task"] == {}


def test_general_knowledge_context_keeps_allergy_but_omits_historical_recipe():
    state = new_agent_state(user_id=1, message="那现在怎么补救")
    state["intent"] = "FOOD_QUESTION"
    state["answer_task"] = {"context": "general", "objective": "补救已经做柴的鸡胸肉"}
    results = [
        search(meal(1, 20)),
        {"tool_name": "get_recipe", "status": "success", "data": {
            "meal_id": 1, "instructions": "旧菜谱煮5分钟",
        }},
        {"tool_name": "get_user_profile", "status": "success", "data": {
            "allergens": ["花生"],
        }},
    ]
    assert answer_evidence(results, state) == [results[2]]


def test_review_keeps_other_candidate_facts_to_verify_exclusive_claims_without_mutation():
    state = new_agent_state(user_id=1, message="换一个")
    state["intent"] = "MEAL_ADJUSTMENT"
    state["answer_task"] = {
        "meal_ids": [1], "nutrient_preference": "lower_fat", "needs_nutrition": True,
    }
    state["conversation_evidence"] = [search(meal(1, 100), meal(9, 50))]
    state["tool_results"] = [search(meal(2, 10), meal(3, 30))]
    original = copy.deepcopy(state)
    draft = RecommendationDraft(
        message="菜2的整菜脂肪估算较少。", recommendations=[{"meal_id": 2, "name": "菜2"}],
    )
    records = answer_review_records(state, draft)
    assert "菜1" in records["history:0"]
    assert "菜9" not in records["history:0"]
    assert "菜2" in records["current:0"]
    assert "菜3" in records["current:0"]
    assert '"fat_g":30' in records["current:0"]
    assert 'ingredients: ["鸡蛋"]' in records["current:0"]
    assert '"fat_g":10' in records["current:0"]
    assert '"source_status":"calculated_estimate"' in records["current:0"]
    assert "meal_role: dish" in records["current:0"]
    assert state == original


def test_normalization_does_not_select_unrequested_candidates_or_write_reasons():
    response = ground_recommendation_facts(
        FinalResponse(message="原稿"),
        [search(meal(1, 100), meal(2, 10))], auto_select=False,
    )
    assert not response.recommendations
    response = ground_recommendation_facts(
        FinalResponse(message="原稿", recommendations=[
            MealRecommendation(meal_id=2, name="菜2", reason="未审核的口感理由"),
        ]),
        [search(meal(1, 100), meal(2, 10))], auto_select=False,
    )
    assert [card.meal_id for card in response.recommendations] == [2]
    assert response.recommendations[0].reason == ""
    assert "配方包含" not in response.message
    assert "口感" not in response.message


def test_relative_fat_and_requested_count_are_checked_without_inventing_a_low_fat_limit():
    state = new_agent_state(user_id=1, message="换一个")
    state["intent"] = "MEAL_ADJUSTMENT"
    state["answer_task"] = {
        "meal_ids": [1], "nutrient_preference": "lower_fat", "recommendation_count": 1,
    }
    state["conversation_evidence"] = [search(meal(1, 100))]
    draft = RecommendationDraft(message="可以选菜2", recommendations=[
        {"meal_id": 2, "name": "菜2"},
    ])
    canonical = FinalResponse(message="事实", recommendations=[
        MealRecommendation(meal_id=2, name="菜2", reason="", fat_g=110),
    ])
    issues = prose_issues(draft, canonical, state, {})
    assert any("LOWER_FAT_NOT_MET" in issue for issue in issues)
    canonical.recommendations[0].fat_g = 50
    assert not prose_issues(draft, canonical, state, {})
    draft.recommendations.append(draft.recommendations[0].model_copy())
    assert any("REQUESTED_COUNT" in issue for issue in prose_issues(draft, canonical, state, {}))


def test_program_supplies_traceable_whole_recipe_comparison_without_mutation():
    state = new_agent_state(user_id=1, message="换一个脂肪少的")
    state["intent"] = "MEAL_ADJUSTMENT"
    state["answer_task"] = {"meal_ids": [1], "nutrient_preference": "lower_fat"}
    state["conversation_evidence"] = [search(meal(1, 111.1104))]
    state["tool_results"] = [search(meal(2, 25.709))]
    original = copy.deepcopy(state)
    comparison, = answer_comparisons(state)
    assert comparison["fat_reduction_percent"] == 76.9
    assert comparison["fat_reduction_g"] == 85.40
    draft = RecommendationDraft(message="整菜脂肪估算减少约77%。")
    assert not prose_issues(draft, FinalResponse(message="事实"), state, {})
    assert "76.9" in answer_review_records(state, draft)["computed:2"]
    assert state == original
    state["tool_results"][0]["data"]["hits"][0]["nutrition"]["nutrient_ranges"]["fat_g"] = {
        "minimum": 20, "maximum": 25.709,
    }
    assert answer_comparisons(state) == []
    state["conversation_evidence"] = [search(meal(1, 0))]
    assert answer_comparisons(state) == []


def test_nutrition_density_and_served_weight_do_not_invent_cooked_weight_or_servings():
    state = new_agent_state(user_id=1, message="哪道更合适")
    first = meal(1, 20)
    first["nutrition"].update(
        calories_kcal=600, protein_g=60, serving_weight_g=500,
        weight_basis="ingredient_input",
    )
    state["tool_results"] = [search(first)]
    original = copy.deepcopy(state)
    row, = answer_nutrition_calculations(state)
    assert row["per_100_kcal"] == {"calories_kcal": 100, "protein_g": 10}
    assert row["half_recipe_estimate"]["calories_kcal"] == 300
    assert row["half_recipe_estimate"]["protein_g"] == 30
    assert "不是来源份数" in row["half_recipe_estimate"]["basis"]
    assert "per_100_g_as_served" not in row
    assert state == original
    first["nutrition"]["weight_basis"] = "as_served"
    row, = answer_nutrition_calculations(state)
    assert row["per_100_g_as_served"]["protein_g"] == 12
    assert row["per_100_g_as_served"]["fat_g"] == 4
    first["nutrition"]["nutrient_ranges"]["protein_g"] = {"minimum": 40, "maximum": 60}
    row, = answer_nutrition_calculations(state)
    assert "per_100_kcal" not in row
    assert "protein_g" not in row["per_100_g_as_served"]
    first["nutrition"]["weight_basis"] = "ingredient_input"
    row, = answer_nutrition_calculations(state)
    assert "per_100_kcal" not in row
    assert "per_100_g_as_served" not in row
    assert "protein_g" not in row["half_recipe_estimate"]
    first["nutrition"]["calories_kcal"] = 0
    row, = answer_nutrition_calculations(state)
    assert "per_100_kcal" not in row
    state["answer_task"] = {"context": "general"}
    assert answer_nutrition_calculations(state) == []


def test_protein_comparison_uses_original_total_and_respects_uncertainty():
    state = new_agent_state(user_id=1, message="换一个蛋白更多的")
    state["answer_task"] = {"meal_ids": [1], "nutrient_preference": "higher_protein"}
    state["conversation_evidence"] = [search(meal(1, 20))]
    candidate = meal(2, 10)
    candidate["nutrition"]["protein_g"] = 45
    state["tool_results"] = [search(candidate)]
    row, = answer_comparisons(state)
    assert row["protein_increase_g"] == 15
    assert row["protein_increase_percent"] == 50
    assert "等份量" in row["basis"]
    candidate["nutrition"]["nutrient_ranges"]["protein_g"] = {"minimum": 30, "maximum": 45}
    assert not answer_comparisons(state)


def test_agent_requested_difference_is_computed_from_known_sources_not_model_numbers():
    state = new_agent_state(user_id=1, message="选哪个")
    state["tool_results"] = [search(meal(1, 111.1104), meal(2, 25.709))]
    draft = RecommendationDraft(
        message="整份配方的脂肪估算少85.4克。",
        calculations=[{"meal_id": 2, "reference_meal_id": 1, "nutrient": "fat_g"}],
    )
    row, = answer_requested_calculations(state, draft)
    assert row["direction"] == "lower"
    assert row["absolute_difference"] == {"fat_g": 85.4}
    assert row["change_percent"] == 76.9
    assert not prose_issues(draft, FinalResponse(message="事实"), state, {})
    assert "85.4" in answer_review_records(state, draft)["calculation:0"]
    draft.calculations[0].reference_meal_id = 999
    assert not answer_requested_calculations(state, draft)
    assert any("UNSUPPORTED_NUMBER" in issue for issue in prose_issues(
        draft, FinalResponse(message="事实"), state, {},
    ))


@pytest.mark.asyncio
async def test_revision_preserves_task_and_the_same_projected_tool_evidence():
    gateway = FakeModelGateway()
    state = new_agent_state(user_id=1, message="推荐一道菜")
    state.update(
        intent="MEAL_RECOMMENDATION", candidate_ids=[1], tool_results=[search(meal(1, 10))],
        answer_task={"recommendation_count": 1, "objective": "推荐一个有依据的选项"},
    )
    gateway.queue_response("recommendation", {"response": {
        "message": "只需9999分钟", "recommendations": [{"meal_id": 1, "name": "菜1"}],
    }})
    gateway.queue_response("recommendation", {"response": {
        "message": "可以选菜1，配方含鸡蛋。", "recommendations": [{"meal_id": 1, "name": "菜1"}],
    }})
    update = await RecommendationAgent(gateway, InMemoryTraceRecorder()).run(state)
    calls = [call for call in gateway.call_history if call["worker"] == "recommendation"]
    assert update["approved_answer"]
    assert calls[0]["system_prompt"] in calls[1]["system_prompt"]
    assert calls[1]["payload"]["answer_task"] == state["answer_task"]
    assert calls[1]["payload"]["tool_results"] == calls[0]["payload"]["tool_results"]
    assert calls[1]["payload"]["conversation_evidence"] == calls[0]["payload"][
        "conversation_evidence"
    ]
    assert "evidence_records" not in calls[1]["payload"]
    assert "message" not in calls[1]["payload"]["answer_to_revise"]
    assert "9999" not in json.dumps(update["final_response"])
