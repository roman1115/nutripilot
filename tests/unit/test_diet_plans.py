from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from nutripilot.application.diet_service import DietService
from nutripilot.application.intake_service import IntakeAlreadyRecorded, IntakeService
from nutripilot.application.plan_service import PlanService
from nutripilot.domain.diet import DietError, SubmissionAnchor
from nutripilot.domain.diet_records import DietChangePreview, PlanChangeContent
from nutripilot.infrastructure.diet_foods import CatalogDietFoodResolver
from nutripilot.infrastructure.diet_store import MemoryDietStore
from nutripilot.retrieval.catalog import FoodCatalog
from nutripilot.tools.local.diet_models import (
    CopyMealPlan,
    CopyPlanWeek,
    CreateIntake,
    CreateMealPlan,
    DeleteMealPlan,
    UpdateMealPlan,
)

NOW = datetime(2026, 11, 12, 4, tzinfo=UTC)
EVIDENCE = [
    {
        "field": "operation",
        "source_kind": "user_form",
        "source_ref": "fixture",
        "original_text": "synthetic plan",
        "operation": "set",
        "status": "explicit",
    }
]


def foods() -> CatalogDietFoodResolver:
    return CatalogDietFoodResolver(FoodCatalog(sources=[], ingredients=[], recipes=[]))


def entry(key: str, day: str, meal_type: str, *, item_key: str = "food") -> dict[str, Any]:
    return {
        "client_entry_key": key,
        "entry": {
            "actuality": "planned",
            "subject": "self",
            "temporal": {"local_date": day, "meal_type": meal_type},
            "items": [
                {
                    "client_item_key": item_key,
                    "food": {"kind": "unknown", "name": "fixture-food"},
                    "quantity": {"kind": "unknown", "description": "unspecified"},
                }
            ],
        },
    }


def create_request(*entries: dict[str, Any]) -> CreateMealPlan:
    return CreateMealPlan.model_validate(
        {
            "action": "create",
            "parent": {"kind": "new", "title": "fixture", "start_date": "2026-11-09"},
            "entries": list(entries),
            "evidence": EVIDENCE,
        }
    )


async def confirm(service: PlanService, preview: DietChangePreview) -> None:
    await service.confirm(
        1, preview.id, version=preview.version, approved_hash=preview.content_hash
    )


async def test_twenty_eight_meals_are_one_atomic_confirmation() -> None:
    store = MemoryDietStore()
    service = PlanService(store, foods(), default_timezone="Asia/Shanghai", clock=lambda: NOW)
    request = create_request(
        *[
            entry(f"d{day}{meal}", f"2026-11-{day:02}", meal)
            for day in range(9, 16)
            for meal in ("breakfast", "lunch", "dinner", "snack")
        ]
    )
    preview = await service.prepare(1, request, idempotency_key="week")
    assert isinstance(preview.content, PlanChangeContent)
    plan_id = preview.content.after_plan.id
    async with store.transaction(1, default_timezone="Asia/Shanghai") as tx:
        assert await tx.get_plan(plan_id) is None
        assert await tx.list_planned_meals(plan_id) == []
    receipts = await asyncio.gather(
        *[
            service.confirm(1, preview.id, version=1, approved_hash=preview.content_hash)
            for _ in range(8)
        ]
    )
    assert len({receipt.id for receipt in receipts}) == 1
    async with store.transaction(1, default_timezone="Asia/Shanghai") as tx:
        meals = await tx.list_planned_meals(plan_id)
        assert len(meals) == 28 and tx.state.diet_epoch == 1
        assert len({item.id for meal in meals for item in meal.items}) == 28
        assert await tx.list_logs(NOW.date(), NOW.date()) == []
    async with store.transaction(2, default_timezone="Asia/Shanghai") as tx:
        assert await tx.get_plan(plan_id) is None
        assert await tx.get_planned_meal(meals[0].id) is None
    assert (await service.prepare(1, request, idempotency_key="week")).state == "committed"


async def test_merge_same_slot_reject_duplicate_keys_and_preserve_both_items() -> None:
    store = MemoryDietStore()
    service = PlanService(store, foods(), default_timezone="Asia/Shanghai", clock=lambda: NOW)
    with pytest.raises(DietError) as exc:
        await service.prepare(
            1,
            create_request(entry("a", "2026-11-12", "lunch"), entry("b", "2026-11-12", "lunch")),
            idempotency_key="duplicate",
        )
    assert exc.value.code == "ITEM_KEY_CONFLICT"
    preview = await service.prepare(
        1,
        create_request(
            entry("a", "2026-11-12", "lunch", item_key="a"),
            entry("b", "2026-11-12", "lunch", item_key="b"),
        ),
        idempotency_key="merged",
    )
    assert isinstance(preview.content, PlanChangeContent)
    assert len(preview.content.after_meals) == 1
    assert [item.position for item in preview.content.after_meals[0].items] == [0, 1]
    await confirm(service, preview)


