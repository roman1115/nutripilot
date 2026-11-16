from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from nutripilot.application.diet_service import DietService
from nutripilot.application.intake_service import IntakeService, ResolvedFood
from nutripilot.domain.diet import DietError, FoodRef, SubmissionAnchor
from nutripilot.infrastructure.diet_store import MemoryDietStore, MemoryDietTransaction
from nutripilot.tools.local.diet_models import CreateIntake, DiscardIntake, ReviseIntake
from nutripilot.tools.local.ports import DietTransaction

NOW = datetime(2026, 11, 12, 4, tzinfo=UTC)
ANCHOR = SubmissionAnchor.capture(NOW, "Asia/Shanghai")


class UnknownFoodResolver:
    async def resolve(
        self,
        food: FoodRef,
        *,
        user_id: int,
        now: datetime,
        tx: DietTransaction,
    ) -> ResolvedFood:
        return ResolvedFood(name="fixture-food", evidence=None)

    async def assert_available(self, food: FoodRef, *, user_id: int, tx: DietTransaction) -> None:
        return None


def request(**changes: Any) -> CreateIntake:
    data: dict[str, Any] = {
        "action": "create",
        "entry": {
            "actuality": "actual",
            "subject": "self",
            "temporal": {"local_date": "2026-11-12", "meal_type": "lunch"},
            "items": [
                {
                    "client_item_key": "food1",
                    "food": {"kind": "unknown", "name": "fixture-food"},
                    "quantity": {"kind": "unknown", "description": "unspecified"},
                }
            ],
        },
        "evidence": [
            {
                "field": "food",
                "item_key": "food1",
                "source_kind": "user_message",
                "source_ref": "fixture-message",
                "original_text": "fixture-food",
                "operation": "set",
                "status": "explicit",
            }
        ],
    }
    data.update(changes)
    return CreateIntake.model_validate(data)


def service(store: MemoryDietStore) -> IntakeService:
    return IntakeService(
        store,
        UnknownFoodResolver(),
        default_timezone="Asia/Shanghai",
        clock=lambda: NOW,
    )


async def test_prepare_is_not_an_actual_record_and_confirm_is_idempotent() -> None:
    store = MemoryDietStore()
    intake = service(store)
    draft = await intake.prepare(1, request(), idempotency_key="prepare1", anchor=ANCHOR)
    with pytest.raises(DietError) as wrong_channel:
        await intake.prepare(
            1, request(), idempotency_key="prepare1", anchor=ANCHOR, channel="chat"
        )
    assert wrong_channel.value.code == "IDEMPOTENCY_CONFLICT"
    with pytest.raises(DietError) as wrong_thread:
        await intake.prepare(
            1, request(), idempotency_key="prepare1", anchor=ANCHOR, thread_id="other"
        )
    assert wrong_thread.value.code == "IDEMPOTENCY_CONFLICT"
    assert draft.state == "ready"
    async with store.transaction(1, default_timezone="Asia/Shanghai") as tx:
        assert await tx.list_logs(ANCHOR.anchor_local_date, ANCHOR.anchor_local_date) == []
        assert tx.state.diet_epoch == 0
    receipts = await asyncio.gather(
        *[
            intake.confirm(1, draft.id, version=1, approved_hash=draft.preview_hash)
            for _ in range(12)
        ]
    )
    assert len({receipt.id for receipt in receipts}) == 1
    async with store.transaction(1, default_timezone="Asia/Shanghai") as tx:
        logs = await tx.list_logs(ANCHOR.anchor_local_date, ANCHOR.anchor_local_date)
        assert len(logs) == 1
        assert logs[0].items[0].nutrition_snapshot.calories_kcal.value is None
        assert tx.state.diet_epoch == 1
        assert (await tx.get_draft(draft.id)).state == "committed"
    retry = await intake.prepare(1, request(), idempotency_key="prepare1", anchor=ANCHOR)
    assert retry.state == "committed"
    assert retry.id == draft.id


async def test_prepare_same_key_different_content_rejected() -> None:
    store = MemoryDietStore()
    intake = service(store)
    await intake.prepare(1, request(), idempotency_key="prepare1", anchor=ANCHOR)
    changed = request().model_dump()
    changed["entry"]["temporal"]["meal_type"] = "dinner"
    with pytest.raises(DietError) as exc:
        await intake.prepare(
            1,
            CreateIntake.model_validate(changed),
            idempotency_key="prepare1",
            anchor=ANCHOR,
        )
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"


