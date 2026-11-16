"""Prepare and atomically confirm complete meal-plan changes."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Literal
from uuid import UUID, uuid4

from nutripilot.application.diet_patches import apply_entry_patch
from nutripilot.domain.diet import (
    DRAFT_LIFETIME,
    TRASH_LIFETIME,
    DietError,
    FieldEvidence,
    ProposedEntry,
    calculate_nutrition,
    content_hash,
)
from nutripilot.domain.diet_records import (
    DietChangePreview,
    DietItem,
    DietPlan,
    OperationReceipt,
    PlanChangeContent,
    PlannedMeal,
    ResourceRef,
)
from nutripilot.tools.local.diet_models import (
    CopyMealPlan,
    CopyPlanWeek,
    CreateMealPlan,
    DeleteMealPlan,
    NewPlan,
    UpdateMealPlan,
)
from nutripilot.tools.local.ports import DietFoodResolver, DietStore, DietTransaction

type PlanRequest = CreateMealPlan | UpdateMealPlan | DeleteMealPlan | CopyMealPlan | CopyPlanWeek


class PlanService:
    def __init__(
        self,
        store: DietStore,
        foods: DietFoodResolver,
        *,
        default_timezone: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store, self.foods = store, foods
        self.default_timezone = default_timezone
        self.clock = clock or (lambda: datetime.now(UTC))

    async def prepare(
        self,
        user_id: int,
        request: PlanRequest,
        *,
        idempotency_key: str,
        thread_id: str | None = None,
        source_turn_id: str | None = None,
    ) -> DietChangePreview:
        now = self.clock()
        async with self.store.transaction(user_id, default_timezone=self.default_timezone) as tx:
            request_hash = content_hash(request)
            retry = await self._retry(tx, idempotency_key, request_hash)
            if retry is not None:
                return retry
            if isinstance(request, CopyPlanWeek):
                content = await self._copy_week(tx, request, now)
            elif isinstance(request, CreateMealPlan):
                if isinstance(request.parent, NewPlan):
                    before_plan = None
                    plan = DietPlan(
                        id=str(uuid4()),
                        user_id=user_id,
                        title=request.parent.title,
                        start_date=request.parent.start_date,
                        created_at=now,
                        updated_at=now,
                        migrated_at=now,
                    )
                else:
                    before_plan = await self._plan(tx, str(request.parent.plan_id))
                    self._version(before_plan.version, request.parent.base_version)
                    plan = before_plan.model_copy(
                        update={
                            "version": before_plan.version + 1,
                            "updated_at": now,
                        }
                    )
                existing = await tx.list_planned_meals(plan.id)
                used = {(meal.local_date, meal.meal_type) for meal in existing}
                merged: dict[tuple[date, str], PlannedMeal] = {}
                for proposed in request.entries:
                    entry = proposed.entry
                    day, meal_type = entry.temporal.local_date, entry.temporal.meal_type
                    assert day is not None and meal_type is not None
                    if (day, meal_type) in used:
                        raise DietError(
                            "PLAN_SLOT_CONFLICT", "Choose append or replace for an existing meal."
                        )
                    self._no_plan_time(entry)
                    items = await self._resolve_items(tx, entry, request.evidence, now)
                    slot = (day, meal_type)
                    previous = merged.get(slot)
                    if previous:
                        if {item.client_item_key for item in previous.items} & {
                            item.client_item_key for item in items
                        }:
                            raise DietError(
                                "ITEM_KEY_CONFLICT", "Merged meals have duplicate item keys."
                            )
                        combined = [
                            item.model_copy(update={"position": position})
                            for position, item in enumerate([*previous.items, *items])
                        ]
                        notes = list(
                            dict.fromkeys(
                                note for note in (previous.note, entry.temporal.note) if note
                            )
                        )
                        merged[slot] = PlannedMeal.model_validate(
                            {
                                **previous.model_dump(),
                                "items": combined,
                                "note": "\n".join(notes) if notes else None,
                            }
                        )
                    else:
                        merged[slot] = PlannedMeal(
                            id=uuid4(),
                            user_id=user_id,
                            plan_id=plan.id,
                            local_date=day,
                            meal_type=meal_type,
                            items=items,
                            note=entry.temporal.note,
                            created_at=now,
                            updated_at=now,
                        )
                content = PlanChangeContent(
                    operation="plan_create",
                    before_plan=before_plan,
                    after_plan=plan,
                    before_meals=[],
                    after_meals=list(merged.values()),
                    base_refs=([self._plan_ref(before_plan)] if before_plan else []),
                )
            else:
                meal = await tx.get_planned_meal(request.planned_meal_id)
                if meal is None or meal.deleted_at is not None:
                    raise DietError("NOT_FOUND", "Planned meal not found.")
                self._version(meal.version, request.base_version)
                before_plan = await self._plan(tx, meal.plan_id)
                plan = before_plan.model_copy(
                    update={
                        "version": before_plan.version + 1,
                        "updated_at": now,
                    }
                )
                if isinstance(request, DeleteMealPlan):
                    after = meal.model_copy(
                        update={
                            "version": meal.version + 1,
                            "deleted_at": now,
                            "updated_at": now,
                        }
                    )
                    operation: Literal["plan_update", "plan_delete_meal", "plan_copy_meal"] = (
                        "plan_delete_meal"
                    )
                elif isinstance(request, CopyMealPlan):
                    after = meal.model_copy(
                        update={
                            "id": uuid4(),
                            "version": 1,
                            "local_date": request.target_date,
                            "meal_type": request.target_meal_type,
                            "created_at": now,
                            "updated_at": now,
                            "intake_generation": 1,
                            "legacy_item_refs": {},
                            "items": self._copied_items(meal.items),
                        }
                    )
                    operation = "plan_copy_meal"
                else:
                    original = self.entry(meal)
                    entry, items = await apply_entry_patch(
                        original,
                        meal.items,
                        request.patch,
                        foods=self.foods,
                        user_id=user_id,
                        now=now,
                        evidence=request.evidence,
                        tx=tx,
                    )
                    self._no_plan_time(entry)
                    if entry.blocking_fields(planned=True, today=plan.start_date):
                        raise DietError(
                            "INCOMPLETE_PLAN", "A plan requires its date and meal type."
                        )
                    if entry == original and items == meal.items:
                        raise DietError("NO_CHANGE", "The planned meal has no changes.")
                    after = PlannedMeal.model_validate(
                        {
                            **meal.model_dump(),
                            "version": meal.version + 1,
                            "updated_at": now,
                            "local_date": entry.temporal.local_date,
                            "meal_type": entry.temporal.meal_type,
                            "note": entry.temporal.note,
                            "items": items,
                        }
                    )
                    operation = "plan_update"
                others = await tx.list_planned_meals(plan.id)
                if after.deleted_at is None and any(
                    other.id != after.id
                    and (other.local_date, other.meal_type) == (after.local_date, after.meal_type)
                    for other in others
                ):
                    raise DietError(
                        "PLAN_SLOT_CONFLICT", "A meal already uses the destination slot."
                    )
                content = PlanChangeContent(
                    operation=operation,
                    before_plan=before_plan,
                    after_plan=plan,
                    before_meals=[] if isinstance(request, CopyMealPlan) else [meal],
                    after_meals=[after],
                    base_refs=[self._plan_ref(before_plan), self._meal_ref(meal)],
                )
            return await self._save_preview(
                tx, content, idempotency_key, request_hash, now, thread_id, source_turn_id
            )

    async def prepare_archive_restore(
        self,
        user_id: int,
        plan_id: str,
        *,
        base_version: int,
        action: Literal["archive", "restore"],
        idempotency_key: str,
    ) -> DietChangePreview:
        async with self.store.transaction(user_id, default_timezone=self.default_timezone) as tx:
            request_hash = content_hash(ResourceRef(kind="plan", id=plan_id, version=base_version))
            key = hashlib.sha256(f"{action}:{idempotency_key}".encode()).hexdigest()
            retry = await self._retry(tx, key, request_hash)
            if retry:
                return retry
            plan = await tx.get_plan(plan_id)
            if plan is None:
                raise DietError("NOT_FOUND", "Plan not found.")
            self._version(plan.version, base_version)
            now = self.clock()
            if action == "archive" and plan.deleted_at is not None:
                raise DietError("NO_CHANGE", "Plan is already archived.")
            if action == "restore":
                self._restore_window(plan, now)
            after = plan.model_copy(
                update={
                    "version": plan.version + 1,
                    "updated_at": now,
                    "deleted_at": now if action == "archive" else None,
                }
            )
            content = PlanChangeContent(
                operation="plan_archive" if action == "archive" else "plan_restore",
                before_plan=plan,
                after_plan=after,
                before_meals=[],
                after_meals=[],
                base_refs=[self._plan_ref(plan)],
            )
            return await self._save_preview(tx, content, key, request_hash, now, None)

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
            if not isinstance(preview.content, PlanChangeContent):
                raise DietError("INVALID_PREVIEW_STATE", "Expected a plan preview.")
            content = preview.content
            operation, key = f"confirm:{content.operation}", f"{preview_id}:{version}"
            previous = await tx.get_receipt(operation, key)
            if previous:
                if not hmac.compare_digest(previous.request_hash, approved_hash):
                    raise DietError("IDEMPOTENCY_CONFLICT", "Approved content is different.")
                current = await tx.get_plan(content.after_plan.id)
                status = (
                    "purged"
                    if current is None
                    else ("deleted" if current.deleted_at else previous.result_status)
                )
                return previous.model_copy(update={"result_status": status})
            now = self.clock()
            if preview.version != version or not hmac.compare_digest(
                preview.content_hash, approved_hash
            ):
                raise DietError("VERSION_CONFLICT", "Preview has changed.")
            if now >= preview.expires_at:
                raise DietError("PREVIEW_EXPIRED", "Preview has expired.")
            if preview.state != "ready":
                raise DietError("INVALID_PREVIEW_STATE", "Preview is not ready.")
            for ref in content.base_refs:
                if ref.kind == "plan":
                    current_plan = await tx.get_plan(str(ref.id))
                    if current_plan is None:
                        raise DietError("NOT_FOUND", "Source plan not found.")
                    self._version(current_plan.version, ref.version)
                    if content.operation == "plan_restore":
                        self._restore_window(current_plan, now)
                    elif current_plan.deleted_at is not None:
                        raise DietError("SOURCE_CHANGED", "Source plan has been archived.")
                elif ref.kind == "planned_meal":
                    current_meal = await tx.get_planned_meal(ref.uuid)
                    if current_meal is None:
                        raise DietError("NOT_FOUND", "Source meal not found.")
                    self._version(current_meal.version, ref.version)
            for meal in content.after_meals:
                for item in meal.items:
                    await self.foods.assert_available(item.food_ref, user_id=user_id, tx=tx)
            receipt = OperationReceipt(
                id=uuid4(),
                user_id=user_id,
                operation=operation,
                idempotency_key=key,
                request_hash=approved_hash,
                preview_id=preview_id,
                preview_version=version,
                result_refs=[
                    self._plan_ref(content.after_plan),
                    *(self._meal_ref(meal) for meal in content.after_meals),
                ],
                result_status="committed",
                created_at=now,
            )
            await tx.save_plan(content.after_plan)
            # Retire replaced slots before inserts so the active-slot index stays valid.
            for meal in sorted(content.after_meals, key=lambda meal: meal.deleted_at is None):
                await tx.save_planned_meal(meal)
                before = next((old for old in content.before_meals if old.id == meal.id), None)
                await tx.add_revision(before, meal, receipt)
            await tx.add_revision(content.before_plan, content.after_plan, receipt)
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
            await tx.advance_epoch()
            return receipt

    async def _resolve_items(
        self,
        tx: DietTransaction,
        entry: ProposedEntry,
        evidence: list[FieldEvidence],
        now: datetime,
    ) -> list[DietItem]:
        items = []
        for position, proposed in enumerate(entry.items):
            resolved = await self.foods.resolve(
                proposed.food, user_id=tx.state.user_id, now=now, tx=tx
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
                    nutrition_snapshot=calculate_nutrition(proposed.quantity, resolved.evidence),
                    field_provenance=[
                        field
                        for field in evidence
                        if field.item_key in (None, proposed.client_item_key)
                    ],
                    note=proposed.note,
                )
            )
        return items

    async def _copy_week(
        self,
        tx: DietTransaction,
        request: CopyPlanWeek,
        now: datetime,
    ) -> PlanChangeContent:
        source = await self._plan(tx, request.source_plan_id)
        self._version(source.version, request.base_version)
        originals = await tx.list_planned_meals(source.id)
        if not originals:
            raise DietError("NO_CHANGE", "The source plan has no active meals.")
        if sum(len(meal.items) for meal in originals) > 100:
            raise DietError("BATCH_TOO_LARGE", "A week copy may include at most 100 items.")
        if isinstance(request.parent, NewPlan):
            before_plan = None
            destination = DietPlan(
                id=str(uuid4()),
                user_id=tx.state.user_id,
                title=request.parent.title,
                start_date=request.target_start_date,
                created_at=now,
                updated_at=now,
                migrated_at=now,
            )
        else:
            before_plan = await self._plan(tx, str(request.parent.plan_id))
            self._version(before_plan.version, request.parent.base_version)
            if before_plan.start_date != request.target_start_date:
                raise DietError(
                    "PLAN_DATE_CONFLICT", "Destination plan belongs to a different week."
                )
            destination = before_plan.model_copy(
                update={
                    "version": before_plan.version + 1,
                    "updated_at": now,
                }
            )
        existing = {
            (meal.local_date, meal.meal_type): meal
            for meal in await tx.list_planned_meals(destination.id)
        }
        before_meals, after_meals = [], []
        shift = request.target_start_date - source.start_date
        for original in originals:
            day = original.local_date + shift
            conflict = existing.get((day, original.meal_type))
            copied = self._copied_items(original.items)
            if conflict is not None:
                if request.conflict_policy == "reject":
                    raise DietError(
                        "PLAN_SLOT_CONFLICT", "Choose append or replace for conflicting meals."
                    )
                before_meals.append(conflict)
                copied = [
                    item.model_copy(
                        update={
                            "client_item_key": f"c{uuid4().hex[:31]}",
                        }
                    )
                    for item in copied
                ]
                if request.conflict_policy == "append_items":
                    copied = [*conflict.items, *copied]
                after_meals.append(
                    PlannedMeal.model_validate(
                        {
                            **conflict.model_dump(),
                            "version": conflict.version + 1,
                            "updated_at": now,
                            "items": [
                                item.model_copy(update={"position": position})
                                for position, item in enumerate(copied)
                            ],
                        }
                    )
                )
            else:
                after_meals.append(
                    PlannedMeal(
                        id=uuid4(),
                        user_id=tx.state.user_id,
                        plan_id=destination.id,
                        local_date=day,
                        meal_type=original.meal_type,
                        items=copied,
                        note=original.note,
                        created_at=now,
                        updated_at=now,
                    )
                )
        refs = [self._plan_ref(source)]
        if before_plan and before_plan.id != source.id:
            refs.append(self._plan_ref(before_plan))
        refs.extend(self._meal_ref(meal) for meal in originals)
        return PlanChangeContent(
            operation="plan_copy_week",
            before_plan=before_plan,
            after_plan=destination,
            before_meals=before_meals,
            after_meals=after_meals,
            base_refs=refs,
        )

    async def _retry(
        self,
        tx: DietTransaction,
        key: str,
        request_hash: str,
    ) -> DietChangePreview | None:
        receipt = await tx.get_receipt("prepare_plan", key)
        if receipt is None:
            return None
        if receipt.request_hash != request_hash:
            raise DietError("IDEMPOTENCY_CONFLICT", "Request key has different content.")
        preview = await tx.get_preview(receipt.result_refs[0].uuid)
        if preview is None:
            raise DietError("RESOURCE_PURGED", "Preview has been purged.")
        return preview

    async def _save_preview(
        self,
        tx: DietTransaction,
        content: PlanChangeContent,
        key: str,
        request_hash: str,
        now: datetime,
        thread_id: str | None,
        source_turn_id: str | None = None,
    ) -> DietChangePreview:
        preview = DietChangePreview(
            id=uuid4(),
            user_id=tx.state.user_id,
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
                user_id=tx.state.user_id,
                operation="prepare_plan",
                idempotency_key=key,
                request_hash=request_hash,
                result_status="prepared",
                result_refs=[ResourceRef(kind="preview", id=preview.id, version=preview.version)],
                created_at=now,
            )
        )
        return preview

    @staticmethod
    async def _plan(tx: DietTransaction, plan_id: str) -> DietPlan:
        plan = await tx.get_plan(plan_id)
        if plan is None or plan.deleted_at is not None:
            raise DietError("NOT_FOUND", "Plan not found.")
        return plan

    @staticmethod
    def entry(meal: PlannedMeal) -> ProposedEntry:
        return ProposedEntry.model_validate(
            {
                "actuality": "planned",
                "subject": "self",
                "temporal": {
                    "local_date": meal.local_date,
                    "meal_type": meal.meal_type,
                    "note": meal.note,
                },
                "items": [
                    {
                        "client_item_key": item.client_item_key,
                        "food": item.food_ref,
                        "quantity": item.quantity,
                        **({"note": item.note} if item.note else {}),
                    }
                    for item in meal.items
                ],
            }
        )

    @staticmethod
    def _copied_items(items: list[DietItem]) -> list[DietItem]:
        return [
            item.model_copy(
                update={
                    "id": uuid4(),
                    "version": 1,
                    "source_planned_item_id": None,
                }
            )
            for item in items
        ]

    @staticmethod
    def _no_plan_time(entry: ProposedEntry) -> None:
        if entry.temporal.local_time is not None or entry.temporal.utc_offset_minutes is not None:
            raise DietError(
                "INVALID_PLAN_TIME", "Plans use dates and meal types, not actual timestamps."
            )

    @staticmethod
    def _version(actual: int, expected: int) -> None:
        if actual != expected:
            raise DietError("VERSION_CONFLICT", "The source has changed.")

    @staticmethod
    def _plan_ref(plan: DietPlan) -> ResourceRef:
        return ResourceRef(kind="plan", id=plan.id, version=plan.version)

    @staticmethod
    def _meal_ref(meal: PlannedMeal) -> ResourceRef:
        return ResourceRef(kind="planned_meal", id=meal.id, version=meal.version)

    @staticmethod
    def _restore_window(plan: DietPlan, now: datetime) -> None:
        if plan.deleted_at is None:
            raise DietError("NO_CHANGE", "Plan is not archived.")
        if now >= plan.deleted_at + TRASH_LIFETIME:
            raise DietError("RESOURCE_EXPIRED", "Plan restore window has expired.")
