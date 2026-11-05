import copy
import json

import pytest

from nutripilot.agent.agents.base import compact_tool_results
from nutripilot.agent.agents.recommendation import RecommendationAgent
from nutripilot.agent.answers import answer_boundary_evidence, answer_evidence, compact_answer
from nutripilot.agent.contracts import RecommendationDraft, RecommendationOutput
from nutripilot.agent.state import AgentState
from nutripilot.domain.models import FinalResponse, MealRecommendation
from nutripilot.model_gateway import FakeModelGateway
from nutripilot.tracing import InMemoryTraceRecorder


def test_recommendation_output_schema_only_requests_model_authored_fields():
    definitions = RecommendationOutput.model_json_schema()["$defs"]
    assert set(definitions["RecommendationDraft"]["properties"]) == {
        "status", "message", "recommendations", "source_ids", "calculations"
    }
    assert set(definitions["RecommendationChoice"]["properties"]) == {"meal_id", "name", "reason"}


def test_review_fact_projection_does_not_carry_template_prose_or_reasons():
    response = FinalResponse(
        message="程序拼接的正文，不应引导生成。",
        recommendations=[MealRecommendation(
            meal_id=1001, name="测试菜", reason="程序拼接的理由",
        )],
    )
    original = response.model_dump()

    assert compact_answer(response) == {
        "status": "completed",
        "recommendations": [{"meal_id": 1001, "name": "测试菜"}],
    }
    assert response.model_dump() == original


@pytest.mark.parametrize(
    ("message", "intent", "slots", "nutrition", "weather"),
    [
        ("想吃鸡胸肉", "MEAL_RECOMMENDATION", {}, False, False),
        ("为什么推荐第二个", "FOOD_QUESTION", {}, False, False),
        ("第二个热量多少", "FOOD_QUESTION", {}, True, False),
        ("查询凉拌鸡丝的营养", "NUTRITION_QUERY", {}, True, False),
        ("想吃鸡胸肉", "MEAL_RECOMMENDATION", {"max_calories_kcal": 500}, True, False),
        ("根据天气推荐午餐", "MEAL_RECOMMENDATION", {}, False, True),
    ],
)
def test_generation_evidence_only_exposes_relevant_numbers_and_retains_safety(
    message, intent, slots, nutrition, weather,
):
    meal = {
        "meal_id": 1001, "name": "测试菜", "ingredients": ["鸡胸肉"],
        "allergens": ["大豆"], "nutrition": {"calories_kcal": 100, "fat_g": 0},
    }
    results = [
        {"tool_name": "search_meals", "status": "success", "data": {"hits": [meal]}},
        {"tool_name": "get_recipe", "status": "success", "data": {
            **meal, "instructions": "步骤原文", "raw_quantities": "原文用量",
        }},
        {"tool_name": "get_user_profile", "status": "success", "data": {
            "allergens": ["花生"], "dislikes": ["香菇"], "health_goals": ["减脂"],
            "daily_calorie_target": 1800, "preferences": ["清淡"],
        }},
        {"tool_name": "get_weather_context", "status": "success", "data": {
            "temperature_c": 22,
        }},
    ]
    original = copy.deepcopy(results)
    state: AgentState = {"user_message": message, "intent": intent, "slots": slots}

    projected = answer_evidence(results, state)

    assert results == original
    assert ("nutrition" in projected[0]["data"]["hits"][0]) is nutrition
    assert ("nutrition" in projected[1]["data"]) is nutrition
    assert projected[0]["data"]["hits"][0]["allergens"] == ["大豆"]
    assert projected[1]["data"]["instructions"] == "步骤原文"
    assert projected[1]["data"]["raw_quantities"] == "原文用量"
    assert projected[2]["data"] == {
        "allergens": ["花生"], "dislikes": ["香菇"], "preferences": ["清淡"],
    }
    assert any(item["tool_name"] == "get_weather_context" for item in projected) is weather


def test_boundary_evidence_binds_ingredients_to_selected_dish_and_keeps_originals():
    state: AgentState = {
        "intent": "MEAL_RECOMMENDATION", "user_message": "推荐午餐",
        "tool_results": [{
            "tool_name": "search_meals", "status": "success", "data": {"hits": [
                {"meal_id": 1, "name": "猪肉饭", "ingredients": ["猪肉", "米饭"],
                 "ingredient_records": [{"name": "猪肉", "aliases": ["肉末"]}],
                 "allergens": ["大豆"]},
                {"meal_id": 2, "name": "鸡肉饭", "ingredients": ["鸡胸肉", "米饭"]},
            ]},
        }],
    }
    original = copy.deepcopy(state)
    draft = RecommendationDraft(
        message="可以选猪肉饭", recommendations=[{"meal_id": 1, "name": "猪肉饭"}],
    )
    result = answer_boundary_evidence(state, draft)
    assert len(result[0]["data"]["hits"]) == 1
    selected = result[0]["data"]["hits"][0]
    assert selected["ingredients"] == ["猪肉", "米饭"]
    assert selected["allergens"] == ["大豆"]
    assert selected["ingredient_records"] == [{"name": "猪肉", "aliases": ["肉末"]}]
    assert state == original


