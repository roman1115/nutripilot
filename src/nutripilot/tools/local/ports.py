from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol
from uuid import UUID

from nutripilot.domain.diet import DayStatus, FoodEvidenceSnapshot, FoodRef
from nutripilot.domain.diet_jobs import MediaAsset
from nutripilot.domain.diet_privacy import DietExportSnapshot, PurgeTombstone
from nutripilot.domain.diet_records import (
    DietChangePreview,
    DietDayDeclaration,
    DietLog,
    DietPlan,
    DietRevisionResource,
    DietUserState,
    FoodHit,
    IntakeDraft,
    NutritionTarget,
    OperationReceipt,
    PersonalFood,
    PlannedMeal,
    ResourceRef,
)
from nutripilot.tools.local.models import (
    CreateMealPlanArgs,
    FeedbackRecord,
    MealPlan,
    RecordFeedbackArgs,
    UpdateUserProfileArgs,
    UserProfile,
    WeatherContext,
)


class LocalDataStore(Protocol):
    async def get_user_profile(self, user_id: int) -> UserProfile: ...

    async def update_user_profile(
        self, user_id: int, update: UpdateUserProfileArgs
    ) -> UserProfile: ...

    async def create_meal_plan(
        self, user_id: int, request: CreateMealPlanArgs, idempotency_key: str
    ) -> MealPlan: ...

    async def record_feedback(
        self, user_id: int, request: RecordFeedbackArgs, idempotency_key: str
    ) -> FeedbackRecord: ...

    async def list_feedback(self, user_id: int) -> list[FeedbackRecord]: ...


class WeatherProvider(Protocol):
    async def get_weather(self, location: str) -> WeatherContext: ...


class DietTransaction(Protocol):
    """All methods are bound to one authenticated owner and one database transaction."""

    async def export_snapshot(self, start_date: date, end_date: date) -> DietExportSnapshot: ...

    def export_batches(
        self, start_date: date, end_date: date
    ) -> AsyncIterator[
        tuple[str, list[DietLog | DietPlan | PlannedMeal | NutritionTarget | PersonalFood]]
    ]: ...

    async def log_media_ids(self, record_ids: list[UUID]) -> list[UUID]: ...

    async def purge_log(self, record_id: UUID, now: datetime) -> list[UUID]: ...

    async def expired_logs(self, now: datetime, limit: int = 100) -> list[DietLog]: ...

    async def expired_resources(self, now: datetime, limit: int = 100) -> list[ResourceRef]: ...

    async def purge_resource(self, ref: ResourceRef, now: datetime) -> list[UUID]: ...

    async def get_tombstone(
        self, entity_type: str, entity_id: UUID | str
    ) -> PurgeTombstone | None: ...

    async def expire_previews(self, now: datetime, limit: int = 100) -> int: ...

    async def list_media(self, after: UUID | None = None, limit: int = 100) -> list[MediaAsset]: ...

    async def get_media(self, media_id: UUID) -> MediaAsset | None: ...

    async def save_media(self, asset: MediaAsset) -> None: ...

    async def media_referenced(self, media_id: UUID) -> bool: ...

    @property
    def state(self) -> DietUserState: ...

    async def get_draft(self, draft_id: UUID) -> IntakeDraft | None: ...

    async def list_drafts(self) -> list[IntakeDraft]: ...

    async def save_draft(self, draft: IntakeDraft) -> None: ...

    async def get_preview(self, preview_id: UUID) -> DietChangePreview | None: ...

    async def save_preview(self, preview: DietChangePreview) -> None: ...

    async def get_plan(self, plan_id: str) -> DietPlan | None: ...

    async def list_plans(
        self, start_date: date, end_date: date, *, include_deleted: bool = False
    ) -> list[DietPlan]: ...

    async def save_plan(self, plan: DietPlan) -> None: ...

    async def get_planned_meal(self, meal_id: UUID) -> PlannedMeal | None: ...

    async def list_planned_meals(
        self, plan_id: str, *, include_deleted: bool = False
    ) -> list[PlannedMeal]: ...

    async def planned_meals_in_range(
        self, start_date: date, end_date: date
    ) -> list[PlannedMeal]: ...

    async def save_planned_meal(self, meal: PlannedMeal) -> None: ...

    async def get_personal_food(self, food_id: UUID) -> PersonalFood | None: ...

    async def list_personal_foods(self, *, include_deleted: bool = False) -> list[PersonalFood]: ...

    async def save_personal_food(self, food: PersonalFood) -> None: ...

    async def get_log(self, record_id: UUID) -> DietLog | None: ...

    async def list_logs(
        self, start_date: date, end_date: date, *, include_deleted: bool = False
    ) -> list[DietLog]: ...

    async def find_source_event(self, source_event_key: str) -> DietLog | None: ...

    async def list_source_logs(self, meal_id: UUID) -> list[DietLog]: ...

    async def save_log(self, log: DietLog) -> None: ...

    async def get_receipt(
        self, operation: str, idempotency_key: str
    ) -> OperationReceipt | None: ...

    async def get_receipt_by_id(self, receipt_id: UUID) -> OperationReceipt | None: ...

    async def save_receipt(self, receipt: OperationReceipt) -> None: ...

    async def add_revision(
        self,
        before: DietRevisionResource | None,
        after: DietRevisionResource,
        receipt: OperationReceipt,
    ) -> None: ...

    async def list_targets(self) -> list[NutritionTarget]: ...

    async def save_target(self, target: NutritionTarget) -> None: ...

    async def get_day_status(self, local_date: date) -> DietDayDeclaration: ...

    async def list_day_status(
        self, start_date: date, end_date: date
    ) -> list[DietDayDeclaration]: ...

    async def set_day_status(self, local_date: date, status: DayStatus) -> None: ...

    async def advance_epoch(self, *, targets_changed: bool = False) -> DietUserState: ...

    async def set_timezone(self, timezone: str) -> DietUserState: ...


class DietStore(Protocol):
    async def media_key_exists(self, key: str) -> bool: ...

    async def list_users(self, after: int = 0, limit: int = 100) -> list[int]: ...

    def transaction(
        self, user_id: int, *, default_timezone: str
    ) -> AbstractAsyncContextManager[DietTransaction]: ...


@dataclass(frozen=True)
class ResolvedFood:
    name: str
    evidence: FoodEvidenceSnapshot | None


class DietFoodResolver(Protocol):
    async def resolve(
        self, food: FoodRef, *, user_id: int, now: datetime, tx: DietTransaction
    ) -> ResolvedFood: ...

    async def assert_available(
        self, food: FoodRef, *, user_id: int, tx: DietTransaction
    ) -> None: ...


class DietFoodSearch(DietFoodResolver, Protocol):
    async def search(
        self,
        query: str,
        *,
        food_kind: str,
        food_state: str | None,
        limit: int,
        now: datetime,
        tx: DietTransaction,
    ) -> list[FoodHit]: ...
