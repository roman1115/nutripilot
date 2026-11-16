"""Resource deletion keeps independent intake and rejects stale restoration."""

from datetime import timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from tests.integration.test_diet_api import payload
from tests.integration.test_diet_purge_export import (
    NOW,
    OTHER,
    OWNER,
    approve,
    finished,
    headers,
)
from tests.integration.test_diet_purge_export import active as active
from tests.unit.test_diet_foods import food_input
from tests.unit.test_diet_plans import EVIDENCE, create_request, entry

from nutripilot.api.app import create_app
from nutripilot.application.personal_food_service import PersonalFoodMutation
from nutripilot.domain.diet import DietError
from nutripilot.domain.diet_records import ResourceRef


def test_food_target_purge_preview_is_owner_scoped_and_replay_safe(active) -> None:
    with TestClient(create_app(active)) as client:
        created = client.post(
            "/api/v1/diet/previews",
            headers=headers(),
            json={
                "operation": "personal_food_create",
                "arguments": {"action": "create", "data": food_input().model_dump(mode="json")},
            },
        ).json()
        food_id = approve(client, created).json()["result_refs"][0]["id"]
        assert (
            client.post(
                "/api/v1/diet/purge/previews", headers=headers(), json={"food_ids": [food_id]}
            ).status_code
            == 422
        )
        deletion = client.post(
            "/api/v1/diet/previews",
            headers=headers(1),
            json={
                "operation": "personal_food_delete",
                "arguments": {"action": "delete", "food_id": food_id, "base_version": 1},
            },
        ).json()
        assert approve(client, deletion).status_code == 200
        target = client.post(
            "/api/v1/diet/previews",
            headers=headers(),
            json={
                "operation": "target_set",
                "arguments": {
                    "action": "set",
                    "effective_date": "2026-11-12",
                    "goals": {"calories_kcal": {"lower": "1800", "upper": "2200"}},
                    "evidence": created.get(
                        "evidence",
                        [
                            {
                                "field": "goal",
                                "source_kind": "user_form",
                                "source_ref": "fixture",
                                "original_text": "fixture",
                                "operation": "set",
                                "status": "explicit",
                            }
                        ],
                    ),
                },
            },
        ).json()
        target_id = approve(client, target).json()["result_refs"][0]["id"]
        selection = {"food_ids": [food_id], "target_ids": [target_id]}
        assert (
            client.post(
                "/api/v1/diet/purge/previews",
                headers={**OTHER, "Idempotency-Key": "other"},
                json=selection,
            ).status_code
            == 404
        )
        preview = client.post("/api/v1/diet/purge/previews", headers=headers(), json=selection)
        assert preview.status_code == 201, preview.text
        body = preview.json()
        assert len(body["content"]["impact_notices"]) == 2
        assert "无目标" in body["content"]["impact_notices"][1]
        result = approve(client, body, purge=True)
        assert result.status_code == 202, result.text
        finished(client, result.json())
        assert approve(client, body, purge=True).json()["id"] == result.json()["id"]
        assert approve(client, created).status_code == 410
        assert approve(client, deletion).status_code == 410
        assert approve(client, target).status_code == 410
        assert (
            client.get(
                "/api/v1/diet/targets?start_date=2026-11-12&end_date=2026-11-12", headers=OWNER
            ).json()["items"]
            == []
        )
        assert (
            client.get("/api/v1/diet/personal-foods?include_deleted=true", headers=OWNER).json()[
                "items"
            ]
            == []
        )


