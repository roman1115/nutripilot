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

DATA_DIR = Path(__file__).resolve().parents[2] / "data"


@pytest.mark.asyncio
async def test_seed_nutrition_is_explicitly_unverified_and_not_missing_provenance() -> None:
    meals = [
        meal for meal in await load_seed_store(DATA_DIR).list_meals() if meal.recipe_id is None
    ]

    assert len(meals) == 18
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
            query="20分钟内的清淡高蛋白午餐",
            filters=MealSearchFilters(meal_type="lunch", max_prep_minutes=20, min_protein_g=20),
            limit=3,
        )
    )

    assert response.hits
    for hit in response.hits:
        assert hit.citation["source_id"] == f"meal:{hit.meal.id}"
        assert hit.meal.prep_minutes is not None and hit.meal.prep_minutes <= 20
        assert hit.meal.nutrition.protein_g >= 20


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
