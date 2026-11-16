"""Composition of shared diet use cases and trusted preview submission."""

from __future__ import annotations

import hmac
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from pydantic import Field

from nutripilot.application.diet_queries import DietQueryService
from nutripilot.application.diet_service import DietService
from nutripilot.application.intake_service import IntakeService
from nutripilot.application.personal_food_service import PersonalFoodService
from nutripilot.application.plan_service import PlanService
from nutripilot.domain.diet import (
    DRAFT_LIFETIME,
    DayStatus,
    DietError,
    DietModel,
    LocalDate,
    SubmissionAnchor,
    content_hash,
)
from nutripilot.domain.diet_records import (
    DayStatusChangeContent,
    DietChangePreview,
    DietDayDeclaration,
    IntakeDraft,
    OperationReceipt,
    PersonalFoodChangeContent,
    PlanChangeContent,
    PurgeChangeContent,
    ResourceRef,
)
from nutripilot.tools.local.ports import DietFoodSearch, DietStore


class DayStatusRequest(DietModel):
    local_date: LocalDate
    status: DayStatus
    base_version: Annotated[int, Field(strict=True, ge=1)]


class DietManagement:
    def __init__(
        self,
        store: DietStore,
        foods: DietFoodSearch,
        *,
        default_timezone: str,
        cursor_secret: bytes,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store, self.foods, self.default_timezone = store, foods, default_timezone
        self.clock = clock or (lambda: datetime.now(UTC))
        self.intake = IntakeService(
            store, foods, default_timezone=default_timezone, clock=self.clock
        )
        self.records = DietService(
            store, default_timezone=default_timezone, clock=self.clock, foods=foods
        )
        self.plans = PlanService(store, foods, default_timezone=default_timezone, clock=self.clock)
        self.personal_foods = PersonalFoodService(
            store, default_timezone=default_timezone, clock=self.clock
        )
        self.queries = DietQueryService(
            store,
            foods,
            default_timezone=default_timezone,
            cursor_secret=cursor_secret,
            clock=self.clock,
        )

    async def preview(self, user_id: int, preview_id: UUID) -> IntakeDraft | DietChangePreview:
        async with self.store.transaction(user_id, default_timezone=self.default_timezone) as tx:
            preview = await tx.get_draft(preview_id) or await tx.get_preview(preview_id)
            if preview is None:
                if await tx.get_tombstone("draft", preview_id) or await tx.get_tombstone(
                    "preview", preview_id
                ):
                    raise DietError("RESOURCE_PURGED", "Preview was permanently removed.")
                raise DietError("NOT_FOUND", "Preview not found.")
            if self.clock() >= preview.expires_at and preview.state in ("ready", "needs_input"):
                return preview.model_copy(update={"state": "expired"})
            return preview

    async def confirm(
        self,
        user_id: int,
        preview_id: UUID,
        *,
        version: int,
        approved_hash: str,
    ) -> OperationReceipt:
        preview = await self.preview(user_id, preview_id)
        if isinstance(preview, DietChangePreview) and isinstance(
            preview.content, PurgeChangeContent
        ):
            raise DietError("PURGE_CONFIRMATION_REQUIRED", "请使用专用的永久清除确认入口。")
        if isinstance(preview, IntakeDraft):
            return await self.intake.confirm(
                user_id, preview_id, version=version, approved_hash=approved_hash
            )
        if isinstance(preview.content, PlanChangeContent):
            return await self.plans.confirm(
                user_id, preview_id, version=version, approved_hash=approved_hash
            )
        if isinstance(preview.content, PersonalFoodChangeContent):
            return await self.personal_foods.confirm(
                user_id, preview_id, version=version, approved_hash=approved_hash
            )
        if isinstance(preview.content, DayStatusChangeContent):
            return await self._confirm_day(
                user_id, preview_id, version=version, approved_hash=approved_hash
            )
        return await self.records.confirm(
            user_id, preview_id, version=version, approved_hash=approved_hash
        )

    async def discard_preview(
        self,
        user_id: int,
        preview_id: UUID,
        *,
        version: int,
        approved_hash: str,
    ) -> IntakeDraft | DietChangePreview:
        async with self.store.transaction(user_id, default_timezone=self.default_timezone) as tx:
            preview = await tx.get_draft(preview_id) or await tx.get_preview(preview_id)
            if preview is None:
                raise DietError("NOT_FOUND", "Preview not found.")
            current_hash = (
                preview.preview_hash if isinstance(preview, IntakeDraft) else preview.content_hash
            )
            if preview.version != version or not hmac.compare_digest(current_hash, approved_hash):
                raise DietError("VERSION_CONFLICT", "Preview has changed.")
            if preview.state == "committed":
                raise DietError("INVALID_PREVIEW_STATE", "A saved operation requires a new change.")
            now = self.clock()
            if now >= preview.expires_at:
                raise DietError("PREVIEW_EXPIRED", "Preview has expired.")
            if preview.state == "discarded":
                return preview
            discarded = preview.model_copy(
                update={
                    "version": version + 1,
                    "state": "discarded",
                    "updated_at": now,
                }
            )
            if isinstance(discarded, IntakeDraft):
                await tx.save_draft(discarded)
            else:
                await tx.save_preview(discarded)
            return discarded

    async def prepare_day(
        self,
        user_id: int,
        request: DayStatusRequest,
        *,
        idempotency_key: str,
    ) -> DietChangePreview:
        async with self.store.transaction(user_id, default_timezone=self.default_timezone) as tx:
            request_hash = content_hash(request)
            receipt = await tx.get_receipt("prepare_day_status", idempotency_key)
            if receipt:
                if receipt.request_hash != request_hash:
                    raise DietError("IDEMPOTENCY_CONFLICT", "Request key has different content.")
                saved = await tx.get_preview(receipt.result_refs[0].uuid)
                if saved is None:
                    raise DietError("RESOURCE_PURGED", "Preview has been purged.")
                return saved
            before = await tx.get_day_status(request.local_date)
            if before.version != request.base_version:
                raise DietError("VERSION_CONFLICT", "Day declaration has changed.")
            if before.status == request.status:
                raise DietError("NO_CHANGE", "Day declaration has no changes.")
            if request.status == "no_intake" and await tx.list_logs(
                request.local_date, request.local_date
            ):
                raise DietError("DAY_HAS_RECORDS", "An active record prevents declaring no intake.")
            now = self.clock()
            today = SubmissionAnchor.capture(now, tx.state.timezone).anchor_local_date
            if request.local_date > today and request.status != "open":
                raise DietError("FUTURE_DAY", "Future days cannot be declared complete.")
            content = DayStatusChangeContent(
                operation="day_status_set",
                before=before,
                after=DietDayDeclaration(
                    local_date=request.local_date,
                    version=before.version + 1,
                    status=request.status,
                ),
                base_refs=[],
                base_diet_epoch=tx.state.diet_epoch,
                owner=user_id,
            )
            preview = DietChangePreview(
                id=uuid4(),
                user_id=user_id,
                created_at=now,
                updated_at=now,
                content=content,
                content_hash=content_hash(content),
                expires_at=now + DRAFT_LIFETIME,
            )
            await tx.save_preview(preview)
            await tx.save_receipt(
                OperationReceipt(
                    id=uuid4(),
                    user_id=user_id,
                    operation="prepare_day_status",
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    result_refs=[ResourceRef(kind="preview", id=preview.id, version=1)],
                    result_status="prepared",
                    created_at=now,
                )
            )
            return preview

    async def _confirm_day(
        self,
        user_id: int,
        preview_id: UUID,
        *,
        version: int,
        approved_hash: str,
    ) -> OperationReceipt:
        async with self.store.transaction(user_id, default_timezone=self.default_timezone) as tx:
            preview = await tx.get_preview(preview_id)
            if preview is None or not isinstance(preview.content, DayStatusChangeContent):
                raise DietError("NOT_FOUND", "Day preview not found.")
            key = f"{preview_id}:{version}"
            previous = await tx.get_receipt("confirm:day_status_set", key)
            if previous:
                if not hmac.compare_digest(previous.request_hash, approved_hash):
                    raise DietError("IDEMPOTENCY_CONFLICT", "Approved content is different.")
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
            content = preview.content
            current = await tx.get_day_status(content.before.local_date)
            if current != content.before or tx.state.diet_epoch != content.base_diet_epoch:
                raise DietError("VERSION_CONFLICT", "Diet data has changed; review the day again.")
            await tx.set_day_status(content.after.local_date, content.after.status)
            receipt = OperationReceipt(
                id=uuid4(),
                user_id=user_id,
                operation="confirm:day_status_set",
                idempotency_key=key,
                request_hash=approved_hash,
                preview_id=preview_id,
                preview_version=version,
                result_refs=[
                    ResourceRef(
                        kind="day_status",
                        id=content.after.local_date.isoformat(),
                        version=content.after.version,
                    )
                ],
                result_status="committed",
                created_at=now,
            )
            await tx.add_revision(content.before, content.after, receipt)
            await tx.save_receipt(receipt)
            await tx.save_preview(
                preview.model_copy(
                    update={
                        "state": "committed",
                        "receipt_id": receipt.id,
                        "updated_at": now,
                    }
                )
            )
            await tx.advance_epoch()
            return receipt
