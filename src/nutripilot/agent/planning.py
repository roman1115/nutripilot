"""Deterministic construction and pre-write validation for meal plans."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timedelta
from itertools import product
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


class _PlanNutrition(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)

    calories_kcal: float = Field(ge=0)
    protein_g: float = Field(ge=0)


class _PlanCandidate(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)

    meal_id: int = Field(gt=0)
    meal_types: list[str] = Field(min_length=1)
    ingredients: list[str]
    allergens: list[str]
    nutrition: _PlanNutrition | None = None

# Product matching tolerance, not a clinical recommendation or a derived dietary target.
CALORIE_TARGET_TOLERANCE = 0.10


class PlanBuilder:
    """Build repeatable plans and reject model-proposed unsafe mutations."""

    def build(self, spec: PlanSpec, candidates: list[dict[str, Any]]) -> CreateMealPlanArgs:
        parsed = self._parse_candidates(candidates)
        pools = self._candidate_pools(spec, parsed)
        combinations = self._day_combinations(spec, pools)
        if spec.meal_type is None:
            matching = [
                combination
                for combination in combinations
                if self._meets_targets(spec, combination)
            ]
            if matching:
                combinations = matching
            elif spec.enforce_targets:
                raise PlanBuildError(
                    "PLAN_NUTRITION_TARGET_UNREACHABLE",
                    "当前已验证餐食的标准份量无法同时满足每日目标，请补充餐食或调整目标。",
                )
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
                    self._target_distance(spec, combination),
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
        self.assess(spec, request, candidates)

    def assess(
        self,
        spec: PlanSpec,
        request: CreateMealPlanArgs,
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Report standard-serving totals; never extrapolate a single meal to a full day."""

        by_id = {candidate.meal_id: candidate for candidate in self._parse_candidates(candidates)}
        days: dict[str, list[_PlanCandidate]] = defaultdict(list)
        for item in request.items:
            if item.meal_id in by_id:
                days[item.meal_date.isoformat()].append(by_id[item.meal_id])
        warnings: list[str] = []
        summaries: list[dict[str, Any]] = []
        full_day = spec.meal_type is None
        if not full_day and (
            spec.daily_calorie_target is not None or spec.daily_protein_target_g is not None
        ):
            warnings.append("本计划仅包含指定餐次，不评定全天营养目标是否达成。")
        for day, meals in sorted(days.items()):
            complete = all(meal.nutrition is not None for meal in meals)
            calories = sum(meal.nutrition.calories_kcal for meal in meals if meal.nutrition)
            protein = sum(meal.nutrition.protein_g for meal in meals if meal.nutrition)
            meets = self._meets_targets(spec, tuple(meals)) if complete and full_day else None
            summaries.append(
                {
                    "date": day,
                    "calories_kcal": round(calories, 2) if complete else None,
                    "protein_g": round(protein, 2) if complete else None,
                    "target_met": meets,
                }
            )
            if not complete:
                warnings.append(f"{day} 营养数据不完整，无法核对目标。")
            elif full_day and meets is False:
                warnings.append(
                    f"{day} 标准份量合计 {calories:g} 千卡、蛋白质 {protein:g} 克，"
                    "未达到已设置的每日目标。"
                )
            if full_day and spec.enforce_targets and meets is not True:
                raise PlanBuildError(
                    "PLAN_NUTRITION_TARGET_UNMET",
                    "计划未满足本轮明确的每日营养目标，已阻止写入。",
                    details={"date": day, "calories_kcal": calories, "protein_g": protein},
                )
        counts = Counter(item.meal_id for item in request.items)
        repeated = sorted(meal_id for meal_id, count in counts.items() if count > 1)
        if repeated:
            warnings.append(
                f"本计划 {len(request.items)} 餐使用 {len(counts)} 道餐食，"
                "包含重复菜品；未宣称实现完整食物多样性。"
            )
        if not spec.daily_calorie_target and spec.daily_protein_target_g is None:
            warnings.append("未设置明确的每日营养目标，本计划未进行营养达标判定。")
        return {
            "scope": "full_day" if full_day else "selected_meals",
            "daily_calorie_target": spec.daily_calorie_target,
            "daily_protein_target_g": spec.daily_protein_target_g,
            "calorie_tolerance_ratio": CALORIE_TARGET_TOLERANCE,
            "daily_totals": summaries,
            "unique_meals": len(counts),
            "meal_count": len(request.items),
            "repeated_meal_ids": repeated,
            "warnings": warnings,
        }

    @staticmethod
    def _day_combinations(
        spec: PlanSpec, pools: dict[str, list[_PlanCandidate]]
    ) -> list[tuple[_PlanCandidate, ...]]:
        return list(product(*(pools[meal_type] for meal_type in spec.meal_types)))

    @staticmethod
    def _meets_targets(spec: PlanSpec, meals: tuple[_PlanCandidate, ...]) -> bool | None:
        if spec.daily_calorie_target is None and spec.daily_protein_target_g is None:
            return None
        if any(meal.nutrition is None for meal in meals):
            return False
        calories = sum(meal.nutrition.calories_kcal for meal in meals if meal.nutrition)
        protein = sum(meal.nutrition.protein_g for meal in meals if meal.nutrition)
        calorie_ok = (
            spec.daily_calorie_target is None
            or abs(calories - spec.daily_calorie_target)
            <= spec.daily_calorie_target * CALORIE_TARGET_TOLERANCE + 1e-6
        )
        protein_ok = (
            spec.daily_protein_target_g is None or protein + 1e-6 >= spec.daily_protein_target_g
        )
        return calorie_ok and protein_ok

    @staticmethod
    def _target_distance(spec: PlanSpec, meals: tuple[_PlanCandidate, ...]) -> float:
        if spec.meal_type is not None or any(meal.nutrition is None for meal in meals):
            return 0
        calories = sum(meal.nutrition.calories_kcal for meal in meals if meal.nutrition)
        protein = sum(meal.nutrition.protein_g for meal in meals if meal.nutrition)
        distance = (
            abs(calories - spec.daily_calorie_target) / spec.daily_calorie_target
            if spec.daily_calorie_target
            else 0
        )
        if spec.daily_protein_target_g:
            distance += max(0, spec.daily_protein_target_g - protein) / spec.daily_protein_target_g
        return distance

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
