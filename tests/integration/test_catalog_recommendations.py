from __future__ import annotations

from typing import Any

import pytest

from nutripilot.application import build_container
from nutripilot.config import Settings
from nutripilot.evaluation import EvalRunner
from nutripilot.evaluation.runner import load_dataset
from nutripilot.model_gateway import FakeModelGateway
from nutripilot.paths import project_root
from nutripilot.retrieval import MealSearchFilters, MealSearchRequest, RetrievalService


@pytest.fixture
def container():
    return build_container(settings=Settings(_env_file=None))


@pytest.mark.asyncio
async def test_all_catalog_names_rank_first_and_keep_whole_recipe_facts(container) -> None:
    service = RetrievalService(container.data_store)
    for recipe in container.food_catalog.recipes:
        result = await service.search_meals(MealSearchRequest(query=recipe.name, limit=1))
        assert result.hits[0].meal.id == recipe.id, recipe.name
        nutrition = result.hits[0].meal.nutrition
        assert recipe.calculation_basis == "whole_recipe"
        assert "整" in nutrition.serving_size
        assert nutrition.weight_basis == "ingredient_input"
        assert nutrition.source_status == "calculated_estimate"
        assert nutrition.source_uri == str(recipe.source_uri)


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["推荐一道清淡的菜", "推荐一道西葫芦炒鸡蛋"])
async def test_explicit_dish_request_does_not_require_a_meal_type(
    container, message: str
) -> None:
    state = await container.chat_service.chat(user_id=910029, message=message)
    assert state["phase"] == "COMPLETED"
    assert not state["missing_slots"]
    assert state["final_response"]["recommendations"]
    if "西葫芦" in message:
        assert any(r["meal_id"] == 1008 for r in state["final_response"]["recommendations"])
    assert not state["requires_confirmation"]


@pytest.mark.asyncio
async def test_catalog_recommendation_retains_range_sources_and_whole_recipe(container) -> None:
    state = await container.chat_service.chat(user_id=910029, message="午餐推荐红烧鲤鱼")
    response = state["final_response"]
    recommendation = next(r for r in response["recommendations"] if r["meal_id"] == 1082)
    meal = await container.data_store.get_meal(1082)
    assert recommendation["nutrient_ranges"] == {
        field: bounds.model_dump() for field, bounds in meal.nutrition.nutrient_ranges.items()
    }
    assert len(recommendation["reason"]) < 100
    assert "投料" not in recommendation["reason"]
    assert any("不代表个人摄入量或完整一餐" in w for w in response["warnings"])
    assert any(c["source_type"] == "ingredient" for c in response["citations"])
    assert any("本配方采用版本" in (c.get("excerpt") or "") for c in response["citations"])
    assert not any("每人" in r["reason"] for r in response["recommendations"])


@pytest.mark.asyncio
async def test_fuzzy_ingredient_request_can_recommend_catalog_recipes(container) -> None:
    state = await container.chat_service.chat(
        user_id=910029, message="午餐想吃番茄和鸡蛋做的菜"
    )
    assert state["phase"] == "COMPLETED"
    assert any(
        item["meal_id"] == 1001 for item in state["final_response"]["recommendations"]
    )


@pytest.mark.asyncio
async def test_adjustment_excludes_previous_candidates_even_when_planner_omits_them(
    container,
) -> None:
    first = await container.chat_service.chat(
        user_id=910029, message="午餐推荐西葫芦炒鸡蛋", thread_id="catalog-adjustment"
    )

    def omit_exclusions(payload: dict[str, Any]) -> dict[str, Any]:
        output = FakeModelGateway._planner(payload)
        for call in output["tool_calls"]:
            call["arguments"].pop("excluded_meal_ids", None)
            if call["name"] == "search_meals":
                call["arguments"]["query"] = "午餐推荐西葫芦炒鸡蛋"
        return output

    container.gateway.register_handler("planner", omit_exclusions)
    second = await container.chat_service.chat(
        user_id=910029, message="换一批", thread_id="catalog-adjustment"
    )
    assert second["phase"] == "COMPLETED"
    assert second["candidate_ids"]
    assert set(first["candidate_ids"]).isdisjoint(second["candidate_ids"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "field", "value", "label"),
    [
        ("午餐推荐一道菜，预算不超过20元", "max_price_cny", 20, "价格"),
        ("午餐推荐一道菜，10分钟内做好", "max_prep_minutes", 10, "准备时间"),
        ("午餐推荐一道菜，20分钟内做好", "max_prep_minutes", 20, "准备时间"),
    ],
)
async def test_unknown_price_or_time_cannot_claim_to_match(
    container, message: str, field: str, value: float, label: str
) -> None:
    state = await container.chat_service.chat(user_id=910029, message=message)
    assert state["phase"] == "COMPLETED"
    assert state["slots"][field] == value
    hits = next(
        r["data"]["hits"] for r in state["tool_results"] if r["tool_name"] == "search_meals"
    )
    assert not hits
    assert not state["final_response"]["recommendations"]
    assert "没有找到" in state["final_response"]["message"]
    assert any(
        f"{label}未知的菜谱未纳入候选" in w for w in state["final_response"]["warnings"]
    )


