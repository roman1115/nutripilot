"""Independent structured-output contracts for each Worker Agent."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nutripilot.domain.enums import Intent
from nutripilot.domain.models import FinalResponse
from nutripilot.tools.models import ToolCall


class IntentSlots(BaseModel):
    """Only supported, type-checked values may enter the shared state."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    meal_type: Literal["breakfast", "lunch", "dinner", "snack", "late_snack"] | None = None
    plan_range: Literal["one_day", "one_week"] | None = None
    start_date: date | None = None
    food_name: str | None = Field(default=None, min_length=1, max_length=120)
    location: str | None = Field(default=None, max_length=100)
    tastes: list[str] = Field(default_factory=list)
    preferences: list[str] = Field(default_factory=list)
    cuisines: list[str] = Field(default_factory=list)
    health_goals: list[str] = Field(default_factory=list)
    dietary_tags: list[str] = Field(default_factory=list)
    allergens: list[str] = Field(default_factory=list)
    dislikes: list[str] = Field(default_factory=list)
    excluded_ingredients: list[str] = Field(default_factory=list)
    max_calories_kcal: float | None = Field(default=None, gt=0)
    min_protein_g: float | None = Field(default=None, ge=0)
    max_price_cny: float | None = Field(default=None, ge=0)
    max_prep_minutes: int | None = Field(default=None, ge=0, le=1440)
    daily_calorie_target: float | None = Field(default=None, gt=0, le=10_000)
    daily_protein_target_g: float | None = Field(default=None, ge=0, le=1_000)


class IntentOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Intent
    confidence: float = Field(ge=0, le=1)
    extracted_slots: dict[str, Any] = Field(
        default_factory=dict, json_schema_extra=IntentSlots.model_json_schema()
    )
    missing_slots: list[str] = Field(default_factory=list)
    notes: str = ""

    @field_validator("extracted_slots")
    @classmethod
    def validate_slots(cls, value: dict[str, Any]) -> dict[str, Any]:
        return IntentSlots.model_validate(value).model_dump(
            mode="json", exclude_none=True, exclude_defaults=True
        )


class PlannerOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    complete: bool
    tool_calls: list[ToolCall] = Field(default_factory=list)
    plan_summary: str


class PlanSpec(BaseModel):
    """Deterministic scope used to build and validate a meal plan."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    plan_range: Literal["one_day", "one_week"] = "one_week"
    meal_type: Literal["breakfast", "lunch", "dinner", "snack"] | None = None
    start_date: date = Field(default_factory=date.today)
    daily_calorie_target: float | None = Field(default=None, gt=0, le=10_000)
    daily_protein_target_g: float | None = Field(default=None, ge=0, le=1_000)
    enforce_targets: bool = False

    @property
    def day_count(self) -> int:
        return 1 if self.plan_range == "one_day" else 7

    @property
    def meal_types(self) -> tuple[str, ...]:
        if self.meal_type is not None:
            return (self.meal_type,)
        return ("breakfast", "lunch", "dinner")

    @classmethod
    def from_slots(
        cls,
        slots: dict[str, Any],
        *,
        start_date: date | None = None,
        profile: dict[str, Any] | None = None,
    ) -> PlanSpec:
        raw_range = str(slots.get("plan_range") or "one_week").casefold()
        plan_range = "one_day" if raw_range == "one_day" else "one_week"
        raw_meal_type = slots.get("meal_type")
        if raw_meal_type == "late_snack":
            raw_meal_type = "snack"
        meal_type = (
            raw_meal_type if raw_meal_type in {"breakfast", "lunch", "dinner", "snack"} else None
        )
        return cls(
            plan_range=plan_range,
            meal_type=meal_type,
            start_date=slots.get("start_date") or start_date or date.today(),
            daily_calorie_target=slots.get("daily_calorie_target")
            or (profile or {}).get("daily_calorie_target"),
            daily_protein_target_g=slots.get("daily_protein_target_g")
            if slots.get("daily_protein_target_g") is not None
            else (profile or {}).get("daily_protein_target_g"),
            enforce_targets=any(
                slots.get(key) is not None
                for key in ("daily_calorie_target", "daily_protein_target_g")
            ),
        )


class RecommendationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response: FinalResponse


class EvaluationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: float = Field(ge=0, le=1)
    reason: str