async def test_copy_uses_new_ids_and_stale_parent_preview_cannot_commit() -> None:
    store = MemoryDietStore()
    service = PlanService(store, foods(), default_timezone="Asia/Shanghai", clock=lambda: NOW)
    initial = await service.prepare(
        1, create_request(entry("a", "2026-11-12", "lunch")), idempotency_key="initial"
    )
    await confirm(service, initial)
    assert isinstance(initial.content, PlanChangeContent)
    meal = initial.content.after_meals[0]
    copied = await service.prepare(
        1,
        CopyMealPlan.model_validate(
            {
                "action": "copy",
                "planned_meal_id": str(meal.id),
                "base_version": 1,
                "target_date": "2026-11-13",
                "target_meal_type": "dinner",
                "evidence": EVIDENCE,
            }
        ),
        idempotency_key="copy",
    )
    stale = await service.prepare(
        1,
        DeleteMealPlan.model_validate(
            {
                "action": "delete",
                "planned_meal_id": str(meal.id),
                "base_version": 1,
                "evidence": EVIDENCE,
            }
        ),
        idempotency_key="delete",
    )
    assert isinstance(copied.content, PlanChangeContent)
    other = copied.content.after_meals[0]
    assert other.id != meal.id and other.items[0].id != meal.items[0].id
    assert other.items[0].evidence_snapshot == meal.items[0].evidence_snapshot
    await confirm(service, copied)
    with pytest.raises(DietError) as exc:
        await confirm(service, stale)
    assert exc.value.code == "VERSION_CONFLICT"
    async with store.transaction(1, default_timezone="Asia/Shanghai") as tx:
        assert len(await tx.list_planned_meals(meal.plan_id)) == 2
        assert (await tx.get_planned_meal(meal.id)) == meal


async def test_update_and_archive_do_not_change_other_meals_or_actuals() -> None:
    store = MemoryDietStore()
    service = PlanService(store, foods(), default_timezone="Asia/Shanghai", clock=lambda: NOW)
    initial = await service.prepare(
        1, create_request(entry("a", "2026-11-12", "lunch")), idempotency_key="initial"
    )
    await confirm(service, initial)
    assert isinstance(initial.content, PlanChangeContent)
    meal = initial.content.after_meals[0]
    updated = await service.prepare(
        1,
        UpdateMealPlan.model_validate(
            {
                "action": "update",
                "planned_meal_id": str(meal.id),
                "base_version": 1,
                "patch": {"temporal": {"note": "edited"}},
                "evidence": EVIDENCE,
            }
        ),
        idempotency_key="edit",
    )
    await confirm(service, updated)
    archived = await service.prepare_archive_restore(
        1, meal.plan_id, base_version=2, action="archive", idempotency_key="archive"
    )
    await confirm(service, archived)
    replay = await service.confirm(1, initial.id, version=1, approved_hash=initial.content_hash)
    assert replay.result_status == "deleted"
    restored = await service.prepare_archive_restore(
        1, meal.plan_id, base_version=3, action="restore", idempotency_key="restore"
    )
    await confirm(service, restored)
    async with store.transaction(1, default_timezone="Asia/Shanghai") as tx:
        current = await tx.get_plan(meal.plan_id)
        assert current is not None and current.version == 4 and current.deleted_at is None
        assert (await tx.get_planned_meal(meal.id)).note == "edited"  # type: ignore[union-attr]


async def test_creation_into_existing_slot_is_a_conflict() -> None:
    service = PlanService(
        MemoryDietStore(), foods(), default_timezone="Asia/Shanghai", clock=lambda: NOW
    )
    initial = await service.prepare(
        1, create_request(entry("a", "2026-11-12", "lunch")), idempotency_key="initial"
    )
    await confirm(service, initial)
    assert isinstance(initial.content, PlanChangeContent)
    request = CreateMealPlan.model_validate(
        {
            **create_request(entry("b", "2026-11-12", "lunch")).model_dump(),
            "parent": {
                "kind": "existing",
                "plan_id": UUID(initial.content.after_plan.id),
                "base_version": 1,
            },
        }
    )
    with pytest.raises(DietError) as exc:
        await service.prepare(1, request, idempotency_key="conflict")
    assert exc.value.code == "PLAN_SLOT_CONFLICT"


