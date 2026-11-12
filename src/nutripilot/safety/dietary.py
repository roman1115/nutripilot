"""Deterministic dietary-constraint matching shared across safety layers."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from nutripilot.domain.models import IngredientIdentity


def _ingredient_term(value: str) -> str:
    # Reviewed spelling equivalents also apply to derivatives when checking avoidance.
    return value.strip().casefold().replace("西红柿", "番茄").replace("马铃薯", "土豆")


def match_ingredient(
    term: str,
    ingredients: Iterable[object],
    records: Iterable[IngredientIdentity | dict[str, Any]] = (),
    *,
    exclusion: bool = False,
) -> list[dict[str, str]]:
    """Use reviewed identities for inclusion, conservative substring matching for avoidance."""
    query = _ingredient_term(term)
    if not query:
        return []
    identities = [
        item if isinstance(item, IngredientIdentity) else IngredientIdentity.model_validate(item)
        for item in records
    ]
    matches = []
    for item in identities:
        if not exclusion and not item.consumed:
            continue
        names = [item.name, *item.aliases]
        matched = next(
            (name for name in names if (
                query in _ingredient_term(name) if exclusion else query == _ingredient_term(name)
            )),
            None,
        )
        if matched:
            matches.append({
                "requested": term, "ingredient_id": item.ingredient_id,
                "name": item.name, "matched_term": matched,
            })
    if exclusion or not identities:
        # Historical records have no identity metadata; retain their conservative legacy match.
        matches.extend(
            {"requested": term, "name": str(name), "matched_term": str(name)}
            for name in ingredients if query in _ingredient_term(str(name))
        )
    return matches


def meets_ingredient_requirements(
    meal: dict[str, Any],
    required: Iterable[str] = (),
    any_groups: Iterable[Iterable[str]] = (),
) -> bool:
    def matches(term: str) -> bool:
        return bool(match_ingredient(
            term, meal.get("ingredients", []), meal.get("ingredient_records", [])
        ))

    return all(matches(term) for term in required) and all(
        any(matches(term) for term in group) for group in any_groups
    )


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
    ingredient_records: Iterable[IngredientIdentity | dict[str, Any]] = (),
) -> bool:
    """Treat every user-declared allergen as both an allergen and ingredient exclusion."""

    allergens = set(merge_dietary_terms(meal_allergens))
    ingredients = merge_dietary_terms(meal_ingredients)
    declared = merge_dietary_terms(declared_allergens)

    if allergens.intersection(declared):
        return True

    ingredient_exclusions = merge_dietary_terms(declared, excluded_ingredients)
    records = list(ingredient_records)
    return any(match_ingredient(term, ingredients, records, exclusion=True)
               for term in ingredient_exclusions)