@pytest.mark.asyncio
async def test_plan_purge_detaches_actual_intake_without_changing_nutrition(active) -> None:
    from nutripilot.domain.diet import SubmissionAnchor
    from nutripilot.tools.local.diet_models import CreateIntake

    diet = active.diet
    preview = await diet.plans.prepare(
        1, create_request(entry("lunch", "2026-11-12", "lunch")), idempotency_key="plan"
    )
    await diet.confirm(1, preview.id, version=1, approved_hash=preview.content_hash)
    plan = preview.content.after_plan
    meal = preview.content.after_meals[0]
    request = CreateIntake.model_validate(
        {
            "action": "create",
            "entry": {**entry("actual", "2026-11-12", "lunch")["entry"], "actuality": "actual"},
            "source": {
                "kind": "plan",
                "planned_meal_id": str(meal.id),
                "base_version": 1,
                "record_again": False,
            },
            "evidence": EVIDENCE,
        }
    )
    draft = await diet.intake.prepare(
        1, request, idempotency_key="actual", anchor=SubmissionAnchor.capture(NOW, "UTC")
    )
    saved = await diet.confirm(1, draft.id, version=1, approved_hash=draft.preview_hash)
    record_id = saved.result_refs[0].uuid
    async with diet.store.transaction(1, default_timezone="UTC") as tx:
        before = await tx.get_log(record_id)
    deletion = await diet.plans.prepare_archive_restore(
        1, plan.id, base_version=1, action="archive", idempotency_key="archive"
    )
    await diet.confirm(1, deletion.id, version=1, approved_hash=deletion.content_hash)
    async with diet.store.transaction(1, default_timezone="UTC") as tx:
        await tx.purge_resource(ResourceRef(kind="plan", id=plan.id, version=2), NOW)
        after = await tx.get_log(record_id)
        assert after and before
        assert after.source_planned_meal_id is None
        assert after.source_event_key == before.source_event_key
        assert after.items[0].nutrition_snapshot == before.items[0].nutrition_snapshot
        assert await tx.get_plan(plan.id) is None
        assert await tx.get_planned_meal(meal.id) is None
        assert await tx.get_tombstone("plan", plan.id)
        with pytest.raises(DietError, match="purged"):
            await tx.save_plan(plan)


@pytest.mark.asyncio
async def test_food_retention_has_exact_seven_day_boundary(active) -> None:
    diet = active.diet
    created = await diet.personal_foods.prepare(
        1, PersonalFoodMutation(action="create", data=food_input()), idempotency_key="food"
    )
    await diet.confirm(1, created.id, version=1, approved_hash=created.content_hash)
    food = created.content.after
    deleted = await diet.personal_foods.prepare(
        1,
        PersonalFoodMutation(action="delete", food_id=food.id, base_version=1),
        idempotency_key="delete",
    )
    await diet.confirm(1, deleted.id, version=1, approved_hash=deleted.content_hash)
    async with diet.store.transaction(1, default_timezone="UTC") as tx:
        assert await tx.expired_resources(NOW + timedelta(days=7, seconds=-1)) == []
        refs = await tx.expired_resources(NOW + timedelta(days=7))
        assert refs == [ResourceRef(kind="personal_food", id=food.id, version=2)]
        await tx.purge_resource(refs[0], NOW + timedelta(days=7))
        assert await tx.get_personal_food(food.id) is None
        assert await tx.get_tombstone("personal_food", UUID(str(food.id)))
        assert not await tx.media_referenced(UUID("00000000-0000-0000-0000-000000000001"))


def test_record_timezone_correction_is_explicit_and_does_not_change_default(active) -> None:
    active.diet.records.clock = lambda: NOW + timedelta(hours=12)
    with TestClient(create_app(active)) as client:
        request = payload()
        request["entry"]["temporal"]["local_time"] = "10:00"
        draft = client.post("/api/v1/diet/drafts", headers=headers(), json=request).json()
        saved = approve(client, draft).json()
        record_id = saved["result_refs"][0]["id"]
        response = client.post(
            "/api/v1/diet/previews",
            headers=headers(1),
            json={
                "operation": "log_correct",
                "timezone": "America/Denver",
                "arguments": {
                    "action": "correct",
                    "record_id": record_id,
                    "base_version": 1,
                    "patch": {"temporal": {"local_time": "10:00"}},
                    "evidence": request["evidence"],
                },
            },
        )
        assert response.status_code == 201, response.text
        preview = response.json()
        assert preview["content"]["before"]["timezone"] == "UTC"
        assert preview["content"]["after"]["timezone"] == "America/Denver"
        assert preview["content"]["after"]["occurred_at"] == "2026-11-12T17:00:00Z"
        assert approve(client, preview).status_code == 200
        assert client.get("/api/v1/diet/context", headers=OWNER).json()["timezone"] == "UTC"
        assert (
            client.get(f"/api/v1/diet/logs/{record_id}", headers=OWNER).json()["timezone"]
            == "America/Denver"
        )
