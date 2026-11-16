from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from nutripilot.domain.diet import (
    NUTRIENTS,
    DecimalValue,
    DietError,
    FoodEvidenceSnapshot,
    NutrientRange,
    NutrientSnapshot,
    NutritionSnapshot,
    Quantity,
    SubmissionAnchor,
    TemporalFields,
    calculate_nutrition,
    compare_goal,
    content_hash,
    resolve_occurred_at,
    summarize_nutrition,
)

QUANTITY = TypeAdapter(Quantity)


def fixture_evidence(**changes: Any) -> FoodEvidenceSnapshot:
    """Synthetic arithmetic fixture, never a catalog or nutritional seed."""
    nutrients = NutritionSnapshot(
        **{
            name: NutrientSnapshot(
                value=NutrientRange(lower="100", upper="120"),
                basis="user_reported",
                evidence_ids=["fixture-evidence"],
            )
            for name in NUTRIENTS
        }
    )
    data: dict[str, Any] = {
        "evidence_id": "fixture-evidence",
        "source_kind": "user_report",
        "source_ref": "fixture",
        "source_version": "v1",
        "retrieved_at": datetime(2026, 11, 12, tzinfo=UTC),
        "food_state": "cooked",
        "basis_quantity": "per_100g",
        "nutrients": nutrients,
        "calculation_note": "Synthetic test values.",
    }
    data.update(changes)
    return FoodEvidenceSnapshot.model_validate(data)


@pytest.mark.parametrize(
    "value",
    [
        0,
        1.2,
        True,
        None,
        "NaN",
        "Infinity",
        "-1",
        "1e2",
        "01",
        "1.0000001",
        "1000000000",
        " 1",
        "1 ",
        ".5",
        "1.",
    ],
)
def test_invalid_decimals(value: object) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(DecimalValue).validate_python(value)


def test_decimal_serialization_is_stable_and_never_float() -> None:
    first = NutrientRange(lower="1.200000", upper="2.00")
    second = NutrientRange(lower="1.2", upper="2")
    assert first.model_dump(mode="json") == {"lower": "1.2", "upper": "2"}
    assert content_hash(first) == content_hash(second)


@pytest.mark.parametrize(
    ("quantity", "low", "high"),
    [
        ({"kind": "mass", "grams": "150", "food_state": "cooked"}, "150", "180"),
        (
            {
                "kind": "mass_range",
                "grams_lower": "100",
                "grams_upper": "150",
                "food_state": "cooked",
            },
            "100",
            "180",
        ),
    ],
)
def test_mass_interval_arithmetic(quantity: dict[str, str], low: str, high: str) -> None:
    result = calculate_nutrition(QUANTITY.validate_python(quantity), fixture_evidence())
    assert result.calories_kcal.value == NutrientRange(lower=low, upper=high)
    assert result.protein_g.basis == "user_reported"


@pytest.mark.parametrize(
    ("quantity", "reason"),
    [
        ({"kind": "unknown", "description": "half a plate"}, "quantity_unknown"),
        ({"kind": "mass", "grams": "150", "food_state": "raw"}, "food_state_mismatch"),
        ({"kind": "mass", "grams": "150", "food_state": "unknown"}, "food_state_unknown"),
        (
            {
                "kind": "servings",
                "count": "1",
                "serving_evidence_id": "invented",
            },
            "serving_evidence_mismatch",
        ),
        (
            {
                "kind": "recipe_fraction",
                "batch_multiplier": "1",
                "share": "0.5",
            },
            "recipe_batch_unavailable",
        ),
    ],
)
def test_no_unjustified_conversion(quantity: dict[str, str], reason: str) -> None:
    result = calculate_nutrition(QUANTITY.validate_python(quantity), fixture_evidence())
    assert result.calories_kcal.value is None
    assert result.calories_kcal.missing_reasons == [reason]


def test_serving_uses_frozen_conversion_evidence() -> None:
    quantity = QUANTITY.validate_python(
        {
            "kind": "servings",
            "count": "2",
            "serving_evidence_id": "fixture-serving",
        }
    )
    evidence = fixture_evidence(serving_evidence_id="fixture-serving", serving_grams="30")
    result = calculate_nutrition(quantity, evidence)
    assert result.calories_kcal.value == NutrientRange(lower="60", upper="72")
    per_serving = fixture_evidence(
        basis_quantity="per_serving",
        serving_evidence_id="fixture-serving",
    )
    assert calculate_nutrition(quantity, per_serving).calories_kcal.value == NutrientRange(
        lower="200",
        upper="240",
    )


def test_recipe_uses_consumed_fraction_not_ingredient_weight() -> None:
    evidence = fixture_evidence(basis_quantity="recipe_batch")
    fraction = QUANTITY.validate_python(
        {
            "kind": "recipe_fraction",
            "batch_multiplier": "2",
            "share": "0.5",
        }
    )
    cooked = QUANTITY.validate_python(
        {
            "kind": "cooked_mass",
            "batch_multiplier": "2",
            "consumed_grams": "100",
            "batch_cooked_grams": "400",
        }
    )
    assert (
        calculate_nutrition(fraction, evidence).calories_kcal.value
        == evidence.nutrients.calories_kcal.value
    )
    assert calculate_nutrition(cooked, evidence).calories_kcal.value == NutrientRange(
        lower="50",
        upper="60",
    )
    mass = QUANTITY.validate_python({"kind": "mass", "grams": "100", "food_state": "cooked"})
    assert calculate_nutrition(mass, evidence).calories_kcal.value is None


