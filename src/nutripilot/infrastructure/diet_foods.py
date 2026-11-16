"""Resolve owned foods and catalog references into immutable, sourced evidence."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from nutripilot.domain.diet import (
    DECIMAL_STEP,
    NUTRIENTS,
    DietError,
    FoodEvidenceSnapshot,
    FoodRef,
    IngredientFood,
    NutrientRange,
    NutrientSnapshot,
    NutritionSnapshot,
    PersonalFoodRef,
    RecipeFood,
    UnknownFood,
)
from nutripilot.domain.diet_records import FoodHit
from nutripilot.retrieval.catalog import CALCULATION_NOTE, FoodCatalog, Ingredient, Recipe
from nutripilot.retrieval.ports import MealRepository
from nutripilot.tools.local.ports import DietTransaction, ResolvedFood


class CatalogDietFoodResolver:
    def __init__(self, catalog: FoodCatalog, meals: MealRepository | None = None) -> None:
        self.catalog = catalog
        self.meals = meals
        self.ingredients = {item.id: item for item in catalog.ingredients}
        self.recipes = {item.id: item for item in catalog.recipes}
        self.sources = {item.id: item for item in catalog.sources}

    def reference(self, item: Ingredient | Recipe) -> IngredientFood | RecipeFood:
        parts = (
            [
                self.ingredients[part.ingredient_id]
                for part in item.ingredients
                if part.ingredient_id in self.ingredients
            ]
            if isinstance(item, Recipe)
            else []
        )
        inputs = {
            "food": item.model_dump(mode="json"),
            "ingredients": [part.model_dump(mode="json") for part in parts],
            "sources": [
                self.sources[source].model_dump(mode="json")
                for source in sorted({item.source_id, *(part.source_id for part in parts)})
            ],
        }
        version = hashlib.sha256(
            json.dumps(inputs, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        return (
            RecipeFood(kind="recipe", id=item.id, version=version)
            if isinstance(item, Recipe)
            else IngredientFood(kind="ingredient", id=item.id, version=version)
        )

    async def resolve(
        self, food: FoodRef, *, user_id: int, now: datetime, tx: DietTransaction
    ) -> ResolvedFood:
        self._owner(tx, user_id)
        if isinstance(food, UnknownFood):
            return ResolvedFood(food.name, None)
        if isinstance(food, PersonalFoodRef):
            owned = await tx.get_personal_food(food.id)
            if owned is None or owned.deleted_at is not None:
                raise DietError("NOT_FOUND", "Personal food not found.")
            if owned.version != food.version:
                raise DietError("SOURCE_CHANGED", "Personal food has changed.")
            return ResolvedFood(owned.name, owned.evidence)
        item = self._catalog_item(food)
        if self.reference(item).version != food.version:
            raise DietError("SOURCE_CHANGED", "Food reference has changed; select it again.")
        evidence_id = f"{food.kind}:{food.id}:{food.version[:16]}"
        values: dict[str, NutrientSnapshot] = {}
        for name in NUTRIENTS:
            if isinstance(item, Ingredient):
                value = getattr(item.nutrients_per_100g, name)
                bounds = None if value is None else (Decimal(str(value)), Decimal(str(value)))
            else:
                low, high = Decimal(0), Decimal(0)
                complete = True
                for part in item.ingredients:
                    if not part.consumed:
                        continue
                    ingredient = self.ingredients.get(part.ingredient_id or "")
                    value = getattr(ingredient.nutrients_per_100g, name) if ingredient else None
                    if value is None or (part.grams is None and part.grams_range is None):
                        complete = False
                        break
                    lower = (
                        part.grams
                        if part.grams is not None
                        else (part.grams_range.minimum if part.grams_range else 0)
                    )
                    upper = (
                        part.grams
                        if part.grams is not None
                        else (part.grams_range.maximum if part.grams_range else 0)
                    )
                    low += Decimal(str(value)) * Decimal(str(lower)) / 100
                    high += Decimal(str(value)) * Decimal(str(upper)) / 100
                bounds = (low, high) if complete else None
            values[name] = (
                NutrientSnapshot(
                    value=NutrientRange(
                        lower=bounds[0].quantize(DECIMAL_STEP, rounding=ROUND_FLOOR),
                        upper=bounds[1].quantize(DECIMAL_STEP, rounding=ROUND_CEILING),
                    ),
                    basis="catalog_value"
                    if isinstance(item, Ingredient)
                    else "calculated_estimate",
                    evidence_ids=[evidence_id],
                )
                if bounds is not None
                else NutrientSnapshot(missing_reasons=["catalog_value_missing"])
            )
        source = self.sources[item.source_id]
        evidence = FoodEvidenceSnapshot(
            evidence_id=evidence_id,
            source_kind="catalog",
            source_ref=f"{item.source_id}:{item.id}",
            source_version=food.version,
            retrieved_at=now,
            # The legacy ingredient catalog has no explicit state; its name is not state evidence.
            food_state="unknown",
            basis_quantity="per_100g" if isinstance(item, Ingredient) else "recipe_batch",
            nutrients=NutritionSnapshot(**values),
            original_excerpt=(
                item.source_description
                if isinstance(item, Ingredient)
                else item.preparation_note or ""
            )[:2000],
            calculation_note=(
                "Catalog per-100g composition; raw/cooked state is unspecified."
                if isinstance(item, Ingredient)
                else CALCULATION_NOTE
            ),
            source_uri=str(item.source_uri),
            license=source.license,
        )
        return ResolvedFood(item.name, evidence)

    async def assert_available(self, food: FoodRef, *, user_id: int, tx: DietTransaction) -> None:
        self._owner(tx, user_id)
        if isinstance(food, UnknownFood):
            return
        if isinstance(food, PersonalFoodRef):
            owned = await tx.get_personal_food(food.id)
            if owned is None or owned.deleted_at is not None:
                raise DietError("SOURCE_CHANGED", "Personal food is no longer available.")
            if owned.version < food.version:
                raise DietError("SOURCE_CHANGED", "Personal food version is unavailable.")
            return
        self._catalog_item(food)
        if isinstance(food, RecipeFood) and self.meals is not None:
            meal = await self.meals.get_meal(food.id)
            if meal is None or not meal.is_active:
                raise DietError("MEAL_RETIRED", "该菜谱已退役，请重新选择来源。")

    async def search(
        self,
        query: str,
        *,
        food_kind: str,
        food_state: str | None,
        limit: int,
        now: datetime,
        tx: DietTransaction,
    ) -> list[FoodHit]:
        query = query.casefold()
        candidates: list[FoodRef] = []
        if food_kind in ("any", "personal_food"):
            candidates.extend(
                PersonalFoodRef(kind="personal_food", id=food.id, version=food.version)
                for food in await tx.list_personal_foods()
                if query in f"{food.name} {food.brand or ''}".casefold()
            )
        catalog_items: list[Ingredient | Recipe] = [
            *self.catalog.ingredients,
            *self.catalog.recipes,
        ]
        for item in catalog_items:
            kind = "ingredient" if isinstance(item, Ingredient) else "recipe"
            if food_kind in ("any", kind) and query in item.name.casefold():
                candidates.append(self.reference(item))
        hits = []
        for candidate in candidates:
            try:
                resolved = await self.resolve(candidate, user_id=tx.state.user_id, now=now, tx=tx)
            except DietError as exc:
                if exc.code in ("SOURCE_CHANGED", "NOT_FOUND"):
                    continue
                raise
            evidence = resolved.evidence
            assert evidence is not None
            if food_state and evidence.food_state != food_state:
                continue
            hits.append(
                FoodHit(
                    food_ref=candidate,
                    name=resolved.name,
                    food_state=evidence.food_state,
                    basis=evidence.basis_quantity,
                    source_ref=evidence.source_ref,
                    available_nutrients=[
                        name
                        for name in NUTRIENTS
                        if evidence.nutrients.nutrient(name).value is not None
                    ],
                    serving_evidence_id=(
                        evidence.serving_evidence_id
                        if evidence.basis_quantity == "per_serving"
                        or evidence.serving_grams is not None
                        else None
                    ),
                )
            )
            if len(hits) >= limit:
                break
        return hits

    def _catalog_item(self, food: IngredientFood | RecipeFood) -> Ingredient | Recipe:
        item: Ingredient | Recipe | None = (
            self.ingredients.get(food.id)
            if isinstance(food, IngredientFood)
            else self.recipes.get(food.id)
        )
        if item is None:
            raise DietError("NOT_FOUND", "Catalog food not found.")
        if isinstance(item, Recipe) and (item.status != "calculation_ready" or item.review_issues):
            raise DietError("SOURCE_CHANGED", "Recipe evidence is unavailable.")
        return item

    @staticmethod
    def _owner(tx: DietTransaction, user_id: int) -> None:
        if tx.state.user_id != user_id:
            raise DietError("NOT_FOUND", "Food not found.")
