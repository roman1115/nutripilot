from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class UserProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: int = Field(gt=0)
    display_name: str | None = Field(default=None, max_length=80)
    preferences: list[str] = Field(default_factory=list)
    cuisines: list[str] = Field(default_factory=list)
    dislikes: list[str] = Field(default_factory=list)
    allergens: list[str] = Field(default_factory=list)
    health_goals: list[str] = Field(default_factory=list)
    dietary_tags: list[str] = Field(default_factory=list)
    daily_calorie_target: float | None = Field(default=None, gt=0, le=10_000)
    location: str | None = Field(default=None, max_length=100)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator(
        "preferences",
        "cuisines",
        "dislikes",
        "allergens",
        "health_goals",
        "dietary_tags",
        mode="after",
    )
    @classmethod
    def normalise_terms(cls, values: list[str]) -> list[str]:
        return sorted({value.strip().casefold() for value in values if value.strip()})


class GetUserProfileArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GetUserFeedbackArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WeatherContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    location: str
    condition: str
    temperature_c: float
    humidity_percent: int = Field(ge=0, le=100)
    recommendation_context: list[str] = Field(default_factory=list)
    observed_at: datetime
    provider: str


class GetWeatherContextArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    location: str = Field(default="上海", min_length=1, max_length=100)


class MealPlanItemInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meal_date: date
    meal_type: Literal["breakfast", "lunch", "dinner", "snack"]
    meal_id: int = Field(gt=0)
    note: str | None = Field(default=None, max_length=300)


class CreateMealPlanArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=120)
    start_date: date
    items: list[MealPlanItemInput] = Field(min_length=1, max_length=28)


class MealPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    user_id: int
    title: str
    start_date: date
    items: list[MealPlanItemInput]
    created_at: datetime


class RecordFeedbackArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meal_id: int = Field(gt=0)
    feedback_type: Literal["like", "dislike", "replace", "rating"]
    rating: int | None = Field(default=None, ge=1, le=5)
    reason: str | None = Field(default=None, max_length=500)


class FeedbackRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    user_id: int
    meal_id: int
    feedback_type: str
    rating: int | None = None
    reason: str | None = None
    created_at: datetime


class FeedbackHistory(BaseModel):
    """Verified feedback used as an explicit recommendation constraint."""

    model_config = ConfigDict(extra="forbid")

    records: list[FeedbackRecord] = Field(default_factory=list)
    excluded_meal_ids: list[int] = Field(default_factory=list)


class UpdateUserProfileArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, max_length=80)
    preferences: list[str] | None = None
    cuisines: list[str] | None = None
    dislikes: list[str] | None = None
    allergens: list[str] | None = None
    health_goals: list[str] | None = None
    dietary_tags: list[str] | None = None
    daily_calorie_target: float | None = Field(default=None, gt=0, le=10_000)
    location: str | None = Field(default=None, max_length=100)

    @field_validator(
        "preferences",
        "cuisines",
        "dislikes",
        "allergens",
        "health_goals",
        "dietary_tags",
        mode="after",
    )
    @classmethod
    def normalise_optional_terms(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        return sorted({value.strip().casefold() for value in values if value.strip()})


class MutationReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_type: str
    resource_id: str
    action: str
