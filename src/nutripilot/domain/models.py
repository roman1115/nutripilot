"""Pydantic domain models crossing orchestration boundaries."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

IngredientTerm = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)
]
IngredientGroup = Annotated[list[IngredientTerm], Field(min_length=1, max_length=10)]


class NumericRange(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    minimum: float = Field(ge=0)
    maximum: float = Field(ge=0)

    @model_validator(mode="after")
    def ordered(self) -> NumericRange:
        if self.maximum < self.minimum:
            raise ValueError("Range maximum must be at least its minimum.")
        return self


class Citation(BaseModel):
    source_id: str
    title: str
    source_type: str = "meal"
    uri: str | None = None
    score: float | None = Field(default=None, ge=0)
    excerpt: str | None = None


class IngredientIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    ingredient_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    consumed: bool = True


class MealRecommendation(BaseModel):
    meal_id: int
    name: str
    reason: str
    calories_kcal: float | None = Field(default=None, ge=0)
    protein_g: float | None = Field(default=None, ge=0)
    carbohydrates_g: float | None = Field(default=None, ge=0)
    fat_g: float | None = Field(default=None, ge=0)
    nutrient_ranges: dict[str, NumericRange] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)


class FinalResponse(BaseModel):
    status: str = "completed"
    message: str
    recommendations: list[MealRecommendation] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    suggested_actions: list[str] = Field(default_factory=list)