def test_recipe_generation_does_not_mix_nutrition_mapping_with_original_ingredients():
    state: AgentState = {"intent": "RECIPE_QUERY", "user_message": "凉拌鸡丝怎么做"}
    results = [
        {"tool_name": "search_meals", "status": "success", "data": {"hits": [{
            "meal_id": 1034, "name": "凉拌鸡丝", "ingredients": ["蒸馏白醋"],
        }]}},
        {"tool_name": "get_recipe", "status": "success", "data": {
            "meal_id": 1034, "name": "凉拌鸡丝",
            "raw_ingredients": "香醋", "raw_quantities": "香醋4毫升", "instructions": "拌匀",
        }},
    ]
    original = copy.deepcopy(results)
    projected = answer_evidence(results, state)
    assert [item["tool_name"] for item in projected] == ["get_recipe"]
    assert projected[0]["data"]["raw_ingredients"] == "香醋"
    assert results == original


@pytest.mark.parametrize("nested", [False, True])
def test_model_context_preserves_constraints_without_repeating_provenance(nested) -> None:
    meal = {
        "meal_id": 1001,
        "name": "测试菜",
        "is_active": False,
        "meal_types": ["lunch"],
        "meal_role": "dish",
        "ingredients": ["鸡蛋"],
        "allergens": ["鸡蛋"],
        "tastes": ["清淡"],
        "nutrition": {
            "calories_kcal": 200,
            "protein_g": 12,
            "source_status": "calculated_estimate",
            "serving_size": "整道菜",
            "weight_basis": "ingredient_input",
            "nutrient_ranges": {"calories_kcal": {"minimum": 180, "maximum": 200}},
            "calculation_note": "完整投料计算依据。" * 2000,
            "source_uri": "https://example.test/recipe",
            "ingredient_source_uris": ["https://example.test/ingredient"],
        },
    }
    results = [
        {
            "tool_name": "search_meals",
            "status": "success",
            "data": {"hits": [{"meal": meal} if nested else meal]},
        },
        {"tool_name": "get_nutrition", "status": "success", "data": meal},
        {"tool_name": "get_nutrition", "status": "error", "error": {"code": "NOT_FOUND"}},
        {
            "tool_name": "get_user_profile",
            "status": "success",
            "data": {"allergens": ["花生"], "dislikes": ["香菇"]},
        },
    ]
    original = copy.deepcopy(results)

    projected = compact_tool_results(results)

    assert results == original
    assert len(json.dumps(projected)) < len(json.dumps(original)) / 10
    hit = projected[0]["data"]["hits"][0]
    assert hit["allergens"] == ["鸡蛋"]
    assert hit["ingredients"] == ["鸡蛋"]
    assert hit["is_active"] is False
    assert hit["nutrition"]["nutrient_ranges"] == meal["nutrition"]["nutrient_ranges"]
    assert hit["nutrition"]["source_status"] == "calculated_estimate"
    assert projected[1]["data"] == hit
    assert projected[2]["error"] == {"code": "NOT_FOUND"}
    assert projected[3]["data"] == original[3]["data"]


@pytest.mark.asyncio
async def test_recommendation_grounds_from_original_results_after_context_projection() -> None:
    gateway = FakeModelGateway()
    note = "完整投料估算说明，未经成菜检测。" * 1000
    state: AgentState = {
        "trace_id": "context-test",
        "thread_id": "context-test",
        "turn_id": "context-test",
        "user_id": 1,
        "intent": "MEAL_RECOMMENDATION",
        "user_message": "推荐一道菜",
        "candidate_ids": [1001],
        "tool_results": [
            {
                "tool_name": "search_meals",
                "status": "success",
                "data": {
                    "hits": [
                        {
                            "meal_id": 1001,
                            "name": "测试菜",
                            "meal_role": "dish",
                            "allergens": ["鸡蛋"],
                            "nutrition": {
                                "calories_kcal": 200,
                                "protein_g": 12,
                                "source_status": "calculated_estimate",
                                "serving_size": "整道菜",
                                "calculation_note": note,
                                "source_uri": "https://example.test/recipe",
                                "ingredient_source_uris": ["https://example.test/ingredient"],
                            },
                        }
                    ]
                },
            }
        ],
    }

    update = await RecommendationAgent(gateway, InMemoryTraceRecorder()).run(state)

    assert "calculation_note" not in json.dumps(gateway.call_history[0]["payload"])
    assert gateway.call_history[0]["payload"]["user_message"] == "推荐一道菜"
    for call in gateway.call_history:
        assert "verified_response" not in call["payload"]
    assert "verified_facts" not in gateway.call_history[0]["payload"]
    assert len(gateway.call_history) == 2
    assert gateway.call_history[0]["worker"] == "recommendation"
    assert gateway.call_history[1]["worker"] == "answer_boundary"
    assert "evidence_records" not in gateway.call_history[1]["payload"]
    assert "nutrition" not in gateway.call_history[0]["payload"]["tool_results"][0][
        "data"
    ]["hits"][0]
    response = update["final_response"]
    assert response["recommendations"][0]["meal_id"] == 1001
    assert response["warnings"] == [
        "营养按整道菜估算，不代表个人摄入量或完整一餐，未经成菜检测。",
        "餐食过敏原标注不涵盖制作过程中的交叉接触，请核对实际食材。",
        "测试菜：餐食记录标注的过敏原：鸡蛋。",
    ]
    assert response["citations"][0]["excerpt"] == note
    assert [citation["uri"] for citation in response["citations"]] == [
        "https://example.test/recipe",
        "https://example.test/ingredient",
    ]
