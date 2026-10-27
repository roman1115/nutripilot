from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest
from pydantic import ValidationError
from scripts.import_food_catalog import import_ingredients

from nutripilot.infrastructure.memory import load_seed_store
from nutripilot.retrieval.catalog import (
    FoodCatalog,
    Ingredient,
    NutrientsPer100g,
    Recipe,
    RecipeIngredient,
    calculate_recipe,
    load_food_catalog,
    recipe_to_meal,
)

DATA_DIR = Path(__file__).resolve().parents[2] / "data"


@pytest.fixture
def ingredient() -> Ingredient:
    return Ingredient(
        id="test:egg",
        name="去壳鸡蛋",
        source_id="test-source",
        source_record_id="1",
        source_description="Test per-100g record",
        source_uri="https://example.test/foods/1",
        nutrients_per_100g=NutrientsPer100g(
            calories_kcal=200,
            protein_g=20,
            carbohydrates_g=10,
            fat_g=10,
            fiber_g=2,
            sodium_mg=400,
        ),
        allergens=["鸡蛋"],
        mapping_note="测试用已知可食生重映射",
    )


@pytest.fixture
def recipe(ingredient: Ingredient) -> Recipe:
    return Recipe(
        id=9001,
        name="测试配方",
        source_id="test-source",
        source_uri="https://example.test/recipes/1",
        source_path="dishes/test.md",
        source_sha256="a" * 64,
        raw_ingredients="去壳鸡蛋",
        raw_quantities="去壳鸡蛋150克；蒸锅用水1000毫升",
        instructions="按投料蒸熟。",
        ingredients=[
            RecipeIngredient(quantity_text="去壳鸡蛋150克", ingredient_id=ingredient.id, grams=150),
            RecipeIngredient(
                quantity_text="蒸锅用水1000毫升", consumed=False, note="仅用于蒸锅，不食用。"
            ),
        ],
        status="calculation_ready",
        servings=2,
        serving_label="半份测试配方",
        meal_types=["breakfast"],
    )


def test_calculation_uses_edible_grams_and_servings_for_all_nutrients(
    recipe: Recipe,
    ingredient: Ingredient,
) -> None:
    nutrition = calculate_recipe(recipe, {ingredient.id: ingredient})
    assert nutrition.calories_kcal == 150
    assert nutrition.protein_g == 15
    assert nutrition.carbohydrates_g == 7.5
    assert nutrition.fat_g == 7.5
    assert nutrition.fiber_g == 1.5
    assert nutrition.sodium_mg == 300
    assert nutrition.serving_weight_g == 75
    assert nutrition.weight_basis == "ingredient_input"
    assert nutrition.source_status == "calculated_estimate"
    assert nutrition.ingredient_source_uris == ["https://example.test/foods/1"]
    meal = recipe_to_meal(recipe, {ingredient.id: ingredient})
    assert meal.allergens == ["鸡蛋"]
    assert meal.prep_minutes is None and meal.price_cny is None


@pytest.mark.parametrize("grams", [None, 0, -1, float("nan"), float("inf"), True])
def test_unknown_or_invalid_weights_cannot_produce_calculated_nutrition(
    grams: float | None,
    recipe: Recipe,
    ingredient: Ingredient,
) -> None:
    raw = recipe.model_dump(mode="json")
    raw["ingredients"][0]["grams"] = grams
    with pytest.raises((ValueError, ValidationError)):
        calculate_recipe(Recipe.model_validate(raw), {ingredient.id: ingredient})


def test_missing_nutrient_and_unchecked_allergens_fail_closed(
    recipe: Recipe,
    ingredient: Ingredient,
) -> None:
    ingredient.nutrients_per_100g.sodium_mg = None
    with pytest.raises(ValueError, match="missing sodium_mg"):
        calculate_recipe(recipe, {ingredient.id: ingredient})
    ingredient.nutrients_per_100g.sodium_mg = 0
    ingredient.allergens = None
    with pytest.raises(ValueError, match="allergen"):
        calculate_recipe(recipe, {ingredient.id: ingredient})


def test_pending_recipe_or_unresolved_issue_is_not_calculated(
    recipe: Recipe,
    ingredient: Ingredient,
) -> None:
    recipe.review_issues = ["待核对份量"]
    with pytest.raises(ValueError, match="review"):
        calculate_recipe(recipe, {ingredient.id: ingredient})
    recipe.review_issues = []
    recipe.status = "pending"
    with pytest.raises(ValueError, match="review"):
        calculate_recipe(recipe, {ingredient.id: ingredient})


