from __future__ import annotations

import pytest

from nutripilot.application import build_container
from nutripilot.config import Settings
from nutripilot.evaluation import EvalRunner
from nutripilot.evaluation.models import EvalCase
from nutripilot.evaluation.runner import load_dataset
from nutripilot.paths import project_root
from nutripilot.retrieval import MealSearchFilters, MealSearchRequest, RetrievalService
from nutripilot.safety.dietary import match_ingredient
from nutripilot.tools.errors import ToolRuntimeError
from nutripilot.tools.mcp import MCPToolAdapter


@pytest.fixture
def container():
    return build_container(settings=Settings(_env_file=None))


@pytest.mark.asyncio
async def test_aliases_and_or_have_same_results_through_mcp(container):
    service = RetrievalService(container.data_store)
    first = await service.search_meals(MealSearchRequest(
        query="推荐菜", filters=MealSearchFilters(required_ingredients=["番茄"]), limit=20
    ))
    second = await service.search_meals(MealSearchRequest(
        query="推荐菜", filters=MealSearchFilters(required_ingredients=["西红柿"]), limit=20
    ))
    assert first.hits
    assert {hit.meal.id for hit in first.hits} == {hit.meal.id for hit in second.hits}
    from apps.nutrition_mcp.service import NutritionMCPService

    from nutripilot.tools.mcp import InProcessMCPTransport

    adapter = MCPToolAdapter(InProcessMCPTransport(
        NutritionMCPService.from_seed_data(project_root() / "data")
    ))
    result = await adapter.call_tool("search_meals", {
        "query": "鸡胸肉或豆腐", "ingredient_any_groups": [["鸡胸肉", "豆腐"]], "limit": 20
    })
    assert result["hits"]
    assert result["match_evidence"]
    assert result["filter_diagnostics"]["eligible_meals"] > 0
    for hit in result["hits"]:
        assert hit["ingredient_records"]
        assert any(
            match_ingredient(term, hit["ingredients"], hit["ingredient_records"])
            for term in ("鸡胸肉", "豆腐")
        )
    limited_adapter = MCPToolAdapter(
        InProcessMCPTransport(NutritionMCPService.from_seed_data(project_root() / "data")),
        max_payload_bytes=100,
    )
    with pytest.raises(ToolRuntimeError, match="too much data"):
        await limited_adapter.call_tool("search_meals", {"query": "菜", "limit": 20})


@pytest.mark.asyncio
async def test_recommendation_dataset_checks_actual_recipes_and_every_turn(container):
    version, cases = load_dataset(project_root() / "evals/datasets/ingredient-dialogue.json")
    assert len(cases) == 40
    report = await EvalRunner(
        container.chat_service, meal_repository=container.data_store
    ).run(cases, dataset_version=version)
    assert report.passed_cases == report.total_cases, report.failures
    assert report.runtime["mode"] == "fake"


@pytest.mark.asyncio
async def test_evaluation_does_not_pass_empty_or_wrong_intermediate_answers(container):
    case = EvalCase(
        id="must-check-first-turn",
        user_message="想吃鸡翅",
        follow_up_messages=["想吃鸡胸肉"],
        expected_intent="MEAL_RECOMMENDATION",
        expected_tools=[
            "get_user_profile", "get_user_feedback", "get_weather_context", "search_meals"
        ],
        min_recommendations=1,
        expected_required_ingredients=["鸡胸肉"],
        turn_expectations=[
            {"min_recommendations": 1, "expected_required_ingredients": ["豆腐"]},
            {"min_recommendations": 1, "expected_required_ingredients": ["鸡胸肉"]},
        ],
    )
    runner = EvalRunner(container.chat_service, meal_repository=container.data_store)
    report = await runner.run([case], dataset_version="test")
    assert not report.results[0].passed
    assert "turn_1_recipe_ingredients" in report.results[0].failure_reasons
    empty = await container.chat_service.chat(user_id=1, message="想吃龙肉")
    checks = await runner._recommendation_checks(empty, case)
    assert checks["minimum_recommendations"] is False


@pytest.mark.asyncio
async def test_or_follow_up_does_not_need_repeating_a_recommendation_verb(container):
    await container.chat_service.chat(user_id=1, thread_id="or-followup", message="想吃豆腐")
    state = await container.chat_service.chat(
        user_id=1, thread_id="or-followup", message="鸡胸肉或豆腐都可以"
    )
    assert state["slots"]["required_ingredients"] == []
    assert state["slots"]["ingredient_any_groups"] == [["鸡胸肉", "豆腐"]]
    assert state["final_response"]["recommendations"]