async def test_draft_owner_and_expiry_checked_before_writing() -> None:
    store = MemoryDietStore()
    intake = service(store)
    draft = await intake.prepare(1, request(), idempotency_key="prepare1", anchor=ANCHOR)
    with pytest.raises(DietError) as exc:
        await intake.confirm(2, draft.id, version=1, approved_hash=draft.preview_hash)
    assert exc.value.code == "NOT_FOUND"
    expired = IntakeService(
        store,
        UnknownFoodResolver(),
        default_timezone="Asia/Shanghai",
        clock=lambda: NOW + timedelta(hours=24),
    )
    with pytest.raises(DietError) as exc:
        await expired.confirm(1, draft.id, version=1, approved_hash=draft.preview_hash)
    assert exc.value.code == "PREVIEW_EXPIRED"


async def test_discard_prevents_late_confirmation_and_prepare_retry_returns_current() -> None:
    store = MemoryDietStore()
    intake = service(store)
    draft = await intake.prepare(1, request(), idempotency_key="prepare1", anchor=ANCHOR)
    discarded = await intake.discard(
        1,
        DiscardIntake(
            action="discard",
            draft_id=draft.id,
            base_version=1,
            evidence=request().evidence,
        ),
        idempotency_key="discard1",
    )
    assert discarded.state == "discarded"
    assert (
        await intake.prepare(
            1,
            request(),
            idempotency_key="prepare1",
            anchor=ANCHOR,
        )
    ).state == "discarded"
    with pytest.raises(DietError):
        await intake.confirm(1, draft.id, version=1, approved_hash=draft.preview_hash)


async def test_revise_invalidates_old_preview_and_preserves_other_fields() -> None:
    store = MemoryDietStore()
    intake = service(store)
    draft = await intake.prepare(1, request(), idempotency_key="prepare1", anchor=ANCHOR)
    item = draft.content.items[0]
    revised = await intake.revise(
        1,
        ReviseIntake.model_validate(
            {
                "action": "revise",
                "draft_id": str(draft.id),
                "base_version": 1,
                "patch": {
                    "item_edits": [
                        {
                            "action": "update",
                            "item_id": str(item.id),
                            "changes": {
                                "quantity": {"kind": "mass", "grams": "100", "food_state": "cooked"}
                            },
                        }
                    ]
                },
                "evidence": [item.model_dump(exclude_unset=True) for item in request().evidence],
            }
        ),
        idempotency_key="revise1",
    )
    assert revised.version == 2
    assert revised.content.items[0].id == item.id
    assert revised.content.entry.temporal == draft.content.entry.temporal
    assert revised.expires_at == draft.expires_at
    with pytest.raises(DietError) as exc:
        await intake.confirm(1, draft.id, version=1, approved_hash=draft.preview_hash)
    assert exc.value.code == "VERSION_CONFLICT"
    receipt = await intake.confirm(1, draft.id, version=2, approved_hash=revised.preview_hash)
    assert receipt.result_status == "committed"


async def test_transaction_rolls_back_all_mutations_on_failure() -> None:
    store = MemoryDietStore()
    intake = service(store)
    draft = await intake.prepare(1, request(), idempotency_key="prepare1", anchor=ANCHOR)
    with pytest.raises(RuntimeError, match="simulated"):
        async with store.transaction(1, default_timezone="Asia/Shanghai") as tx:
            await tx.save_draft(draft.discarded(NOW))
            await tx.advance_epoch()
            raise RuntimeError("simulated storage failure")
    async with store.transaction(1, default_timezone="Asia/Shanghai") as tx:
        assert (await tx.get_draft(draft.id)).state == "ready"
        assert tx.state.diet_epoch == 0


async def test_future_actuals_and_third_party_are_not_confirmable() -> None:
    store = MemoryDietStore()
    intake = service(store)
    data = request().model_dump(exclude_unset=True)
    data["entry"]["temporal"]["local_date"] = "2026-11-13"
    draft = await intake.prepare(
        1,
        CreateIntake.model_validate(data),
        idempotency_key="future",
        anchor=ANCHOR,
    )
    assert draft.state == "needs_input"
    with pytest.raises(DietError):
        await intake.confirm(1, draft.id, version=1, approved_hash=draft.preview_hash)


