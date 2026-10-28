from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest
from pydantic import ValidationError
from scripts.import_food_catalog import import_ingredients, import_supplement

from nutripilot.domain.models import NumericRange
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
from nutripilot.retrieval.catalog_mapping import CatalogMappings, apply_recipe_mappings

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
        calculation_basis="per_serving",
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
    assert len(catalog.ingredients) == 7802
    assert len(catalog.recipes) == 50
    assert sum(item.status == "pending" for item in catalog.recipes) == 0
    assert len(catalog.calculated_meals()) == 50
    assert any(item.nutrients_per_100g.fiber_g is None for item in catalog.ingredients)
    assert all(item.source_sha256 for item in catalog.recipes)
    tomato = next(item for item in catalog.ingredients if item.id == "fdc:170457")
    assert tomato.nutrients_per_100g.calories_kcal == 18
    assert tomato.source_description == "Tomatoes, red, ripe, raw, year round average"
    prepared = {item.name: item for item in catalog.calculated_meals()}
    assert prepared["糖拌西红柿"].nutrition.calories_kcal == pytest.approx(113.4)
    assert prepared["糖拌西红柿"].nutrition.serving_weight_g == 220
    assert prepared["小米粥"].nutrition.calories_kcal == 378
    assert "整份配方" in prepared["小米粥"].nutrition.serving_size


@pytest.mark.asyncio
async def test_only_calculation_ready_recipes_join_legacy_meals() -> None:
    store = load_seed_store(DATA_DIR)
    meals = await store.list_meals()
    assert {meal.id for meal in meals if meal.recipe_id is None} == set(range(1, 19))
    assert len([meal for meal in meals if meal.recipe_id is not None]) == 50
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


def test_whole_recipe_preserves_ranges_without_division_or_midpoint(
    recipe: Recipe,
    ingredient: Ingredient,
) -> None:
    recipe.calculation_basis = "whole_recipe"
    recipe.serving_label = "整道菜"
    recipe.ingredients[0].grams = None
    recipe.ingredients[0].grams_range = NumericRange(minimum=100, maximum=200)
    nutrition = calculate_recipe(recipe, {ingredient.id: ingredient})
    assert nutrition.bounds("calories_kcal") == NumericRange(minimum=200, maximum=400)
    assert nutrition.bounds("protein_g") == NumericRange(minimum=20, maximum=40)
    assert nutrition.bounds("sodium_mg") == NumericRange(minimum=400, maximum=800)
    assert nutrition.calories_kcal == 400
    assert nutrition.input_weight_range_g == NumericRange(minimum=100, maximum=200)
    recipe.servings = None
    assert calculate_recipe(recipe, {ingredient.id: ingredient}) == nutrition


@pytest.mark.parametrize(
    "low,high",
    [(2, 1), (-1, 2), (float("nan"), 2), (1, float("inf")), (True, 2)],
)
def test_ranges_reject_invalid_bounds(low: float, high: float) -> None:
    with pytest.raises(ValidationError):
        NumericRange(minimum=low, maximum=high)


def test_estimates_need_evidence_and_cannot_mix_weight_representations() -> None:
    with pytest.raises(ValidationError, match="evidence"):
        RecipeIngredient(quantity_text="油适量", grams=5, quantity_basis="project_default")
    with pytest.raises(ValidationError, match="not both"):
        RecipeIngredient(
            quantity_text="盐1～2克",
            grams=1,
            grams_range=NumericRange(minimum=1, maximum=2),
        )


def test_frying_counts_absorbed_oil_range_not_fryer_oil(recipe: Recipe) -> None:
    catalog = load_food_catalog(DATA_DIR)
    oil = next(item for item in catalog.ingredients if item.id == "fdc:172336")
    recipe.calculation_basis = "whole_recipe"
    recipe.ingredients = [
        RecipeIngredient(
            quantity_text="配方吸油估算5～15克",
            ingredient_id=oil.id,
            grams_range=NumericRange(minimum=5, maximum=15),
            quantity_basis="oil_absorption",
            evidence="测试用项目假设，非实测。",
        ),
        RecipeIngredient(quantity_text="炸锅油1000克", consumed=False, note="加工油不全部计入。"),
    ]
    result = calculate_recipe(recipe, {oil.id: oil})
    assert result.bounds("fat_g") == NumericRange(minimum=5, maximum=15)
    assert result.bounds("calories_kcal") == NumericRange(minimum=44.2, maximum=132.6)
    assert "项目假设" in (result.calculation_note or "")


def test_excluding_marinade_nutrition_preserves_contact_allergens(
    recipe: Recipe,
    ingredient: Ingredient,
) -> None:
    recipe.ingredients[0].consumed = False
    recipe.ingredients[0].note = "测试腌制液，不计营养但保留鸡蛋接触。"
    catalog = load_food_catalog(DATA_DIR)
    tomato = next(item for item in catalog.ingredients if item.id == "fdc:170457")
    recipe.ingredients.append(
        RecipeIngredient(quantity_text="番茄100克", ingredient_id=tomato.id, grams=100)
    )
    recipe.calculation_basis = "whole_recipe"
    meal = recipe_to_meal(recipe, {ingredient.id: ingredient, tomato.id: tomato})
    assert meal.nutrition.calories_kcal == 18
    assert "鸡蛋" in meal.allergens