@pytest.mark.asyncio
async def test_multiple_any_groups_and_alias_exclusions_cover_derivatives(container):
    service = RetrievalService(container.data_store)
    result = await service.search_meals(MealSearchRequest(
        query="推荐菜",
        filters=MealSearchFilters(
            ingredient_any_groups=[["番茄", "鸡胸肉"], ["食盐", "豆腐"]],
            excluded_ingredients=["西红柿"],
        ),
        limit=20,
    ))
    assert result.hits
    for hit in result.hits:
        meal = hit.meal
        assert match_ingredient("鸡胸肉", meal.ingredients, meal.ingredient_records)
        assert any(
            match_ingredient(term, meal.ingredients, meal.ingredient_records)
            for term in ("食盐", "豆腐")
        )
        assert not match_ingredient(
            "西红柿", meal.ingredients, meal.ingredient_records, exclusion=True
        )


@pytest.mark.asyncio
async def test_structured_ingredient_search_uses_user_message_when_model_query_is_empty(container):
    from nutripilot.model_gateway import FakeModelGateway

    def blank_query(payload):
        result = FakeModelGateway._planner(payload)
        for call in result["tool_calls"]:
            if call["name"] == "search_meals":
                call["arguments"]["query"] = "  "
        return result

    container.gateway.register_handler("planner", blank_query)
    state = await container.chat_service.chat(user_id=1, message="想吃鸡胸肉或豆腐")
    assert state["final_response"]["recommendations"]
    assert not state.get("fallback_events")


@pytest.mark.asyncio
@pytest.mark.parametrize("term", ["虾", "猪肉", "鸡肉", "香菇", "花椰菜"])
async def test_common_food_categories_keep_their_specific_source_records(container, term):
    result = await RetrievalService(container.data_store).search_meals(MealSearchRequest(
        query="推荐菜", filters=MealSearchFilters(required_ingredients=[term]), limit=20
    ))
    assert result.hits
    for hit in result.hits:
        matches = match_ingredient(term, hit.meal.ingredients, hit.meal.ingredient_records)
        assert matches
        assert all(match.get("ingredient_id") for match in matches)


@pytest.mark.asyncio
@pytest.mark.parametrize("early_complete", [True, False])
async def test_planner_cannot_skip_current_profile_feedback_and_retrieval(
    container, early_complete
):
    container.gateway.register_handler("planner", lambda payload: {
        "complete": early_complete,
        "plan_summary": "故意漏读前置资料",
        "tool_calls": [] if early_complete else [{
            "call_id": f"{payload['turn_id']}:search",
            "name": "search_meals", "arguments": {"query": payload["user_message"]},
        }],
    })
    first = await container.chat_service.chat(
        user_id=1, thread_id=f"preconditions-{early_complete}", message="想吃鸡胸肉或豆腐"
    )
    state = await container.chat_service.chat(
        user_id=1, thread_id=first["thread_id"], message="取消食材限制"
    )
    assert state["phase"] == "COMPLETED"
    tools = [r["tool_name"] for r in state["tool_results"] if r["status"] == "success"]
    assert tools.index("get_user_profile") < tools.index("search_meals")
    assert tools.index("get_user_feedback") < tools.index("search_meals")
    assert state["final_response"]["recommendations"]


@pytest.mark.asyncio
async def test_unrecognised_named_food_is_not_replaced_with_other_dishes(container):
    from nutripilot.model_gateway import FakeModelGateway

    def intent(payload):
        result = FakeModelGateway._intent(payload)
        result["extracted_slots"] = {"meal_type": "lunch", "food_name": "龙肉"}
        return result

    container.gateway.register_handler("intent", intent)
    state = await container.chat_service.chat(user_id=1, message="午餐想吃龙肉")
    assert state["phase"] == "COMPLETED"
    assert not state["final_response"]["recommendations"]
    assert "没有找到" in state["final_response"]["message"]


@pytest.mark.asyncio
async def test_explicit_weather_request_is_preserved_when_planner_completes_early(container):
    container.gateway.register_handler("planner", lambda payload: {
        "complete": True, "tool_calls": [], "plan_summary": "故意提前结束",
    })
    state = await container.chat_service.chat(
        user_id=1, message="根据北京今天的天气推荐午餐"
    )
    assert state["phase"] == "COMPLETED"
    tools = [r["tool_name"] for r in state["tool_results"] if r["status"] == "success"]
    assert tools.index("get_weather_context") < tools.index("search_meals")


@pytest.mark.asyncio
async def test_first_positive_food_request_is_not_an_adjustment_without_prior_context(container):
    from nutripilot.model_gateway import FakeModelGateway

    def misclassified(payload):
        result = FakeModelGateway._intent(payload)
        result["intent"] = "MEAL_ADJUSTMENT"
        return result

    container.gateway.register_handler("intent", misclassified)
    state = await container.chat_service.chat(user_id=1, message="不吃鸡翅，想吃鸡胸肉")
    assert state["intent"] == "MEAL_RECOMMENDATION"
    assert state["final_response"]["recommendations"]