async def test_old_confirmation_never_resurrects_a_deleted_record() -> None:
    store = MemoryDietStore()
    intake = service(store)
    diet = DietService(store, default_timezone="Asia/Shanghai", clock=lambda: NOW)
    draft = await intake.prepare(1, request(), idempotency_key="prepare", anchor=ANCHOR)
    saved = await intake.confirm(1, draft.id, version=1, approved_hash=draft.preview_hash)
    record_id = saved.result_refs[0].id
    delete = await diet.prepare_delete_restore(
        1,
        record_id,
        base_version=1,
        action="delete",
        idempotency_key="delete",
    )
    assert (await diet.get_log(1, record_id)).deleted_at is None
    await diet.confirm(1, delete.id, version=1, approved_hash=delete.content_hash)
    replay = await intake.confirm(1, draft.id, version=1, approved_hash=draft.preview_hash)
    assert replay.id == saved.id
    assert replay.result_status == "deleted"
    assert (await diet.day_summary(1, ANCHOR.anchor_local_date)).record_count == 0
    restore = await diet.prepare_delete_restore(
        1,
        record_id,
        base_version=2,
        action="restore",
        idempotency_key="restore",
    )
    await diet.confirm(1, restore.id, version=1, approved_hash=restore.content_hash)
    assert (await diet.day_summary(1, ANCHOR.anchor_local_date)).record_count == 1
    assert (await diet.get_log(1, record_id)).version == 3


async def test_failed_receipt_rolls_back_record_revision_and_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryDietStore()
    intake = service(store)
    draft = await intake.prepare(1, request(), idempotency_key="prepare", anchor=ANCHOR)
    original = MemoryDietTransaction.save_receipt

    async def fail(self: MemoryDietTransaction, receipt: Any) -> None:
        if receipt.operation.startswith("confirm:"):
            raise RuntimeError("simulated receipt failure")
        await original(self, receipt)

    monkeypatch.setattr(MemoryDietTransaction, "save_receipt", fail)
    with pytest.raises(RuntimeError, match="receipt failure"):
        await intake.confirm(1, draft.id, version=1, approved_hash=draft.preview_hash)
    async with store.transaction(1, default_timezone="Asia/Shanghai") as tx:
        current = await tx.get_draft(draft.id)
        assert current is not None and current.state == "ready"
        assert tx.state.diet_epoch == 0
        assert not await tx.list_logs(ANCHOR.anchor_local_date, ANCHOR.anchor_local_date)
    monkeypatch.setattr(MemoryDietTransaction, "save_receipt", original)
    assert (
        await intake.confirm(
            1,
            draft.id,
            version=1,
            approved_hash=draft.preview_hash,
        )
    ).result_status == "committed"


async def test_restore_expiry_is_rechecked_when_user_confirms() -> None:
    store = MemoryDietStore()
    intake = service(store)
    diet = DietService(store, default_timezone="Asia/Shanghai", clock=lambda: NOW)
    draft = await intake.prepare(1, request(), idempotency_key="prepare", anchor=ANCHOR)
    saved = await intake.confirm(1, draft.id, version=1, approved_hash=draft.preview_hash)
    delete = await diet.prepare_delete_restore(
        1,
        saved.result_refs[0].id,
        base_version=1,
        action="delete",
        idempotency_key="delete",
    )
    await diet.confirm(1, delete.id, version=1, approved_hash=delete.content_hash)
    before_expiry = DietService(
        store,
        default_timezone="Asia/Shanghai",
        clock=lambda: NOW + timedelta(days=6, hours=23),
    )
    restore = await before_expiry.prepare_delete_restore(
        1,
        saved.result_refs[0].id,
        base_version=2,
        action="restore",
        idempotency_key="restore",
    )
    after_expiry = DietService(
        store,
        default_timezone="Asia/Shanghai",
        clock=lambda: NOW + timedelta(days=7, minutes=1),
    )
    with pytest.raises(DietError) as exc:
        await after_expiry.confirm(1, restore.id, version=1, approved_hash=restore.content_hash)
    assert exc.value.code == "RESOURCE_EXPIRED"


async def test_noop_patch_and_last_item_removal_are_rejected() -> None:
    store = MemoryDietStore()
    intake = service(store)
    draft = await intake.prepare(1, request(), idempotency_key="prepare", anchor=ANCHOR)
    base = {
        "action": "revise",
        "draft_id": str(draft.id),
        "base_version": 1,
        "evidence": [item.model_dump(exclude_unset=True) for item in request().evidence],
    }
    with pytest.raises(DietError) as exc:
        await intake.revise(
            1,
            ReviseIntake.model_validate(
                {
                    **base,
                    "patch": {"temporal": {"meal_type": "lunch"}},
                }
            ),
            idempotency_key="noop",
        )
    assert exc.value.code == "NO_CHANGE"
    with pytest.raises(DietError) as exc:
        await intake.revise(
            1,
            ReviseIntake.model_validate(
                {
                    **base,
                    "patch": {
                        "item_edits": [
                            {
                                "action": "remove",
                                "item_id": str(draft.content.items[0].id),
                            }
                        ]
                    },
                }
            ),
            idempotency_key="remove",
        )
    assert exc.value.code == "EMPTY_RECORD"
