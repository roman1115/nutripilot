from datetime import date

import pytest

from nutripilot.agent.contracts import PlanSpec
from nutripilot.agent.planning import PlanBuilder, PlanBuildError


def _candidates() -> list[dict[str, object]]:
    return [
        {
            "meal_id": 1,
            "meal_types": ["breakfast"],
            "ingredients": ["燕麦"],
            "allergens": [],
        },
        {
            "meal_id": 2,
            "meal_types": ["lunch", "dinner"],
            "ingredients": ["鸡胸肉"],
            "allergens": [],
        },
        {
            "meal_id": 3,
            "meal_types": ["lunch", "dinner"],
            "ingredients": ["香菇"],
            "allergens": [],
        },
    ]


def test_week_plan_builds_seven_days_and_three_meals_per_day() -> None:
    spec = PlanSpec(plan_range="one_week", start_date=date(2026, 7, 28))

    plan = PlanBuilder().build(spec, _candidates())

    assert len(plan.items) == 21
    assert len({item.meal_date for item in plan.items}) == 7
    for meal_date in {item.meal_date for item in plan.items}:
        assert {item.meal_type for item in plan.items if item.meal_date == meal_date} == {
            "breakfast",
            "lunch",
            "dinner",
        }


def test_one_day_and_single_meal_specs_control_item_count() -> None:
    builder = PlanBuilder()

    one_day = builder.build(
        PlanSpec(plan_range="one_day", start_date=date(2026, 7, 28)),
        _candidates(),
    )
    week_dinners = builder.build(
        PlanSpec(
            plan_range="one_week",
            meal_type="dinner",
            start_date=date(2026, 7, 28),
        ),
        _candidates(),
    )

    assert len(one_day.items) == 3
    assert len(week_dinners.items) == 7
    assert {item.meal_type for item in week_dinners.items} == {"dinner"}


def test_prewrite_rejects_candidate_outside_verified_set() -> None:
    builder = PlanBuilder()
    spec = PlanSpec(
        plan_range="one_week",
        meal_type="dinner",
        start_date=date(2026, 7, 28),
    )
    plan = builder.build(spec, _candidates())

    with pytest.raises(PlanBuildError, match="verified candidate set") as error:
        builder.validate_prewrite(
            spec=spec,
            request=plan,
            candidates=_candidates(),
            allowed_meal_ids={1},
            declared_allergens=[],
            excluded_ingredients=[],
        )

    assert error.value.code == "PLAN_CANDIDATE_NOT_ALLOWED"


def test_prewrite_rechecks_allergies_against_candidate_ingredients() -> None:
    builder = PlanBuilder()
    spec = PlanSpec(
        plan_range="one_week",
        meal_type="dinner",
        start_date=date(2026, 7, 28),
    )
    candidates = [_candidates()[2]]
    plan = builder.build(spec, candidates)

    with pytest.raises(PlanBuildError, match="allergy or avoidance") as error:
        builder.validate_prewrite(
            spec=spec,
            request=plan,
            candidates=candidates,
            allowed_meal_ids={3},
            declared_allergens=["香菇"],
            excluded_ingredients=["香菇"],
        )

    assert error.value.code == "PLAN_DIETARY_CONFLICT"
