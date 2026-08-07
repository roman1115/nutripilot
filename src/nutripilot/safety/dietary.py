"""Deterministic dietary-constraint matching shared across safety layers."""

from __future__ import annotations

from collections.abc import Iterable


def merge_dietary_terms(*groups: Iterable[object] | object | None) -> list[str]:
    """Return stable, case-insensitive terms without trusting model formatting."""

    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        if group is None:
            continue
        values = [group] if isinstance(group, str) or not isinstance(group, Iterable) else group
        for value in values:
            term = str(value).strip().casefold()
            if term and term not in seen:
                seen.add(term)
                merged.append(term)
    return merged


def has_dietary_conflict(
    *,
    meal_allergens: Iterable[object] | None,
    meal_ingredients: Iterable[object] | None,
    declared_allergens: Iterable[object] | None = None,
    excluded_ingredients: Iterable[object] | None = None,
) -> bool:
    """Treat every user-declared allergen as both an allergen and ingredient exclusion."""

    allergens = set(merge_dietary_terms(meal_allergens))
    ingredients = merge_dietary_terms(meal_ingredients)
    declared = merge_dietary_terms(declared_allergens)

    if allergens.intersection(declared):
        return True

    ingredient_exclusions = merge_dietary_terms(declared, excluded_ingredients)
    return any(
        excluded in ingredient
        for excluded in ingredient_exclusions
        for ingredient in ingredients
    )
