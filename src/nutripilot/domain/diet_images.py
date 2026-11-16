"""Reviewable photo estimates, separate from measured food and packaging facts."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import Field

from nutripilot.domain.diet import (
    NUTRIENTS,
    ClientKey,
    DietModel,
    FoodEvidenceSnapshot,
    FoodState,
    NutrientRange,
    NutrientSnapshot,
    NutritionSnapshot,
    ShortText,
    content_hash,
)


class EstimatedNutrients(DietModel):
    calories_kcal: NutrientRange | None = None
    protein_g: NutrientRange | None = None
    fat_g: NutrientRange | None = None
    carbohydrates_g: NutrientRange | None = None
    fiber_g: NutrientRange | None = None
    sodium_mg: NutrientRange | None = None


class ImageFoodEstimate(DietModel):
    entry_index: Annotated[int, Field(strict=True, ge=0, le=9)] = 0
    client_item_key: ClientKey
    name: ShortText
    food_state: FoodState
    nutrients_per_100g: EstimatedNutrients
    rationale: Annotated[str, Field(min_length=1, max_length=1000)]


class ImageEstimation(DietModel):
    model_name: ShortText
    prompt_version: ShortText
    estimates: Annotated[list[ImageFoodEstimate], Field(max_length=500)]

    def evidence_for(
        self,
        *,
        item_key: str,
        name: str,
        media_ids: list[UUID],
        now: datetime,
    ) -> FoodEvidenceSnapshot | None:
        matches = [
            item
            for item in self.estimates
            if item.entry_index == 0
            and item.client_item_key == item_key
            and item.name.strip().casefold() == name.strip().casefold()
        ]
        if len(matches) != 1 or not media_ids:
            return None
        estimate = matches[0]
        evidence_id = f"vision:{content_hash(estimate)[:24]}"
        return FoodEvidenceSnapshot(
            evidence_id=evidence_id,
            source_kind="model_estimate",
            source_ref=f"media:{','.join(str(value) for value in media_ids)}",
            source_version=self.prompt_version,
            retrieved_at=now,
            food_state=estimate.food_state,
            basis_quantity="per_100g",
            nutrients=NutritionSnapshot(
                **{
                    name: NutrientSnapshot(
                        value=value, basis="model_estimate", evidence_ids=[evidence_id]
                    )
                    if (value := getattr(estimate.nutrients_per_100g, name)) is not None
                    else NutrientSnapshot(missing_reasons=["image_estimate_unavailable"])
                    for name in NUTRIENTS
                }
            ),
            original_excerpt=estimate.rationale,
            calculation_note="餐照与常见做法的粗略估算；非称重、配方实测或包装标签。",
            model_name=self.model_name,
            prompt_version=self.prompt_version,
            media_ids=media_ids,
        )
