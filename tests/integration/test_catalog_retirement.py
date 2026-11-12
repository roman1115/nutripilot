from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from apps.nutrition_mcp.service import NutritionMCPService
from fastapi.testclient import TestClient

from nutripilot.api.app import create_app
from nutripilot.application import build_container
from nutripilot.application.chat_service import InvalidConfirmationStateError
from nutripilot.config import Settings
from nutripilot.infrastructure.memory import InMemoryDataStore, load_seed_store
from nutripilot.model_gateway import FakeModelGateway
from nutripilot.tools.errors import ToolRuntimeError
from nutripilot.tools.local.models import (
    CreateMealPlanArgs,
    MealPlan,
    RecordFeedbackArgs,
    UpdateUserProfileArgs,
)
from nutripilot.tools.mcp.models import GetNutritionMCPArgs


@pytest.mark.asyncio
async def test_retired_ids_and_names_cannot_be_used_for_new_nutrition_queries() -> None:
    service = NutritionMCPService.from_seed_data("data")
    for request in (
        GetNutritionMCPArgs(meal_id=2),
        GetNutritionMCPArgs(name="鸡胸藜麦能量碗"),
        GetNutritionMCPArgs(name="鸡胸肉藜麦能量碗"),
    ):
        with pytest.raises(ToolRuntimeError) as caught:
            await service.get_nutrition(request)
        assert caught.value.code == "MEAL_NOT_FOUND"
    assert len(await service.store.list_meals()) == 100
    assert (await service.store.get_meal(2)).name == "鸡胸藜麦能量碗"
    assert [meal.id for meal in await service.store.get_meals([2, 1004])] == [2, 1004]


@pytest.mark.asyncio
async def test_retired_meals_cannot_enter_new_plans_but_old_idempotent_replays_work() -> None:
    store = load_seed_store("data")
    legacy = (await store.get_meal(2)).model_copy(update={"is_active": True})
    old_store = InMemoryDataStore(meals=[legacy])
    request = CreateMealPlanArgs.model_validate(
        {
            "title": "历史午餐",
            "start_date": "2026-10-20",
            "items": [{"meal_id": 2, "meal_type": "lunch", "meal_date": "2026-10-20"}],
        }
    )
    existing = await old_store.create_meal_plan(23, request, "original")
    old_store._meals[2] = legacy.model_copy(update={"is_active": False})
    assert (await old_store.create_meal_plan(23, request, "original")).id == existing.id
    with pytest.raises(ToolRuntimeError) as caught:
        await old_store.create_meal_plan(23, request, "new")
    assert caught.value.code == "MEAL_RETIRED"
    assert len(await old_store.list_plans(23)) == 1


def test_historical_plan_names_nutrition_feedback_and_user_isolation_survive() -> None:
    container = build_container(settings=Settings(_env_file=None))
    request = CreateMealPlanArgs.model_validate(
        {
            "title": "旧版餐单",
            "start_date": "2026-10-20",
            "items": [{"meal_id": 2, "meal_type": "lunch", "meal_date": "2026-10-20"}],
        }
    )
    container.data_store._plans["historical"] = MealPlan(
        id="historical",
        user_id=23,
        created_at=datetime.now(UTC),
        **request.model_dump(),
    )
    with TestClient(create_app(container)) as client:
        response = client.get("/api/v1/meal-plans", headers={"X-User-ID": "23"})
        item = response.json()[0]["items"][0]
        assert item["meal_name"] == "鸡胸藜麦能量碗"
        assert item["calories"] == 516
        assert item["is_active"] is False
        assert item["nutrition"]["source_status"] == "unverified_estimate"
        assert not client.get("/api/v1/meal-plans", headers={"X-User-ID": "24"}).json()
        payload = RecordFeedbackArgs(meal_id=2, feedback_type="like").model_dump()
        first = client.post(
            "/api/v1/feedback",
            json=payload,
            headers={"X-User-ID": "23", "Idempotency-Key": "legacy-feedback"},
        )
        replay = client.post(
            "/api/v1/feedback",
            json=payload,
            headers={"X-User-ID": "23", "Idempotency-Key": "legacy-feedback"},
        )
        assert first.status_code == 200
        assert replay.json()["id"] == first.json()["id"]
        assert replay.json()["meal_id"] == 2


