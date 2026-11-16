"""Consistent export snapshots and owner-scoped, content-free purge tombstones."""

from datetime import date
from typing import Literal

from pydantic import AwareDatetime

from nutripilot.domain.diet import DietModel
from nutripilot.domain.diet_records import (
    DietLog,
    DietPlan,
    NutritionTarget,
    PersonalFood,
    PlannedMeal,
)


class DietExportSnapshot(DietModel):
    user_id: int
    diet_epoch: int
    start_date: date
    end_date: date
    logs: list[DietLog]
    plans: list[DietPlan]
    planned_meals: list[PlannedMeal]
    targets: list[NutritionTarget]
    personal_foods: list[PersonalFood]


class PurgeTombstone(DietModel):
    user_id: int
    entity_type: Literal[
        "log", "draft", "preview", "plan", "planned_meal", "personal_food", "target"
    ]
    entity_id: str
    version: int
    purged_at: AwareDatetime