def test_recurring_ratio_rounds_outward() -> None:
    evidence = fixture_evidence(basis_quantity="recipe_batch")
    quantity = QUANTITY.validate_python(
        {
            "kind": "cooked_mass",
            "batch_multiplier": "1",
            "consumed_grams": "1",
            "batch_cooked_grams": "3",
        }
    )
    assert calculate_nutrition(quantity, evidence).calories_kcal.value == NutrientRange(
        lower="33.333333",
        upper="40",
    )


def test_unknown_nutrient_is_not_zero_and_confirmed_day_does_not_fill_it() -> None:
    known = fixture_evidence().nutrients
    unknown = NutritionSnapshot.unknown("no_label")
    summary = summarize_nutrition([known, unknown], record_count=1, day_status="complete")
    calories = summary.nutrients["calories_kcal"]
    assert summary.state == "partial"
    assert calories.total is None
    assert calories.known_subtotal == NutrientRange(lower="100", upper="120")
    assert calories.known_item_count == 1
    assert calories.total_item_count == 2
    assert (
        compare_goal(
            calories,
            NutrientRange(lower="100", upper="200"),
            day_status="complete",
        ).reason
        == "nutrition_incomplete"
    )


def test_empty_day_and_explicit_no_intake_are_different() -> None:
    empty = summarize_nutrition([], record_count=0)
    zero = summarize_nutrition([], record_count=0, day_status="no_intake")
    assert empty.state == "no_records"
    assert empty.nutrients["calories_kcal"].total is None
    assert zero.state == "no_intake"
    assert zero.nutrients["calories_kcal"].total == NutrientRange(lower="0", upper="0")
    with pytest.raises(DietError, match="no-intake"):
        summarize_nutrition([fixture_evidence().nutrients], record_count=1, day_status="no_intake")


@pytest.mark.parametrize(
    ("goal_low", "goal_high", "expected"),
    [
        ("90", "130", "within"),
        ("130", "150", "below"),
        ("10", "90", "above"),
        ("110", "130", "overlap"),
    ],
)
def test_goal_comparison_uses_intervals(
    goal_low: str,
    goal_high: str,
    expected: str,
) -> None:
    total = summarize_nutrition(
        [fixture_evidence().nutrients],
        record_count=1,
    ).nutrients["calories_kcal"]
    result = compare_goal(
        total,
        NutrientRange(lower=goal_low, upper=goal_high),
        day_status="complete",
    )
    assert result.status == expected
    assert result.difference is not None
    assert result.difference.lower == Decimal(100) - Decimal(goal_high)
    assert (
        compare_goal(
            total,
            NutrientRange(lower=goal_low, upper=goal_high),
            day_status="open",
        ).status
        == "not_comparable"
    )


def test_model_source_cannot_be_upgraded_to_user_report() -> None:
    with pytest.raises(ValidationError, match="remain marked"):
        fixture_evidence(
            source_kind="model_estimate",
            model_name="fixture",
            prompt_version="v1",
        )


def test_submission_date_anchor_survives_midnight_and_serialization() -> None:
    anchor = SubmissionAnchor.capture(datetime(2026, 11, 11, 16, 30, tzinfo=UTC), "Asia/Shanghai")
    assert anchor.anchor_local_date == date(2026, 11, 12)
    restored = SubmissionAnchor.model_validate_json(anchor.model_dump_json())
    assert restored.relative_date("yesterday") == date(2026, 11, 11)
    with pytest.raises(ValidationError, match="Anchor date"):
        SubmissionAnchor(
            submitted_at=anchor.submitted_at,
            timezone=anchor.timezone,
            anchor_local_date=date(2026, 11, 11),
        )


def test_no_time_is_not_fabricated() -> None:
    temporal = TemporalFields(local_date="2026-11-12")  # type: ignore[arg-type]
    assert resolve_occurred_at(temporal, "Asia/Shanghai") is None


def test_dst_repeated_and_nonexistent_local_times() -> None:
    repeated = {"local_date": "2026-11-01", "local_time": "01:30"}
    with pytest.raises(DietError, match="explicit UTC offset"):
        resolve_occurred_at(TemporalFields.model_validate(repeated), "America/New_York")
    chosen = TemporalFields.model_validate({**repeated, "utc_offset_minutes": -300})
    assert resolve_occurred_at(chosen, "America/New_York") == datetime(
        2026,
        11,
        1,
        6,
        30,
        tzinfo=UTC,
    )
    with pytest.raises(DietError):
        resolve_occurred_at(
            TemporalFields.model_validate(
                {
                    "local_date": "2026-03-08",
                    "local_time": "02:30",
                }
            ),
            "America/New_York",
        )
    with pytest.raises(DietError):
        resolve_occurred_at(
            TemporalFields.model_validate(
                {
                    "local_date": "2026-11-12",
                    "local_time": "12:00",
                    "utc_offset_minutes": 0,
                }
            ),
            "Asia/Shanghai",
        )