@pytest.mark.asyncio
async def test_pending_plan_replacement_preserves_scope_and_invalidates_old_confirmation() -> None:
    container = build_container(settings=Settings(_env_file=None))
    first = await container.chat_service.chat(
        user_id=23,
        thread_id="replace-plan",
        message="从2026-11-02开始制定一日三餐餐单",
    )
    original = first["pending_tool_calls"][0]
    original_ids = {item["meal_id"] for item in original["arguments"]["items"]}
    assert len(original_ids) == 3
    replacement = await container.chat_service.chat(
        user_id=23,
        thread_id="replace-plan",
        message="换一批菜品，重新制定餐单",
    )
    assert replacement["phase"] == "AWAITING_CONFIRMATION"
    pending = replacement["pending_tool_calls"][0]
    assert pending["arguments"]["start_date"] == date(2026, 11, 2).isoformat()
    items = pending["arguments"]["items"]
    assert len(items) == 3
    assert not original_ids.intersection(item["meal_id"] for item in items)
    assert all(item["meal_id"] >= 1000 for item in items)
    assert not await container.data_store.list_plans(23)
    with pytest.raises(InvalidConfirmationStateError):
        await container.chat_service.confirm(
            thread_id="replace-plan",
            user_id=23,
            approved=True,
            confirmation_id=original["call_id"],
        )
    confirmed = await container.chat_service.confirm(
        thread_id="replace-plan",
        user_id=23,
        approved=True,
        confirmation_id=pending["call_id"],
    )
    assert confirmed["phase"] == "COMPLETED"
    saved = (await container.data_store.list_plans(23))[0]
    assert [item.model_dump(mode="json") for item in saved.items] == items
    assert saved.quality == replacement["plan_quality"]


@pytest.mark.asyncio
async def test_plan_candidate_coverage_keeps_allergy_constraints_in_every_search() -> None:
    container = build_container(settings=Settings(_env_file=None))
    state = await container.chat_service.chat(
        user_id=23,
        message="制定一日三餐餐单，我对鸡蛋过敏",
    )
    assert state["requires_confirmation"]
    searches = [r for r in state["tool_results"] if r["tool_name"] == "search_meals"]
    assert len(searches) == 3
    assert all(
        "鸡蛋" not in hit["allergens"] for search in searches for hit in search["data"]["hits"]
    )
    for item in state["pending_tool_calls"][0]["arguments"]["items"]:
        meal = await container.data_store.get_meal(item["meal_id"])
        assert meal.is_active and meal.recipe_id is not None
        assert item["meal_type"] in meal.meal_types
        assert "鸡蛋" not in meal.allergens


@pytest.mark.parametrize(
    "requested_types", [("breakfast", "lunch", "dinner"), ("lunch",), (None,)]
)
@pytest.mark.asyncio
async def test_plan_searches_cover_each_meal_once_without_multiplying_calls(
    requested_types,
) -> None:
    gateway = FakeModelGateway()

    def planner(payload):
        output = FakeModelGateway._planner(payload)
        if output["tool_calls"] and output["tool_calls"][0]["name"] == "search_meals":
            base = output["tool_calls"][0]
            output["tool_calls"] = [
                {
                    **base,
                    "call_id": f"search-{index}",
                    "arguments": {
                        **base["arguments"],
                        "query": f"菜品-{meal_type}",
                        "meal_type": meal_type,
                    },
                }
                for index, meal_type in enumerate(requested_types)
            ]
        return output

    gateway.register_handler("planner", planner)
    container = build_container(settings=Settings(_env_file=None), gateway=gateway)

    state = await container.chat_service.chat(
        user_id=23, message="制定一日三餐餐单，我对鸡蛋过敏"
    )

    assert state["phase"] == "AWAITING_CONFIRMATION"
    searches = [r for r in state["tool_results"] if r["tool_name"] == "search_meals"]
    assert len(searches) == 3
    assert all(result["status"] == "success" for result in searches)
    assert not await container.data_store.list_plans(23)
    for search in searches:
        assert all("鸡蛋" not in hit["allergens"] for hit in search["data"]["hits"])
    assert len(state["pending_tool_calls"][0]["arguments"]["items"]) == 3


