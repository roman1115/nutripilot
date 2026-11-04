from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from nutripilot.infrastructure import load_seed_store
from nutripilot.retrieval import (
    MealSearchFilters,
    MealSearchRequest,
    NutritionFacts,
    RetrievalService,
)
from nutripilot.retrieval.models import Meal, ScoredId

DATA_DIR = Path(__file__).resolve().parents[2] / "data"


@pytest.mark.asyncio
async def test_named_filter_and_alias_are_applied_before_candidate_limit() -> None:
    from apps.nutrition_mcp.service import NutritionMCPService

    from nutripilot.infrastructure.memory import InMemoryDataStore
    from nutripilot.tools.mcp.models import SearchMealsMCPArgs

    meals = await load_seed_store(DATA_DIR).list_meals()
    target = meals[-1].model_copy(update={"aliases": ["测试可靠别名"]})
    store = InMemoryDataStore(meals=[*meals[:-1], target])
    service = NutritionMCPService(store, RetrievalService(store))
    for name in (target.name, "测试可靠别名"):
        result = await service.search_meals(SearchMealsMCPArgs(
            query="随便推荐菜", food_name=name, limit=1,
        ))
        assert [hit.meal_id for hit in result.hits] == [target.id]
        assert result.hits[0].aliases == ["测试可靠别名"]
    result = await service.search_meals(SearchMealsMCPArgs(
        query="随便推荐菜", food_name="未收录的龙肉",
    ))
    assert result.hits == []
    assert result.filter_diagnostics["food_name"] == len(meals)


@pytest.mark.asyncio
async def test_seed_nutrition_is_explicitly_unverified_and_not_missing_provenance() -> None:
    meals = [
        meal for meal in await load_seed_store(DATA_DIR).all_meals() if meal.recipe_id is None
    ]

    assert len(meals) == 18
    assert all(not meal.is_active for meal in meals)
    assert all(meal.nutrition.source_status == "unverified_estimate" for meal in meals)
    assert all(meal.nutrition.serving_weight_g is None for meal in meals)
    assert all(meal.nutrition.source_uri is None for meal in meals)
    assert all("未独立核验" in meal.source for meal in meals)


def test_verified_nutrition_requires_source_and_serving_weight() -> None:
    with pytest.raises(ValidationError, match="source URI and serving weight"):
        NutritionFacts(
            calories_kcal=300,
            protein_g=20,
            carbohydrates_g=30,
            fat_g=10,
            source_status="verified",
        )


@pytest.mark.asyncio
async def test_hard_allergen_filter_cannot_be_bypassed_by_semantic_rank() -> None:
    store = load_seed_store(DATA_DIR)
    service = RetrievalService(store)

    response = await service.search_meals(
        MealSearchRequest(
            query="我很想吃花生酱香蕉燕麦",
            filters=MealSearchFilters(excluded_allergens=["花生"]),
            limit=10,
        )
    )

    assert response.hits
    assert all("花生" not in hit.meal.allergens for hit in response.hits)
    assert all(hit.meal.id != 13 for hit in response.hits)


@pytest.mark.asyncio
async def test_retrieval_returns_authoritative_ids_and_citations() -> None:
    store = load_seed_store(DATA_DIR)
    service = RetrievalService(store)

    response = await service.search_meals(
        MealSearchRequest(
            query="清淡高蛋白菜品",
            filters=MealSearchFilters(meal_type="lunch", min_protein_g=20),
            limit=3,
        )
    )

    assert response.hits
    for hit in response.hits:
        assert hit.citation["source_id"] == f"meal:{hit.meal.id}"
        assert hit.meal.recipe_id is not None
        assert hit.meal.nutrition.bounds("protein_g").minimum >= 20


@pytest.mark.asyncio
async def test_user_declared_allergen_also_filters_matching_ingredients() -> None:
    store = load_seed_store(DATA_DIR)
    service = RetrievalService(store)

    response = await service.search_meals(
        MealSearchRequest(
            query="午餐吃什么",
            filters=MealSearchFilters(excluded_allergens=["香菇"]),
            limit=20,
        )
    )

    assert response.hits
    assert all(
        "香菇" not in ingredient
        for hit in response.hits
        for ingredient in hit.meal.ingredients
    )
    assert all(hit.meal.id != 5 for hit in response.hits)


@pytest.mark.asyncio
async def test_disliked_food_filters_matching_ingredients() -> None:
    store = load_seed_store(DATA_DIR)
    service = RetrievalService(store)

    response = await service.search_meals(
        MealSearchRequest(
            query="午餐吃什么",
            filters=MealSearchFilters(excluded_ingredients=["香菇"]),
            limit=20,
        )
    )

    assert response.hits
    assert all(
        "香菇" not in ingredient
        for hit in response.hits
        for ingredient in hit.meal.ingredients
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("required", "excluded"),
    [
        (["鸡胸肉"], []),
        (["番茄", "鸡蛋"], []),
        (["鸡胸肉", "不存在的食材"], []),
        (["鸡胸肉"], ["鸡胸肉"]),
    ],
)
async def test_required_ingredients_are_all_hard_filters(required, excluded) -> None:
    service = RetrievalService(load_seed_store(DATA_DIR))
    result = await service.search_meals(
        MealSearchRequest(
            query="烤鸡翅",
            filters=MealSearchFilters(
                required_ingredients=required, excluded_allergens=excluded
            ),
            limit=20,
        )
    )
    if excluded or "不存在的食材" in required:
        assert not result.hits
        assert not result.relaxed_filters
    else:
        assert result.hits
        assert all(
            any(term in ingredient for ingredient in hit.meal.ingredients)
            for term in required for hit in result.hits
        )
        if required == ["鸡胸肉"]:
            assert 1080 not in [hit.meal.id for hit in result.hits]


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate_limit", [5, 30])
async def test_exact_name_survives_semantic_truncation_and_hostile_reranking(
    candidate_limit: int,
) -> None:
    class WrongIndex:
        version = "test"

        async def search(
            self, vector: list[float], *, limit: int, eligible_ids: set[int] | None = None
        ) -> list[ScoredId]:
            return [ScoredId(id=1, score=100, source="semantic")]

    class WrongReranker:
        version = "test"

        async def score(self, query: str, meals: list[Meal]) -> list[float]:
            return [0 if meal.id == 1024 else 100 for meal in meals]

    store = load_seed_store(DATA_DIR)
    service = RetrievalService(store, vector_index=WrongIndex(), reranker=WrongReranker())
    result = await service.search_meals(
        MealSearchRequest(query=" 米粥 ", candidate_limit=candidate_limit, limit=1)
    )
    assert result.hits[0].meal.id == 1024
