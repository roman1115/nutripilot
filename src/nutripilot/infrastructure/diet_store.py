"""Owner-scoped in-memory and PostgreSQL diet transactions."""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from nutripilot.domain.diet import (
    TRASH_LIFETIME,
    DayStatus,
    DietError,
    NutrientRange,
    NutritionGoals,
)
from nutripilot.domain.diet_jobs import MediaAsset
from nutripilot.domain.diet_privacy import DietExportSnapshot, PurgeTombstone
from nutripilot.domain.diet_records import (
    ChangeContent,
    DietChangePreview,
    DietDayDeclaration,
    DietItem,
    DietLog,
    DietPlan,
    DietRevisionResource,
    DietUserState,
    IntakeDraft,
    NutritionTarget,
    OperationReceipt,
    PersonalFood,
    PlannedMeal,
    PurgeChangeContent,
    ResourceRef,
    ServingEvidence,
)
from nutripilot.tools.local.ports import DietTransaction

if TYPE_CHECKING:
    from nutripilot.infrastructure.diet_deletions import DeletionJournal


def _check_owner(user_id: int, actual_user_id: int) -> None:
    if user_id != actual_user_id:
        raise DietError("NOT_FOUND", "Resource not found.")


def _contains_media(value: object, media_id: str) -> bool:
    if isinstance(value, dict):
        return any(
            (key == "media_id" and part == media_id)
            or (key == "media_ids" and isinstance(part, list) and media_id in part)
            or _contains_media(part, media_id)
            for key, part in value.items()
        )
    return isinstance(value, list) and any(_contains_media(part, media_id) for part in value)


def _media_ids(value: object) -> set[UUID]:
    result: set[UUID] = set()
    if isinstance(value, dict):
        for key, part in value.items():
            if key == "media_id" and isinstance(part, str):
                result.add(UUID(part))
            elif key == "media_ids" and isinstance(part, list):
                result.update(UUID(item) for item in part)
            else:
                result.update(_media_ids(part))
    elif isinstance(value, list):
        for part in value:
            result.update(_media_ids(part))
    return result


def _contains_reference(value: object, ref: ResourceRef) -> bool:
    if isinstance(value, dict):
        if value.get("kind") == ref.kind and value.get("id") == str(ref.id):
            return True
        field_name = {
            "plan": "plan_id",
            "planned_meal": "planned_meal_id",
            "log": "record_id",
            "personal_food": "food_id",
            "target": "target_id",
        }.get(ref.kind)
        if field_name and value.get(field_name) == str(ref.id):
            return True
        return any(_contains_reference(part, ref) for part in value.values())
    return isinstance(value, list) and any(_contains_reference(part, ref) for part in value)


def _revision_matches(value: DietRevisionResource, refs: list[ResourceRef]) -> bool:
    kinds = {
        DietLog: "log",
        DietPlan: "plan",
        PlannedMeal: "planned_meal",
        PersonalFood: "personal_food",
        NutritionTarget: "target",
    }
    return not isinstance(value, DietDayDeclaration) and any(
        kinds.get(type(value)) == ref.kind and str(value.id) == str(ref.id) for ref in refs
    )


async def _purge_resource(
    tx: DietTransaction, ref: ResourceRef
) -> DietPlan | PlannedMeal | PersonalFood | NutritionTarget | None:
    resource: DietPlan | PlannedMeal | PersonalFood | NutritionTarget | None
    if ref.kind == "plan":
        resource = await tx.get_plan(str(ref.id))
    elif ref.kind == "planned_meal":
        resource = await tx.get_planned_meal(ref.uuid)
    elif ref.kind == "personal_food":
        resource = await tx.get_personal_food(ref.uuid)
    elif ref.kind == "target":
        resource = next((target for target in await tx.list_targets() if target.id == ref.id), None)
    else:
        raise DietError("INVALID_FIELDS", "Unsupported purge resource.")
    if resource is not None:
        if resource.version != ref.version:
            raise DietError("VERSION_CONFLICT", "Resource changed; review purge again.")
        if not isinstance(resource, NutritionTarget) and resource.deleted_at is None:
            raise DietError("PURGE_REQUIRES_DELETED", "请先删除资料，再核对永久清除影响。")
    return resource


@dataclass
class _MemoryData:
    state: DietUserState
    drafts: dict[UUID, IntakeDraft] = field(default_factory=dict)
    previews: dict[UUID, DietChangePreview] = field(default_factory=dict)
    logs: dict[UUID, DietLog] = field(default_factory=dict)
    plans: dict[str, DietPlan] = field(default_factory=dict)
    planned_meals: dict[UUID, PlannedMeal] = field(default_factory=dict)
    personal_foods: dict[UUID, PersonalFood] = field(default_factory=dict)
    receipts: dict[tuple[str, str], OperationReceipt] = field(default_factory=dict)
    revisions: list[tuple[DietRevisionResource | None, DietRevisionResource, UUID]] = field(
        default_factory=list
    )
    targets: dict[UUID, NutritionTarget] = field(default_factory=dict)
    days: dict[date, DietDayDeclaration] = field(default_factory=dict)
    media: dict[UUID, MediaAsset] = field(default_factory=dict)
    tombstones: dict[tuple[str, str], PurgeTombstone] = field(default_factory=dict)


class MemoryDietStore:
    def __init__(self) -> None:
        self._users: dict[int, _MemoryData] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    async def list_users(self, after: int = 0, limit: int = 100) -> list[int]:
        return sorted(owner for owner in self._users if owner > after)[:limit]

    async def media_key_exists(self, key: str) -> bool:
        return any(
            asset.storage_key == key
            for data in self._users.values()
            for asset in data.media.values()
        )

    @asynccontextmanager
    async def transaction(
        self, user_id: int, *, default_timezone: str
    ) -> AsyncIterator[DietTransaction]:
        if type(user_id) is not int or user_id < 1:
            raise DietError("INVALID_IDENTITY", "An authenticated identity is required.")
        async with self._locks.setdefault(user_id, asyncio.Lock()):
            data = copy.deepcopy(self._users.get(user_id)) or _MemoryData(
                state=DietUserState(
                    user_id=user_id,
                    timezone=default_timezone,
                    updated_at=datetime.now(UTC),
                )
            )
            yield MemoryDietTransaction(data)
            self._users[user_id] = data


