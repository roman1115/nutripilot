"""Owner-scoped diet queries and frozen record/target changes."""

from __future__ import annotations

import hmac
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Literal
from uuid import UUID, uuid4

from nutripilot.application.diet_patches import apply_entry_patch
from nutripilot.domain.diet import (
    DRAFT_LIFETIME,
    DietError,
    DietModel,
    NutritionGoals,
    NutritionSummary,
    ProposedEntry,
    ShortText,
    SubmissionAnchor,
    content_hash,
    resolve_occurred_at,
    summarize_nutrition,
)
from nutripilot.domain.diet_records import (
    ChangeContent,
    DietChangePreview,
    DietLog,
    NutritionTarget,
    OperationReceipt,
    ResourceRef,
    TargetPeriod,
    target_on,
    target_periods,
)
from nutripilot.tools.local.diet_models import CorrectDietLog, CorrectTarget, SetTarget, StopTarget
from nutripilot.tools.local.ports import DietFoodResolver, DietStore


class CorrectionRequest(DietModel):
    request: CorrectDietLog
    timezone: ShortText | None = None


class DietService:
    def __init__(
        self,
        store: DietStore,
        *,
        default_timezone: str,
        clock: Callable[[], datetime] | None = None,
        foods: DietFoodResolver | None = None,
    ) -> None:
        self.store = store
        self.default_timezone = default_timezone
        self.clock = clock or (lambda: datetime.now(UTC))
        self.foods = foods

    async def get_log(self, user_id: int, record_id: UUID) -> DietLog:
        async with self.store.transaction(user_id, default_timezone=self.default_timezone) as tx:
            log = await tx.get_log(record_id)
            if log is None:
                if await tx.get_tombstone("log", record_id):
                    raise DietError("RESOURCE_PURGED", "Record was permanently removed.")
                raise DietError("NOT_FOUND", "Record not found.")
            return log

    async def day_summary(self, user_id: int, local_date: date) -> NutritionSummary:
        async with self.store.transaction(user_id, default_timezone=self.default_timezone) as tx:
            records = await tx.list_logs(local_date, local_date)
            declaration = await tx.get_day_status(local_date)
            return summarize_nutrition(
                (item.nutrition_snapshot for log in records for item in log.items),
                record_count=len(records),
                day_status=declaration.status,
            )

    async def targets(self, user_id: int) -> list[TargetPeriod]:
        async with self.store.transaction(user_id, default_timezone=self.default_timezone) as tx:
            return target_periods(await tx.list_targets())

    async def prepare_target(
        self,
        user_id: int,
        request: SetTarget | CorrectTarget | StopTarget,
        *,
        idempotency_key: str,
        source: Literal["user_form", "user_message"],
        source_ref: str,
        thread_id: str | None = None,
        source_turn_id: str | None = None,
    ) -> DietChangePreview:
        async with self.store.transaction(user_id, default_timezone=self.default_timezone) as tx:
            request_hash = content_hash(request)
            previous = await tx.get_receipt("prepare_target", idempotency_key)
            if previous:
                if previous.request_hash != request_hash:
                    raise DietError("IDEMPOTENCY_CONFLICT", "Request key has different content.")
                preview = await tx.get_preview(previous.result_refs[0].uuid)
                if preview is None:
                    raise DietError("RESOURCE_PURGED", "The original preview was purged.")
                return preview
            targets = await tx.list_targets()
            before: NutritionTarget | None
            operation: Literal["target_set", "target_correct", "target_stop"]
            if isinstance(request, SetTarget):
                operation = "target_set"
                before = next(
                    (
                        target
                        for target in targets
                        if target.effective_date == request.effective_date
                    ),
                    None,
                )
                active = target_on(targets, request.effective_date)
                goals = (active.goals if active else NutritionGoals()).merged(request.goals)
                if before is None and goals == (active.goals if active else NutritionGoals()):
                    raise DietError("NO_CHANGE", "The effective goals have no changes.")
            else:
                selected = next(
                    (target for target in targets if target.id == request.target_id), None
                )
                if selected is None:
                    raise DietError("NOT_FOUND", "Target not found.")
                if selected.version != request.base_version:
                    raise DietError("VERSION_CONFLICT", "Target has changed.")
                if isinstance(request, StopTarget):
                    operation = "target_stop"
                    active = target_on(targets, request.effective_date)
                    if active is None or active.id != selected.id:
                        raise DietError(
                            "SOURCE_CHANGED", "Stop date is outside the selected target."
                        )
                    before = selected if selected.effective_date == request.effective_date else None
                    goals = NutritionGoals()
                    if selected.goals == goals:
                        raise DietError("NO_CHANGE", "The selected target is already stopped.")
                else:
                    operation = "target_correct"
                    before = selected
                    if any(
                        target.id != before.id and target.effective_date == request.effective_date
                        for target in targets
                    ):
                        raise DietError(
                            "TARGET_DATE_CONFLICT", "Another target starts on this date."
                        )
                    goals = before.goals.merged(request.goals)
            if (
                before is not None
                and before.goals == goals
                and before.effective_date == request.effective_date
            ):
                raise DietError("NO_CHANGE", "The target has no changes.")
            now = self.clock()
            after = NutritionTarget(
                id=before.id if before else uuid4(),
                user_id=user_id,
                version=before.version + 1 if before else 1,
                created_at=before.created_at if before else now,
                updated_at=now,
                effective_date=request.effective_date,
                goals=goals,
                source=source,
                source_ref=source_ref,
            )
            content = ChangeContent(
                operation=operation,
                before=before,
                after=after,
                base_refs=[
                    ResourceRef(kind="target", id=target.id, version=target.version)
                    for target in targets
                ],
                base_target_epoch=tx.state.target_epoch,
            )
            preview = DietChangePreview(
                id=uuid4(),
                user_id=user_id,
                created_at=now,
                updated_at=now,
                thread_id=thread_id,
                source_turn_id=source_turn_id,
                content=content,
                content_hash=content_hash(content),
                expires_at=now + DRAFT_LIFETIME,
            )
            await tx.save_preview(preview)
            await tx.save_receipt(
                OperationReceipt(
                    id=uuid4(),
                    user_id=user_id,
                    operation="prepare_target",
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    result_refs=[
                        ResourceRef(kind="preview", id=preview.id, version=preview.version)
                    ],
                    result_status="prepared",
                    created_at=now,
                )
            )
            return preview

    async def prepare_delete_restore(
        self,
        user_id: int,
        record_id: UUID,
        *,
        base_version: int,
        action: Literal["delete", "restore"],
        idempotency_key: str,
        thread_id: str | None = None,
        source_turn_id: str | None = None,
    ) -> DietChangePreview:
        async with self.store.transaction(user_id, default_timezone=self.default_timezone) as tx:
            previous = await tx.get_receipt(f"prepare_log_{action}", idempotency_key)
            if previous:
                preview = await tx.get_preview(previous.result_refs[0].uuid)
                if preview is None:
                    raise DietError("RESOURCE_PURGED", "The preview no longer exists.")
                if (
                    not isinstance(preview.content, ChangeContent)
                    or preview.content.before is None
                    or (
                        preview.content.before.id != record_id
                        or preview.content.before.version != base_version
                    )
                ):
                    raise DietError("IDEMPOTENCY_CONFLICT", "Request key has different content.")
                return preview
            log = await tx.get_log(record_id)
            if log is None:
                raise DietError("NOT_FOUND", "Record not found.")
            if log.version != base_version:
                raise DietError("VERSION_CONFLICT", "Record has changed.")
            now = self.clock()
            after = log.soft_deleted(now) if action == "delete" else log.restored(now)
            content = ChangeContent(
                operation="log_delete" if action == "delete" else "log_restore",
                before=log,
                after=after,
                base_refs=[ResourceRef(kind="log", id=log.id, version=log.version)],
            )
            preview = DietChangePreview(
                id=uuid4(),
                user_id=user_id,
                created_at=now,
                updated_at=now,
                content=content,
                content_hash=content_hash(content),
                expires_at=now + DRAFT_LIFETIME,
                thread_id=thread_id,
                source_turn_id=source_turn_id,
            )
            await tx.save_preview(preview)
            await tx.save_receipt(
                OperationReceipt(
                    id=uuid4(),
                    user_id=user_id,
                    operation=f"prepare_log_{action}",
                    idempotency_key=idempotency_key,
                    request_hash=content_hash(content),
                    result_refs=[
                        ResourceRef(kind="preview", id=preview.id, version=preview.version)
                    ],
                    result_status="prepared",
                    created_at=now,
                )
            )
            return preview

    async def prepare_correction(
        self,
        user_id: int,
        request: CorrectDietLog,
        *,
        idempotency_key: str,
        foods: DietFoodResolver,
        thread_id: str | None = None,
        source_turn_id: str | None = None,
        timezone: str | None = None,
    ) -> DietChangePreview:
        async with self.store.transaction(user_id, default_timezone=self.default_timezone) as tx:
            request_hash = content_hash(CorrectionRequest(request=request, timezone=timezone))
            previous = await tx.get_receipt("prepare_log_correct", idempotency_key)
            if previous:
                if previous.request_hash != request_hash:
                    raise DietError("IDEMPOTENCY_CONFLICT", "Request key has different content.")
                preview = await tx.get_preview(previous.result_refs[0].uuid)
                if preview is None:
                    raise DietError("RESOURCE_PURGED", "Preview no longer exists.")
                return preview
            log = await tx.get_log(request.record_id)
            if log is None:
                raise DietError("NOT_FOUND", "Record not found.")
            if log.version != request.base_version:
                raise DietError("VERSION_CONFLICT", "Record has changed.")
            if log.deleted_at is not None:
                raise DietError("INVALID_RECORD_STATE", "Restore the record before editing it.")
            original = ProposedEntry.model_validate(
                {
                    "actuality": "actual",
                    "subject": "self",
                    "temporal": log.temporal,
                    "items": [
                        {
                            "client_item_key": item.client_item_key,
                            "food": item.food_ref,
                            "quantity": item.quantity,
                            **({"note": item.note} if item.note is not None else {}),
                        }
                        for item in log.items
                    ],
                }
            )
            now = self.clock()
            entry, items = await apply_entry_patch(
                original,
                log.items,
                request.patch,
                foods=foods,
                user_id=user_id,
                now=now,
                evidence=request.evidence,
                tx=tx,
            )
            selected_timezone = timezone if timezone is not None else log.timezone
            if entry == original and items == log.items and selected_timezone == log.timezone:
                raise DietError("NO_CHANGE", "The record has no changes.")
            if entry.blocking_fields(
                today=SubmissionAnchor.capture(now, selected_timezone).anchor_local_date
            ):
                raise DietError("INCOMPLETE_RECORD", "Resolve the required date and meal fields.")
            after = DietLog.model_validate(
                {
                    **log.model_dump(),
                    "version": log.version + 1,
                    "updated_at": now,
                    "temporal": entry.temporal,
                    "items": items,
                    "timezone": selected_timezone,
                    "occurred_at": resolve_occurred_at(entry.temporal, selected_timezone),
                }
            )
            content = ChangeContent(
                operation="log_correct",
                before=log,
                after=after,
                base_refs=[ResourceRef(kind="log", id=log.id, version=log.version)],
            )
            preview = DietChangePreview(
                id=uuid4(),
                user_id=user_id,
                created_at=now,
                updated_at=now,
                content=content,
                content_hash=content_hash(content),
                expires_at=now + DRAFT_LIFETIME,
                thread_id=thread_id,
                source_turn_id=source_turn_id,
            )
            await tx.save_preview(preview)
            await tx.save_receipt(
                OperationReceipt(
                    id=uuid4(),
                    user_id=user_id,
                    operation="prepare_log_correct",
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    result_refs=[
                        ResourceRef(kind="preview", id=preview.id, version=preview.version)
                    ],
                    result_status="prepared",
                    created_at=now,
                )
            )
            return preview

    async def confirm(
        self,
        user_id: int,
        preview_id: UUID,
        *,
        version: int,
        approved_hash: str,
    ) -> OperationReceipt:
        async with self.store.transaction(user_id, default_timezone=self.default_timezone) as tx:
            preview = await tx.get_preview(preview_id)
            if preview is None:
                raise DietError("NOT_FOUND", "Preview not found.")
            if not isinstance(preview.content, ChangeContent):
                raise DietError("INVALID_PREVIEW_STATE", "This service cannot submit this preview.")
            operation = f"confirm:{preview.content.operation}"
            key = f"{preview_id}:{version}"
            previous = await tx.get_receipt(operation, key)
            if previous:
                if not hmac.compare_digest(previous.request_hash, approved_hash):
                    raise DietError("IDEMPOTENCY_CONFLICT", "Approved content is different.")
                if previous.result_refs[0].kind == "log":
                    current = await tx.get_log(previous.result_refs[0].uuid)
                    if current is None:
                        return previous.model_copy(update={"result_status": "purged"})
                    if current.deleted_at is not None:
                        return previous.model_copy(update={"result_status": "deleted"})
                return previous
            if preview.version != version or not hmac.compare_digest(
                preview.content_hash, approved_hash
            ):
                raise DietError("VERSION_CONFLICT", "Preview has changed.")
            now = self.clock()
            if now >= preview.expires_at:
                raise DietError("PREVIEW_EXPIRED", "Preview has expired.")
            if preview.state != "ready":
                raise DietError("INVALID_PREVIEW_STATE", "Preview is not ready.")
            after = preview.content.after
            if isinstance(after, NutritionTarget):
                if tx.state.target_epoch != preview.content.base_target_epoch:
                    raise DietError("VERSION_CONFLICT", "The target timeline has changed.")
                await tx.save_target(after)
            else:
                current = await tx.get_log(after.id)
                if current is None:
                    raise DietError("NOT_FOUND", "Record not found.")
                if current.version + 1 != after.version:
                    raise DietError("VERSION_CONFLICT", "Record has changed.")
                if preview.content.operation == "log_restore":
                    current.restored(now)
                if preview.content.operation == "log_correct":
                    for item in after.items:
                        if item.food_ref.kind != "unknown":
                            if self.foods is None:
                                raise DietError(
                                    "DEPENDENCY_UNAVAILABLE", "Food resolver unavailable."
                                )
                            await self.foods.assert_available(item.food_ref, user_id=user_id, tx=tx)
                await tx.save_log(after)
                if current.temporal.local_date is not None:
                    await tx.set_day_status(current.temporal.local_date, "open")
                if (
                    after.temporal.local_date is not None
                    and after.temporal.local_date != current.temporal.local_date
                ):
                    await tx.set_day_status(after.temporal.local_date, "open")
            receipt = OperationReceipt(
                id=uuid4(),
                user_id=user_id,
                operation=operation,
                idempotency_key=key,
                request_hash=approved_hash,
                preview_id=preview.id,
                preview_version=preview.version,
                result_refs=[
                    ResourceRef(
                        kind="target" if isinstance(after, NutritionTarget) else "log",
                        id=after.id,
                        version=after.version,
                    )
                ],
                result_status="committed",
                created_at=now,
            )
            await tx.add_revision(preview.content.before, after, receipt)
            if (
                isinstance(after, DietLog)
                and preview.content.operation == "log_delete"
                and after.source_event_kind == "default_plan"
                and after.source_planned_meal_id is not None
            ):
                meal = await tx.get_planned_meal(after.source_planned_meal_id)
                if meal is None:
                    raise DietError("SOURCE_CHANGED", "Linked planned meal is unavailable.")
                plan = await tx.get_plan(meal.plan_id)
                if plan is None:
                    raise DietError("SOURCE_CHANGED", "Linked plan is unavailable.")
                updated_meal = meal.model_copy(
                    update={
                        "intake_generation": meal.intake_generation + 1,
                        "version": meal.version + 1,
                        "updated_at": now,
                    }
                )
                updated_plan = plan.model_copy(
                    update={
                        "version": plan.version + 1,
                        "updated_at": now,
                    }
                )
                await tx.save_planned_meal(updated_meal)
                await tx.save_plan(updated_plan)
                await tx.add_revision(meal, updated_meal, receipt)
                await tx.add_revision(plan, updated_plan, receipt)
            await tx.save_receipt(receipt)
            await tx.save_preview(
                DietChangePreview.model_validate(
                    {
                        **preview.model_dump(),
                        "state": "committed",
                        "receipt_id": receipt.id,
                        "updated_at": now,
                    }
                )
            )
            await tx.advance_epoch(targets_changed=isinstance(after, NutritionTarget))
            return receipt
