from __future__ import annotations

from datetime import date
from itertools import pairwise
from typing import Any

import pytest

from nutripilot.agent.contracts import PlanSpec
from nutripilot.agent.planning import PlanBuilder, PlanBuildError
from nutripilot.agent.slots import extract_plan_scope
from nutripilot.application import build_container
from nutripilot.config import Settings
from nutripilot.tools.local.models import UpdateUserProfileArgs


def candidates() -> list[dict[str, Any]]:
    return [
        {
            "meal_id": index,
            "meal_types": [meal_type],
            "ingredients": ["测试食材"],
            "allergens": [],
            "nutrition": {"calories_kcal": calories, "protein_g": protein},
        }
        for index, meal_type, calories, protein in [
            (1, "breakfast", 400, 20),
            (2, "breakfast", 420, 25),
            (3, "lunch", 600, 35),
            (4, "lunch", 580, 40),
            (5, "dinner", 500, 30),
            (6, "dinner", 520, 35),
        ]
    ]


def test_plan_uses_nutrition_targets_without_multiplying_servings() -> None:
    spec = PlanSpec(
        plan_range="one_week",
        daily_calorie_target=1500,
        daily_protein_target_g=90,
        enforce_targets=True,
    )
    builder = PlanBuilder()
    plan = builder.build(spec, candidates())
    quality = builder.assess(spec, plan, candidates())
    assert all(day["target_met"] is True for day in quality["daily_totals"])
    assert all(1350 <= day["calories_kcal"] <= 1650 for day in quality["daily_totals"])
    assert all(day["protein_g"] >= 90 for day in quality["daily_totals"])
    assert all(item.model_dump().get("servings") is None for item in plan.items)


def test_plan_alternates_meals_and_is_deterministic() -> None:
    spec = PlanSpec(plan_range="one_week")
    builder = PlanBuilder()
    plan = builder.build(spec, candidates())
    assert plan == builder.build(spec, candidates())
    for meal_type in spec.meal_types:
        ids = [item.meal_id for item in plan.items if item.meal_type == meal_type]
        assert all(left != right for left, right in pairwise(ids))
    assert len({item.meal_id for item in plan.items}) == 6


def test_plan_avoids_same_meal_in_two_slots_on_the_same_day() -> None:
    meals = candidates()
    meals[2]["meal_types"] = ["lunch", "dinner"]
    meals[4]["meal_types"] = ["lunch", "dinner"]
    plan = PlanBuilder().build(PlanSpec(plan_range="one_day"), meals)
    assert len({item.meal_id for item in plan.items}) == 3


def test_explicit_unreachable_target_fails_closed() -> None:
    spec = PlanSpec(daily_calorie_target=5000, enforce_targets=True)
    with pytest.raises(PlanBuildError) as error:
        PlanBuilder().build(spec, candidates())
    assert error.value.code == "PLAN_NUTRITION_TARGET_UNREACHABLE"


def test_profile_target_shortfall_is_visible_but_not_silently_fabricated() -> None:
    spec = PlanSpec.from_slots({}, profile={"daily_calorie_target": 5000})
    builder = PlanBuilder()
    plan = builder.build(spec, candidates())
    quality = builder.assess(spec, plan, candidates())
    assert any("未达到" in warning for warning in quality["warnings"])
    assert all(day["target_met"] is False for day in quality["daily_totals"])


def test_single_meal_plan_does_not_claim_full_day_target_met() -> None:
    spec = PlanSpec(
        meal_type="dinner",
        daily_calorie_target=2000,
        enforce_targets=True,
    )
    builder = PlanBuilder()
    quality = builder.assess(spec, builder.build(spec, candidates()), candidates())
    assert quality["scope"] == "selected_meals"
    assert all(day["target_met"] is None for day in quality["daily_totals"])
    assert any("仅包含指定餐次" in warning for warning in quality["warnings"])


def test_prewrite_rejects_plan_below_explicit_protein_target() -> None:
    builder = PlanBuilder()
    plan = builder.build(PlanSpec(plan_range="one_day"), candidates())
    with pytest.raises(PlanBuildError) as error:
        builder.validate_prewrite(
            spec=PlanSpec(
                plan_range="one_day",
                daily_protein_target_g=300,
                enforce_targets=True,
            ),
            request=plan,
            candidates=candidates(),
            allowed_meal_ids=set(range(1, 7)),
            declared_allergens=[],
            excluded_ingredients=[],
        )
    assert error.value.code == "PLAN_NUTRITION_TARGET_UNMET"


def test_missing_nutrition_cannot_pass_explicit_target_check() -> None:
    meals = candidates()
    for meal in meals:
        meal.pop("nutrition")
    with pytest.raises(PlanBuildError):
        PlanBuilder().build(
            PlanSpec(daily_calorie_target=1500, enforce_targets=True),
            meals,
        )


def test_daily_targets_are_extracted_separately_from_single_meal_filters() -> None:
    slots, missing = extract_plan_scope(
        "从2026-10-22制定一周三餐计划，每日1500千卡，每天蛋白质至少90克"
    )
    assert not missing
    assert slots["daily_calorie_target"] == 1500
    assert slots["daily_protein_target_g"] == 90
    assert slots["start_date"] == date(2026, 10, 22).isoformat()