def test_mappings_are_repeatable_cover_every_recipe_and_pin_source_hashes() -> None:
    mappings = CatalogMappings.model_validate_json(
        (DATA_DIR / "recipe_mappings.json").read_text(encoding="utf-8")
    )
    raw = json.loads((DATA_DIR / "recipes.json").read_text(encoding="utf-8"))
    first = apply_recipe_mappings(raw, mappings)
    assert first == apply_recipe_mappings(raw, mappings)
    assert all(item.review_issues for item in first if item.status == "pending")
    mappings.recipes["1007"].issues = ["测试新增来源问题，必须重新退出推荐。"]
    reviewed = apply_recipe_mappings(raw, mappings)
    assert next(item for item in reviewed if item.id == 1007).status == "pending"
    raw[0]["source_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="changed"):
        apply_recipe_mappings(raw, mappings)
    with pytest.raises(ValueError, match="exactly one"):
        apply_recipe_mappings(raw[1:], mappings)


def test_batch_keeps_source_ranges_step_only_ingredients_and_optional_choices() -> None:
    catalog = load_food_catalog(DATA_DIR)
    by_id = {recipe.id: recipe for recipe in catalog.recipes}
    parts = by_id[1001].ingredients
    assert next(p for p in parts if p.ingredient_id == "fdc:173468").grams_range == (
        NumericRange(minimum=1.5, maximum=2)
    )
    assert any(p.ingredient_id == "fdc:172336" for p in by_id[1002].ingredients)
    assert any(p.ingredient_id == "fdc:169655" and p.grams == 5 for p in by_id[1005].ingredients)
    assert not any(p.ingredient_id == "fdc:169655" for p in by_id[1006].ingredients)


def test_supplement_preserves_source_qualifiers_and_rejects_changes(tmp_path: Path) -> None:
    selection = json.loads((DATA_DIR / "catalog_selection.json").read_text(encoding="utf-8"))
    source = DATA_DIR / "supplemental_ingredients.json"
    supplement = import_supplement(source, selection["supplemental_sha256"])
    ingredients = {item.id: item for item in supplement.ingredients}
    assert len(ingredients) == 9
    assert ingredients["mext:06207"].source_description == "（にら類） にら 葉 生"
    assert ingredients["mext:09003"].nutrients_per_100g.calories_kcal == 276
    assert ingredients["mext:02034"].source_nutrient_values["fiber_g"] == "(0)"
    assert ingredients["mext:02034"].nutrients_per_100g.fiber_g == 0
    msg = ingredients["cj:msg-2018"]
    assert msg.nutrients_per_100g.protein_g == 0
    assert msg.nutrients_per_100g.calories_kcal == 288
    assert msg.nutrients_per_100g.sodium_mg == 12300
    assert "78.6" in msg.source_nutrient_values["protein_g"]
    changed = tmp_path / source.name
    changed.write_bytes(source.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="checksum"):
        import_supplement(changed, selection["supplemental_sha256"])


def test_new_batch_counts_sauce_portion_and_separates_extra_oil() -> None:
    catalog = load_food_catalog(DATA_DIR)
    recipes = {item.id: item for item in catalog.recipes}
    scallion_sauce = recipes[1033]
    sauce_oil = next(p for p in scallion_sauce.ingredients if p.ingredient_id == "fdc:172336")
    assert sauce_oil.grams_range is not None
    assert sauce_oil.grams_range.minimum == pytest.approx(92 * 15.75 / 199, abs=0.000001)
    assert sauce_oil.grams_range.maximum == 9.2
    assert "180～199克" in (scallion_sauce.preparation_note or "")
    garlic_sauce = [
        p for p in recipes[1041].ingredients if "50克蒜蓉酱子配方" in p.quantity_text
    ]
    assert sum(p.grams or 0 for p in garlic_sauce) == 50
    assert sum(
        p.grams or 0 for p in recipes[1041].ingredients if p.ingredient_id == "fdc:172336"
    ) == pytest.approx(28.4)
    spice_parts = [
        p for p in recipes[1032].ingredients if "3克综合香料子配方" in p.quantity_text
    ]
    assert sum(p.grams or 0 for p in spice_parts) == pytest.approx(3)


def test_new_batch_edible_weights_optional_choices_and_marinade_contacts() -> None:
    catalog = load_food_catalog(DATA_DIR)
    recipes = {item.id: item for item in catalog.recipes}
    ingredients = {item.id: item for item in catalog.ingredients}
    shrimp = calculate_recipe(recipes[1040], ingredients)
    assert shrimp.serving_weight_g == 125
    assert shrimp.calories_kcal == pytest.approx(97.5)
    assert recipe_to_meal(recipes[1040], ingredients).allergens == ["亚硫酸盐", "甲壳类", "虾"]
    fish = next(p for p in recipes[1042].ingredients if p.ingredient_id == "fdc:175142")
    assert fish.grams == 300
    tofu_ids = {p.ingredient_id for p in recipes[1043].ingredients if p.consumed}
    assert {"mext:12020", "mext:04035"}.issubset(tofu_ids)
    assert "fdc:171016" not in tofu_ids
    chicken = recipe_to_meal(recipes[1039], ingredients)
    assert {"大豆", "麸质"}.issubset(chicken.allergens)
    assert all(
        not p.consumed
        for p in recipes[1039].ingredients
        if p.ingredient_id == "fdc:169070"
    )
    assert "本配方采用版本" in (chicken.nutrition.calculation_note or "")
