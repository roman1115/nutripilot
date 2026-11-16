"""Observed label fields retain units and excerpts until explicit user review."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from nutripilot.domain.diet import (
    NUTRIENTS,
    DietModel,
    Grams,
    Nutrient,
    NutrientRange,
    ShortText,
    convert_label_range,
)


class LabelNutrient(DietModel):
    value: NutrientRange
    unit: Literal["kJ", "kcal", "g", "mg"]
    original_excerpt: Annotated[str, Field(min_length=1, max_length=200)]


class ParsedLabel(DietModel):
    name: ShortText | None = None
    brand: ShortText | None = None
    basis: Literal["per_100g", "per_serving", "unsupported", "unknown"]
    serving_grams: Grams | None = None
    nutrients: dict[Nutrient, LabelNutrient | None]
    original_excerpt: Annotated[str, Field(max_length=2000)]
    clarification: Annotated[str, Field(max_length=1000)] = ""

    @model_validator(mode="after")
    def explicit_observations(self) -> Self:
        if set(self.nutrients) != set(NUTRIENTS):
            raise ValueError("All six label nutrients must be explicitly observed or unknown.")
        for nutrient, observation in self.nutrients.items():
            if observation is not None:
                allowed = ("kcal", "kJ") if nutrient == "calories_kcal" else ("g", "mg")
                if observation.unit not in allowed:
                    raise ValueError("Label nutrient has an incompatible unit.")
        return self

    def normalized(self) -> dict[Nutrient, NutrientRange | None]:
        return {
            nutrient: convert_label_range(
                observation.value,
                from_unit=observation.unit,
                to_unit="kcal"
                if nutrient == "calories_kcal"
                else "mg"
                if nutrient == "sodium_mg"
                else "g",
            )
            if observation is not None
            else None
            for nutrient, observation in self.nutrients.items()
        }
