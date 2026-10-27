"""Versioned ingredient and recipe records, independent from model-generated text."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from nutripilot.retrieval.models import Meal, NutritionFacts

NUTRIENT_FIELDS = (
    "calories_kcal",
    "protein_g",
    "carbohydrates_g",
    "fat_g",
    "fiber_g",
    "sodium_mg",
)
CALCULATION_NOTE = (
    "按配方可食用投料克重与食材每100克数据汇总；未校正烹调损失、吸水、蒸发和油盐残留。"
    "投料重量不是成菜重量，计算估算不等于成菜检测结果。"
)


class CatalogModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", allow_inf_nan=False, str_strip_whitespace=True, strict=True
    )


class DataSource(CatalogModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    uri: HttpUrl
    license: str = Field(min_length=1)
    version: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    checksum_scope: Literal["archive", "license"]


class NutrientsPer100g(CatalogModel):
    # Missing source values remain unknown; they are never silently converted to zero.
    calories_kcal: float | None = Field(ge=0)
    protein_g: float | None = Field(ge=0)
    carbohydrates_g: float | None = Field(ge=0)
    fat_g: float | None = Field(ge=0)
    fiber_g: float | None = Field(ge=0)
    sodium_mg: float | None = Field(ge=0)


class Ingredient(CatalogModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    source_id: str = Field(min_length=1)
    source_record_id: str = Field(min_length=1)
    source_description: str = Field(min_length=1)
    source_uri: HttpUrl
    nutrients_per_100g: NutrientsPer100g
    allergens: list[str] | None = None
    mapping_note: str | None = None


class RecipeIngredient(CatalogModel):
    quantity_text: str = Field(min_length=1)
    ingredient_id: str | None = None
    grams: float | None = Field(default=None, gt=0)
    consumed: bool = True
    note: str | None = None

    @model_validator(mode="after")
    def require_exclusion_reason(self) -> RecipeIngredient:
        if not self.consumed and not self.note:
            raise ValueError("Non-consumed ingredients require an exclusion reason.")
        return self


class Recipe(CatalogModel):
    id: int = Field(ge=1000)
    name: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_uri: HttpUrl
    source_path: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    raw_ingredients: str = Field(min_length=1)
    raw_quantities: str = Field(min_length=1)
    instructions: str = Field(min_length=1)
    ingredients: list[RecipeIngredient] = Field(min_length=1)
    status: Literal["pending", "calculation_ready"] = "pending"
    review_issues: list[str] = Field(default_factory=list)
    servings: int | None = Field(default=None, gt=0, le=100)
    serving_label: str | None = Field(default=None, min_length=1)
    meal_types: list[str] = Field(default_factory=list)
    meal_role: Literal["complete_meal", "dish", "staple", "soup", "snack"] = "dish"
    preparation_note: str | None = None


class FoodCatalog(CatalogModel):
    sources: list[DataSource]
    ingredients: list[Ingredient]
    recipes: list[Recipe]

    @model_validator(mode="after")
    def validate_references(self) -> FoodCatalog:
        for label, records in (
            ("source", self.sources),
            ("ingredient", self.ingredients),
            ("recipe", self.recipes),
        ):
            ids = [record.id for record in records]
            if len(ids) != len(set(ids)):
                raise ValueError(f"Duplicate {label} IDs in food catalog.")
        sources = {source.id for source in self.sources}
        by_id = {ingredient.id: ingredient for ingredient in self.ingredients}
        for ingredient in self.ingredients:
            if ingredient.source_id not in sources:
                raise ValueError(f"Unknown source for ingredient {ingredient.id}.")
        for recipe in self.recipes:
            if recipe.source_id not in sources:
                raise ValueError(f"Unknown source for recipe {recipe.id}.")
            for part in recipe.ingredients:
                if part.ingredient_id is not None and part.ingredient_id not in by_id:
                    raise ValueError(f"Unknown ingredient in recipe {recipe.id}.")
            if recipe.status == "calculation_ready":
                calculate_recipe(recipe, by_id)
        return self

    def calculated_meals(self) -> list[Meal]:
        ingredients = {item.id: item for item in self.ingredients}
        return [
            recipe_to_meal(recipe, ingredients)
            for recipe in self.recipes
            if recipe.status == "calculation_ready"
        ]


def calculate_recipe(recipe: Recipe, ingredients: dict[str, Ingredient]) -> NutritionFacts:
    if recipe.status != "calculation_ready" or recipe.review_issues:
        raise ValueError(f"Recipe {recipe.id} still has unresolved review issues.")
    if not recipe.servings or not recipe.serving_label or not recipe.meal_types:
        raise ValueError(f"Recipe {recipe.id} needs explicit servings, label and meal types.")
    totals = dict.fromkeys(NUTRIENT_FIELDS, 0.0)
    input_weight = 0.0
    sources: set[str] = set()
    for part in recipe.ingredients:
        if not part.consumed:
            continue
        if part.ingredient_id not in ingredients or part.grams is None:
            raise ValueError(f"Recipe {recipe.id} has an unresolved ingredient or weight.")
        ingredient = ingredients[part.ingredient_id]
        if ingredient.allergens is None or not ingredient.mapping_note:
            raise ValueError(f"Recipe {recipe.id} requires allergen and ingredient mapping notes.")
        for field in NUTRIENT_FIELDS:
            value = getattr(ingredient.nutrients_per_100g, field)
            if value is None:
                raise ValueError(f"Ingredient {ingredient.id} is missing {field}.")
            totals[field] += float(value) * part.grams / 100
        input_weight += part.grams
        sources.add(str(ingredient.source_uri))
    if not input_weight:
        raise ValueError(f"Recipe {recipe.id} has no consumed ingredients.")
    return NutritionFacts(
        **{field: round(value / recipe.servings, 4) for field, value in totals.items()},
        serving_size=recipe.serving_label,
        serving_weight_g=round(input_weight / recipe.servings, 4),
        weight_basis="ingredient_input",
        source_status="calculated_estimate",
        source_uri=str(recipe.source_uri),
        ingredient_source_uris=sorted(sources),
        calculation_note=CALCULATION_NOTE,
    )


def recipe_to_meal(recipe: Recipe, ingredients: dict[str, Ingredient]) -> Meal:
    nutrition = calculate_recipe(recipe, ingredients)
    consumed = [
        ingredients[part.ingredient_id]
        for part in recipe.ingredients
        if part.consumed and part.ingredient_id is not None
    ]
    return Meal(
        id=recipe.id,
        name=recipe.name,
        description=f"来源菜谱的{recipe.serving_label}；营养按明确投料计算，未独立检测。",
        meal_types=recipe.meal_types,
        meal_role=recipe.meal_role,
        recipe_id=recipe.id,
        cuisines=["中式"],
        ingredients=list(dict.fromkeys(item.name for item in consumed)),
        allergens=sorted({term for item in consumed for term in item.allergens or []}),
        nutrition=nutrition,
        prep_minutes=None,
        price_cny=None,
        source="HowToCook 菜谱；USDA SR Legacy 食材数据；配方计算估算",
        source_uri=str(recipe.source_uri),
        data_version="catalog-2026-10-27",
    )


def load_food_catalog(data_directory: str | Path) -> FoodCatalog:
    directory = Path(data_directory)
    payload = {}
    for key in ("sources", "ingredients", "recipes"):
        with (directory / f"{key}.json").open(encoding="utf-8") as handle:
            payload[key] = json.load(handle)
    return FoodCatalog.model_validate(payload)
