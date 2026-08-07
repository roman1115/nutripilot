"""Deterministic construction and pre-write validation for meal plans."""

from __future__ import annotations

from collections import Counter
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from nutripilot.agent.contracts import PlanSpec
from nutripilot.safety import has_dietary_conflict
from nutripilot.tools.local.models import CreateMealPlanArgs, MealPlanItemInput


class PlanBuildError(ValueError):
    def __init__(
        self,
        code: str,
        safe_message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message
        self.details = details or {}


class _PlanCandidate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    meal_id: int = Field(gt=0)
    meal_types: list[str] = Field(min_length=1)
    ingredients: list[str]
    allergens: list[str]


class PlanBuilder:
    """Build repeatable plans and reject model-proposed unsafe mutations."""

    def build(self, spec: PlanSpec, candidates: list[dict[str, Any]]) -> CreateMealPlanArgs:
        parsed = self._parse_candidates(candidates)
        pools = self._candidate_pools(spec, parsed)
        items: list[MealPlanItemInput] = []
        for day_index in range(spec.day_count):
            meal_date = spec.start_date + timedelta(days=day_index)
            for meal_type_index, meal_type in enumerate(spec.meal_types):
                pool = pools[meal_type]
                candidate = pool[(day_index + meal_type_index) % len(pool)]
                items.append(
                    MealPlanItemInput(
                        meal_date=meal_date,
                        meal_type=meal_type,
                        meal_id=candidate.meal_id,
                    )
                )
        return CreateMealPlanArgs(
            title=self._title(spec),
            start_date=spec.start_date,
            items=items,
        )

    def validate_prewrite(
        self,
        *,
        spec: PlanSpec,
        request: CreateMealPlanArgs,
        candidates: list[dict[str, Any]],
        allowed_meal_ids: set[int],
        declared_allergens: list[str],
        excluded_ingredients: list[str],
    ) -> None:
        parsed = self._parse_candidates(candidates)
        by_id = {candidate.meal_id: candidate for candidate in parsed}
        expected_slots = {
            (request.start_date + timedelta(days=day_index), meal_type)
            for day_index in range(spec.day_count)
            for meal_type in spec.meal_types
        }
        actual_slots = Counter((item.meal_date, item.meal_type) for item in request.items)
        if set(actual_slots) != expected_slots or any(
            count != 1 for count in actual_slots.values()
        ):
            raise PlanBuildError(
                "PLAN_STRUCTURE_INVALID",
                "The meal plan does not cover every required date and meal type exactly once.",
                details={"expected_items": len(expected_slots), "actual_items": len(request.items)},
            )

        invalid_ids = sorted(
            {
                item.meal_id
                for item in request.items
                if item.meal_id not in allowed_meal_ids or item.meal_id not in by_id
            }
        )
        if invalid_ids:
            raise PlanBuildError(
                "PLAN_CANDIDATE_NOT_ALLOWED",
                "The meal plan contained an item outside the current verified candidate set.",
                details={"meal_ids": invalid_ids},
            )

        incompatible_ids: set[int] = set()
        unsafe_ids: set[int] = set()
        for item in request.items:
            candidate = by_id[item.meal_id]
            if item.meal_type not in candidate.meal_types:
                incompatible_ids.add(item.meal_id)
            if has_dietary_conflict(
                meal_allergens=candidate.allergens,
                meal_ingredients=candidate.ingredients,
                declared_allergens=declared_allergens,
                excluded_ingredients=excluded_ingredients,
            ):
                unsafe_ids.add(item.meal_id)
        if incompatible_ids:
            raise PlanBuildError(
                "PLAN_MEAL_TYPE_MISMATCH",
                "The meal plan assigned a candidate to an unsupported meal type.",
                details={"meal_ids": sorted(incompatible_ids)},
            )
        if unsafe_ids:
            raise PlanBuildError(
                "PLAN_DIETARY_CONFLICT",
                "The meal plan contained an item that conflicts with an allergy or avoidance.",
                details={"meal_ids": sorted(unsafe_ids)},
            )

    @staticmethod
    def parse_request(arguments: dict[str, Any]) -> CreateMealPlanArgs:
        try:
            return CreateMealPlanArgs.model_validate(arguments)
        except ValidationError as exc:
            raise PlanBuildError(
                "PLAN_ARGUMENTS_INVALID",
                "The proposed meal plan did not match the required structure.",
                details={"field_count": len(exc.errors())},
            ) from exc

    @staticmethod
    def _parse_candidates(candidates: list[dict[str, Any]]) -> list[_PlanCandidate]:
        parsed: list[_PlanCandidate] = []
        invalid_ids: list[int] = []
        for candidate in candidates:
            raw = dict(candidate)
            raw["meal_id"] = raw.get("meal_id") or raw.get("id")
            try:
                parsed.append(_PlanCandidate.model_validate(raw))
            except ValidationError:
                raw_id = raw.get("meal_id")
                if isinstance(raw_id, int):
                    invalid_ids.append(raw_id)
        if invalid_ids:
            raise PlanBuildError(
                "PLAN_CANDIDATE_METADATA_MISSING",
                "One or more meal candidates lacked metadata required for safe planning.",
                details={"meal_ids": sorted(set(invalid_ids))},
            )
        return parsed

    @staticmethod
    def _candidate_pools(
        spec: PlanSpec,
        candidates: list[_PlanCandidate],
    ) -> dict[str, list[_PlanCandidate]]:
        pools = {
            meal_type: [candidate for candidate in candidates if meal_type in candidate.meal_types]
            for meal_type in spec.meal_types
        }
        missing = [meal_type for meal_type, pool in pools.items() if not pool]
        if missing:
            raise PlanBuildError(
                "PLAN_CANDIDATES_INSUFFICIENT",
                "There are not enough verified candidates to cover the requested meal types.",
                details={"meal_types": missing},
            )
        return pools

    @staticmethod
    def _title(spec: PlanSpec) -> str:
        range_name = "一日" if spec.plan_range == "one_day" else "一周"
        meal_names = {
            "breakfast": "早餐",
            "lunch": "午餐",
            "dinner": "晚餐",
            "snack": "加餐",
        }
        meal_name = meal_names.get(spec.meal_type or "", "三餐")
        return f"{range_name}个性化{meal_name}计划"


def candidate_meals_from_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Read only successful search results; remote payloads were already validated."""

    for result in reversed(results):
        if result.get("tool_name") != "search_meals" or result.get("status") != "success":
            continue
        data = result.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("hits"), list):
            return []
        meals: list[dict[str, Any]] = []
        for hit in data["hits"]:
            meal = hit.get("meal", hit) if isinstance(hit, dict) else None
            if isinstance(meal, dict):
                meals.append(meal)
        return meals
    return []