def test_non_consumed_ingredient_requires_reason() -> None:
    with pytest.raises(ValidationError, match="exclusion reason"):
        RecipeIngredient(quantity_text="油", consumed=False)


def test_catalog_rejects_unknown_references_and_duplicate_ids(
    recipe: Recipe,
    ingredient: Ingredient,
) -> None:
    with pytest.raises(ValidationError, match="Unknown source"):
        FoodCatalog(sources=[], ingredients=[ingredient], recipes=[])
    with pytest.raises(ValidationError, match="Duplicate ingredient"):
        FoodCatalog(sources=[], ingredients=[ingredient, ingredient], recipes=[])
    catalog = load_food_catalog(DATA_DIR)
    raw = catalog.model_dump(mode="json")
    raw["recipes"][0]["ingredients"][0]["ingredient_id"] = "missing:ingredient"
    with pytest.raises(ValidationError, match="Unknown ingredient"):
        FoodCatalog.model_validate(raw)


def test_checked_in_catalog_counts_missing_values_and_pending_gates() -> None:
    catalog = load_food_catalog(DATA_DIR)
    assert len(catalog.ingredients) == 7793
    assert len(catalog.recipes) == 50
    assert sum(item.status == "pending" for item in catalog.recipes) == 47
    assert len(catalog.calculated_meals()) == 3
    assert any(item.nutrients_per_100g.fiber_g is None for item in catalog.ingredients)
    assert all(item.source_sha256 for item in catalog.recipes)
    tomato = next(item for item in catalog.ingredients if item.id == "fdc:170457")
    assert tomato.nutrients_per_100g.calories_kcal == 18
    assert tomato.source_description == "Tomatoes, red, ripe, raw, year round average"
    prepared = {item.name: item for item in catalog.calculated_meals()}
    assert prepared["糖拌西红柿"].nutrition.calories_kcal == pytest.approx(56.7)
    assert prepared["糖拌西红柿"].nutrition.serving_weight_g == 110
    assert prepared["小米粥"].nutrition.calories_kcal == 378
    assert "整锅" in prepared["小米粥"].nutrition.serving_size


@pytest.mark.asyncio
async def test_only_calculation_ready_recipes_join_legacy_meals() -> None:
    store = load_seed_store(DATA_DIR)
    meals = await store.list_meals()
    assert {meal.id for meal in meals if meal.recipe_id is None} == set(range(1, 19))
    assert {meal.id for meal in meals if meal.recipe_id is not None} == {1003, 1004, 1023}
    assert all(
        item.id not in {meal.id for meal in meals}
        for item in store.catalog.recipes
        if item.status == "pending"
    )


def test_partial_catalog_files_fail_startup(tmp_path: Path) -> None:
    for name in ("meals.json", "dietary_guidelines.json"):
        (tmp_path / name).write_text("[]", encoding="utf-8")
    (tmp_path / "ingredients.json").write_text("[]", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        load_seed_store(tmp_path)


def test_usda_import_preserves_unknowns_and_checks_units_and_checksum(tmp_path: Path) -> None:
    archive_path = tmp_path / "source.zip"

    def write_archive(unit: str) -> str:
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr(
                "source/food.csv",
                "fdc_id,data_type,description\n1,sr_legacy_food,Test food\n",
            )
            archive.writestr(
                "source/nutrient.csv",
                "id,unit_name\n1008,KCAL\n1003,G\n1004,G\n1005,G\n1079,G\n1093," + unit + "\n",
            )
            archive.writestr(
                "source/food_nutrient.csv",
                "fdc_id,nutrient_id,amount\n1,1008,25\n",
            )
        return hashlib.sha256(archive_path.read_bytes()).hexdigest()

    checksum = write_archive("MG")
    item = import_ingredients(archive_path, checksum, {})[0]
    assert item["nutrients_per_100g"]["calories_kcal"] == 25
    assert item["nutrients_per_100g"]["sodium_mg"] is None
    with pytest.raises(ValueError, match="checksum"):
        import_ingredients(archive_path, "0" * 64, {})
    checksum = write_archive("G")
    with pytest.raises(ValueError, match="unit"):
        import_ingredients(archive_path, checksum, {})