class MemoryDietTransaction:
    def __init__(self, data: _MemoryData) -> None:
        self._data = data

    @property
    def state(self) -> DietUserState:
        return self._data.state

    async def export_batches(
        self, start_date: date, end_date: date
    ) -> AsyncIterator[
        tuple[str, list[DietLog | DietPlan | PlannedMeal | NutritionTarget | PersonalFood]]
    ]:
        logs = [
            log
            for log in self._data.logs.values()
            if log.temporal.local_date and start_date <= log.temporal.local_date <= end_date
        ]
        if len(logs) > 10000:
            raise DietError("EXPORT_TOO_LARGE", "每次最多10000条记录，请拆分日期范围。")
        groups: dict[
            str, list[DietLog | DietPlan | PlannedMeal | NutritionTarget | PersonalFood]
        ] = {
            "logs": list(logs),
            "plans": [
                plan
                for plan in self._data.plans.values()
                if plan.start_date <= end_date and plan.start_date + timedelta(days=6) >= start_date
            ],
            "planned_meals": [
                meal
                for meal in self._data.planned_meals.values()
                if start_date <= meal.local_date <= end_date
            ],
            "targets": list(self._data.targets.values()),
            "personal_foods": list(self._data.personal_foods.values()),
        }
        for kind, values in groups.items():
            for offset in range(0, max(1, len(values)), 100):
                yield kind, copy.deepcopy(values[offset : offset + 100])

    async def log_media_ids(self, record_ids: list[UUID]) -> list[UUID]:
        result = set()
        for draft in self._data.drafts.values():
            receipt = await self.get_receipt_by_id(draft.receipt_id) if draft.receipt_id else None
            if receipt and any(
                ref.kind == "log" and ref.id in record_ids for ref in receipt.result_refs
            ):
                result.update(draft.media_ids)
        return list(result)

    async def expire_previews(self, now: datetime, limit: int = 100) -> int:
        removed = 0
        groups: list[tuple[str, list[IntakeDraft | DietChangePreview]]] = [
            ("draft", list(self._data.drafts.values())),
            ("preview", list(self._data.previews.values())),
        ]
        for kind, values in groups:
            for value in values:
                if removed >= limit:
                    return removed
                if value.state == "committed" or value.expires_at > now:
                    continue
                self._data.tombstones[(kind, str(value.id))] = PurgeTombstone(
                    user_id=self.state.user_id,
                    entity_type=kind,
                    entity_id=str(value.id),
                    version=value.version,
                    purged_at=now,
                )
                if kind == "draft":
                    del self._data.drafts[value.id]
                else:
                    del self._data.previews[value.id]
                removed += 1
        return removed

    async def list_media(self, after: UUID | None = None, limit: int = 100) -> list[MediaAsset]:
        return copy.deepcopy(
            sorted(
                (asset for asset in self._data.media.values() if after is None or asset.id > after),
                key=lambda asset: asset.id,
            )[:limit]
        )

    async def get_tombstone(self, entity_type: str, entity_id: UUID | str) -> PurgeTombstone | None:
        return copy.deepcopy(self._data.tombstones.get((entity_type, str(entity_id))))

    async def export_snapshot(self, start_date: date, end_date: date) -> DietExportSnapshot:
        if (
            sum(
                start_date <= log.temporal.local_date <= end_date
                for log in self._data.logs.values()
                if log.temporal.local_date
            )
            > 10000
        ):
            raise DietError("EXPORT_TOO_LARGE", "每次最多10000条记录，请拆分日期范围。")
        plans = await self.list_plans(start_date, end_date, include_deleted=True)
        return DietExportSnapshot(
            user_id=self.state.user_id,
            diet_epoch=self.state.diet_epoch,
            start_date=start_date,
            end_date=end_date,
            logs=await self.list_logs(start_date, end_date, include_deleted=True),
            plans=plans,
            planned_meals=[
                meal
                for plan in plans
                for meal in await self.list_planned_meals(plan.id, include_deleted=True)
            ],
            targets=await self.list_targets(),
            personal_foods=await self.list_personal_foods(include_deleted=True),
        )

    async def expired_logs(self, now: datetime, limit: int = 100) -> list[DietLog]:
        return copy.deepcopy(
            sorted(
                (
                    log
                    for log in self._data.logs.values()
                    if log.deleted_at is not None and log.deleted_at <= now - TRASH_LIFETIME
                ),
                key=lambda log: (log.deleted_at, log.id),
            )[:limit]
        )

    async def purge_log(self, record_id: UUID, now: datetime) -> list[UUID]:
        log = self._data.logs.get(record_id)
        if log is None:
            return []
        if log.deleted_at is None:
            raise DietError("PURGE_REQUIRES_DELETED", "请先删除记录并核对不可恢复影响。")
        self._data.tombstones[("log", str(record_id))] = PurgeTombstone(
            user_id=self.state.user_id,
            entity_type="log",
            entity_id=str(record_id),
            version=log.version,
            purged_at=now,
        )
        receipts = [
            receipt
            for receipt in self._data.receipts.values()
            if any(ref.kind == "log" and ref.id == record_id for ref in receipt.result_refs)
        ]
        preview_ids = {receipt.preview_id for receipt in receipts}
        affected_drafts = [
            draft
            for draft in self._data.drafts.values()
            if draft.id in preview_ids
            or any(ref.kind == "log" and ref.id == record_id for ref in draft.content.source_refs)
        ]
        media = _media_ids(log.model_dump(mode="json"))
        for before, after, _ in self._data.revisions:
            if isinstance(after, DietLog) and after.id == record_id:
                media.update(_media_ids(after.model_dump(mode="json")))
                if before is not None:
                    media.update(_media_ids(before.model_dump(mode="json")))
        for draft in affected_drafts:
            media.update(_media_ids(draft.model_dump(mode="json")))
            self._data.tombstones[("draft", str(draft.id))] = PurgeTombstone(
                user_id=self.state.user_id,
                entity_type="draft",
                entity_id=str(draft.id),
                version=draft.version,
                purged_at=now,
            )
            del self._data.drafts[draft.id]
        for preview in list(self._data.previews.values()):
            if isinstance(preview.content, PurgeChangeContent):
                continue
            if preview.id in preview_ids or any(
                ref.kind == "log" and ref.id == record_id
                for ref in getattr(preview.content, "base_refs", [])
            ):
                media.update(_media_ids(preview.model_dump(mode="json")))
                self._data.tombstones[("preview", str(preview.id))] = PurgeTombstone(
                    user_id=self.state.user_id,
                    entity_type="preview",
                    entity_id=str(preview.id),
                    version=preview.version,
                    purged_at=now,
                )
                del self._data.previews[preview.id]
        self._data.revisions = [
            row
            for row in self._data.revisions
            if not isinstance(row[1], DietLog) or row[1].id != record_id
        ]
        for receipt in receipts:
            self._data.receipts[(receipt.operation, receipt.idempotency_key)] = receipt.model_copy(
                update={"result_status": "purged"},
            )
        del self._data.logs[record_id]
        return list(media)

    async def expired_resources(self, now: datetime, limit: int = 100) -> list[ResourceRef]:
        resources = [
            ResourceRef(kind=kind, id=value.id, version=value.version)
            for kind, values in (
                ("personal_food", self._data.personal_foods.values()),
                ("plan", self._data.plans.values()),
                ("planned_meal", self._data.planned_meals.values()),
            )
            for value in values
            if value.deleted_at is not None and value.deleted_at <= now - TRASH_LIFETIME
        ]
        return resources[:limit]

    async def purge_resource(self, ref: ResourceRef, now: datetime) -> list[UUID]:
        if ref.kind == "log":
            return await self.purge_log(ref.uuid, now)
        resource = await _purge_resource(self, ref)
        if resource is None:
            return []
        if ref.kind == "plan":
            for meal in await self.list_planned_meals(str(ref.id), include_deleted=True):
                await self._detach_meal(meal, now)
        elif isinstance(resource, PlannedMeal):
            await self._detach_meal(resource, now)
        refs = [ref]
        if ref.kind == "plan":
            refs.extend(
                ResourceRef(kind="planned_meal", id=meal.id, version=meal.version)
                for meal in await self.list_planned_meals(str(ref.id), include_deleted=True)
            )
        media = _media_ids(resource.model_dump(mode="json"))
        for affected in refs:
            self._data.tombstones[(affected.kind, str(affected.id))] = PurgeTombstone(
                user_id=self.state.user_id,
                entity_type=affected.kind,
                entity_id=str(affected.id),
                version=affected.version,
                purged_at=now,
            )

        def matches(value: object) -> bool:
            return any(_contains_reference(value, affected) for affected in refs)

        receipts = [
            r
            for r in self._data.receipts.values()
            if any((a.kind, a.id) == (b.kind, b.id) for a in r.result_refs for b in refs)
        ]
        preview_ids = {r.preview_id for r in receipts}
        for draft in list(self._data.drafts.values()):
            if draft.state == "committed":
                continue
            if draft.id in preview_ids or matches(draft.model_dump(mode="json")):
                media.update(_media_ids(draft.model_dump(mode="json")))
                self._data.tombstones[("draft", str(draft.id))] = PurgeTombstone(
                    user_id=self.state.user_id,
                    entity_type="draft",
                    entity_id=str(draft.id),
                    version=draft.version,
                    purged_at=now,
                )
                del self._data.drafts[draft.id]
        for preview in list(self._data.previews.values()):
            if isinstance(preview.content, PurgeChangeContent):
                continue
            if preview.id in preview_ids or matches(preview.content.model_dump(mode="json")):
                media.update(_media_ids(preview.model_dump(mode="json")))
                self._data.tombstones[("preview", str(preview.id))] = PurgeTombstone(
                    user_id=self.state.user_id,
                    entity_type="preview",
                    entity_id=str(preview.id),
                    version=preview.version,
                    purged_at=now,
                )
                del self._data.previews[preview.id]
        revisions = []
        for row in self._data.revisions:
            after = row[1]
            if _revision_matches(after, refs):
                for value in row[:2]:
                    if value is not None:
                        media.update(_media_ids(value.model_dump(mode="json")))
            else:
                revisions.append(row)
        self._data.revisions = revisions
        for receipt in receipts:
            self._data.receipts[(receipt.operation, receipt.idempotency_key)] = receipt.model_copy(
                update={"result_status": "purged"}
            )
        if ref.kind == "plan":
            for affected in refs[1:]:
                meal = self._data.planned_meals.pop(affected.uuid)
                media.update(_media_ids(meal.model_dump(mode="json")))
            del self._data.plans[str(ref.id)]
        elif ref.kind == "planned_meal":
            del self._data.planned_meals[ref.uuid]
        elif ref.kind == "personal_food":
            del self._data.personal_foods[ref.uuid]
        else:
            del self._data.targets[ref.uuid]
        return list(media)

    async def _detach_meal(self, meal: PlannedMeal, now: datetime) -> None:
        for log in await self.list_source_logs(meal.id):
            self._data.logs[log.id] = log.model_copy(
                update={
                    "source_planned_meal_id": None,
                    "source_event_kind": None,
                    "version": log.version + 1,
                    "updated_at": now,
                    "items": [
                        item.model_copy(update={"source_planned_item_id": None})
                        for item in log.items
                    ],
                }
            )

    async def get_media(self, media_id: UUID) -> MediaAsset | None:
        return copy.deepcopy(self._data.media.get(media_id))

    async def save_media(self, asset: MediaAsset) -> None:
        _check_owner(self.state.user_id, asset.user_id)
        self._data.media[asset.id] = copy.deepcopy(asset)

    async def media_referenced(self, media_id: UUID) -> bool:
        if any(
            media_id in draft.media_ids
            for draft in self._data.drafts.values()
            if draft.state not in ("discarded", "expired")
        ):
            return True
        if any(
            food.evidence.media_id == media_id or media_id in food.evidence.media_ids
            for food in self._data.personal_foods.values()
        ):
            return True
        resources = [
            *self._data.logs.values(),
            *self._data.planned_meals.values(),
            *self._data.previews.values(),
            *(
                value
                for before, after, _ in self._data.revisions
                for value in (before, after)
                if value is not None
            ),
        ]
        return any(
            _contains_media(value.model_dump(mode="json"), str(media_id)) for value in resources
        )

    async def get_draft(self, draft_id: UUID) -> IntakeDraft | None:
        return copy.deepcopy(self._data.drafts.get(draft_id))

    async def list_drafts(self) -> list[IntakeDraft]:
        return copy.deepcopy(
            sorted(self._data.drafts.values(), key=lambda draft: (draft.created_at, draft.id))
        )

    async def save_draft(self, draft: IntakeDraft) -> None:
        _check_owner(self.state.user_id, draft.user_id)
        if await self.get_tombstone("draft", draft.id):
            raise DietError("RESOURCE_PURGED", "Draft was purged.")
        self._data.drafts[draft.id] = copy.deepcopy(draft)

    async def get_preview(self, preview_id: UUID) -> DietChangePreview | None:
        return copy.deepcopy(self._data.previews.get(preview_id))

    async def save_preview(self, preview: DietChangePreview) -> None:
        _check_owner(self.state.user_id, preview.user_id)
        if await self.get_tombstone("preview", preview.id):
            raise DietError("RESOURCE_PURGED", "Preview was purged.")
        self._data.previews[preview.id] = copy.deepcopy(preview)

    async def get_plan(self, plan_id: str) -> DietPlan | None:
        return copy.deepcopy(self._data.plans.get(plan_id))

    async def list_plans(
        self, start_date: date, end_date: date, *, include_deleted: bool = False
    ) -> list[DietPlan]:
        return copy.deepcopy(
            sorted(
                (
                    plan
                    for plan in self._data.plans.values()
                    if plan.start_date <= end_date
                    and plan.start_date + timedelta(days=6) >= start_date
                    and (include_deleted or plan.deleted_at is None)
                ),
                key=lambda plan: (plan.start_date, plan.id),
            )
        )

    async def save_plan(self, plan: DietPlan) -> None:
        _check_owner(self.state.user_id, plan.user_id)
        if await self.get_tombstone("plan", plan.id):
            raise DietError("RESOURCE_PURGED", "Plan was purged.")
        self._data.plans[plan.id] = copy.deepcopy(plan)

    async def get_planned_meal(self, meal_id: UUID) -> PlannedMeal | None:
        return copy.deepcopy(self._data.planned_meals.get(meal_id))

    async def list_planned_meals(
        self, plan_id: str, *, include_deleted: bool = False
    ) -> list[PlannedMeal]:
        return copy.deepcopy(
            sorted(
                (
                    meal
                    for meal in self._data.planned_meals.values()
                    if meal.plan_id == plan_id and (include_deleted or meal.deleted_at is None)
                ),
                key=lambda meal: (meal.local_date, meal.meal_type, meal.id),
            )
        )

    async def save_planned_meal(self, meal: PlannedMeal) -> None:
        _check_owner(self.state.user_id, meal.user_id)
        if await self.get_tombstone("planned_meal", meal.id):
            raise DietError("RESOURCE_PURGED", "Meal was purged.")
        if await self.get_plan(meal.plan_id) is None:
            raise DietError("NOT_FOUND", "Plan not found.")
        if meal.deleted_at is None and any(
            other.id != meal.id
            and other.deleted_at is None
            and (other.plan_id, other.local_date, other.meal_type)
            == (meal.plan_id, meal.local_date, meal.meal_type)
            for other in self._data.planned_meals.values()
        ):
            raise DietError("PLAN_SLOT_CONFLICT", "An active meal already uses this slot.")
        self._data.planned_meals[meal.id] = copy.deepcopy(meal)

    async def planned_meals_in_range(self, start_date: date, end_date: date) -> list[PlannedMeal]:
        return copy.deepcopy(
            sorted(
                (
                    meal
                    for meal in self._data.planned_meals.values()
                    if start_date <= meal.local_date <= end_date
                    and meal.deleted_at is None
                    and (plan := self._data.plans.get(meal.plan_id)) is not None
                    and plan.deleted_at is None
                ),
                key=lambda meal: (meal.local_date, meal.meal_type, meal.id),
            )
        )

    async def get_personal_food(self, food_id: UUID) -> PersonalFood | None:
        return copy.deepcopy(self._data.personal_foods.get(food_id))

    async def list_personal_foods(self, *, include_deleted: bool = False) -> list[PersonalFood]:
        return copy.deepcopy(
            sorted(
                (
                    food
                    for food in self._data.personal_foods.values()
                    if include_deleted or food.deleted_at is None
                ),
                key=lambda food: (food.name.casefold(), food.id),
            )
        )

    async def save_personal_food(self, food: PersonalFood) -> None:
        _check_owner(self.state.user_id, food.user_id)
        if await self.get_tombstone("personal_food", food.id):
            raise DietError("RESOURCE_PURGED", "Food was purged.")
        self._data.personal_foods[food.id] = copy.deepcopy(food)

    async def get_log(self, record_id: UUID) -> DietLog | None:
        return copy.deepcopy(self._data.logs.get(record_id))

    async def list_logs(
        self, start_date: date, end_date: date, *, include_deleted: bool = False
    ) -> list[DietLog]:
        return copy.deepcopy(
            sorted(
                (
                    item
                    for item in self._data.logs.values()
                    if item.temporal.local_date is not None
                    and start_date <= item.temporal.local_date <= end_date
                    and (include_deleted or item.deleted_at is None)
                ),
                key=lambda item: (item.temporal.local_date, item.temporal.meal_type, item.id),
            )
        )

    async def find_source_event(self, source_event_key: str) -> DietLog | None:
        return copy.deepcopy(
            next(
                (
                    log
                    for log in self._data.logs.values()
                    if log.source_event_key == source_event_key
                ),
                None,
            )
        )

    async def list_source_logs(self, meal_id: UUID) -> list[DietLog]:
        return copy.deepcopy(
            [log for log in self._data.logs.values() if log.source_planned_meal_id == meal_id]
        )

    async def save_log(self, log: DietLog) -> None:
        _check_owner(self.state.user_id, log.user_id)
        if await self.get_tombstone("log", log.id):
            raise DietError("RESOURCE_PURGED", "Record was purged.")
        if log.source_event_key is not None:
            existing = await self.find_source_event(log.source_event_key)
            if existing is not None and existing.id != log.id:
                raise DietError("IDEMPOTENCY_CONFLICT", "The food event already exists.")
        self._data.logs[log.id] = copy.deepcopy(log)

    async def get_receipt(self, operation: str, idempotency_key: str) -> OperationReceipt | None:
        return copy.deepcopy(self._data.receipts.get((operation, idempotency_key)))

    async def save_receipt(self, receipt: OperationReceipt) -> None:
        _check_owner(self.state.user_id, receipt.user_id)
        key = (receipt.operation, receipt.idempotency_key)
        if key in self._data.receipts:
            raise DietError("IDEMPOTENCY_CONFLICT", "The operation receipt already exists.")
        if receipt.operation.startswith("confirm:") and any(
            item.operation.startswith("confirm:")
            and (item.preview_id, item.preview_version)
            == (receipt.preview_id, receipt.preview_version)
            for item in self._data.receipts.values()
        ):
            raise DietError("IDEMPOTENCY_CONFLICT", "The preview has already been submitted.")
        self._data.receipts[key] = copy.deepcopy(receipt)

    async def get_receipt_by_id(self, receipt_id: UUID) -> OperationReceipt | None:
        return copy.deepcopy(
            next(
                (receipt for receipt in self._data.receipts.values() if receipt.id == receipt_id),
                None,
            )
        )

    async def add_revision(
        self,
        before: DietRevisionResource | None,
        after: DietRevisionResource,
        receipt: OperationReceipt,
    ) -> None:
        if not isinstance(after, DietDayDeclaration):
            _check_owner(self.state.user_id, after.user_id)
        _check_owner(self.state.user_id, receipt.user_id)
        if before is not None and not isinstance(before, DietDayDeclaration):
            _check_owner(self.state.user_id, before.user_id)
        if any(
            type(existing) is type(after)
            and (
                existing.local_date if isinstance(existing, DietDayDeclaration) else existing.id,
                existing.version,
            )
            == (
                after.local_date if isinstance(after, DietDayDeclaration) else after.id,
                after.version,
            )
            for _, existing, _ in self._data.revisions
        ):
            raise DietError("VERSION_CONFLICT", "A revision for this version already exists.")
        self._data.revisions.append(copy.deepcopy((before, after, receipt.id)))

    async def list_targets(self) -> list[NutritionTarget]:
        return copy.deepcopy(
            sorted(
                self._data.targets.values(),
                key=lambda item: item.effective_date,
            )
        )

    async def save_target(self, target: NutritionTarget) -> None:
        _check_owner(self.state.user_id, target.user_id)
        if await self.get_tombstone("target", target.id):
            raise DietError("RESOURCE_PURGED", "Target was purged.")
        if any(
            item.id != target.id and item.effective_date == target.effective_date
            for item in self._data.targets.values()
        ):
            raise DietError("TARGET_DATE_CONFLICT", "Target date already exists.")
        self._data.targets[target.id] = copy.deepcopy(target)

    async def get_day_status(self, local_date: date) -> DietDayDeclaration:
        return copy.deepcopy(self._data.days.get(local_date)) or DietDayDeclaration(
            local_date=local_date,
            status="open",
            version=1,
        )

    async def set_day_status(self, local_date: date, status: DayStatus) -> None:
        if status == "no_intake" and await self.list_logs(local_date, local_date):
            raise DietError("DAY_HAS_RECORDS", "A no-intake day cannot have active records.")
        previous = self._data.days.get(local_date)
        self._data.days[local_date] = DietDayDeclaration(
            local_date=local_date,
            status=status,
            version=previous.version + 1 if previous else 2,
        )

    async def list_day_status(self, start_date: date, end_date: date) -> list[DietDayDeclaration]:
        return copy.deepcopy(
            [
                status
                for day, status in sorted(self._data.days.items())
                if start_date <= day <= end_date
            ]
        )

    async def set_timezone(self, timezone: str) -> DietUserState:
        self._data.state = DietUserState.model_validate(
            {
                **self.state.model_dump(),
                "timezone": timezone,
                "diet_epoch": self.state.diet_epoch + 1,
                "updated_at": datetime.now(UTC),
            }
        )
        return self.state

    async def advance_epoch(self, *, targets_changed: bool = False) -> DietUserState:
        self._data.state = DietUserState(
            user_id=self.state.user_id,
            timezone=self.state.timezone,
            diet_epoch=self.state.diet_epoch + 1,
            target_epoch=self.state.target_epoch + int(targets_changed),
            updated_at=datetime.now(UTC),
        )
        return self.state


