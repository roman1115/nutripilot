from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from nutripilot.domain.diet import (
    NUTRIENTS,
    FoodEvidenceSnapshot,
    MassQuantity,
    NutrientRange,
    NutrientSnapshot,
    NutritionSnapshot,
    calculate_nutrition,
    convert_label_range,
)
from nutripilot.domain.diet_analysis import daily_analysis, diet_deviation, weekly_analysis
from nutripilot.domain.diet_records import (
    DietDayDeclaration,
    DietItem,
    DietLog,
    PlannedMeal,
    ResourceRef,
)

NOW = datetime(2026, 11, 12, 4, tzinfo=UTC)


def record(day: date, *, missing: bool = False) -> DietLog:
    evidence = FoodEvidenceSnapshot(
        evidence_id="fixture",
        source_kind="user_report",
        source_ref="fixture",
        source_version="1",
        retrieved_at=NOW,
        food_state="as_sold",
        basis_quantity="per_100g",
        nutrients=NutritionSnapshot(
            **{
                name: NutrientSnapshot(
                    missing_reasons=["not_reported"],
                )
                if missing and name == "protein_g"
                else NutrientSnapshot(
                    value=NutrientRange(lower="10", upper="20"),
                    basis="user_reported",
                    evidence_ids=["fixture"],
                )
                for name in NUTRIENTS
            }
        ),
        calculation_note="Synthetic test data.",
    )
    quantity = MassQuantity(kind="mass", grams="100", food_state="as_sold")
    return DietLog.model_validate(
        {
            "id": uuid4(),
            "user_id": 1,
            "created_at": NOW,
            "updated_at": NOW,
            "temporal": {"local_date": day, "meal_type": "lunch"},
            "timezone": "Asia/Shanghai",
            "occurred_at": None,
            "channel": "manual",
            "items": [
                DietItem.model_validate(
                    {
                        "id": uuid4(),
                        "client_item_key": "fixture",
                        "position": 0,
                        "food_ref": {"kind": "personal_food", "id": uuid4(), "version": 1},
                        "name_snapshot": "fixture",
                        "quantity": quantity,
                        "evidence_snapshot": evidence,
                        "nutrition_snapshot": calculate_nutrition(quantity, evidence),
                    }
                )
            ],
        }
    )


def test_week_averages_use_explicit_denominators_without_zero_filling() -> None:
    start, end = date(2026, 11, 9), date(2026, 11, 15)
    days = daily_analysis(
        start,
        end,
        [record(start)],
        [],
        [
            DietDayDeclaration(local_date=start, status="complete", version=1),
            DietDayDeclaration(local_date=date(2026, 11, 10), status="no_intake", version=1),
        ],
        [],
    )
    week = weekly_analysis(days)[0]
    assert week.recorded_days == 1 and week.complete_days == 2
    nutrition = week.nutrients["calories_kcal"]
    assert nutrition.recorded_day_average == NutrientRange(lower="10", upper="20")
    assert nutrition.complete_day_average == NutrientRange(lower="5", upper="10")
    assert nutrition.seven_day_average is None
    assert days[2].actual.state == "no_records"
    assert days[2].actual.nutrients["calories_kcal"].total is None
    assert days[0].target_comparison["calories_kcal"].reason == "target_unknown"


def test_unknown_nutrient_cannot_become_a_complete_week_average() -> None:
    day = date(2026, 11, 12)
    values = daily_analysis(
        day,
        day,
        [record(day, missing=True)],
        [],
        [
            DietDayDeclaration(local_date=day, status="complete", version=1),
        ],
        [],
    )
    week = weekly_analysis(values)[0]
    assert week.nutrients["protein_g"].complete_day_average is None
    assert week.nutrients["protein_g"].recorded_day_average is None
    assert week.nutrients["protein_g"].known_subtotal == NutrientRange(lower="0", upper="0")
    assert week.nutrients["calories_kcal"].complete_day_average is not None
    assert week.covered_days == 1 and week.week_start == date(2026, 11, 9)


