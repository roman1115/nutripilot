from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from nutripilot.application.intake_service import IntakeService
from nutripilot.application.personal_food_service import (
    PersonalFoodInput,
    PersonalFoodMutation,
    PersonalFoodService,
)
from nutripilot.domain.diet import (
    NUTRIENTS,
    DietError,
    MassQuantity,
    NutritionSnapshot,
    PersonalFoodRef,
    RecipeFractionQuantity,
    SubmissionAnchor,
    calculate_nutrition,
    content_hash,
)
from nutripilot.domain.diet_records import DietChangePreview, PersonalFoodChangeContent
from nutripilot.infrastructure.diet_foods import CatalogDietFoodResolver
from nutripilot.infrastructure.diet_store import MemoryDietStore
from nutripilot.retrieval.catalog import FoodCatalog, load_food_catalog
from nutripilot.tools.local.diet_models import CreateIntake

NOW = datetime(2026, 11, 12, 4, tzinfo=UTC)


def food_input() -> PersonalFoodInput:
    return PersonalFoodInput(
        name="fixture-food",
        food_state="as_sold",
        basis="per_100g",
        nutrients={
            name: ({"lower": "10", "upper": "20"} if name != "fiber_g" else None)
            for name in NUTRIENTS
        },
        source="manual_label",
        source_note="Synthetic label values for boundary testing.",
    )


async def test_owned_food_lifecycle_keeps_old_record_snapshot_and_unknown_fields() -> None:
    store = MemoryDietStore()
    service = PersonalFoodService(store, default_timezone="Asia/Shanghai", clock=lambda: NOW)
    resolver = CatalogDietFoodResolver(FoodCatalog(sources=[], ingredients=[], recipes=[]))
    preview = await service.prepare(
        1, PersonalFoodMutation(action="create", data=food_input()), idempotency_key="food"
    )
    assert isinstance(preview.content, PersonalFoodChangeContent)
    food = preview.content.after
    async with store.transaction(1, default_timezone="Asia/Shanghai") as tx:
        assert await tx.get_personal_food(food.id) is None
    await service.confirm(1, preview.id, version=1, approved_hash=preview.content_hash)
    ref = PersonalFoodRef(kind="personal_food", id=food.id, version=1)
    async with store.transaction(2, default_timezone="Asia/Shanghai") as tx:
        with pytest.raises(DietError) as exc:
            await resolver.resolve(ref, user_id=2, now=NOW, tx=tx)
        assert exc.value.code == "NOT_FOUND"
    intake = IntakeService(store, resolver, default_timezone="Asia/Shanghai", clock=lambda: NOW)
    draft = await intake.prepare(
        1,
        CreateIntake.model_validate(
            {
                "action": "create",
                "entry": {
                    "actuality": "actual",
                    "subject": "self",
                    "temporal": {"local_date": "2026-11-12", "meal_type": "lunch"},
                    "items": [
                        {
                            "client_item_key": "food",
                            "food": ref,
                            "quantity": {"kind": "mass", "grams": "50", "food_state": "as_sold"},
                        }
                    ],
                },
                "evidence": [
                    {
                        "field": "food",
                        "source_kind": "user_form",
                        "source_ref": "fixture",
                        "original_text": "fixture",
                        "operation": "set",
                        "status": "explicit",
                    }
                ],
            }
        ),
        idempotency_key="intake",
        anchor=SubmissionAnchor.capture(NOW, "Asia/Shanghai"),
    )
    nutrition = draft.content.items[0].nutrition_snapshot
    assert nutrition.calories_kcal.value is not None
    assert nutrition.calories_kcal.value.lower == Decimal("5")
    assert nutrition.fiber_g.value is None
    assert nutrition.calories_kcal.basis == "label_reported"
    receipt = await intake.confirm(1, draft.id, version=1, approved_hash=draft.preview_hash)
    for action, version in (("delete", 1), ("restore", 2)):
        change = await service.prepare(
            1,
            PersonalFoodMutation(action=action, food_id=food.id, base_version=version),
            idempotency_key=action,
        )
        await service.confirm(1, change.id, version=1, approved_hash=change.content_hash)
        async with store.transaction(1, default_timezone="Asia/Shanghai") as tx:
            record = await tx.get_log(receipt.result_refs[0].uuid)
            assert record is not None and record.items[0].nutrition_snapshot == nutrition
            assert record.items[0].evidence_snapshot == food.evidence
            if action == "delete":
                with pytest.raises(DietError):
                    await resolver.assert_available(ref, user_id=1, tx=tx)
            else:
                await resolver.assert_available(ref, user_id=1, tx=tx)
    retry = await service.confirm(1, preview.id, version=1, approved_hash=preview.content_hash)
    assert retry.result_status == "committed"


async def test_catalog_batch_uses_whole_recipe_and_preserves_missing_state() -> None:
    catalog = load_food_catalog("data")
    resolver = CatalogDietFoodResolver(catalog)
    store = MemoryDietStore()
    recipe = next(item for item in catalog.recipes if item.status == "calculation_ready")
    async with store.transaction(1, default_timezone="Asia/Shanghai") as tx:
        resolved = await resolver.resolve(resolver.reference(recipe), user_id=1, now=NOW, tx=tx)
        assert resolved.evidence is not None and resolved.evidence.basis_quantity == "recipe_batch"
        actual = calculate_nutrition(
            RecipeFractionQuantity(kind="recipe_fraction", batch_multiplier="1", share="0.5"),
            resolved.evidence,
        )
        expected = (
            sum(
                Decimal(
                    str(resolver.ingredients[part.ingredient_id].nutrients_per_100g.calories_kcal)
                )
                * Decimal(str(part.grams if part.grams is not None else part.grams_range.minimum))
                / 100
                for part in recipe.ingredients
                if part.consumed
            )
            / 2
        )
        assert actual.calories_kcal.value is not None
        assert abs(actual.calories_kcal.value.lower - expected) <= Decimal("0.000001")
        ingredient = catalog.ingredients[0]
        resolved_ingredient = await resolver.resolve(
            resolver.reference(ingredient), user_id=1, now=NOW, tx=tx
        )
        assert resolved_ingredient.evidence is not None
        assert resolved_ingredient.evidence.food_state == "unknown"
        value = calculate_nutrition(
            MassQuantity(kind="mass", grams="100", food_state="raw"), resolved_ingredient.evidence
        )
        assert value == NutritionSnapshot.unknown("food_state_unknown")


def test_food_data_requires_all_fields_and_allergen_evidence() -> None:
    data = food_input().model_dump()
    data["nutrients"].pop("sodium_mg")
    with pytest.raises(ValidationError):
        PersonalFoodInput.model_validate(data)
    with pytest.raises(ValidationError):
        PersonalFoodInput.model_validate(
            {
                **food_input().model_dump(),
                "allergens": [
                    {
                        "name": "fixture-allergen",
                        "state": "known_absent",
                        "evidence_ref": None,
                        "explanation": "unsupported",
                    }
                ],
            }
        )


async def test_empty_media_extension_preserves_old_frozen_evidence_hash() -> None:
    store = MemoryDietStore()
    service = PersonalFoodService(store, default_timezone="UTC", clock=lambda: NOW)
    preview = await service.prepare(
        1, PersonalFoodMutation(action="create", data=food_input()), idempotency_key="old"
    )
    old = preview.model_dump(mode="json")
    assert "media_ids" not in old["content"]["after"]["evidence"]
    restored = DietChangePreview.model_validate(old)
    assert restored.content_hash == preview.content_hash == content_hash(restored.content)
    await service.confirm(1, restored.id, version=1, approved_hash=preview.content_hash)