@pytest.mark.asyncio
async def test_unknown_dietary_tags_do_not_create_a_fake_plan_or_relax_constraints() -> None:
    container = build_container(settings=Settings(_env_file=None))
    state = await container.chat_service.chat(user_id=23, message="帮我做七天素食餐单")
    assert state["phase"] == "FAILED"
    assert state["error"]["code"] == "PLAN_CANDIDATES_INSUFFICIENT"
    assert not state["requires_confirmation"]
    assert not await container.data_store.list_plans(23)
    assert "来源菜谱不足" in state["final_response"]["message"]


@pytest.mark.asyncio
async def test_retirement_between_preview_and_confirmation_blocks_the_write() -> None:
    container = build_container(settings=Settings(_env_file=None))
    waiting = await container.chat_service.chat(
        user_id=23,
        thread_id="stale-preview",
        message="制定一日午餐餐单",
    )
    meal_id = waiting["pending_tool_calls"][0]["arguments"]["items"][0]["meal_id"]
    meal = await container.data_store.get_meal(meal_id)
    container.data_store._meals[meal_id] = meal.model_copy(update={"is_active": False})
    final = await container.chat_service.confirm(
        user_id=23,
        thread_id="stale-preview",
        approved=True,
    )
    assert final["phase"] == "FAILED"
    assert final["error"]["code"] == "MEAL_RETIRED"
    assert "已退役" in final["final_response"]["message"]
    assert not await container.data_store.list_plans(23)


@pytest.mark.asyncio
async def test_model_cannot_turn_personal_profile_targets_into_whole_dish_filters() -> None:
    class WrongTargetGateway(FakeModelGateway):
        @staticmethod
        def _planner(payload):
            output = FakeModelGateway._planner(payload)
            for call in output["tool_calls"]:
                if call["name"] == "search_meals":
                    call["arguments"].update(
                        min_protein_g=900,
                        max_calories_kcal=5000,
                        max_price_cny=20,
                        max_prep_minutes=20,
                    )
            return output

    container = build_container(
        settings=Settings(_env_file=None),
        gateway=WrongTargetGateway(),
    )
    await container.data_store.update_user_profile(
        23,
        UpdateUserProfileArgs(daily_calorie_target=5000, daily_protein_target_g=900),
    )
    state = await container.chat_service.chat(user_id=23, message="制定一日三餐餐单")
    assert state["requires_confirmation"]
    assert state["plan_quality"]["nutrition_basis"] == "whole_recipe"
    assert len(state["pending_tool_calls"][0]["arguments"]["items"]) == 3


def test_preview_and_saved_api_preserve_whole_recipe_ranges_and_sources() -> None:
    container = build_container(settings=Settings(_env_file=None))
    with TestClient(create_app(container)) as client:
        waiting = client.post(
            "/api/v1/chat",
            json={
                "user_id": 23,
                "thread_id": "ranges-plan",
                "message": "制定一周三餐餐单",
            },
        ).json()
        action = waiting["pending_action"]
        preview = action["meal_preview"]
        assert len(preview) == 21
        assert all(item["nutrition"]["source_uri"] for item in preview)
        assert any(item["nutrition"]["nutrient_ranges"] for item in preview)
        assert all("整道菜" in item["nutrition"]["serving_size"] for item in preview)
        assert waiting["plan_quality"]["nutrition_basis"] == "whole_recipe"
        assert (
            client.post(
                "/api/v1/chat/ranges-plan/confirm",
                json={
                    "user_id": 23,
                    "approved": True,
                    "confirmation_id": action["id"],
                },
            ).json()["phase"]
            == "COMPLETED"
        )
        saved = client.get("/api/v1/meal-plans", headers={"X-User-ID": "23"}).json()[0]
        assert saved["quality"] == waiting["plan_quality"]
        assert [item["nutrition"] for item in saved["items"]] == [
            item["nutrition"] for item in preview
        ]