class PostgresDietStore:
    def __init__(self, engine: AsyncEngine, journal: DeletionJournal | None = None) -> None:
        self.engine, self.journal = engine, journal

    async def media_key_exists(self, key: str) -> bool:
        async with self.engine.connect() as connection:
            return bool(
                (
                    await connection.execute(
                        text("SELECT EXISTS(SELECT 1 FROM media_assets WHERE storage_key=:key)"),
                        {"key": key},
                    )
                ).scalar_one()
            )

    async def list_users(self, after: int = 0, limit: int = 100) -> list[int]:
        async with self.engine.connect() as connection:
            return list(
                (
                    await connection.execute(
                        text(
                            "SELECT user_id FROM diet_user_state WHERE user_id>:after "
                            "ORDER BY user_id LIMIT :limit"
                        ),
                        {"after": after, "limit": limit},
                    )
                ).scalars()
            )

    @asynccontextmanager
    async def transaction(
        self, user_id: int, *, default_timezone: str
    ) -> AsyncIterator[DietTransaction]:
        if type(user_id) is not int or user_id < 1:
            raise DietError("INVALID_IDENTITY", "An authenticated identity is required.")
        async with self.engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO diet_user_state(user_id,timezone) VALUES (:user_id,:timezone) "
                    "ON CONFLICT(user_id) DO NOTHING"
                ),
                {"user_id": user_id, "timezone": default_timezone},
            )
            row = (
                (
                    await connection.execute(
                        text("SELECT * FROM diet_user_state WHERE user_id=:user_id FOR UPDATE"),
                        {"user_id": user_id},
                    )
                )
                .mappings()
                .one()
            )
            transaction = PostgresDietTransaction(
                connection, DietUserState.model_validate(dict(row))
            )
            yield transaction
            if self.journal is not None and transaction.privacy_changed:
                # Persist approved deletion intent before COMMIT, including an unknown outcome.
                current_xid = "xmin=CAST(CAST(pg_current_xact_id() AS TEXT) AS xid)"
                deleted = (
                    await connection.execute(
                        text(
                            "SELECT * FROM diet_purge_tombstones "
                            f"WHERE user_id=:owner AND {current_xid}"
                        ),
                        {"owner": user_id},
                    )
                ).mappings()
                media = (
                    await connection.execute(
                        text(
                            "SELECT * FROM media_assets WHERE user_id=:owner "
                            f"AND state IN ('purge_pending','purged') AND {current_xid}"
                        ),
                        {"owner": user_id},
                    )
                ).mappings()
                await asyncio.to_thread(
                    self.journal.append,
                    [PurgeTombstone.model_validate(dict(value)) for value in deleted],
                    [MediaAsset.model_validate(dict(value)) for value in media],
                )


