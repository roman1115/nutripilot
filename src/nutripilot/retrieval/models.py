from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _normalise_terms(values: list[str]) -> list[str]:
    return sorted({value.strip().casefold() for value in values if value.strip()})


class NutritionFacts(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    calories_kcal: float = Field(ge=0)
    protein_g: float = Field(ge=0)
    carbohydrates_g: float = Field(ge=0)
    fat_g: float = Field(ge=0)
    fiber_g: float = Field(default=0, ge=0)
    sodium_mg: float = Field(default=0, ge=0)
    serving_size: str = Field(default="1 serving", min_length=1)


class Meal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=120)
    aliases: list[str] = Field(default_factory=list)
    description: str = Field(min_length=1, max_length=1000)
    meal_types: list[str] = Field(min_length=1)
    cuisines: list[str] = Field(default_factory=list)
    tastes: list[str] = Field(default_factory=list)
    health_goals: list[str] = Field(default_factory=list)
    dietary_tags: list[str] = Field(default_factory=list)
    allergens: list[str] = Field(default_factory=list)
    ingredients: list[str] = Field(default_factory=list)
    nutrition: NutritionFacts
    prep_minutes: int = Field(ge=0, le=1440)
    price_cny: float = Field(ge=0)
    source: str = Field(min_length=1)
    source_uri: str | None = None
    data_version: str = "2026.07"

    @field_validator(
        "meal_types",
        "cuisines",
        "tastes",
        "health_goals",
        "dietary_tags",
        "allergens",
        mode="after",
    )
    @classmethod
    def normalise_classifiers(cls, value: list[str]) -> list[str]:
        return _normalise_terms(value)

    def searchable_text(self) -> str:
        parts = [
            self.name,
            self.description,
            *self.meal_types,
            *self.cuisines,
            *self.tastes,
            *self.health_goals,
            *self.dietary_tags,
            *self.ingredients,
        ]
        return " ".join(parts)


class DietaryGuideline(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    content: str
    source: str
    source_uri: str | None = None
    topics: list[str] = Field(default_factory=list)
    version: str = "2026.07"
    disclaimer: str = "仅供一般饮食信息参考，不构成医疗诊断或治疗建议。"


class MealSearchFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meal_type: str | None = None
    cuisines: list[str] = Field(default_factory=list)
    tastes: list[str] = Field(default_factory=list)
    health_goals: list[str] = Field(default_factory=list)
    dietary_tags: list[str] = Field(default_factory=list)
    excluded_allergens: list[str] = Field(default_factory=list)
    excluded_ingredients: list[str] = Field(default_factory=list)
    excluded_meal_ids: list[int] = Field(default_factory=list)
    max_calories_kcal: float | None = Field(default=None, gt=0)
    min_protein_g: float | None = Field(default=None, ge=0)
    max_prep_minutes: int | None = Field(default=None, ge=0)
    max_price_cny: float | None = Field(default=None, ge=0)

    @field_validator(
        "cuisines",
        "tastes",
        "health_goals",
        "dietary_tags",
        "excluded_allergens",
        "excluded_ingredients",
        mode="after",
    )
    @classmethod
    def normalise_filters(cls, value: list[str]) -> list[str]:
        return _normalise_terms(value)


class MealSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=500)
    filters: MealSearchFilters = Field(default_factory=MealSearchFilters)
    limit: int = Field(default=5, ge=1, le=20)
    candidate_limit: int = Field(default=30, ge=5, le=100)


class ScoredId(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    score: float
    source: str


class MealSearchHit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meal: Meal
    score: float = Field(ge=0)
    rank: int = Field(gt=0)
    reasons: list[str] = Field(default_factory=list)
    retrieval_sources: list[str] = Field(default_factory=list)
    citation: dict[str, Any]


class MealSearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    hits: list[MealSearchHit]
    total_candidates: int = Field(ge=0)
    relaxed_filters: list[str] = Field(default_factory=list)
    retrieval_version: str = "hybrid-rrf-v1"


class GuidelineSearchHit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    guideline: DietaryGuideline
    score: float = Field(ge=0)