async def test_copy_week_reject_append_replace_are_explicit() -> None:
    store = MemoryDietStore()
    service = PlanService(store, foods(), default_timezone="Asia/Shanghai", clock=lambda: NOW)
    source = await service.prepare(
        1, create_request(entry("a", "2026-11-12", "lunch")), idempotency_key="source"
    )
    await confirm(service, source)
    destination = await service.prepare(
        1, create_request(entry("b", "2026-11-12", "lunch")), idempotency_key="destination"
    )
    await confirm(service, destination)
    assert isinstance(source.content, PlanChangeContent)
    assert isinstance(destination.content, PlanChangeContent)
    for policy, base_version in (("reject", 1), ("append_items", 1), ("replace_meals", 2)):
        request = CopyPlanWeek.model_validate(
            {
                "action": "copy_week",
                "source_plan_id": source.content.after_plan.id,
                "base_version": 1,
                "parent": {
                    "kind": "existing",
                    "plan_id": destination.content.after_plan.id,
                    "base_version": base_version,
                },
                "target_start_date": "2026-11-09",
                "conflict_policy": policy,
                "evidence": EVIDENCE,
            }
        )
        if policy == "reject":
            with pytest.raises(DietError) as exc:
                await service.prepare(1, request, idempotency_key=policy)
            assert exc.value.code == "PLAN_SLOT_CONFLICT"
            continue
        preview = await service.prepare(1, request, idempotency_key=policy)
        await confirm(service, preview)
        async with store.transaction(1, default_timezone="Asia/Shanghai") as tx:
            meals = await tx.list_planned_meals(destination.content.after_plan.id)
            assert len(meals) == 1
            assert len(meals[0].items) == (2 if policy == "append_items" else 1)


async def test_default_event_repeat_undo_and_restore_do_not_replay_old_approvals() -> None:
    store = MemoryDietStore()
    resolver = foods()
    plans = PlanService(store, resolver, default_timezone="Asia/Shanghai", clock=lambda: NOW)
    intake = IntakeService(store, resolver, default_timezone="Asia/Shanghai", clock=lambda: NOW)
    changes = DietService(store, default_timezone="Asia/Shanghai", clock=lambda: NOW)
    preview = await plans.prepare(
        1, create_request(entry("a", "2026-11-12", "lunch")), idempotency_key="plan"
    )
    await confirm(plans, preview)
    assert isinstance(preview.content, PlanChangeContent)
    meal = preview.content.after_meals[0]
    request = CreateIntake.model_validate(
        {
            "action": "create",
            "entry": {**PlanService.entry(meal).model_dump(), "actuality": "actual"},
            "source": {
                "kind": "plan",
                "planned_meal_id": meal.id,
                "base_version": 1,
                "record_again": False,
            },
            "evidence": EVIDENCE,
        }
    )
    anchor = SubmissionAnchor.capture(NOW, "Asia/Shanghai")
    first = await intake.prepare(1, request, idempotency_key="first", anchor=anchor)
    receipt = await intake.confirm(1, first.id, version=1, approved_hash=first.preview_hash)
    with pytest.raises(IntakeAlreadyRecorded):
        await intake.prepare(1, request, idempotency_key="again-no", anchor=anchor)
    repeat_request = CreateIntake.model_validate(
        {
            **request.model_dump(),
            "source": {**request.source.model_dump(), "record_again": True},  # type: ignore[union-attr]
        }
    )
    with pytest.raises(DietError) as exc:
        await intake.prepare(1, repeat_request, idempotency_key="repeat-denied", anchor=anchor)
    assert exc.value.code == "EXPLICIT_ACTION_REQUIRED"
    repeated = await intake.prepare(
        1, repeat_request, idempotency_key="repeat", anchor=anchor, explicit_repeat_authorized=True
    )
    repeat_receipt = await intake.confirm(
        1, repeated.id, version=1, approved_hash=repeated.preview_hash
    )
    assert repeat_receipt.result_refs != receipt.result_refs
    deletion = await changes.prepare_delete_restore(
        1, receipt.result_refs[0].uuid, base_version=1, action="delete", idempotency_key="undo"
    )
    await changes.confirm(1, deletion.id, version=1, approved_hash=deletion.content_hash)
    async with store.transaction(1, default_timezone="Asia/Shanghai") as tx:
        latest = await tx.get_planned_meal(meal.id)
        assert latest is not None and latest.intake_generation == 2
    old = await intake.confirm(1, first.id, version=1, approved_hash=first.preview_hash)
    assert old.result_status == "deleted"
    restore = await changes.prepare_delete_restore(
        1, receipt.result_refs[0].uuid, base_version=2, action="restore", idempotency_key="restore"
    )
    await changes.confirm(1, restore.id, version=1, approved_hash=restore.content_hash)
    newest_request = CreateIntake.model_validate(
        {
            **request.model_dump(),
            "source": {
                "kind": "plan",
                "planned_meal_id": meal.id,
                "base_version": 2,
                "record_again": False,
            },
        }
    )
    with pytest.raises(IntakeAlreadyRecorded):
        await intake.prepare(1, newest_request, idempotency_key="restored-no", anchor=anchor)