def _json_payload(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        result: dict[str, Any] = json.loads(value)
        return result
    if isinstance(value, dict):
        return value
    raise ValueError("Expected a JSON object from PostgreSQL.")


class PostgresDietTransaction:
    def __init__(self, connection: AsyncConnection, state: DietUserState) -> None:
        self.connection = connection
        self.state = state
        self.privacy_changed = False

    async def export_batches(
        self, start_date: date, end_date: date
    ) -> AsyncIterator[
        tuple[str, list[DietLog | DietPlan | PlannedMeal | NutritionTarget | PersonalFood]]
    ]:
        values: dict[str, Any] = {
            "owner": self.state.user_id,
            "start": start_date,
            "end": end_date,
            "earliest": start_date - timedelta(days=min(6, start_date.toordinal() - 1)),
        }
        count = (
            await self.connection.execute(
                text(
                    "SELECT count(*) FROM diet_logs WHERE user_id=:owner "
                    "AND local_date BETWEEN :start AND :end"
                ),
                values,
            )
        ).scalar_one()
        if count > 10000:
            raise DietError("EXPORT_TOO_LARGE", "每次最多10000条记录，请拆分日期范围。")
        for kind, table, condition in (
            ("logs", "diet_logs", "local_date BETWEEN :start AND :end"),
            ("plans", "meal_plans", "title IS NOT NULL AND start_date BETWEEN :earliest AND :end"),
            ("planned_meals", "planned_meals", "local_date BETWEEN :start AND :end"),
            ("targets", "nutrition_targets", "TRUE"),
            ("personal_foods", "personal_foods", "TRUE"),
        ):
            after: str | None = None
            while True:
                rows = (
                    (
                        await self.connection.execute(
                            text(
                                f"SELECT * FROM {table} WHERE user_id=:owner AND {condition} "
                                "AND (CAST(:after AS TEXT) IS NULL "
                                "OR id::TEXT>CAST(:after AS TEXT)) "
                                "ORDER BY id::TEXT LIMIT 100"
                            ),
                            {**values, "after": after},
                        )
                    )
                    .mappings()
                    .all()
                )
                result: list[DietLog | DietPlan | PlannedMeal | NutritionTarget | PersonalFood]
                if kind == "logs":
                    result = list(await self._logs(list(rows)))
                elif kind == "planned_meals":
                    result = list(await self._planned_meals(list(rows)))
                elif kind == "plans":
                    result = [self._plan(row) for row in rows]
                elif kind == "personal_foods":
                    result = [self._personal_food(row) for row in rows]
                else:
                    result = [self._target(row) for row in rows]
                yield kind, result
                if len(rows) < 100:
                    break
                after = str(rows[-1]["id"])

    async def log_media_ids(self, record_ids: list[UUID]) -> list[UUID]:
        if not record_ids:
            return []
        rows = (
            await self.connection.execute(
                text("""
            SELECT d.body->'media_ids' FROM intake_drafts d JOIN operation_receipts r
              ON r.user_id=d.user_id AND r.id=d.receipt_id
            WHERE d.user_id=:owner AND EXISTS (
              SELECT 1 FROM jsonb_array_elements(r.result_refs) ref
              WHERE ref->>'kind'='log' AND ref->>'id'=ANY(:ids))
        """),
                {"owner": self.state.user_id, "ids": [str(value) for value in record_ids]},
            )
        ).scalars()
        return list({UUID(value) for ids in rows if ids for value in ids})

    async def expire_previews(self, now: datetime, limit: int = 100) -> int:
        self.privacy_changed = True
        removed = 0
        for kind, table in (("draft", "intake_drafts"), ("preview", "diet_change_previews")):
            rows = (
                (
                    await self.connection.execute(
                        text(
                            f"SELECT id,version FROM {table} WHERE user_id=:owner "
                            "AND state<>'committed' AND expires_at<=:now "
                            "ORDER BY expires_at,id LIMIT :limit"
                        ),
                        {"owner": self.state.user_id, "now": now, "limit": limit - removed},
                    )
                )
                .mappings()
                .all()
            )
            for row in rows:
                await self.connection.execute(
                    text(
                        "INSERT INTO diet_purge_tombstones"
                        "(user_id,entity_type,entity_id,version,purged_at) "
                        "VALUES (:owner,:kind,CAST(:id AS TEXT),:version,:now) "
                        "ON CONFLICT DO NOTHING"
                    ),
                    {
                        "owner": self.state.user_id,
                        "kind": kind,
                        "now": now,
                        "id": str(row["id"]),
                        "version": row["version"],
                    },
                )
                await self.connection.execute(
                    text(f"DELETE FROM {table} WHERE user_id=:owner AND id=:id"),
                    {"owner": self.state.user_id, "id": row["id"]},
                )
                removed += 1
        return removed

    async def list_media(self, after: UUID | None = None, limit: int = 100) -> list[MediaAsset]:
        rows = (
            (
                await self.connection.execute(
                    text(
                        "SELECT * FROM media_assets WHERE user_id=:owner "
                        "AND (CAST(:after AS UUID) IS NULL OR id>CAST(:after AS UUID)) "
                        "ORDER BY id LIMIT :limit"
                    ),
                    {"owner": self.state.user_id, "after": after, "limit": limit},
                )
            )
            .mappings()
            .all()
        )
        return [MediaAsset.model_validate(dict(row)) for row in rows]

    async def get_tombstone(self, entity_type: str, entity_id: UUID | str) -> PurgeTombstone | None:
        row = (
            (
                await self.connection.execute(
                    text(
                        "SELECT * FROM diet_purge_tombstones WHERE user_id=:owner "
                        "AND entity_type=:kind AND entity_id=:id"
                    ),
                    {"owner": self.state.user_id, "kind": entity_type, "id": str(entity_id)},
                )
            )
            .mappings()
            .first()
        )
        return PurgeTombstone.model_validate(dict(row)) if row else None

    async def export_snapshot(self, start_date: date, end_date: date) -> DietExportSnapshot:
        count = (
            await self.connection.execute(
                text(
                    "SELECT count(*) FROM diet_logs WHERE user_id=:owner "
                    "AND local_date BETWEEN :start AND :end"
                ),
                {"owner": self.state.user_id, "start": start_date, "end": end_date},
            )
        ).scalar_one()
        if count > 10000:
            raise DietError("EXPORT_TOO_LARGE", "每次最多10000条记录，请拆分日期范围。")
        plans = await self.list_plans(start_date, end_date, include_deleted=True)
        return DietExportSnapshot(
            user_id=self.state.user_id,
            diet_epoch=self.state.diet_epoch,
            start_date=start_date,
            end_date=end_date,
            logs=await self.list_logs(start_date, end_date, include_deleted=True),
            plans=plans,
            planned_meals=[
                meal
                for plan in plans
                for meal in await self.list_planned_meals(plan.id, include_deleted=True)
            ],
            targets=await self.list_targets(),
            personal_foods=await self.list_personal_foods(include_deleted=True),
        )

    async def expired_logs(self, now: datetime, limit: int = 100) -> list[DietLog]:
        rows = (
            (
                await self.connection.execute(
                    text(
                        "SELECT * FROM diet_logs WHERE user_id=:owner AND deleted_at<=:cutoff "
                        "ORDER BY deleted_at,id LIMIT :limit"
                    ),
                    {"owner": self.state.user_id, "cutoff": now - TRASH_LIFETIME, "limit": limit},
                )
            )
            .mappings()
            .all()
        )
        return await self._logs(list(rows))

    async def purge_log(self, record_id: UUID, now: datetime) -> list[UUID]:
        self.privacy_changed = True
        log = await self.get_log(record_id)
        if log is None:
            return []
        if log.deleted_at is None:
            raise DietError("PURGE_REQUIRES_DELETED", "请先删除记录并核对不可恢复影响。")
        values = {
            "owner": self.state.user_id,
            "id": record_id,
            "now": now,
            "version": log.version,
            "text_id": str(record_id),
        }
        await self.connection.execute(
            text("""
            INSERT INTO diet_purge_tombstones(user_id,entity_type,entity_id,version,purged_at)
            VALUES (:owner,'log',:text_id,:version,:now) ON CONFLICT DO NOTHING
        """),
            values,
        )
        draft_rows = (
            (
                await self.connection.execute(
                    text("""
            SELECT id,version,body FROM intake_drafts WHERE user_id=:owner AND (
              id IN (SELECT preview_id FROM operation_receipts WHERE user_id=:owner
                AND result_refs @> CAST(:ref AS JSONB))
              OR source_refs @> CAST(:ref AS JSONB))
        """),
                    {**values, "ref": json.dumps([{"kind": "log", "id": str(record_id)}])},
                )
            )
            .mappings()
            .all()
        )
        media = _media_ids(log.model_dump(mode="json"))
        revisions = (
            await self.connection.execute(
                text(
                    "SELECT before_body,after_body FROM diet_revisions WHERE user_id=:owner "
                    "AND entity_type='log' AND entity_id=:id"
                ),
                {"owner": self.state.user_id, "id": str(record_id)},
            )
        ).all()
        for before, after in revisions:
            media.update(_media_ids(before))
            media.update(_media_ids(after))
        for row in draft_rows:
            media.update(_media_ids(row["body"]))
            await self.connection.execute(
                text("""
                INSERT INTO diet_purge_tombstones(user_id,entity_type,entity_id,version,purged_at)
                VALUES (:owner,'draft',:draft,:version,:now) ON CONFLICT DO NOTHING
            """),
                {**values, "draft": str(row["id"]), "version": row["version"]},
            )
        if draft_rows:
            await self.connection.execute(
                text("DELETE FROM intake_drafts WHERE user_id=:owner AND id=ANY(:ids)"),
                {"owner": self.state.user_id, "ids": [row["id"] for row in draft_rows]},
            )
        previews = (
            await self.connection.execute(
                text("""
            SELECT payload FROM diet_change_previews WHERE user_id=:owner AND operation<>'purge'
              AND (base_refs @> CAST(:ref AS JSONB) OR id IN (
                SELECT preview_id FROM operation_receipts WHERE user_id=:owner
                AND result_refs @> CAST(:ref AS JSONB)))
        """),
                {**values, "ref": json.dumps([{"kind": "log", "id": str(record_id)}])},
            )
        ).scalars()
        for body in previews:
            media.update(_media_ids(body))
        await self.connection.execute(
            text("""
            INSERT INTO diet_purge_tombstones(user_id,entity_type,entity_id,version,purged_at)
            SELECT user_id,'preview',id::TEXT,version,:now FROM diet_change_previews
            WHERE user_id=:owner AND operation<>'purge'
              AND (base_refs @> CAST(:ref AS JSONB) OR id IN (
                SELECT preview_id FROM operation_receipts
                WHERE user_id=:owner AND result_refs @> CAST(:ref AS JSONB))) ON CONFLICT DO NOTHING
        """),
            {**values, "ref": json.dumps([{"kind": "log", "id": str(record_id)}])},
        )
        await self.connection.execute(
            text("""
            DELETE FROM diet_change_previews WHERE user_id=:owner AND id IN (
              SELECT entity_id::UUID FROM diet_purge_tombstones
              WHERE user_id=:owner AND entity_type='preview')
        """),
            values,
        )
        await self.connection.execute(
            text(
                "UPDATE operation_receipts SET result_status='purged' WHERE user_id=:owner "
                "AND result_refs @> CAST(:ref AS JSONB)"
            ),
            {**values, "ref": json.dumps([{"kind": "log", "id": str(record_id)}])},
        )
        for statement in (
            "DELETE FROM diet_revisions WHERE user_id=:owner "
            "AND entity_type='log' AND entity_id=:text_id",
            "DELETE FROM diet_log_items WHERE user_id=:owner AND diet_log_id=:id",
            "DELETE FROM diet_logs WHERE user_id=:owner AND id=:id",
        ):
            await self.connection.execute(text(statement), {**values, "text_id": str(record_id)})
        return list(media)

    async def expired_resources(self, now: datetime, limit: int = 100) -> list[ResourceRef]:
        rows = (
            await self.connection.execute(
                text("""
            SELECT 'personal_food' AS kind,id::TEXT AS id,version,deleted_at FROM personal_foods
              WHERE user_id=:owner AND deleted_at<=:cutoff
            UNION ALL
            SELECT 'plan',id,version,deleted_at FROM meal_plans
              WHERE user_id=:owner AND title IS NOT NULL AND deleted_at<=:cutoff
            UNION ALL
            SELECT 'planned_meal',id::TEXT,version,deleted_at FROM planned_meals
              WHERE user_id=:owner AND deleted_at<=:cutoff
            ORDER BY deleted_at,kind,id LIMIT :limit
        """),
                {"owner": self.state.user_id, "cutoff": now - TRASH_LIFETIME, "limit": limit},
            )
        ).mappings()
        return [ResourceRef(kind=row["kind"], id=row["id"], version=row["version"]) for row in rows]

    async def purge_resource(self, ref: ResourceRef, now: datetime) -> list[UUID]:
        self.privacy_changed = True
        if ref.kind == "log":
            return await self.purge_log(ref.uuid, now)
        resource = await _purge_resource(self, ref)
        if resource is None:
            return []
        refs = [ref]
        if ref.kind == "plan":
            refs.extend(
                ResourceRef(kind="planned_meal", id=meal.id, version=meal.version)
                for meal in await self.list_planned_meals(str(ref.id), include_deleted=True)
            )
        media = _media_ids(resource.model_dump(mode="json"))
        for affected in refs:
            values = {
                "owner": self.state.user_id,
                "kind": affected.kind,
                "id": str(affected.id),
                "version": affected.version,
                "now": now,
                "ref": json.dumps([{"kind": affected.kind, "id": str(affected.id)}]),
            }
            await self.connection.execute(
                text("""
                INSERT INTO diet_purge_tombstones(user_id,entity_type,entity_id,version,purged_at)
                VALUES (:owner,:kind,:id,:version,:now) ON CONFLICT DO NOTHING
            """),
                values,
            )
            field_name = {
                "plan": "plan_id",
                "planned_meal": "planned_meal_id",
                "personal_food": "food_id",
                "target": "target_id",
            }[affected.kind]
            values["vars"] = json.dumps({"kind": affected.kind, "id": str(affected.id)})
            reference_path = f"$.** ? ((@.kind == $kind && @.id == $id) || @.{field_name} == $id)"
            values["path"] = reference_path
            for table, column, kind in (
                ("intake_drafts", "body", "draft"),
                ("diet_change_previews", "payload", "preview"),
            ):
                rows = (
                    (
                        await self.connection.execute(
                            text(f"""
                    SELECT id,version,{column} AS body FROM {table} WHERE user_id=:owner
                      {"AND operation<>'purge'" if kind == "preview" else ""}
                              {"AND state<>'committed'" if kind == "draft" else ""}
                      AND (jsonb_path_exists({column},CAST(:path AS JSONPATH),CAST(:vars AS JSONB))
                        OR id IN (SELECT preview_id FROM operation_receipts WHERE user_id=:owner
                          AND result_refs @> CAST(:ref AS JSONB)))
                """),
                            values,
                        )
                    )
                    .mappings()
                    .all()
                )
                for row in rows:
                    media.update(_media_ids(row["body"]))
                    await self.connection.execute(
                        text("""
                        INSERT INTO diet_purge_tombstones
                          (user_id,entity_type,entity_id,version,purged_at)
                        VALUES (:owner,:copy_kind,:copy_id,:copy_version,:now)
                          ON CONFLICT DO NOTHING
                    """),
                        {
                            **values,
                            "copy_kind": kind,
                            "copy_id": str(row["id"]),
                            "copy_version": row["version"],
                        },
                    )
                    await self.connection.execute(
                        text(f"DELETE FROM {table} WHERE user_id=:owner AND id=:copy_id"),
                        {"owner": self.state.user_id, "copy_id": row["id"]},
                    )
            revisions = (
                await self.connection.execute(
                    text("""
                DELETE FROM diet_revisions
                  WHERE user_id=:owner AND entity_type=:kind AND entity_id=:id
                RETURNING before_body,after_body
            """),
                    values,
                )
            ).all()
            for before, after in revisions:
                media.update(_media_ids(before))
                media.update(_media_ids(after))
            await self.connection.execute(
                text("""
                UPDATE operation_receipts SET result_status='purged'
                WHERE user_id=:owner AND result_refs @> CAST(:ref AS JSONB)
            """),
                values,
            )
            if affected.kind == "planned_meal":
                meal = await self.get_planned_meal(affected.uuid)
                if meal:
                    media.update(_media_ids(meal.model_dump(mode="json")))
                # Actual intake is independent. Detach FKs, retaining quantities and event keys.
                await self.connection.execute(
                    text("""
                    UPDATE diet_logs SET source_planned_meal_id=NULL,source_event_kind=NULL,
                      version=version+1,updated_at=:now
                    WHERE user_id=:owner AND source_planned_meal_id=CAST(:id AS UUID)
                """),
                    values,
                )
                await self.connection.execute(
                    text("""
                    UPDATE diet_log_items SET source_planned_item_id=NULL,
                      body=jsonb_set(body,'{source_planned_item_id}','null'::JSONB)
                    WHERE user_id=:owner AND source_planned_item_id IN
                      (SELECT id FROM planned_meal_items WHERE user_id=:owner
                        AND planned_meal_id=CAST(:id AS UUID))
                """),
                    values,
                )
                await self.connection.execute(
                    text(
                        "DELETE FROM planned_meal_items WHERE user_id=:owner "
                        "AND planned_meal_id=CAST(:id AS UUID)"
                    ),
                    values,
                )
                await self.connection.execute(
                    text("DELETE FROM planned_meals WHERE user_id=:owner AND id=CAST(:id AS UUID)"),
                    values,
                )
        resource_table = {
            "plan": "meal_plans",
            "personal_food": "personal_foods",
            "target": "nutrition_targets",
        }.get(ref.kind)
        if resource_table:
            identity = ":id" if ref.kind == "plan" else "CAST(:id AS UUID)"
            await self.connection.execute(
                text(f"DELETE FROM {resource_table} WHERE user_id=:owner AND id={identity}"),
                {"owner": self.state.user_id, "id": str(ref.id)},
            )
        return list(media)

    async def get_media(self, media_id: UUID) -> MediaAsset | None:
        row = (
            (
                await self.connection.execute(
                    text("SELECT * FROM media_assets WHERE user_id=:owner AND id=:id"),
                    {"owner": self.state.user_id, "id": media_id},
                )
            )
            .mappings()
            .first()
        )
        return MediaAsset.model_validate(dict(row)) if row else None

    async def save_media(self, asset: MediaAsset) -> None:
        _check_owner(self.state.user_id, asset.user_id)
        values = asset.model_dump()
        self.privacy_changed |= asset.state in ("purge_pending", "purged")
        values["owner_refs"] = json.dumps([ref.model_dump(mode="json") for ref in asset.owner_refs])
        result = await self.connection.execute(
            text("""
            INSERT INTO media_assets(id,user_id,version,kind,storage_key,content_sha256,mime,bytes,
              width,height,state,owner_refs,expires_at,purged_at,created_at,updated_at)
            VALUES (:id,:user_id,:version,:kind,:storage_key,:content_sha256,:mime,:bytes,
              :width,:height,:state,CAST(:owner_refs AS JSONB),:expires_at,:purged_at,:created_at,
              :updated_at) ON CONFLICT(id) DO UPDATE SET version=EXCLUDED.version,
              state=EXCLUDED.state,owner_refs=EXCLUDED.owner_refs,expires_at=EXCLUDED.expires_at,
              purged_at=EXCLUDED.purged_at,updated_at=EXCLUDED.updated_at
            WHERE media_assets.user_id=EXCLUDED.user_id
        """),
            values,
        )
        if result.rowcount != 1:
            raise DietError("NOT_FOUND", "Media not found.")

    async def media_referenced(self, media_id: UUID) -> bool:
        return bool(
            (
                await self.connection.execute(
                    text("""
            SELECT EXISTS(SELECT 1 FROM intake_drafts WHERE user_id=:owner
              AND state NOT IN ('discarded','expired') AND body->'media_ids' @> CAST(:ids AS JSONB))
            OR EXISTS(SELECT 1 FROM personal_foods WHERE user_id=:owner
              AND (source->>'media_id'=:id OR source->'media_ids' @> CAST(:ids AS JSONB)))
            OR EXISTS(SELECT 1 FROM diet_log_items WHERE user_id=:owner
              AND (body->'evidence_snapshot'->>'media_id'=:id
                OR body->'evidence_snapshot'->'media_ids' @> CAST(:ids AS JSONB)))
            OR EXISTS(SELECT 1 FROM planned_meal_items WHERE user_id=:owner
              AND (body->'evidence_snapshot'->>'media_id'=:id
                OR body->'evidence_snapshot'->'media_ids' @> CAST(:ids AS JSONB)))
            OR EXISTS(SELECT 1 FROM diet_change_previews WHERE user_id=:owner
              AND jsonb_path_exists(payload,'$.**.media_id ? (@ == $id)',
                jsonb_build_object('id',CAST(:id AS TEXT))))
            OR EXISTS(SELECT 1 FROM diet_change_previews WHERE user_id=:owner
              AND jsonb_path_exists(payload,'$.**.media_ids[*] ? (@ == $id)',
                jsonb_build_object('id',CAST(:id AS TEXT))))
            OR EXISTS(SELECT 1 FROM diet_revisions WHERE user_id=:owner
              AND (jsonb_path_exists(before_body,'$.**.media_id ? (@ == $id)',
                jsonb_build_object('id',CAST(:id AS TEXT)))
                OR jsonb_path_exists(after_body,'$.**.media_id ? (@ == $id)',
                jsonb_build_object('id',CAST(:id AS TEXT)))
                OR jsonb_path_exists(before_body,'$.**.media_ids[*] ? (@ == $id)',
                jsonb_build_object('id',CAST(:id AS TEXT)))
                OR jsonb_path_exists(after_body,'$.**.media_ids[*] ? (@ == $id)',
                jsonb_build_object('id',CAST(:id AS TEXT)))))
        """),
                    {
                        "owner": self.state.user_id,
                        "id": str(media_id),
                        "ids": json.dumps([str(media_id)]),
                    },
                )
            ).scalar_one()
        )

    async def get_draft(self, draft_id: UUID) -> IntakeDraft | None:
        row = (
            (
                await self.connection.execute(
                    text("SELECT body FROM intake_drafts WHERE user_id=:owner AND id=:id"),
                    {"owner": self.state.user_id, "id": draft_id},
                )
            )
            .mappings()
            .first()
        )
        return IntakeDraft.model_validate(_json_payload(row["body"])) if row else None

    async def list_drafts(self) -> list[IntakeDraft]:
        rows = (
            (
                await self.connection.execute(
                    text(
                        "SELECT body FROM intake_drafts WHERE user_id=:owner ORDER BY created_at,id"
                    ),
                    {"owner": self.state.user_id},
                )
            )
            .mappings()
            .all()
        )
        return [IntakeDraft.model_validate(_json_payload(row["body"])) for row in rows]

    async def save_draft(self, draft: IntakeDraft) -> None:
        _check_owner(self.state.user_id, draft.user_id)
        if await self.get_tombstone("draft", draft.id):
            raise DietError("RESOURCE_PURGED", "Draft was purged.")
        result = await self.connection.execute(
            text("""
            INSERT INTO intake_drafts(
              id,user_id,version,thread_id,source_turn_id,channel,state,body,field_locks,
              field_locks_version,preview_hash,anchor,source_refs,expires_at,receipt_id,
              created_at,updated_at)
            VALUES (:id,:owner,:version,:thread,:turn,:channel,:state,CAST(:body AS JSONB),
              CAST(:locks AS JSONB),:locks_version,:hash,CAST(:anchor AS JSONB),
              CAST(:sources AS JSONB),:expires,:receipt,:created,:updated)
            ON CONFLICT(id) DO UPDATE SET
              version=EXCLUDED.version,state=EXCLUDED.state,body=EXCLUDED.body,
              thread_id=EXCLUDED.thread_id,source_turn_id=EXCLUDED.source_turn_id,
              field_locks=EXCLUDED.field_locks,field_locks_version=EXCLUDED.field_locks_version,
              preview_hash=EXCLUDED.preview_hash,receipt_id=EXCLUDED.receipt_id,
              updated_at=EXCLUDED.updated_at
            WHERE intake_drafts.user_id=EXCLUDED.user_id
        """),
            {
                "id": draft.id,
                "owner": draft.user_id,
                "version": draft.version,
                "thread": draft.thread_id,
                "turn": draft.source_turn_id,
                "channel": draft.channel,
                "state": draft.state,
                "body": draft.model_dump_json(),
                "locks": json.dumps(draft.content.field_locks),
                "locks_version": draft.field_locks_version,
                "hash": draft.preview_hash,
                "anchor": draft.content.anchor.model_dump_json(),
                "sources": json.dumps(
                    [ref.model_dump(mode="json") for ref in draft.content.source_refs]
                ),
                "expires": draft.expires_at,
                "receipt": draft.receipt_id,
                "created": draft.created_at,
                "updated": draft.updated_at,
            },
        )
        if result.rowcount != 1:
            raise DietError("NOT_FOUND", "Draft not found.")

    async def get_preview(self, preview_id: UUID) -> DietChangePreview | None:
        row = (
            (
                await self.connection.execute(
                    text(
                        "SELECT payload FROM diet_change_previews WHERE user_id=:owner AND id=:id"
                    ),
                    {"owner": self.state.user_id, "id": preview_id},
                )
            )
            .mappings()
            .first()
        )
        return DietChangePreview.model_validate(_json_payload(row["payload"])) if row else None

    async def save_preview(self, preview: DietChangePreview) -> None:
        _check_owner(self.state.user_id, preview.user_id)
        if await self.get_tombstone("preview", preview.id):
            raise DietError("RESOURCE_PURGED", "Preview was purged.")
        result = await self.connection.execute(
            text("""
            INSERT INTO diet_change_previews(
              id,user_id,version,thread_id,source_turn_id,operation,payload,base_refs,
              base_target_epoch,content_hash,calculation_version,state,expires_at,receipt_id,
              created_at,updated_at)
            VALUES (:id,:owner,:version,:thread,:turn,:operation,CAST(:payload AS JSONB),
              CAST(:refs AS JSONB),:target_epoch,:hash,:calculation,:state,:expires,
              :receipt,:created,:updated)
            ON CONFLICT(id) DO UPDATE SET version=EXCLUDED.version,
              thread_id=EXCLUDED.thread_id,source_turn_id=EXCLUDED.source_turn_id,
              payload=EXCLUDED.payload,content_hash=EXCLUDED.content_hash,
              state=EXCLUDED.state,receipt_id=EXCLUDED.receipt_id,updated_at=EXCLUDED.updated_at
            WHERE diet_change_previews.user_id=EXCLUDED.user_id
        """),
            {
                "id": preview.id,
                "owner": preview.user_id,
                "version": preview.version,
                "thread": preview.thread_id,
                "turn": preview.source_turn_id,
                "operation": preview.content.operation,
                "payload": preview.model_dump_json(),
                "refs": json.dumps(
                    [ref.model_dump(mode="json") for ref in preview.content.base_refs]
                ),
                "target_epoch": (
                    preview.content.base_target_epoch
                    if isinstance(preview.content, ChangeContent)
                    else None
                ),
                "hash": preview.content_hash,
                "calculation": preview.content.calculation_version,
                "state": preview.state,
                "expires": preview.expires_at,
                "receipt": preview.receipt_id,
                "created": preview.created_at,
                "updated": preview.updated_at,
            },
        )
        if result.rowcount != 1:
            raise DietError("NOT_FOUND", "Preview not found.")

    @staticmethod
    def _plan(row: RowMapping) -> DietPlan:
        return DietPlan.model_validate({key: row[key] for key in DietPlan.model_fields})

    async def get_plan(self, plan_id: str) -> DietPlan | None:
        row = (
            (
                await self.connection.execute(
                    text(
                        "SELECT * FROM meal_plans WHERE user_id=:owner AND id=:id "
                        "AND title IS NOT NULL AND start_date IS NOT NULL"
                    ),
                    {"owner": self.state.user_id, "id": plan_id},
                )
            )
            .mappings()
            .first()
        )
        return self._plan(row) if row else None

    async def list_plans(
        self, start_date: date, end_date: date, *, include_deleted: bool = False
    ) -> list[DietPlan]:
        rows = (
            (
                await self.connection.execute(
                    text(
                        "SELECT * FROM meal_plans WHERE user_id=:owner "
                        "AND title IS NOT NULL AND start_date BETWEEN :earliest AND :end "
                        "AND (:deleted OR deleted_at IS NULL) ORDER BY start_date,id"
                    ),
                    {
                        "owner": self.state.user_id,
                        "earliest": start_date - timedelta(days=min(6, start_date.toordinal() - 1)),
                        "end": end_date,
                        "deleted": include_deleted,
                    },
                )
            )
            .mappings()
            .all()
        )
        return [self._plan(row) for row in rows]

    async def save_plan(self, plan: DietPlan) -> None:
        _check_owner(self.state.user_id, plan.user_id)
        if await self.get_tombstone("plan", plan.id):
            raise DietError("RESOURCE_PURGED", "Plan was purged.")
        result = await self.connection.execute(
            text("""
            INSERT INTO meal_plans(
              id,user_id,idempotency_key,payload,title,start_date,version,
              created_at,updated_at,deleted_at,migrated_at)
            VALUES (:id,:user_id,:key,'{}',:title,:start_date,:version,
              :created_at,:updated_at,:deleted_at,:migrated_at)
            ON CONFLICT(id) DO UPDATE SET title=EXCLUDED.title,start_date=EXCLUDED.start_date,
              version=EXCLUDED.version,updated_at=EXCLUDED.updated_at,
              deleted_at=EXCLUDED.deleted_at,migrated_at=EXCLUDED.migrated_at
            WHERE meal_plans.user_id=EXCLUDED.user_id
            """),
            {**plan.model_dump(), "key": f"diet:{plan.id}"},
        )
        if result.rowcount != 1:
            raise DietError("NOT_FOUND", "Plan not found.")

    async def _planned_meals(self, rows: list[RowMapping]) -> list[PlannedMeal]:
        if not rows:
            return []
        items = (
            (
                await self.connection.execute(
                    text(
                        "SELECT planned_meal_id,body FROM planned_meal_items "
                        "WHERE user_id=:owner AND planned_meal_id=ANY(:ids) "
                        "AND deleted_at IS NULL ORDER BY planned_meal_id,position,id"
                    ),
                    {"owner": self.state.user_id, "ids": [row["id"] for row in rows]},
                )
            )
            .mappings()
            .all()
        )
        grouped: dict[UUID, list[DietItem]] = {}
        for item in items:
            grouped.setdefault(item["planned_meal_id"], []).append(
                DietItem.model_validate(_json_payload(item["body"]))
            )
        return [
            PlannedMeal.model_validate(
                {
                    **dict(row),
                    "items": grouped.get(row["id"], []),
                }
            )
            for row in rows
        ]

    async def get_planned_meal(self, meal_id: UUID) -> PlannedMeal | None:
        row = (
            (
                await self.connection.execute(
                    text("SELECT * FROM planned_meals WHERE user_id=:owner AND id=:id"),
                    {"owner": self.state.user_id, "id": meal_id},
                )
            )
            .mappings()
            .first()
        )
        return (await self._planned_meals([row]))[0] if row else None

    async def list_planned_meals(
        self, plan_id: str, *, include_deleted: bool = False
    ) -> list[PlannedMeal]:
        rows = (
            (
                await self.connection.execute(
                    text(
                        "SELECT * FROM planned_meals WHERE user_id=:owner AND plan_id=:plan "
                        "AND (:deleted OR deleted_at IS NULL) ORDER BY local_date,meal_type,id"
                    ),
                    {"owner": self.state.user_id, "plan": plan_id, "deleted": include_deleted},
                )
            )
            .mappings()
            .all()
        )
        return await self._planned_meals(list(rows))

    async def planned_meals_in_range(self, start_date: date, end_date: date) -> list[PlannedMeal]:
        rows = (
            (
                await self.connection.execute(
                    text(
                        "SELECT m.* FROM planned_meals m JOIN meal_plans p "
                        "ON p.user_id=m.user_id AND p.id=m.plan_id "
                        "WHERE m.user_id=:owner AND m.local_date BETWEEN :start AND :end "
                        "AND m.deleted_at IS NULL AND p.deleted_at IS NULL "
                        "ORDER BY m.local_date,m.meal_type,m.id"
                    ),
                    {"owner": self.state.user_id, "start": start_date, "end": end_date},
                )
            )
            .mappings()
            .all()
        )
        return await self._planned_meals(list(rows))

    async def save_planned_meal(self, meal: PlannedMeal) -> None:
        _check_owner(self.state.user_id, meal.user_id)
        if await self.get_tombstone("planned_meal", meal.id):
            raise DietError("RESOURCE_PURGED", "Meal was purged.")
        result = await self.connection.execute(
            text("""
            INSERT INTO planned_meals(
              id,user_id,plan_id,version,local_date,meal_type,note,intake_generation,
              legacy_item_refs,created_at,updated_at,deleted_at)
            VALUES (:id,:user_id,:plan_id,:version,:local_date,:meal_type,:note,
              :intake_generation,CAST(:legacy AS JSONB),:created_at,:updated_at,:deleted_at)
            ON CONFLICT(id) DO UPDATE SET version=EXCLUDED.version,
              local_date=EXCLUDED.local_date,meal_type=EXCLUDED.meal_type,note=EXCLUDED.note,
              intake_generation=EXCLUDED.intake_generation,legacy_item_refs=EXCLUDED.legacy_item_refs,
              updated_at=EXCLUDED.updated_at,deleted_at=EXCLUDED.deleted_at
            WHERE planned_meals.user_id=EXCLUDED.user_id AND planned_meals.plan_id=EXCLUDED.plan_id
            """),
            {
                **meal.model_dump(exclude={"items", "legacy_item_refs"}),
                "legacy": json.dumps(
                    {key: str(value) for key, value in meal.legacy_item_refs.items()}
                ),
            },
        )
        if result.rowcount != 1:
            raise DietError("NOT_FOUND", "Planned meal not found.")
        await self.connection.execute(
            text(
                "UPDATE planned_meal_items SET deleted_at=:now WHERE user_id=:owner "
                "AND planned_meal_id=:parent AND deleted_at IS NULL"
            ),
            {"now": meal.updated_at, "owner": meal.user_id, "parent": meal.id},
        )
        for item in meal.items:
            result = await self.connection.execute(
                text("""
                INSERT INTO planned_meal_items(
                  id,user_id,planned_meal_id,version,position,body,created_at,updated_at)
                VALUES (:id,:owner,:parent,:version,:position,CAST(:body AS JSONB),:now,:now)
                ON CONFLICT(id) DO UPDATE SET version=EXCLUDED.version,
                  position=EXCLUDED.position,body=EXCLUDED.body,updated_at=EXCLUDED.updated_at,
                  deleted_at=NULL
                WHERE planned_meal_items.user_id=EXCLUDED.user_id
                  AND planned_meal_items.planned_meal_id=EXCLUDED.planned_meal_id
                """),
                {
                    "id": item.id,
                    "owner": meal.user_id,
                    "parent": meal.id,
                    "version": item.version,
                    "position": item.position,
                    "body": item.model_dump_json(),
                    "now": meal.updated_at,
                },
            )
            if result.rowcount != 1:
                raise DietError("NOT_FOUND", "Item not found in this planned meal.")

    @staticmethod
    def _personal_food(row: RowMapping) -> PersonalFood:
        food = PersonalFood(
            id=row["id"],
            user_id=row["user_id"],
            version=row["version"],
            name=row["name"],
            brand=row["brand"],
            evidence=_json_payload(row["source"]),
            allergens=row["allergens"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            deleted_at=row["deleted_at"],
        )
        if (
            food.evidence.nutrients.model_dump(mode="json") != row["nutrients"]
            or food.evidence.food_state != row["food_state"]
            or food.evidence.basis_quantity != row["basis_type"]
            or row["basis_amount"] != (100 if row["basis_type"] == "per_100g" else 1)
            or row["basis_unit"] != ("g" if row["basis_type"] == "per_100g" else "serving")
            or ServingEvidence.model_validate(row["serving_evidence"])
            != ServingEvidence(
                id=food.evidence.serving_evidence_id, grams=food.evidence.serving_grams
            )
        ):
            raise DietError("PERSISTENCE_CONFLICT", "Personal food metadata is inconsistent.")
        return food

    async def get_personal_food(self, food_id: UUID) -> PersonalFood | None:
        row = (
            (
                await self.connection.execute(
                    text("SELECT * FROM personal_foods WHERE user_id=:owner AND id=:id"),
                    {"owner": self.state.user_id, "id": food_id},
                )
            )
            .mappings()
            .first()
        )
        return self._personal_food(row) if row else None

    async def list_personal_foods(self, *, include_deleted: bool = False) -> list[PersonalFood]:
        rows = (
            (
                await self.connection.execute(
                    text(
                        "SELECT * FROM personal_foods WHERE user_id=:owner "
                        "AND (:deleted OR deleted_at IS NULL) ORDER BY lower(name),id"
                    ),
                    {"owner": self.state.user_id, "deleted": include_deleted},
                )
            )
            .mappings()
            .all()
        )
        return [self._personal_food(row) for row in rows]

    async def save_personal_food(self, food: PersonalFood) -> None:
        _check_owner(self.state.user_id, food.user_id)
        if await self.get_tombstone("personal_food", food.id):
            raise DietError("RESOURCE_PURGED", "Food was purged.")
        evidence = food.evidence
        result = await self.connection.execute(
            text("""
            INSERT INTO personal_foods(
              id,user_id,version,name,brand,basis_type,basis_amount,basis_unit,food_state,
              nutrients,serving_evidence,allergens,source,created_at,updated_at,deleted_at)
            VALUES (:id,:user_id,:version,:name,:brand,:basis,:amount,:unit,:state,
              CAST(:nutrients AS JSONB),CAST(:serving AS JSONB),CAST(:allergens AS JSONB),
              CAST(:source AS JSONB),:created_at,:updated_at,:deleted_at)
            ON CONFLICT(id) DO UPDATE SET version=EXCLUDED.version,name=EXCLUDED.name,
              brand=EXCLUDED.brand,basis_type=EXCLUDED.basis_type,basis_amount=EXCLUDED.basis_amount,
              basis_unit=EXCLUDED.basis_unit,food_state=EXCLUDED.food_state,
              nutrients=EXCLUDED.nutrients,serving_evidence=EXCLUDED.serving_evidence,
              allergens=EXCLUDED.allergens,source=EXCLUDED.source,
              updated_at=EXCLUDED.updated_at,deleted_at=EXCLUDED.deleted_at
            WHERE personal_foods.user_id=EXCLUDED.user_id
            """),
            {
                **food.model_dump(exclude={"evidence", "allergens"}),
                "basis": evidence.basis_quantity,
                "amount": 100 if evidence.basis_quantity == "per_100g" else 1,
                "unit": "g" if evidence.basis_quantity == "per_100g" else "serving",
                "state": evidence.food_state,
                "nutrients": evidence.nutrients.model_dump_json(),
                "serving": ServingEvidence(
                    id=evidence.serving_evidence_id, grams=evidence.serving_grams
                ).model_dump_json(),
                "allergens": json.dumps([fact.model_dump(mode="json") for fact in food.allergens]),
                "source": evidence.model_dump_json(),
            },
        )
        if result.rowcount != 1:
            raise DietError("NOT_FOUND", "Personal food not found.")

    async def get_log(self, record_id: UUID) -> DietLog | None:
        row = (
            (
                await self.connection.execute(
                    text("SELECT * FROM diet_logs WHERE user_id=:owner AND id=:id"),
                    {"owner": self.state.user_id, "id": record_id},
                )
            )
            .mappings()
            .first()
        )
        return (await self._logs([row]))[0] if row else None

    async def _logs(self, rows: list[RowMapping]) -> list[DietLog]:
        if not rows:
            return []
        items = (
            (
                await self.connection.execute(
                    text(
                        "SELECT diet_log_id,body FROM diet_log_items WHERE user_id=:owner "
                        "AND diet_log_id=ANY(:ids) AND deleted_at IS NULL "
                        "ORDER BY diet_log_id,position,id"
                    ),
                    {"owner": self.state.user_id, "ids": [row["id"] for row in rows]},
                )
            )
            .mappings()
            .all()
        )
        grouped: dict[UUID, list[DietItem]] = {}
        for item in items:
            grouped.setdefault(item["diet_log_id"], []).append(
                DietItem.model_validate(_json_payload(item["body"]))
            )
        return [self._log(row, grouped.get(row["id"], [])) for row in rows]

    @staticmethod
    def _log(row: RowMapping, items: list[DietItem]) -> DietLog:
        return DietLog(
            id=row["id"],
            user_id=row["user_id"],
            version=row["version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            temporal={
                key: row[key]
                for key in ("local_date", "local_time", "utc_offset_minutes", "meal_type", "note")
            },
            timezone=row["timezone"],
            occurred_at=row["occurred_at"],
            channel=row["channel"],
            items=items,
            source_planned_meal_id=row["source_planned_meal_id"],
            source_event_kind=row["source_event_kind"],
            source_event_key=row["source_event_key"],
            deleted_at=row["deleted_at"],
        )

    async def list_logs(
        self, start_date: date, end_date: date, *, include_deleted: bool = False
    ) -> list[DietLog]:
        rows = (
            (
                await self.connection.execute(
                    text(
                        "SELECT * FROM diet_logs WHERE user_id=:owner "
                        "AND local_date BETWEEN :start AND :end "
                        "AND (:deleted OR deleted_at IS NULL) "
                        "ORDER BY local_date,meal_type,id"
                    ),
                    {
                        "owner": self.state.user_id,
                        "start": start_date,
                        "end": end_date,
                        "deleted": include_deleted,
                    },
                )
            )
            .mappings()
            .all()
        )
        return await self._logs(list(rows))

    async def find_source_event(self, source_event_key: str) -> DietLog | None:
        row = (
            (
                await self.connection.execute(
                    text(
                        "SELECT * FROM diet_logs WHERE user_id=:owner AND source_event_key=:event"
                    ),
                    {"owner": self.state.user_id, "event": source_event_key},
                )
            )
            .mappings()
            .first()
        )
        return (await self._logs([row]))[0] if row else None

    async def list_source_logs(self, meal_id: UUID) -> list[DietLog]:
        rows = (
            (
                await self.connection.execute(
                    text(
                        "SELECT * FROM diet_logs WHERE user_id=:owner "
                        "AND source_planned_meal_id=:id "
                        "ORDER BY created_at,id"
                    ),
                    {"owner": self.state.user_id, "id": meal_id},
                )
            )
            .mappings()
            .all()
        )
        return await self._logs(list(rows))

    async def save_log(self, log: DietLog) -> None:
        _check_owner(self.state.user_id, log.user_id)
        if await self.get_tombstone("log", log.id):
            raise DietError("RESOURCE_PURGED", "Record was purged.")
        values = {
            "id": log.id,
            "owner": log.user_id,
            "version": log.version,
            **log.temporal.model_dump(),
            "timezone": log.timezone,
            "occurred_at": log.occurred_at,
            "channel": log.channel,
            "plan": log.source_planned_meal_id,
            "event": log.source_event_key,
            "event_kind": log.source_event_kind,
            "created": log.created_at,
            "updated": log.updated_at,
            "deleted": log.deleted_at,
        }
        result = await self.connection.execute(
            text("""
            INSERT INTO diet_logs(id,user_id,version,local_date,local_time,timezone,
              utc_offset_minutes,occurred_at,meal_type,channel,note,source_planned_meal_id,
              source_event_kind,source_event_key,created_at,updated_at,deleted_at)
            VALUES (:id,:owner,:version,:local_date,:local_time,:timezone,:utc_offset_minutes,
              :occurred_at,:meal_type,:channel,:note,:plan,:event_kind,:event,:created,:updated,:deleted)
            ON CONFLICT(id) DO UPDATE SET version=EXCLUDED.version,local_date=EXCLUDED.local_date,
              local_time=EXCLUDED.local_time,timezone=EXCLUDED.timezone,
              utc_offset_minutes=EXCLUDED.utc_offset_minutes,occurred_at=EXCLUDED.occurred_at,
              meal_type=EXCLUDED.meal_type,note=EXCLUDED.note,
              updated_at=EXCLUDED.updated_at,deleted_at=EXCLUDED.deleted_at
            WHERE diet_logs.user_id=EXCLUDED.user_id
        """),
            values,
        )
        if result.rowcount != 1:
            raise DietError("NOT_FOUND", "Record not found.")
        # Temporarily retire item positions so a reorder cannot violate the active index.
        await self.connection.execute(
            text(
                "UPDATE diet_log_items SET deleted_at=:now "
                "WHERE user_id=:owner AND diet_log_id=:id AND deleted_at IS NULL"
            ),
            {"now": log.updated_at, "owner": log.user_id, "id": log.id},
        )
        for item in log.items:
            result = await self.connection.execute(
                text("""
                INSERT INTO diet_log_items(id,user_id,diet_log_id,version,position,body,
                  source_planned_item_id,created_at,updated_at)
                VALUES (:id,:owner,:parent,:version,:position,CAST(:body AS JSONB),
                  :source,:now,:now)
                ON CONFLICT(id) DO UPDATE SET version=EXCLUDED.version,
                  position=EXCLUDED.position,body=EXCLUDED.body,updated_at=EXCLUDED.updated_at,
                  deleted_at=NULL
                WHERE diet_log_items.user_id=EXCLUDED.user_id
                  AND diet_log_items.diet_log_id=EXCLUDED.diet_log_id
            """),
                {
                    "id": item.id,
                    "owner": log.user_id,
                    "parent": log.id,
                    "version": item.version,
                    "position": item.position,
                    "body": item.model_dump_json(),
                    "source": item.source_planned_item_id,
                    "now": log.updated_at,
                },
            )
            if result.rowcount != 1:
                raise DietError("NOT_FOUND", "Item not found in this record.")

    async def get_receipt(self, operation: str, idempotency_key: str) -> OperationReceipt | None:
        row = (
            (
                await self.connection.execute(
                    text(
                        "SELECT * FROM operation_receipts WHERE user_id=:owner "
                        "AND operation=:operation AND idempotency_key=:key"
                    ),
                    {
                        "owner": self.state.user_id,
                        "operation": operation,
                        "key": idempotency_key,
                    },
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        values = dict(row)
        values.pop("expires_at")
        return OperationReceipt.model_validate(values)

    async def save_receipt(self, receipt: OperationReceipt) -> None:
        _check_owner(self.state.user_id, receipt.user_id)
        values = receipt.model_dump()
        values["result_refs"] = json.dumps(
            [ref.model_dump(mode="json") for ref in receipt.result_refs]
        )
        await self.connection.execute(
            text("""
            INSERT INTO operation_receipts(
              id,user_id,operation,idempotency_key,request_hash,preview_id,preview_version,
              result_refs,result_status,created_at)
            VALUES (:id,:user_id,:operation,:idempotency_key,:request_hash,
              :preview_id,:preview_version,
              CAST(:result_refs AS JSONB),:result_status,:created_at)
        """),
            values,
        )

    async def get_receipt_by_id(self, receipt_id: UUID) -> OperationReceipt | None:
        row = (
            (
                await self.connection.execute(
                    text("SELECT * FROM operation_receipts WHERE user_id=:owner AND id=:id"),
                    {"owner": self.state.user_id, "id": receipt_id},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        values = dict(row)
        values.pop("expires_at")
        return OperationReceipt.model_validate(values)

    async def add_revision(
        self,
        before: DietRevisionResource | None,
        after: DietRevisionResource,
        receipt: OperationReceipt,
    ) -> None:
        if not isinstance(after, DietDayDeclaration):
            _check_owner(self.state.user_id, after.user_id)
        _check_owner(self.state.user_id, receipt.user_id)
        if before is not None and not isinstance(before, DietDayDeclaration):
            _check_owner(self.state.user_id, before.user_id)
        await self.connection.execute(
            text("""
            INSERT INTO diet_revisions(id,user_id,entity_type,entity_id,entity_version,
              operation,before_body,after_body,receipt_id,actor_kind,created_at)
            VALUES (:id,:owner,:type,:entity,:version,:operation,CAST(:before AS JSONB),
              CAST(:after AS JSONB),:receipt,'user',:now)
        """),
            {
                "id": uuid4(),
                "owner": self.state.user_id,
                "entity": str(
                    after.local_date if isinstance(after, DietDayDeclaration) else after.id
                ),
                "version": after.version,
                "type": {
                    DietLog: "log",
                    NutritionTarget: "target",
                    PlannedMeal: "planned_meal",
                    DietPlan: "plan",
                    PersonalFood: "personal_food",
                    DietDayDeclaration: "day_status",
                }[type(after)],
                "operation": receipt.operation,
                "before": before.model_dump_json() if before else None,
                "after": after.model_dump_json(),
                "receipt": receipt.id,
                "now": receipt.created_at,
            },
        )

    async def list_targets(self) -> list[NutritionTarget]:
        rows = (
            (
                await self.connection.execute(
                    text(
                        "SELECT * FROM nutrition_targets WHERE user_id=:owner "
                        "ORDER BY effective_date,id"
                    ),
                    {"owner": self.state.user_id},
                )
            )
            .mappings()
            .all()
        )
        return [self._target(row) for row in rows]

    @staticmethod
    def _target(row: RowMapping) -> NutritionTarget:
        goals = {}
        for name, prefix in (
            ("calories_kcal", "calories"),
            ("protein_g", "protein"),
            ("fat_g", "fat"),
            ("carbohydrates_g", "carbohydrates"),
        ):
            low, high = row[f"{prefix}_lower"], row[f"{prefix}_upper"]
            goals[name] = NutrientRange(lower=low, upper=high) if low is not None else None
        return NutritionTarget(
            id=row["id"],
            user_id=row["user_id"],
            version=row["version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            effective_date=row["effective_date"],
            goals=NutritionGoals(**goals),
            source=row["source"],
            source_ref=row["source_ref"],
        )

    async def save_target(self, target: NutritionTarget) -> None:
        _check_owner(self.state.user_id, target.user_id)
        if await self.get_tombstone("target", target.id):
            raise DietError("RESOURCE_PURGED", "Target was purged.")
        values: dict[str, object] = {
            "id": target.id,
            "owner": target.user_id,
            "version": target.version,
            "date": target.effective_date,
            "source": target.source,
            "ref": target.source_ref,
            "created": target.created_at,
            "updated": target.updated_at,
        }
        for name, prefix in (
            ("calories_kcal", "calories"),
            ("protein_g", "protein"),
            ("fat_g", "fat"),
            ("carbohydrates_g", "carbohydrates"),
        ):
            goal: NutrientRange | None = getattr(target.goals, name)
            values[f"{prefix}_lower"] = goal.lower if goal else None
            values[f"{prefix}_upper"] = goal.upper if goal else None
        result = await self.connection.execute(
            text("""
            INSERT INTO nutrition_targets(id,user_id,version,effective_date,
              calories_lower,calories_upper,protein_lower,protein_upper,fat_lower,fat_upper,
              carbohydrates_lower,carbohydrates_upper,source,source_ref,created_at,updated_at)
            VALUES (:id,:owner,:version,:date,:calories_lower,:calories_upper,
              :protein_lower,:protein_upper,:fat_lower,:fat_upper,
              :carbohydrates_lower,:carbohydrates_upper,:source,:ref,:created,:updated)
            ON CONFLICT(id) DO UPDATE SET version=EXCLUDED.version,
              effective_date=EXCLUDED.effective_date,calories_lower=EXCLUDED.calories_lower,
              calories_upper=EXCLUDED.calories_upper,protein_lower=EXCLUDED.protein_lower,
              protein_upper=EXCLUDED.protein_upper,fat_lower=EXCLUDED.fat_lower,
              fat_upper=EXCLUDED.fat_upper,carbohydrates_lower=EXCLUDED.carbohydrates_lower,
              carbohydrates_upper=EXCLUDED.carbohydrates_upper,source=EXCLUDED.source,
              source_ref=EXCLUDED.source_ref,updated_at=EXCLUDED.updated_at
            WHERE nutrition_targets.user_id=EXCLUDED.user_id
        """),
            values,
        )
        if result.rowcount != 1:
            raise DietError("NOT_FOUND", "Target not found.")

    async def get_day_status(self, local_date: date) -> DietDayDeclaration:
        row = (
            (
                await self.connection.execute(
                    text(
                        "SELECT local_date,status,version FROM diet_day_status "
                        "WHERE user_id=:owner AND local_date=:date"
                    ),
                    {"owner": self.state.user_id, "date": local_date},
                )
            )
            .mappings()
            .first()
        )
        return (
            DietDayDeclaration.model_validate(dict(row))
            if row
            else DietDayDeclaration(
                local_date=local_date,
                status="open",
                version=1,
            )
        )

    async def set_day_status(self, local_date: date, status: DayStatus) -> None:
        if status == "no_intake":
            count = (
                await self.connection.execute(
                    text(
                        "SELECT COUNT(*) FROM diet_logs WHERE user_id=:owner "
                        "AND local_date=:date AND deleted_at IS NULL"
                    ),
                    {"owner": self.state.user_id, "date": local_date},
                )
            ).scalar_one()
            if count:
                raise DietError("DAY_HAS_RECORDS", "A no-intake day cannot have active records.")
        await self.connection.execute(
            text("""
            INSERT INTO diet_day_status(user_id,local_date,version,status,declared_at,updated_at)
            VALUES (:owner,:date,2,:status,:declared,:now)
            ON CONFLICT(user_id,local_date) DO UPDATE SET
              version=diet_day_status.version+1,status=EXCLUDED.status,
              declared_at=EXCLUDED.declared_at,updated_at=EXCLUDED.updated_at
        """),
            {
                "owner": self.state.user_id,
                "date": local_date,
                "status": status,
                "declared": datetime.now(UTC) if status != "open" else None,
                "now": datetime.now(UTC),
            },
        )

    async def list_day_status(self, start_date: date, end_date: date) -> list[DietDayDeclaration]:
        rows = (
            (
                await self.connection.execute(
                    text(
                        "SELECT local_date,status,version FROM diet_day_status "
                        "WHERE user_id=:owner AND local_date BETWEEN :start AND :end "
                        "ORDER BY local_date"
                    ),
                    {"owner": self.state.user_id, "start": start_date, "end": end_date},
                )
            )
            .mappings()
            .all()
        )
        return [DietDayDeclaration.model_validate(dict(row)) for row in rows]

    async def set_timezone(self, timezone: str) -> DietUserState:
        candidate = DietUserState.model_validate(
            {
                **self.state.model_dump(),
                "timezone": timezone,
                "diet_epoch": self.state.diet_epoch + 1,
                "updated_at": datetime.now(UTC),
            }
        )
        row = (
            (
                await self.connection.execute(
                    text(
                        "UPDATE diet_user_state SET timezone=:zone,"
                        "diet_epoch=diet_epoch+1,updated_at=NOW() "
                        "WHERE user_id=:owner RETURNING *"
                    ),
                    {"zone": candidate.timezone, "owner": self.state.user_id},
                )
            )
            .mappings()
            .one()
        )
        self.state = DietUserState.model_validate(dict(row))
        return self.state

    async def advance_epoch(self, *, targets_changed: bool = False) -> DietUserState:
        row = (
            (
                await self.connection.execute(
                    text(
                        "UPDATE diet_user_state SET diet_epoch=diet_epoch+1,"
                        "target_epoch=target_epoch+:target,updated_at=NOW() "
                        "WHERE user_id=:owner RETURNING *"
                    ),
                    {"owner": self.state.user_id, "target": int(targets_changed)},
                )
            )
            .mappings()
            .one()
        )
        self.state = DietUserState.model_validate(dict(row))
        return self.state