@pytest.mark.asyncio
async def test_calorie_limit_uses_whole_recipe_upper_bound(container) -> None:
    state = await container.chat_service.chat(
        user_id=910029, message="午餐推荐一道菜，不超过400千卡"
    )
    recommendations = state["final_response"]["recommendations"]
    assert recommendations
    for item in recommendations:
        meal = await container.data_store.get_meal(item["meal_id"])
        assert meal.nutrition.bounds("calories_kcal").maximum <= 400
        if meal.recipe_id is not None:
            assert any("整道菜" in w for w in state["final_response"]["warnings"])


@pytest.mark.asyncio
async def test_allergen_filter_still_precedes_exact_name_ranking(container) -> None:
    service = RetrievalService(container.data_store)
    result = await service.search_meals(
        MealSearchRequest(
            query="西葫芦炒鸡蛋",
            filters=MealSearchFilters(excluded_allergens=["鸡蛋"]),
        )
    )
    assert all(hit.meal.id != 1008 for hit in result.hits)
    assert all("鸡蛋" not in hit.meal.allergens for hit in result.hits)


@pytest.mark.asyncio
async def test_impossible_calorie_limit_does_not_invent_a_smaller_serving(container) -> None:
    state = await container.chat_service.chat(
        user_id=910029, message="午餐推荐一道菜，不超过1千卡"
    )
    assert not state["final_response"]["recommendations"]
    assert "没有找到" in state["final_response"]["message"]
    assert not state["requires_confirmation"]


@pytest.mark.asyncio
async def test_catalog_recommendation_dataset_runs_offline(container) -> None:
    version, cases = load_dataset(
        project_root() / "evals/datasets/catalog-recommendations.json"
    )
    report = await EvalRunner(container.chat_service).run(cases, dataset_version=version)
    assert report.passed_cases == report.total_cases, report.failures
    assert report.runtime["mode"] == "fake"


@pytest.mark.asyncio
async def test_chicken_request_survives_recipe_context_and_planner_omission(container) -> None:
    thread = "recipe-to-chicken"
    await container.chat_service.chat(
        user_id=910029, message="西红柿炒鸡蛋怎么做", thread_id=thread
    )

    def omit_required(payload: dict[str, Any]) -> dict[str, Any]:
        output = FakeModelGateway._planner(payload)
        for call in output["tool_calls"]:
            call["arguments"].pop("required_ingredients", None)
            if call["name"] == "search_meals":
                call["arguments"]["query"] = "烤鸡翅"
        return output

    container.gateway.register_handler("planner", omit_required)
    state = await container.chat_service.chat(
        user_id=910029, message="想吃鸡胸肉", thread_id=thread
    )
    assert state["intent"] == "MEAL_RECOMMENDATION"
    assert not state["missing_slots"]
    assert not state["slots"].get("food_name")
    assert state["slots"]["required_ingredients"] == ["鸡胸肉"]
    cards = state["final_response"]["recommendations"]
    assert cards
    for item in cards:
        meal = await container.data_store.get_meal(item["meal_id"])
        assert any("鸡胸肉" in ingredient for ingredient in meal.ingredients)
        assert "鸡胸肉" in item["reason"]
        assert "中式" not in item["reason"]
    assert 1080 not in [item["meal_id"] for item in cards]


@pytest.mark.asyncio
async def test_ingredient_adjustment_inherits_and_new_request_replaces(container) -> None:
    async def send(message):
        return await container.chat_service.chat(
            user_id=910029, message=message, thread_id="ingredient-lifecycle"
        )

    first = await send("想吃鸡胸肉")
    second = await send("换一批")
    assert second["slots"]["required_ingredients"] == ["鸡胸肉"]
    assert set(first["candidate_ids"]).isdisjoint(second["candidate_ids"])
    third = await send("想吃鸡翅")
    assert third["slots"]["required_ingredients"] == ["鸡翅"]
    assert third["final_response"]["recommendations"]
    fourth = await send("午餐推荐一道菜")
    assert not fourth["slots"]["required_ingredients"]
