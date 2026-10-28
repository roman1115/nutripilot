"""Deterministic recipe preparation from explicit, versioned mapping decisions."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from nutripilot.domain.models import NumericRange
from nutripilot.retrieval.catalog import CatalogModel, Recipe, RecipeIngredient


class QuantityRule(CatalogModel):
    ingredient_id: str
    grams_per_unit: float = Field(gt=0)
    unit: str = Field(min_length=1)
    evidence: str = Field(min_length=1)


class MappedPart(CatalogModel):
    rule: str
    amount: float | NumericRange
    text: str = Field(min_length=1)
    consumed: bool = True
    basis: Literal["source", "project_default", "oil_absorption"] = "source"
    note: str | None = None


class RecipeMapping(CatalogModel):
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    parts: list[MappedPart] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    servings: int | None = Field(default=None, gt=0)
    meal_types: list[str] = Field(default_factory=lambda: ["lunch", "dinner"])
    meal_role: Literal["dish", "staple", "soup", "snack"] = "dish"
    note: str = Field(min_length=1)


class CatalogMappings(CatalogModel):
    ingredient_mappings: dict[str, dict[str, Any]] = Field(default_factory=dict)
    quantity_rules: dict[str, QuantityRule]
    recipes: dict[str, RecipeMapping]


def apply_recipe_mappings(
    raw_recipes: list[dict[str, Any]], mappings: CatalogMappings
) -> list[Recipe]:
    known_ids = {str(recipe["id"]) for recipe in raw_recipes}
    if set(mappings.recipes) != known_ids:
        raise ValueError("Every imported recipe needs exactly one mapping or exception record.")
    results = []
    for raw in raw_recipes:
        mapping = mappings.recipes[str(raw["id"])]
        if raw["source_sha256"] != mapping.source_sha256:
            raise ValueError(f"Recipe {raw['id']} changed; review its mapping before rebuilding.")
        patch: dict[str, Any] = {
            **raw,
            "review_issues": mapping.issues,
            "preparation_note": mapping.note,
        }
        if mapping.issues:
            patch["status"] = "pending"
            results.append(Recipe.model_validate(patch))
            continue
        parts = []
        for part in mapping.parts:
            rule = mappings.quantity_rules[part.rule]
            amount = (
                part.amount
                if isinstance(part.amount, NumericRange)
                else NumericRange(minimum=part.amount, maximum=part.amount)
            )
            if amount.minimum <= 0:
                raise ValueError("Mapping amounts must be positive.")
            weight = NumericRange(
                minimum=round(amount.minimum * rule.grams_per_unit, 6),
                maximum=round(amount.maximum * rule.grams_per_unit, 6),
            )
            basis: Literal[
                "source",
                "source_range",
                "conversion",
                "project_default",
                "oil_absorption",
                "excluded",
            ] = part.basis
            if basis == "source":
                basis = (
                    "conversion"
                    if rule.unit != "g"
                    else ("source_range" if weight.minimum != weight.maximum else "source")
                )
            if not part.consumed:
                basis = "excluded"
            parts.append(
                RecipeIngredient(
                    quantity_text=part.text,
                    ingredient_id=rule.ingredient_id,
                    grams=weight.maximum if weight.minimum == weight.maximum else None,
                    grams_range=weight if weight.minimum != weight.maximum else None,
                    consumed=part.consumed,
                    quantity_basis=basis,
                    evidence=f"{rule.evidence}；{part.note or '采用原文用量'}",
                    note=part.note,
                )
            )
        parts.extend(
            RecipeIngredient(
                quantity_text=text,
                consumed=False,
                quantity_basis="excluded",
                evidence="按项目约定排除加工用水、腌制液、独立蘸料或未选可选项。",
                note=text,
            )
            for text in mapping.exclusions
        )
        patch.update(
            ingredients=[part.model_dump(mode="json") for part in parts],
            status="calculation_ready",
            servings=mapping.servings,
            serving_label="整道菜（整份配方）",
            calculation_basis="whole_recipe",
            meal_types=mapping.meal_types,
            meal_role=mapping.meal_role,
        )
        results.append(Recipe.model_validate(patch))
    return results