@pytest.mark.asyncio
async def test_graph_blocks_unreachable_daily_target_without_pending_write() -> None:
    container = build_container(settings=Settings(_env_file=None))
    state = await container.chat_service.chat(
        user_id=22,
        message="制定一日三餐饮食计划，每日5000千卡，每天蛋白质至少100克",
    )
    assert state["phase"] == "FAILED"
    assert state["error"]["code"] == "PLAN_NUTRITION_TARGET_UNREACHABLE"
    assert not state["requires_confirmation"]
    assert not await container.data_store.list_plans(22)
    assert "无法同时满足" in state["final_response"]["message"]


@pytest.mark.asyncio
async def test_graph_plan_discloses_profile_target_shortfall_before_confirmation() -> None:
    container = build_container(settings=Settings(_env_file=None))
    await container.data_store.update_user_profile(
        22,
        UpdateUserProfileArgs(daily_calorie_target=5000),
    )
    state = await container.chat_service.chat(user_id=22, message="制定一周三餐饮食计划")
    assert state["phase"] == "AWAITING_CONFIRMATION"
    assert state["plan_quality"]["meal_count"] == 21
    assert any("未达到" in warning for warning in state["plan_quality"]["warnings"])
    items = state["pending_tool_calls"][0]["arguments"]["items"]
    assert len(items) == 21
    for day in {item["meal_date"] for item in items}:
        ids = [item["meal_id"] for item in items if item["meal_date"] == day]
        assert len(set(ids)) == 3


@pytest.mark.asyncio
async def test_confirmed_plan_retains_the_same_quality_report() -> None:
    container = build_container(settings=Settings(_env_file=None))
    waiting = await container.chat_service.chat(
        user_id=22,
        thread_id="quality-persist",
        message="制定一日三餐饮食计划，每日1500千卡，每天蛋白质至少80克",
    )
    assert waiting["requires_confirmation"]
    confirmed_items = waiting["pending_tool_calls"][0]["arguments"]["items"]
    await container.chat_service.confirm(thread_id="quality-persist", user_id=22, approved=True)
    plan = (await container.data_store.list_plans(22))[0]
    assert plan.quality == waiting["plan_quality"]
    assert [item.model_dump(mode="json") for item in plan.items] == confirmed_items


@pytest.mark.asyncio
async def test_clearing_a_numeric_profile_target_does_not_restore_the_old_value() -> None:
    container = build_container(settings=Settings(_env_file=None))
    await container.data_store.update_user_profile(
        22,
        UpdateUserProfileArgs(daily_calorie_target=2000, daily_protein_target_g=90),
    )
    await container.data_store.update_user_profile(
        22,
        UpdateUserProfileArgs(daily_calorie_target=None, daily_protein_target_g=None),
    )
    profile = await container.data_store.get_user_profile(22)
    assert profile.daily_calorie_target is None
    assert profile.daily_protein_target_g is None


@pytest.mark.asyncio
async def test_invalid_plan_date_is_rejected_without_model_cost() -> None:
    from nutripilot.model_gateway import FakeModelGateway

    gateway = FakeModelGateway()
    container = build_container(gateway=gateway, settings=Settings(_env_file=None))
    state = await container.chat_service.chat(
        user_id=22,
        message="从2026-02-30起制定一周晚餐计划",
    )
    assert state["phase"] == "CLARIFY"
    assert not gateway.call_history
    assert not state["fallback_events"]


def test_plan_canonicalisation_cannot_reintroduce_filtered_candidates() -> None:
    from nutripilot.agent.nodes.tool import _validate_meal_plan_call
    from nutripilot.agent.state import new_agent_state
    from nutripilot.tools import ToolCall

    meals = [
        {
            "meal_id": 1,
            "meal_types": ["dinner"],
            "ingredients": ["米饭"],
            "allergens": [],
            "nutrition": {"calories_kcal": 500, "protein_g": 20},
        },
        {
            "meal_id": 2,
            "meal_types": ["dinner"],
            "ingredients": ["花生"],
            "allergens": ["花生"],
            "nutrition": {"calories_kcal": 600, "protein_g": 30},
        },
    ]
    state = new_agent_state(message="一周晚餐计划", user_id=1)
    state["slots"] = {"meal_type": "dinner"}
    state["candidate_ids"] = [1]
    plan = PlanBuilder().build(PlanSpec(meal_type="dinner"), meals[:1])
    call = _validate_meal_plan_call(
        ToolCall(call_id="write", name="create_meal_plan", arguments=plan.model_dump(mode="json")),
        state=state,
        results=[
            {
                "tool_name": "search_meals",
                "status": "success",
                "data": {"hits": meals},
            }
        ],
        dietary_constraints={"allergens": ["花生"]},
    )
    assert {item["meal_id"] for item in call.arguments["items"]} == {1}


@pytest.mark.asyncio
async def test_explicit_three_meal_plan_clears_the_previous_turns_single_meal_slot() -> None:
    container = build_container(settings=Settings(_env_file=None))
    await container.chat_service.chat(
        user_id=22,
        thread_id="scope-change",
        message="推荐午餐",
    )
    plan = await container.chat_service.chat(
        user_id=22,
        thread_id="scope-change",
        message="制定一日三餐饮食计划",
    )
    assert plan["phase"] == "AWAITING_CONFIRMATION"
    assert len(plan["pending_tool_calls"][0]["arguments"]["items"]) == 3
    assert plan["plan_quality"]["scope"] == "full_day"
