"""Independent structured-output contracts for each Worker Agent."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from nutripilot.domain.enums import Intent
from nutripilot.domain.models import FinalResponse
from nutripilot.tools.models import ToolCall


class IntentOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Intent
    confidence: float = Field(ge=0, le=1)
    extracted_slots: dict[str, Any] = Field(default_factory=dict)
    missing_slots: list[str] = Field(default_factory=list)
    notes: str = ""


class PlannerOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    complete: bool
    tool_calls: list[ToolCall] = Field(default_factory=list)
    plan_summary: str


class PlanSpec(BaseModel):
    """Deterministic scope used to build and validate a meal plan."""

    model_config = ConfigDict(extra="forbid")

    plan_range: Literal["one_day", "one_week"] = "one_week"
    meal_type: Literal["breakfast", "lunch", "dinner", "snack"] | None = None
    start_date: date = Field(default_factory=date.today)

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
    ) -> PlanSpec:
        raw_range = str(slots.get("plan_range") or "one_week").casefold()
        plan_range = "one_day" if raw_range == "one_day" else "one_week"
        raw_meal_type = slots.get("meal_type")
        if raw_meal_type == "late_snack":
            raw_meal_type = "snack"
        meal_type = (
            raw_meal_type
            if raw_meal_type in {"breakfast", "lunch", "dinner", "snack"}
            else None
        )
        return cls(
            plan_range=plan_range,
            meal_type=meal_type,
            start_date=start_date or date.today(),
        )


class RecommendationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response: FinalResponse


class EvaluationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: float = Field(ge=0, le=1)
    reason: str
