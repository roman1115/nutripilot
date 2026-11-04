"""Deterministic construction and pre-write validation for meal plans."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timedelta
from itertools import product
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from nutripilot.agent.contracts import PlanSpec
from nutripilot.domain.models import IngredientIdentity, NumericRange
from nutripilot.retrieval.models import NutrientField
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


class _PlanNutrition(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)

    calories_kcal: float | None = Field(default=None, ge=0)
    protein_g: float | None = Field(default=None, ge=0)
    carbohydrates_g: float | None = Field(default=None, ge=0)
    fat_g: float | None = Field(default=None, ge=0)
    fiber_g: float | None = Field(default=None, ge=0)
    sodium_mg: float | None = Field(default=None, ge=0)
    nutrient_ranges: dict[NutrientField, NumericRange] = Field(default_factory=dict)
    source_status: str = "unverified_estimate"


class _PlanCandidate(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)

    meal_id: int = Field(gt=0)
    is_active: bool = True
    meal_types: list[str] = Field(min_length=1)
    ingredients: list[str]
    ingredient_records: list[IngredientIdentity] = Field(default_factory=list)
    allergens: list[str]
    nutrition: _PlanNutrition | None = None
    meal_role: Literal["complete_meal", "dish", "staple", "soup", "snack"] = "complete_meal"


NUTRIENTS: tuple[NutrientField, ...] = (
    "calories_kcal",
    "protein_g",
    "carbohydrates_g",
    "fat_g",
    "fiber_g",
    "sodium_mg",
)


class PlanBuilder:
    """Build repeatable plans and reject model-proposed unsafe mutations."""

    def build(self, spec: PlanSpec, candidates: list[dict[str, Any]]) -> CreateMealPlanArgs:
        parsed = self._parse_candidates(candidates)
        pools = self._candidate_pools(spec, parsed)
        combinations = list(product(*(pools[meal_type] for meal_type in spec.meal_types)))
        usage: Counter[int] = Counter()
        previous: dict[str, int] = {}
        items: list[MealPlanItemInput] = []
        for day_index in range(spec.day_count):
            meal_date = spec.start_date + timedelta(days=day_index)
            best = min(
                combinations,
                key=lambda combination: (
                    len(combination) - len({candidate.meal_id for candidate in combination}),
                    sum(
                        previous.get(meal_type) == candidate.meal_id
                        for meal_type, candidate in zip(spec.meal_types, combination, strict=True)
                    ),
                    sum(usage[candidate.meal_id] for candidate in combination),
                    tuple(candidate.meal_id for candidate in combination),
                ),
            )
            for meal_type, candidate in zip(spec.meal_types, best, strict=True):
                items.append(
                    MealPlanItemInput(
                        meal_date=meal_date,
                        meal_type=meal_type,
                        meal_id=candidate.meal_id,
                    )
                )
                usage[candidate.meal_id] += 1
                previous[meal_type] = candidate.meal_id
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
        if request.start_date != spec.start_date:
            raise PlanBuildError(
                "PLAN_START_DATE_MISMATCH",
                "The meal plan start date differs from the requested date.",
            )
        expected_slots = {
            (spec.start_date + timedelta(days=day_index), meal_type)
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
            if item.meal_type not in candidate.meal_types or not candidate.is_active:
                incompatible_ids.add(item.meal_id)
            if has_dietary_conflict(
                meal_allergens=candidate.allergens,
                meal_ingredients=candidate.ingredients,
                declared_allergens=declared_allergens,
                excluded_ingredients=excluded_ingredients,
                ingredient_records=candidate.ingredient_records,
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
        self.assess(spec, request, candidates)

    def assess(
        self,
        spec: PlanSpec,
        request: CreateMealPlanArgs,
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Sum whole recipes without inferring a person's intake or target attainment."""

        by_id = {candidate.meal_id: candidate for candidate in self._parse_candidates(candidates)}
        days: dict[str, list[_PlanCandidate]] = defaultdict(list)
        for item in request.items:
            if item.meal_id in by_id:
                days[item.meal_date.isoformat()].append(by_id[item.meal_id])
        warnings = [
            "营养按所选整道菜的投料合计，不代表个人摄入量，不评定个人每日营养目标是否达成。",
            "每个餐次安排一道菜，不表示该菜构成完整一餐。",
        ]
        summaries: list[dict[str, Any]] = []
        selected = {item.meal_id for item in request.items}
        if any(
            meal_id in selected and meal.nutrition and meal.nutrition.source_status != "verified"
            for meal_id, meal in by_id.items()
        ):
            warnings.append("餐单营养值包含未独立核验的配方估算，未经成菜检测。")
        if spec.daily_calorie_target is not None or spec.daily_protein_target_g is not None:
            warnings.append("已记录的每日目标不用于本次整菜安排，不推断人数、分餐或份量。")
        for day, meals in sorted(days.items()):
            totals = self._totals(meals)
            summaries.append({"date": day, **totals})
            if any(totals[field] is None for field in NUTRIENTS):
                warnings.append(f"{day} 部分营养数据不完整，相应合计保留未知。")
        counts = Counter(item.meal_id for item in request.items)
        repeated = sorted(meal_id for meal_id, count in counts.items() if count > 1)
        if repeated:
            warnings.append(
                f"本计划 {len(request.items)} 个餐次使用 {len(counts)} 道菜，"
                "包含重复菜品；未宣称实现完整食物多样性。"
            )
        return {
            "nutrition_basis": "whole_recipe",
            "scope": "all_meal_slots" if spec.meal_type is None else "selected_meals",
            "daily_totals": summaries,
            "totals": self._totals([meal for meals in days.values() for meal in meals]),
            "unique_meals": len(counts),
            "meal_count": len(request.items),
            "repeated_meal_ids": repeated,
            "warnings": warnings,
        }

    @staticmethod
    def _totals(meals: list[_PlanCandidate]) -> dict[str, Any]:
        result: dict[str, Any] = {"nutrient_ranges": {}}
        for field in NUTRIENTS:
            bounds: list[NumericRange] = []
            for meal in meals:
                nutrition = meal.nutrition
                value = getattr(nutrition, field, None)
                if nutrition is None or value is None:
                    break
                bounds.append(
                    nutrition.nutrient_ranges.get(field, NumericRange(minimum=value, maximum=value))
                )
            if not meals or len(bounds) != len(meals):
                result[field] = None
                continue
            low = round(sum(bound.minimum for bound in bounds), 2)
            high = round(sum(bound.maximum for bound in bounds), 2)
            result[field] = high
            result["nutrient_ranges"][field] = {"minimum": low, "maximum": high}
        return result

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
            meal_type: [
                candidate
                for candidate in candidates
                if meal_type in candidate.meal_types and candidate.is_active
            ]
            for meal_type in spec.meal_types
        }
        missing = [meal_type for meal_type, pool in pools.items() if not pool]
        if missing:
            labels = {"breakfast": "早餐", "lunch": "午餐", "dinner": "晚餐", "snack": "加餐"}
            raise PlanBuildError(
                "PLAN_CANDIDATES_INSUFFICIENT",
                f"符合当前条件的来源菜谱不足，无法覆盖{'、'.join(labels[t] for t in missing)}。"
                "请调整非过敏、非忌口条件，或仅安排已有候选的餐次。",
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
        return f"{range_name}{meal_name}菜品安排"


def candidate_meals_from_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Read only successful search results; remote payloads were already validated."""

    meals: dict[int, dict[str, Any]] = {}
    for result in results:
        if result.get("tool_name") != "search_meals" or result.get("status") != "success":
            continue
        data = result.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("hits"), list):
            continue
        for hit in data["hits"]:
            meal = hit.get("meal", hit) if isinstance(hit, dict) else None
            if isinstance(meal, dict) and isinstance(
                meal_id := meal.get("meal_id") or meal.get("id"), int
            ):
                meals[meal_id] = meal
    return list(meals.values())
