"""Independent structured-output contracts for each Worker Agent."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nutripilot.domain.enums import Intent
from nutripilot.domain.models import IngredientGroup, IngredientTerm
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
    required_ingredients: list[IngredientTerm] = Field(default_factory=list, max_length=10)
    ingredient_any_groups: list[IngredientGroup] = Field(default_factory=list, max_length=10)
    ingredient_action: Literal["replace", "append", "keep", "clear"] | None = None
    max_calories_kcal: float | None = Field(default=None, gt=0)
    min_protein_g: float | None = Field(default=None, ge=0)
    max_price_cny: float | None = Field(default=None, ge=0)
    max_prep_minutes: int | None = Field(default=None, ge=0, le=1440)
    clear_constraints: list[
        Literal["max_calories_kcal", "min_protein_g", "max_price_cny", "max_prep_minutes"]
    ] = Field(default_factory=list, max_length=4)
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
        )


class RecommendationChoice(BaseModel):
    model_config = ConfigDict(extra="ignore")

    meal_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=120)
    reason: str = Field(default="", max_length=300)


class RecommendationDraft(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Literal["completed", "degraded"] = "completed"
    message: str = Field(min_length=1, max_length=6000)
    recommendations: list[RecommendationChoice] = Field(default_factory=list, max_length=3)
    source_ids: list[str] = Field(default_factory=list, max_length=12)


class RecommendationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response: RecommendationDraft


class AnswerEvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    record_id: str = Field(min_length=1, max_length=100)
    quote: str = Field(min_length=1, max_length=1200)


class AnswerConditionReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source: AnswerEvidenceReference
    answer_quote: str = Field(max_length=1200)


class AnswerObjectCount(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    quote: str = Field(min_length=1, max_length=300)
    scope: Literal["recommendations", "display"]
    meal_ids: list[int] = Field(default_factory=list, max_length=100)
    minimum: int = Field(ge=0, le=1000)
    maximum: int | None = Field(ge=0, le=1000)


class AnswerUnitReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    unit_id: str = Field(min_length=1, max_length=100)
    kind: Literal["source_fact", "general_advice", "ranking", "health_effect", "conversation"]
    object_counts: list[AnswerObjectCount] = Field(max_length=10)
    conditions: list[AnswerConditionReview] = Field(max_length=12)
    conditions_preserved: bool
    evidence: list[AnswerEvidenceReference] = Field(max_length=8)
    explanation: str = Field(min_length=1, max_length=240)
    supported: bool


class AnswerReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    checks: list[AnswerUnitReview] = Field(max_length=80)
    issues: list[str] = Field(default_factory=list, max_length=8)
    supported: bool


class EvaluationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: float = Field(ge=0, le=1)
    reason: str
