from __future__ import annotations

import pytest
from apps.nutrition_mcp.service import NutritionMCPService

from nutripilot.agent.grounding import ground_recommendation_facts
from nutripilot.application import build_container
from nutripilot.config import Settings
from nutripilot.domain.models import FinalResponse, MealRecommendation
from nutripilot.paths import project_root
from nutripilot.tools.errors import ToolRuntimeError
from nutripilot.tools.mcp import InProcessMCPTransport, MCPToolAdapter


@pytest.fixture
def container():
    return build_container(settings=Settings(_env_file=None))


@pytest.mark.asyncio
async def test_actual_follow_up_returns_sourced_steps_instead_of_more_recommendations(container):
    first = await container.chat_service.chat(
        user_id=1, thread_id="recipe-actual", message="推荐午餐"
    )
    assert first["final_response"]["recommendations"]
    state = await container.chat_service.chat(
        user_id=1, thread_id="recipe-actual", message="给我西葫芦炒鸡蛋的做法"
    )
    assert state["intent"] == "RECIPE_QUERY"
    assert state["phase"] == "COMPLETED"
    assert [item["tool_name"] for item in state["tool_results"]] == ["get_recipe"]
    response = state["final_response"]
    assert not response["recommendations"]
    assert "西葫芦 500g" in response["message"]
    assert "西红柿（可选）" in response["message"]
    assert "等待 4 - 5 分钟" in response["message"]
    assert "中火翻炒 15 秒" in response["message"]
    assert "**" not in response["message"]
    recipe = next(item for item in container.food_catalog.recipes if item.id == 1008)
    assert response["citations"][0]["uri"] == str(recipe.source_uri)
    assert any("过敏原：鸡蛋" in warning for warning in response["warnings"])


@pytest.mark.asyncio
async def test_ordered_reference_uses_display_order_then_single_dish_context(container):
    first = await container.chat_service.chat(
        user_id=1, thread_id="recipe-ordinal", message="推荐午餐"
    )
    second_dish = first["final_response"]["recommendations"][1]
    state = await container.chat_service.chat(
        user_id=1, thread_id="recipe-ordinal", message="第二道怎么做？"
    )
    assert state["slots"]["food_name"] == second_dish["name"]
    assert state["tool_results"][0]["data"]["meal_id"] == second_dish["meal_id"]
    again = await container.chat_service.chat(
        user_id=1, thread_id="recipe-ordinal", message="这个的做法"
    )
    assert again["phase"] == "COMPLETED"
    assert again["slots"]["food_name"] == second_dish["name"]


@pytest.mark.asyncio
async def test_ambiguous_reference_clarifies_without_reusing_stale_food_name(container):
    await container.chat_service.chat(
        user_id=1, thread_id="recipe-ambiguous", message="查询糖拌西红柿的营养"
    )
    first = await container.chat_service.chat(
        user_id=1, thread_id="recipe-ambiguous", message="推荐午餐"
    )
    state = await container.chat_service.chat(
        user_id=1, thread_id="recipe-ambiguous", message="这个怎么做？"
    )
    assert state["phase"] == "CLARIFY"
    assert "food_name" not in state["slots"]
    assert not state["tool_results"]
    assert "哪道菜的做法" in state["final_response"]["message"]
    reply = await container.chat_service.chat(
        user_id=1, thread_id="recipe-ambiguous", message="第二道"
    )
    assert reply["phase"] == "COMPLETED"
    assert reply["slots"]["food_name"] == first["final_response"]["recommendations"][1]["name"]
    explicit = await container.chat_service.chat(
        user_id=1, thread_id="recipe-ambiguous", message="给我西葫芦炒鸡蛋的做法"
    )
    assert explicit["phase"] == "COMPLETED"
    assert explicit["clarification_intent"] is None
    assert explicit["slots"]["food_name"] == "西葫芦炒鸡蛋"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["月球神仙菜", "糖拌"])
async def test_unknown_or_fuzzy_recipe_does_not_invent_steps(container, name):
    state = await container.chat_service.chat(user_id=1, message=f"给我{name}的做法")
    assert state["phase"] == "CLARIFY"
    assert state["final_response"]["status"] == "needs_clarification"
    assert not state["final_response"]["recommendations"]
    assert "做法记录" in state["final_response"]["message"] or "请确认" in (
        state["final_response"]["message"]
    )
    assert "营养记录" not in state["final_response"]["message"]


@pytest.mark.asyncio
async def test_model_misclassification_or_outage_cannot_turn_explicit_recipe_into_recommendation(
    container,
):
    for output in (
        {"intent": "MEAL_RECOMMENDATION", "confidence": 1, "extracted_slots": {}},
        RuntimeError("model offline"),
    ):
        container.gateway.queue_response("intent", output)
        container.gateway.queue_response("planner", RuntimeError("model offline"))
        state = await container.chat_service.chat(user_id=1, message="给我西葫芦炒鸡蛋的做法")
        assert state["phase"] == "COMPLETED"
        assert state["intent"] == "RECIPE_QUERY"
        assert "等待 4 - 5 分钟" in state["final_response"]["message"]
        assert not state["final_response"]["recommendations"]


@pytest.mark.asyncio
async def test_all_catalog_recipe_steps_survive_mcp_contract_without_rewriting(container):
    service = NutritionMCPService.from_seed_data(project_root() / "data")
    adapter = MCPToolAdapter(InProcessMCPTransport(service))
    for recipe in container.food_catalog.recipes:
        result = await adapter.call_tool("get_recipe", {"name": recipe.name})
        assert result["instructions"] == recipe.instructions
        assert result["raw_ingredients"] == recipe.raw_ingredients
        assert result["raw_quantities"] == recipe.raw_quantities
        assert result["citation"]["source_sha256"] == recipe.source_sha256
        assert result["citation"]["license"]
    with pytest.raises(ToolRuntimeError, match="营养记录"):
        await adapter.call_tool("get_recipe", {"meal_id": 1})


def test_recipe_grounding_clears_model_recommendations_and_preserves_missing_quantity():
    response = ground_recommendation_facts(
        FinalResponse(
            message="编造做法",
            recommendations=[MealRecommendation(meal_id=1, name="错误菜名", reason="错误")],
        ),
        [{
            "tool_name": "get_recipe",
            "status": "success",
            "data": {
                "meal_id": 1001, "name": "测试菜", "raw_ingredients": "- 鸡蛋",
                "raw_quantities": "", "instructions": "1. 原始步骤", "allergens": ["鸡蛋"],
                "citation": {"uri": "https://example.test/recipe"},
            },
        }],
    )
    assert "来源未提供明确用量" in response.message
    assert "1. 原始步骤" in response.message
    assert not response.recommendations
    assert "编造" not in response.message
