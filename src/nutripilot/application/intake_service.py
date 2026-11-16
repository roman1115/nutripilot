"""One owner-scoped prepare/confirm path for manual, model and plan intake."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field

from nutripilot.application.diet_patches import apply_entry_patch
from nutripilot.domain.diet import (
    DRAFT_LIFETIME,
    DietError,
    DietModel,
    DraftEntry,
    SubmissionAnchor,
    calculate_nutrition,
    content_hash,
    resolve_occurred_at,
)
from nutripilot.domain.diet_records import (
    DietItem,
    DietLog,
    IntakeDraft,
    OperationReceipt,
    PreviewContent,
    ResourceRef,
)
from nutripilot.tools.local.diet_models import (
    CreateIntake,
    DiscardIntake,
    PlanSource,
    ReviseIntake,
)
from nutripilot.tools.local.ports import DietFoodResolver, DietStore, DietTransaction
from nutripilot.tools.local.ports import ResolvedFood as ResolvedFood


class IntakeAlreadyRecorded(DietError):
    def __init__(self, record: DietLog) -> None:
        super().__init__("ALREADY_RECORDED", "This planned meal already has an active record.")
        self.record = record


class IntakePreparation(DietModel):
    request: CreateIntake
    channel: Literal["manual", "text", "image", "chat", "plan"]
    thread_id: str | None
    source_turn_id: str | None
    explicit_repeat_authorized: bool


class UserDraftEdit(DietModel):
    actuality: Literal["actual", "planned", "hypothetical", "unknown"] | None = None
    subject: Literal["self", "third_party", "unknown"] | None = None
    unlock_fields: list[str] = Field(default_factory=list, max_length=200)


class IntakeRevision(DietModel):
    request: ReviseIntake
    actor: Literal["user", "model"]
    user_edit: UserDraftEdit | None = None


class IntakeService:
    def __init__(
        self,
        store: DietStore,
        foods: DietFoodResolver,
        *,
        default_timezone: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.foods = foods
        self.default_timezone = default_timezone
        self.clock = clock or (lambda: datetime.now(UTC))

    async def prepare(
        self,
        user_id: int,
        request: CreateIntake,
        *,
        idempotency_key: str,
        anchor: SubmissionAnchor,
        channel: Literal["manual", "text", "image", "chat", "plan"] = "manual",
        thread_id: str | None = None,
        source_turn_id: str | None = None,
        explicit_repeat_authorized: bool = False,
    ) -> IntakeDraft:
        if request.media_ids:
            raise DietError(
                "SOURCE_REQUIRED",
                "Media intake requires the authorized media preparation path.",
            )
        now = self.clock()
        async with self.store.transaction(user_id, default_timezone=self.default_timezone) as tx:
            previous = await tx.get_receipt("prepare_intake", idempotency_key)
            # HTTP retries keep the original submission anchor, including across midnight.
            request_hash = content_hash(
                IntakePreparation(
                    request=request,
                    channel=channel,
                    thread_id=thread_id,
                    source_turn_id=source_turn_id,
                    explicit_repeat_authorized=explicit_repeat_authorized,
                )
            )
            if previous is not None:
                return await self._prepared_retry(tx, previous, request_hash)
            source_items, source_refs, event_kind, event_key = await self._source(
                tx, request, explicit_repeat_authorized=explicit_repeat_authorized
            )
            frozen_by_key = {item.client_item_key: item for item in source_items}
            items = []
            for position, proposed in enumerate(request.entry.items):
                frozen = frozen_by_key.get(proposed.client_item_key)
                matched = frozen is not None and frozen.food_ref == proposed.food
                resolved = (
                    ResolvedFood(frozen.name_snapshot, frozen.evidence_snapshot)
                    if matched and frozen is not None
                    else await self.foods.resolve(proposed.food, user_id=user_id, now=now, tx=tx)
                )
                items.append(
                    DietItem(
                        id=uuid4(),
                        client_item_key=proposed.client_item_key,
                        position=position,
                        food_ref=proposed.food,
                        name_snapshot=resolved.name,
                        quantity=proposed.quantity,
                        evidence_snapshot=resolved.evidence,
                        nutrition_snapshot=calculate_nutrition(
                            proposed.quantity, resolved.evidence
                        ),
                        field_provenance=[
                            item
                            for item in request.evidence
                            if item.item_key in (None, proposed.client_item_key)
                        ],
                        note=proposed.note,
                        source_planned_item_id=(
                            frozen.id
                            if matched
                            and frozen is not None
                            and isinstance(request.source, PlanSource)
                            else None
                        ),
                    )
                )
            content = PreviewContent(
                operation="log_create",
                entry=request.entry,
                items=items,
                anchor=anchor,
                source_refs=source_refs,
                source_event_kind=event_kind,
                source_event_key=event_key,
                field_locks=(
                    [
                        "actuality",
                        "subject",
                        "items",
                        *(f"temporal.{field}" for field in request.entry.temporal.model_fields_set),
                    ]
                    if channel in ("manual", "plan")
                    else []
                ),
            )
            blocking = request.entry.blocking_fields(today=anchor.anchor_local_date)
            if not blocking:
                resolve_occurred_at(request.entry.temporal, anchor.timezone)
            draft = IntakeDraft(
                id=uuid4(),
                user_id=user_id,
                created_at=now,
                updated_at=now,
                thread_id=thread_id,
                source_turn_id=source_turn_id,
                channel=channel,
                state="needs_input" if blocking else "ready",
                content=content,
                evidence=request.evidence,
                preview_hash=content_hash(content),
                expires_at=now + DRAFT_LIFETIME,
            )
            await tx.save_draft(draft)
            await tx.save_receipt(
                OperationReceipt(
                    id=uuid4(),
                    user_id=user_id,
                    operation="prepare_intake",
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    result_refs=[ResourceRef(kind="draft", id=draft.id, version=draft.version)],
                    result_status="prepared",
                    created_at=now,
                )
            )
            return draft

    async def _prepared_retry(
        self,
        tx: DietTransaction,
        receipt: OperationReceipt,
        request_hash: str,
    ) -> IntakeDraft:
        if receipt.request_hash != request_hash:
            raise DietError("IDEMPOTENCY_CONFLICT", "This request key has different content.")
        draft = await tx.get_draft(receipt.result_refs[0].uuid)
        if draft is None:
            raise DietError("RESOURCE_PURGED", "The original draft is no longer available.")
        return draft

    async def confirm(
        self,
        user_id: int,
        draft_id: UUID,
        *,
        version: int,
        approved_hash: str,
    ) -> OperationReceipt:
        operation, key = "confirm:log_create", f"{draft_id}:{version}"
        async with self.store.transaction(user_id, default_timezone=self.default_timezone) as tx:
            previous = await tx.get_receipt(operation, key)
            if previous is not None:
                if previous.request_hash != approved_hash:
                    raise DietError("IDEMPOTENCY_CONFLICT", "The approved content is different.")
                current = await tx.get_log(previous.result_refs[0].uuid)
                if current is None:
                    return previous.model_copy(update={"result_status": "purged"})
                if current.deleted_at is not None:
                    return previous.model_copy(update={"result_status": "deleted"})
                return previous
            draft = await tx.get_draft(draft_id)
            if draft is None:
                raise DietError("NOT_FOUND", "Draft not found.")
            now = self.clock()
            draft.assert_confirmable(
                user_id=user_id,
                version=version,
                approved_hash=approved_hash,
                now=now,
            )
            for item in draft.content.items:
                await self.foods.assert_available(item.food_ref, user_id=user_id, tx=tx)
            content = draft.content
            planned_id: UUID | None = None
            for ref in content.source_refs:
                if ref.kind == "planned_meal":
                    meal = await tx.get_planned_meal(ref.uuid)
                    if meal is None or meal.deleted_at is not None:
                        raise DietError("SOURCE_CHANGED", "Planned meal is no longer active.")
                    if meal.version != ref.version:
                        raise DietError("VERSION_CONFLICT", "Planned meal has changed.")
                    planned_id = meal.id
                elif ref.kind == "plan":
                    plan = await tx.get_plan(str(ref.id))
                    if plan is None or plan.deleted_at is not None:
                        raise DietError("SOURCE_CHANGED", "Source plan is no longer active.")
                    if plan.version != ref.version:
                        raise DietError("VERSION_CONFLICT", "Source plan has changed.")
                elif ref.kind == "log":
                    source = await tx.get_log(ref.uuid)
                    if source is None or source.deleted_at is not None:
                        raise DietError("SOURCE_CHANGED", "Source record is no longer active.")
                    if source.version != ref.version:
                        raise DietError("VERSION_CONFLICT", "Source record has changed.")
            existing = (
                await tx.find_source_event(content.source_event_key)
                if content.source_event_key
                else None
            )
            if planned_id and content.source_event_kind == "default_plan":
                active = next(
                    (
                        log
                        for log in await tx.list_source_logs(planned_id)
                        if log.deleted_at is None and log.source_event_kind == "default_plan"
                    ),
                    None,
                )
                if active is not None and (existing is None or active.id != existing.id):
                    raise IntakeAlreadyRecorded(active)
            if existing is not None:
                if existing.deleted_at is not None:
                    raise DietError("SOURCE_CHANGED", "The original event has been deleted.")
                if not self._same_event(existing, content):
                    raise IntakeAlreadyRecorded(existing)
                log = existing
            else:
                log = DietLog(
                    id=uuid4(),
                    user_id=user_id,
                    created_at=now,
                    updated_at=now,
                    temporal=content.entry.temporal,
                    timezone=content.anchor.timezone,
                    occurred_at=resolve_occurred_at(
                        content.entry.temporal, content.anchor.timezone
                    ),
                    channel=draft.channel,
                    items=content.items,
                    source_planned_meal_id=planned_id,
                    source_event_kind=content.source_event_kind,
                    source_event_key=content.source_event_key,
                )
            receipt = OperationReceipt(
                id=uuid4(),
                user_id=user_id,
                operation=operation,
                idempotency_key=key,
                request_hash=approved_hash,
                preview_id=draft_id,
                preview_version=version,
                result_refs=[ResourceRef(kind="log", id=log.id, version=log.version)],
                result_status="committed",
                created_at=now,
            )
            if existing is None:
                await tx.save_log(log)
                if log.temporal.local_date is not None:
                    await tx.set_day_status(log.temporal.local_date, "open")
                await tx.add_revision(None, log, receipt)
            await tx.save_receipt(receipt)
            await tx.save_draft(
                IntakeDraft.model_validate(
                    {
                        **draft.model_dump(),
                        "state": "committed",
                        "receipt_id": receipt.id,
                        "updated_at": now,
                    }
                )
            )
            if existing is None:
                await tx.advance_epoch()
            return receipt

    async def _source(
        self,
        tx: DietTransaction,
        request: CreateIntake,
        *,
        explicit_repeat_authorized: bool,
    ) -> tuple[
        list[DietItem],
        list[ResourceRef],
        Literal["default_plan", "explicit_repeat", "copy"] | None,
        str | None,
    ]:
        source = request.source
        if source is None:
            return [], [], None, None
        if isinstance(source, PlanSource):
            meal = await tx.get_planned_meal(source.planned_meal_id)
            if meal is None or meal.deleted_at is not None:
                raise DietError("NOT_FOUND", "Planned meal not found.")
            if meal.version != source.base_version:
                raise DietError("VERSION_CONFLICT", "Planned meal has changed.")
            plan = await tx.get_plan(meal.plan_id)
            if plan is None or plan.deleted_at is not None:
                raise DietError("NOT_FOUND", "Plan not found.")
            if source.record_again:
                if not explicit_repeat_authorized:
                    raise DietError(
                        "EXPLICIT_ACTION_REQUIRED",
                        "Repeat intake requires an explicit user action.",
                    )
                kind: Literal["default_plan", "explicit_repeat", "copy"] = "explicit_repeat"
                key = f"repeat:{uuid4()}"
            else:
                active = next(
                    (
                        log
                        for log in await tx.list_source_logs(meal.id)
                        if log.deleted_at is None and log.source_event_kind == "default_plan"
                    ),
                    None,
                )
                if active:
                    raise IntakeAlreadyRecorded(active)
                kind, key = (
                    "default_plan",
                    f"plan:{tx.state.user_id}:{meal.id}:{meal.intake_generation}",
                )
            return (
                meal.items,
                [
                    ResourceRef(kind="planned_meal", id=meal.id, version=meal.version),
                    ResourceRef(kind="plan", id=plan.id, version=plan.version),
                ],
                kind,
                key,
            )
        record = await tx.get_log(source.record_id)
        if record is None or record.deleted_at is not None:
            raise DietError("NOT_FOUND", "Source record not found.")
        if record.version != source.base_version:
            raise DietError("VERSION_CONFLICT", "Source record has changed.")
        return (
            record.items,
            [
                ResourceRef(kind="log", id=record.id, version=record.version),
            ],
            "copy",
            f"copy:{uuid4()}",
        )

    @staticmethod
    def _same_event(log: DietLog, content: PreviewContent) -> bool:
        return (
            log.temporal == content.entry.temporal
            and log.timezone == content.anchor.timezone
            and [
                item.model_dump(exclude={"id", "version", "field_provenance"}) for item in log.items
            ]
            == [
                item.model_dump(exclude={"id", "version", "field_provenance"})
                for item in content.items
            ]
        )

    async def revise(
        self,
        user_id: int,
        request: ReviseIntake,
        *,
        idempotency_key: str,
        actor: Literal["user", "model"] = "user",
        user_edit: UserDraftEdit | None = None,
    ) -> IntakeDraft:
        async with self.store.transaction(user_id, default_timezone=self.default_timezone) as tx:
            previous = await tx.get_receipt("revise_intake", idempotency_key)
            if user_edit is not None and actor != "user":
                raise DietError("EXPLICIT_ACTION_REQUIRED", "Only the user may unlock fields.")
            request_hash = content_hash(
                IntakeRevision(request=request, actor=actor, user_edit=user_edit)
            )
            if previous:
                return await self._prepared_retry(tx, previous, request_hash)
            draft = await self._editable(tx, request.draft_id, request.base_version)
            touched = self._patch_fields(request, draft)
            if actor == "model" and set(touched) & set(draft.content.field_locks):
                raise DietError("FIELD_LOCKED", "人工修改的字段已锁定，请在页面核对或明确解锁。")
            now = self.clock()
            entry, items = await apply_entry_patch(
                draft.content.entry,
                draft.content.items,
                request.patch,
                foods=self.foods,
                user_id=user_id,
                now=now,
                evidence=request.evidence,
                tx=tx,
                allow_empty=isinstance(draft.content.entry, DraftEntry) and not draft.content.items,
                image_estimation=draft.image_estimation,
                media_ids=draft.media_ids,
            )
            if user_edit:
                changes = user_edit.model_dump(exclude_none=True, exclude={"unlock_fields"})
                entry = DraftEntry.model_validate({**entry.model_dump(), **changes})
                touched.extend(changes)
                if set(user_edit.unlock_fields) - set(draft.content.field_locks):
                    raise DietError("INVALID_FIELD_LOCK", "Only existing locks may be removed.")
            if (
                entry == draft.content.entry
                and items == draft.content.items
                and not (user_edit and user_edit.unlock_fields)
            ):
                raise DietError("NO_CHANGE", "The draft has no changes.")
            content = PreviewContent.model_validate(
                {
                    **draft.content.model_dump(),
                    "entry": entry,
                    "items": items,
                    "field_locks": sorted(
                        (set(draft.content.field_locks) | set(touched if actor == "user" else []))
                        - set(user_edit.unlock_fields if user_edit else [])
                    ),
                }
            )
            if content == draft.content:
                raise DietError("NO_CHANGE", "The draft has no changes.")
            blocking = entry.blocking_fields(today=content.anchor.anchor_local_date)
            if not blocking:
                resolve_occurred_at(entry.temporal, content.anchor.timezone)
            revised = IntakeDraft.model_validate(
                {
                    **draft.model_dump(),
                    "version": draft.version + 1,
                    "updated_at": now,
                    "state": "needs_input" if blocking else "ready",
                    "content": content,
                    "preview_hash": content_hash(content),
                    "field_locks_version": draft.field_locks_version
                    + int(content.field_locks != draft.content.field_locks),
                    "evidence": [
                        *draft.evidence,
                        *request.evidence,
                    ],
                }
            )
            await tx.save_draft(revised)
            await tx.save_receipt(
                OperationReceipt(
                    id=uuid4(),
                    user_id=user_id,
                    operation="revise_intake",
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    result_refs=[ResourceRef(kind="draft", id=revised.id, version=revised.version)],
                    result_status="prepared",
                    created_at=now,
                )
            )
            return revised

    @staticmethod
    def _patch_fields(request: ReviseIntake, draft: IntakeDraft) -> list[str]:
        from nutripilot.tools.local.diet_models import AddItem, RemoveItem

        fields = [
            f"temporal.{field}"
            for field in (request.patch.temporal.model_fields_set if request.patch.temporal else [])
        ]
        for edit in request.patch.item_edits or []:
            if isinstance(edit, AddItem):
                fields.append("items")
            elif isinstance(edit, RemoveItem):
                fields.extend(
                    [
                        "items",
                        *(
                            f"items.{edit.item_id}.{field}"
                            for field in ("food", "quantity", "note")
                        ),
                    ]
                )
            else:
                fields.extend(
                    f"items.{edit.item_id}.{field}" for field in edit.changes.model_fields_set
                )
        if "items" in draft.content.field_locks and request.patch.item_edits:
            fields.append("items")
        return fields

    async def discard(
        self,
        user_id: int,
        request: DiscardIntake,
        *,
        idempotency_key: str,
    ) -> IntakeDraft:
        async with self.store.transaction(user_id, default_timezone=self.default_timezone) as tx:
            previous = await tx.get_receipt("discard_intake", idempotency_key)
            request_hash = content_hash(request)
            if previous:
                return await self._prepared_retry(tx, previous, request_hash)
            draft = await self._editable(tx, request.draft_id, request.base_version)
            discarded = draft.discarded(self.clock())
            await tx.save_draft(discarded)
            await tx.save_receipt(
                OperationReceipt(
                    id=uuid4(),
                    user_id=user_id,
                    operation="discard_intake",
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    result_refs=[
                        ResourceRef(kind="draft", id=discarded.id, version=discarded.version)
                    ],
                    result_status="discarded",
                    created_at=self.clock(),
                )
            )
            return discarded

    async def _editable(
        self,
        tx: DietTransaction,
        draft_id: UUID,
        base_version: int,
    ) -> IntakeDraft:
        draft = await tx.get_draft(draft_id)
        if draft is None:
            raise DietError("NOT_FOUND", "Draft not found.")
        if draft.version != base_version:
            raise DietError("VERSION_CONFLICT", "The draft has changed.")
        if self.clock() >= draft.expires_at:
            raise DietError("PREVIEW_EXPIRED", "The draft has expired.")
        if draft.state not in ("ready", "needs_input"):
            raise DietError("INVALID_PREVIEW_STATE", "The draft cannot be edited.")
        return draft
