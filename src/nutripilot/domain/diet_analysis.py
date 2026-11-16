"""Deterministic daily and calendar-week analysis without filling blank days with zero."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID

from nutripilot.domain.diet import (
    GOAL_NUTRIENTS,
    NUTRIENTS,
    DietModel,
    DifferenceRange,
    GoalComparison,
    GoalNutrient,
    Nutrient,
    NutrientRange,
    NutritionSummary,
    Quantity,
    compare_goal,
    summarize_nutrition,
)
from nutripilot.domain.diet_records import (
    DietDayDeclaration,
    DietLog,
    NutritionTarget,
    PlannedMeal,
    ResourceRef,
    target_on,
)


class DailyAnalysis(DietModel):
    local_date: date
    actual: NutritionSummary
    planned: NutritionSummary
    target_ref: ResourceRef | None
    target_comparison: dict[GoalNutrient, GoalComparison]
    record_refs: list[ResourceRef]
    plan_refs: list[ResourceRef]


class WeeklyNutrient(DietModel):
    known_subtotal: NutrientRange
    recorded_days: int
    computable_recorded_days: int
    complete_days: int
    computable_complete_days: int
    recorded_day_average: NutrientRange | None
    complete_day_average: NutrientRange | None
    seven_day_average: NutrientRange | None
    missing_reasons: list[str]


class WeeklyAnalysis(DietModel):
    week_start: date
    covered_start_date: date
    covered_end_date: date
    covered_days: int
    recorded_days: int
    complete_days: int
    nutrients: dict[Nutrient, WeeklyNutrient]
    days: list[DailyAnalysis]


class IntakeExecution(DietModel):
    planned_item_id: UUID
    planned_item_version: int
    planned_quantity: Quantity
    actual_quantities: list[Quantity]
    quantity_difference: DifferenceRange | None
    reason: str | None


class PlanExecution(DietModel):
    planned_meal_ref: ResourceRef
    plan_ref: ResourceRef
    status: Literal["recorded", "not_recorded"]
    actual_refs: list[ResourceRef]
    recorded_count: int
    items: list[IntakeExecution]


class DailyDeviation(DietModel):
    analysis: DailyAnalysis
    plan_actual_difference: dict[Nutrient, DifferenceRange | None]
    comparison_reasons: dict[Nutrient, str | None]
    execution: list[PlanExecution]


def diet_deviation(
    days: list[DailyAnalysis],
    plans: list[PlannedMeal],
    logs: list[DietLog],
    plan_refs: dict[str, ResourceRef],
) -> list[DailyDeviation]:
    result = []
    for day in days:
        differences: dict[Nutrient, DifferenceRange | None] = {}
        reasons: dict[Nutrient, str | None] = {}
        for name in NUTRIENTS:
            actual, planned = day.actual.nutrients[name], day.planned.nutrients[name]
            reason = (
                "day_not_complete"
                if day.actual.day_status == "open"
                else "no_plan"
                if day.planned.record_count == 0
                else "nutrition_incomplete"
                if not actual.complete or not planned.complete
                else None
            )
            reasons[name] = reason
            differences[name] = None
            if reason is None and actual.total is not None and planned.total is not None:
                differences[name] = DifferenceRange(
                    lower=actual.total.lower - planned.total.upper,
                    upper=actual.total.upper - planned.total.lower,
                )
        execution = []
        for meal in plans:
            if meal.deleted_at or meal.local_date != day.local_date:
                continue
            linked = [
                log
                for log in logs
                if log.deleted_at is None and log.source_planned_meal_id == meal.id
            ]
            item_comparisons = []
            for item in meal.items:
                actual_items = [
                    part
                    for log in linked
                    for part in log.items
                    if part.source_planned_item_id == item.id
                ]
                quantities = [part.quantity for part in actual_items]
                planned_measure = _quantity_measure(item.quantity)
                actual_measures = [_quantity_measure(quantity) for quantity in quantities]
                comparable = all(part.food_ref == item.food_ref for part in actual_items) and (
                    bool(quantities)
                    and planned_measure is not None
                    and all(
                        measure is not None and measure[0] == planned_measure[0]
                        for measure in actual_measures
                    )
                )
                difference = None
                if comparable:
                    assert planned_measure is not None
                    values = [measure for measure in actual_measures if measure is not None]
                    difference = DifferenceRange(
                        lower=sum((value[1] for value in values), Decimal(0)) - planned_measure[2],
                        upper=sum((value[2] for value in values), Decimal(0)) - planned_measure[1],
                    )
                item_comparisons.append(
                    IntakeExecution(
                        planned_item_id=item.id,
                        planned_item_version=item.version,
                        planned_quantity=item.quantity,
                        actual_quantities=quantities,
                        quantity_difference=difference,
                        reason=None
                        if comparable
                        else "not_recorded"
                        if not quantities
                        else "quantity_basis_mismatch_or_unknown",
                    )
                )
            execution.append(
                PlanExecution(
                    planned_meal_ref=ResourceRef(
                        kind="planned_meal", id=meal.id, version=meal.version
                    ),
                    plan_ref=plan_refs[meal.plan_id],
                    status="recorded" if linked else "not_recorded",
                    actual_refs=[
                        ResourceRef(kind="log", id=log.id, version=log.version) for log in linked
                    ],
                    recorded_count=len(linked),
                    items=item_comparisons,
                )
            )
        result.append(
            DailyDeviation(
                analysis=day,
                plan_actual_difference=differences,
                comparison_reasons=reasons,
                execution=execution,
            )
        )
    return result


def _quantity_measure(quantity: Quantity) -> tuple[str, Decimal, Decimal] | None:
    if quantity.kind == "mass" and quantity.food_state != "unknown":
        return f"mass:{quantity.food_state}", quantity.grams, quantity.grams
    if quantity.kind == "mass_range" and quantity.food_state != "unknown":
        return f"mass:{quantity.food_state}", quantity.grams_lower, quantity.grams_upper
    if quantity.kind == "servings":
        return f"servings:{quantity.serving_evidence_id}", quantity.count, quantity.count
    if quantity.kind == "recipe_fraction":
        value = quantity.batch_multiplier * quantity.share
        return "recipe_fraction", value, value
    if quantity.kind == "cooked_mass":
        portion = (
            NutrientRange(lower=quantity.consumed_grams, upper=quantity.consumed_grams)
            .divided(quantity.batch_cooked_grams)
            .scaled(quantity.batch_multiplier)
        )
        return "recipe_fraction", portion.lower, portion.upper
    return None


def date_range(start: date, end: date) -> list[date]:
    if start > end or (end - start).days >= 366:
        raise ValueError("Analysis ranges must contain between 1 and 366 days.")
    return [start + timedelta(days=index) for index in range((end - start).days + 1)]


def daily_analysis(
    start: date,
    end: date,
    logs: list[DietLog],
    plans: list[PlannedMeal],
    declarations: list[DietDayDeclaration],
    targets: list[NutritionTarget],
) -> list[DailyAnalysis]:
    result = []
    statuses = {item.local_date: item.status for item in declarations}
    for day in date_range(start, end):
        actual_logs = [
            log for log in logs if log.deleted_at is None and log.temporal.local_date == day
        ]
        planned = [meal for meal in plans if meal.deleted_at is None and meal.local_date == day]
        actual = summarize_nutrition(
            (item.nutrition_snapshot for log in actual_logs for item in log.items),
            record_count=len(actual_logs),
            day_status=statuses.get(day, "open"),
        )
        planned_summary = summarize_nutrition(
            (item.nutrition_snapshot for meal in planned for item in meal.items),
            record_count=len(planned),
        )
        target = target_on(targets, day)
        result.append(
            DailyAnalysis(
                local_date=day,
                actual=actual,
                planned=planned_summary,
                target_ref=ResourceRef(kind="target", id=target.id, version=target.version)
                if target
                else None,
                target_comparison={
                    name: compare_goal(
                        actual.nutrients[name],
                        getattr(target.goals, name) if target else None,
                        day_status=actual.day_status,
                    )
                    for name in GOAL_NUTRIENTS
                },
                record_refs=[
                    ResourceRef(kind="log", id=log.id, version=log.version) for log in actual_logs
                ],
                plan_refs=[
                    ResourceRef(kind="planned_meal", id=meal.id, version=meal.version)
                    for meal in planned
                ],
            )
        )
    return result


def weekly_analysis(days: list[DailyAnalysis]) -> list[WeeklyAnalysis]:
    if len({day.local_date for day in days}) != len(days):
        raise ValueError("Weekly analysis cannot contain duplicate dates.")
    grouped: dict[date, list[DailyAnalysis]] = defaultdict(list)
    for day in sorted(days, key=lambda item: item.local_date):
        grouped[day.local_date - timedelta(days=day.local_date.weekday())].append(day)
    result = []
    for week_start, covered in sorted(grouped.items()):
        if (covered[-1].local_date - covered[0].local_date).days + 1 != len(covered):
            raise ValueError("Weekly covered dates must be contiguous.")
        recorded = [day for day in covered if day.actual.record_count]
        complete = [day for day in covered if day.actual.day_status in ("complete", "no_intake")]
        nutrients: dict[Nutrient, WeeklyNutrient] = {}
        for name in NUTRIENTS:
            known = [day.actual.nutrients[name].known_subtotal for day in covered]
            recorded_values = [
                day.actual.nutrients[name].total
                for day in recorded
                if day.actual.nutrients[name].complete
            ]
            complete_values = [
                day.actual.nutrients[name].total
                for day in complete
                if day.actual.nutrients[name].complete
            ]
            nutrients[name] = WeeklyNutrient(
                known_subtotal=_sum(known),
                recorded_days=len(recorded),
                computable_recorded_days=len(recorded_values),
                complete_days=len(complete),
                computable_complete_days=len(complete_values),
                recorded_day_average=_average(recorded_values, len(recorded)),
                complete_day_average=_average(complete_values, len(complete)),
                seven_day_average=(
                    _average(complete_values, 7) if len(covered) == len(complete) == 7 else None
                ),
                missing_reasons=sorted(
                    {
                        reason
                        for day in covered
                        for reason in day.actual.nutrients[name].missing_reasons
                    }
                    | ({"week_incomplete"} if len(complete_values) != 7 else set())
                ),
            )
        result.append(
            WeeklyAnalysis(
                week_start=week_start,
                covered_start_date=covered[0].local_date,
                covered_end_date=covered[-1].local_date,
                covered_days=len(covered),
                recorded_days=len(recorded),
                complete_days=len(complete),
                nutrients=nutrients,
                days=covered,
            )
        )
    return result


def _sum(values: list[NutrientRange]) -> NutrientRange:
    return NutrientRange(
        lower=sum((value.lower for value in values), Decimal(0)),
        upper=sum((value.upper for value in values), Decimal(0)),
    )


def _average(values: list[NutrientRange | None], denominator: int) -> NutrientRange | None:
    if denominator == 0 or len(values) != denominator or any(value is None for value in values):
        return None
    return _sum([value for value in values if value is not None]).divided(Decimal(denominator))