def test_complete_seven_no_intake_days_are_explicit_zero() -> None:
    declarations = [
        DietDayDeclaration(local_date=date(2026, 11, day), status="no_intake", version=1)
        for day in range(9, 16)
    ]
    values = daily_analysis(date(2026, 11, 9), date(2026, 11, 15), [], [], declarations, [])
    week = weekly_analysis(values)[0]
    assert week.recorded_days == 0 and week.complete_days == 7
    assert week.nutrients["calories_kcal"].seven_day_average == NutrientRange(lower="0", upper="0")
    assert week.nutrients["calories_kcal"].recorded_day_average is None


def test_week_bucket_respects_monday_and_query_clipping() -> None:
    values = daily_analysis(date(2026, 11, 15), date(2026, 11, 17), [], [], [], [])
    weeks = weekly_analysis(values)
    assert [week.covered_days for week in weeks] == [1, 2]
    assert [week.week_start for week in weeks] == [date(2026, 11, 9), date(2026, 11, 16)]


def test_label_units_and_density_do_not_invent_volume_mass_conversion() -> None:
    assert convert_label_range(
        NutrientRange(lower="418.4", upper="836.8"),
        from_unit="kJ",
        to_unit="kcal",
    ) == NutrientRange(lower="100", upper="200")
    assert convert_label_range(
        NutrientRange(lower="1", upper="2"),
        from_unit="g",
        to_unit="mg",
    ) == NutrientRange(lower="1000", upper="2000")
    assert convert_label_range(
        NutrientRange(lower="10", upper="20"),
        from_unit="ml",
        to_unit="g",
        density_grams_per_ml=NutrientRange(lower="0.8", upper="1.2"),
    ) == NutrientRange(lower="8", upper="24")
    with pytest.raises(ValueError):
        convert_label_range(NutrientRange.exact(Decimal(10)), from_unit="ml", to_unit="g")


def test_plan_deviation_uses_source_links_not_names_and_includes_other_dates() -> None:
    day = date(2026, 11, 12)
    planned_record = record(day)
    plan = PlannedMeal(
        id=uuid4(),
        user_id=1,
        plan_id="legacy-plan",
        local_date=day,
        meal_type="lunch",
        items=planned_record.items,
        created_at=NOW,
        updated_at=NOW,
    )
    linked = planned_record.model_copy(
        update={
            "id": uuid4(),
            "source_planned_meal_id": plan.id,
            "items": [
                planned_record.items[0].model_copy(
                    update={
                        "id": uuid4(),
                        "source_planned_item_id": planned_record.items[0].id,
                    }
                )
            ],
        }
    )
    unlinked = planned_record.model_copy(update={"id": uuid4()})
    next_day = linked.model_copy(
        update={
            "id": uuid4(),
            "temporal": linked.temporal.model_copy(update={"local_date": date(2026, 11, 13)}),
        }
    )
    days = daily_analysis(
        day,
        day,
        [linked, unlinked],
        [plan],
        [DietDayDeclaration(local_date=day, status="complete", version=1)],
        [],
    )
    deviation = diet_deviation(
        days,
        [plan],
        [linked, unlinked, next_day],
        {plan.plan_id: ResourceRef(kind="plan", id=plan.plan_id, version=1)},
    )[0]
    assert deviation.analysis.actual.record_count == 2
    assert deviation.plan_actual_difference["calories_kcal"].model_dump(mode="json") == {
        "lower": "0",
        "upper": "30",
    }
    assert deviation.execution[0].recorded_count == 2
    assert len(deviation.execution[0].actual_refs) == 2
    assert deviation.execution[0].items[0].quantity_difference.model_dump(mode="json") == {
        "lower": "100",
        "upper": "100",
    }
    open_days = daily_analysis(day, day, [linked], [plan], [], [])
    unknown = diet_deviation(
        open_days,
        [plan],
        [linked],
        {plan.plan_id: ResourceRef(kind="plan", id=plan.plan_id, version=1)},
    )[0]
    assert unknown.plan_actual_difference["calories_kcal"] is None
    assert unknown.comparison_reasons["calories_kcal"] == "day_not_complete"
