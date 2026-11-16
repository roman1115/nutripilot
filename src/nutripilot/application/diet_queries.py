"""Bounded owner-scoped projections with epoch-bound signed keyset cursors."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Literal, TypeVar

from pydantic import AwareDatetime, BaseModel, Field, ValidationError, model_validator

from nutripilot.domain.diet import (
    DietError,
    DietModel,
)
from nutripilot.domain.diet_analysis import (
    DailyAnalysis,
    DailyDeviation,
    WeeklyAnalysis,
    daily_analysis,
    diet_deviation,
    weekly_analysis,
)
from nutripilot.domain.diet_records import (
    DietLog,
    DietPlan,
    FoodHit,
    IntakeDraft,
    PersonalFood,
    PlannedMeal,
    ResourceRef,
    TargetPeriod,
    target_periods,
)
from nutripilot.tools.local.diet_models import (
    GetDietDeviation,
    GetDietLogs,
    GetMealPlans,
    GetNutritionSummary,
    GetNutritionTargets,
    IDScope,
    RangeScope,
    SearchFoods,
)
from nutripilot.tools.local.ports import DietFoodSearch, DietStore, DietTransaction

T = TypeVar("T", bound=BaseModel)


class CollectionScope(DietModel):
    kind: Literal["collection"] = "collection"
    resource: Literal["drafts", "personal_foods"]


class CollectionQuery(DietModel):
    scope: CollectionScope
    cursor: Annotated[str, Field(min_length=1, max_length=2048)] | None = None
    limit: Annotated[int, Field(strict=True, ge=1, le=50)] = 20
    include_deleted: bool = False


class PlanLibraryQuery(DietModel):
    scope: RangeScope
    cursor: Annotated[str, Field(min_length=1, max_length=2048)] | None = None
    limit: Annotated[int, Field(strict=True, ge=1, le=50)] = 20
    include_deleted: bool = False

    @model_validator(mode="after")
    def bounded(self) -> PlanLibraryQuery:
        self.scope.check_limit(31)
        return self


class PlanIDScope(DietModel):
    kind: Literal["plan"] = "plan"
    id: Annotated[str, Field(min_length=1, max_length=120)]


class PlanMealsQuery(DietModel):
    scope: PlanIDScope
    cursor: Annotated[str, Field(min_length=1, max_length=2048)] | None = None
    limit: Annotated[int, Field(strict=True, ge=1, le=50)] = 20


class QueryWarning(DietModel):
    code: str
    field: str | None = None
    message: str


class PlanMealView(DietModel):
    plan: DietPlan
    meal: PlannedMeal
    actual_refs: list[ResourceRef]


class ReferenceOnly(DietModel):
    ref: ResourceRef
    reason: Literal["object_too_large"] = "object_too_large"


class QueryPage[T: BaseModel](DietModel):
    kind: Literal[
        "food_hits",
        "plans",
        "logs",
        "summary",
        "targets",
        "deviation",
        "drafts",
        "personal_foods",
        "plan_library",
    ]
    scope: RangeScope | IDScope | SearchFoods | CollectionScope | PlanIDScope
    as_of: AwareDatetime
    diet_epoch: int
    items: list[T | ReferenceOnly]
    next_cursor: str | None = None
    truncated: bool = False
    warnings: list[QueryWarning] = Field(default_factory=list)
    preview: None = None
    job: None = None


class _Cursor(DietModel):
    owner: Annotated[int, Field(strict=True, gt=0)]
    query_hash: str
    diet_epoch: Annotated[int, Field(strict=True, ge=0)]
    last_key: str


class DietQueryService:
    def __init__(
        self,
        store: DietStore,
        foods: DietFoodSearch,
        *,
        default_timezone: str,
        cursor_secret: bytes,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if len(cursor_secret) < 32:
            raise ValueError("Diet cursors require at least 32 bytes of signing material.")
        self.store, self.foods = store, foods
        self.default_timezone, self.cursor_secret = default_timezone, cursor_secret
        self.clock = clock or (lambda: datetime.now(UTC))

    async def drafts(self, user_id: int, request: CollectionQuery) -> QueryPage[IntakeDraft]:
        async with self.store.transaction(user_id, default_timezone=self.default_timezone) as tx:
            rows = [
                draft
                for draft in await tx.list_drafts()
                if draft.expires_at > self.clock() and draft.state in ("ready", "needs_input")
            ]
            return self._page(
                tx,
                request,
                "drafts",
                request.scope,
                rows,
                key=lambda row: f"{row.created_at.isoformat()}:{row.id}",
                ref=lambda row: ResourceRef(kind="draft", id=row.id, version=row.version),
            )

    async def personal_foods(
        self, user_id: int, request: CollectionQuery
    ) -> QueryPage[PersonalFood]:
        async with self.store.transaction(user_id, default_timezone=self.default_timezone) as tx:
            rows = await tx.list_personal_foods(include_deleted=request.include_deleted)
            return self._page(
                tx,
                request,
                "personal_foods",
                request.scope,
                rows,
                key=lambda row: f"{row.name.casefold()}:{row.id}",
                ref=lambda row: ResourceRef(kind="personal_food", id=row.id, version=row.version),
            )

    async def plan_library(self, user_id: int, request: PlanLibraryQuery) -> QueryPage[DietPlan]:
        async with self.store.transaction(user_id, default_timezone=self.default_timezone) as tx:
            rows = await tx.list_plans(
                request.scope.start_date,
                request.scope.end_date,
                include_deleted=request.include_deleted,
            )
            return self._page(
                tx,
                request,
                "plan_library",
                request.scope,
                rows,
                key=lambda row: f"{row.start_date}:{row.id}",
                ref=lambda row: ResourceRef(kind="plan", id=row.id, version=row.version),
            )

    async def plan_meals(self, user_id: int, request: PlanMealsQuery) -> QueryPage[PlanMealView]:
        async with self.store.transaction(user_id, default_timezone=self.default_timezone) as tx:
            plan = await tx.get_plan(request.scope.id)
            if plan is None:
                raise DietError("NOT_FOUND", "Plan not found.")
            rows = []
            for meal in await tx.list_planned_meals(plan.id):
                actual = await tx.list_source_logs(meal.id)
                rows.append(
                    PlanMealView(
                        plan=plan,
                        meal=meal,
                        actual_refs=[
                            ResourceRef(kind="log", id=log.id, version=log.version)
                            for log in actual
                            if log.deleted_at is None
                        ],
                    )
                )
            return self._page(
                tx,
                request,
                "plans",
                request.scope,
                rows,
                key=lambda row: f"{row.meal.local_date}:{row.meal.meal_type}:{row.meal.id}",
                ref=lambda row: ResourceRef(
                    kind="planned_meal", id=row.meal.id, version=row.meal.version
                ),
            )

    async def logs(self, user_id: int, request: GetDietLogs) -> QueryPage[DietLog]:
        async with self.store.transaction(user_id, default_timezone=self.default_timezone) as tx:
            if isinstance(request.scope, IDScope):
                selected = await tx.get_log(request.scope.id)
                if selected is None or (selected.deleted_at and not request.include_deleted):
                    raise DietError("NOT_FOUND", "Record not found.")
                rows = [selected]
            else:
                rows = await tx.list_logs(
                    request.scope.start_date,
                    request.scope.end_date,
                    include_deleted=request.include_deleted,
                )
            rows = [
                row
                for row in rows
                if (request.meal_type is None or row.temporal.meal_type == request.meal_type)
                and (
                    not request.food_query
                    or any(
                        request.food_query.casefold() in item.name_snapshot.casefold()
                        for item in row.items
                    )
                )
            ]
            return self._page(
                tx,
                request,
                "logs",
                request.scope,
                rows,
                key=lambda row: f"{row.temporal.local_date}:{row.temporal.meal_type}:{row.id}",
                ref=lambda row: ResourceRef(kind="log", id=row.id, version=row.version),
            )

    async def plans(self, user_id: int, request: GetMealPlans) -> QueryPage[PlanMealView]:
        async with self.store.transaction(user_id, default_timezone=self.default_timezone) as tx:
            if isinstance(request.scope, IDScope):
                plan = await tx.get_plan(str(request.scope.id))
                if plan is None or plan.deleted_at:
                    raise DietError("NOT_FOUND", "Plan not found.")
                selected = [plan]
            else:
                selected = await tx.list_plans(request.scope.start_date, request.scope.end_date)
            rows = []
            for plan in selected:
                for meal in await tx.list_planned_meals(plan.id):
                    if request.meal_type and meal.meal_type != request.meal_type:
                        continue
                    if isinstance(request.scope, RangeScope) and not (
                        request.scope.start_date <= meal.local_date <= request.scope.end_date
                    ):
                        continue
                    actual = await tx.list_source_logs(meal.id)
                    rows.append(
                        PlanMealView(
                            plan=plan,
                            meal=meal,
                            actual_refs=[
                                ResourceRef(kind="log", id=log.id, version=log.version)
                                for log in actual
                                if log.deleted_at is None
                            ],
                        )
                    )
            return self._page(
                tx,
                request,
                "plans",
                request.scope,
                rows,
                key=lambda row: f"{row.meal.local_date}:{row.meal.meal_type}:{row.meal.id}",
                ref=lambda row: ResourceRef(
                    kind="planned_meal", id=row.meal.id, version=row.meal.version
                ),
            )

    async def summary(
        self,
        user_id: int,
        request: GetNutritionSummary,
    ) -> QueryPage[DailyAnalysis | WeeklyAnalysis]:
        async with self.store.transaction(user_id, default_timezone=self.default_timezone) as tx:
            scope = request.scope
            days = daily_analysis(
                scope.start_date,
                scope.end_date,
                await tx.list_logs(scope.start_date, scope.end_date),
                await tx.planned_meals_in_range(scope.start_date, scope.end_date),
                await tx.list_day_status(scope.start_date, scope.end_date),
                await tx.list_targets(),
            )
            if request.nutrients:
                days = (
                    [
                        day.model_copy(
                            update={
                                "actual": day.actual.model_copy(
                                    update={
                                        "nutrients": {
                                            name: day.actual.nutrients[name]
                                            for name in request.nutrients
                                        }
                                    }
                                ),
                                "planned": day.planned.model_copy(
                                    update={
                                        "nutrients": {
                                            name: day.planned.nutrients[name]
                                            for name in request.nutrients
                                        }
                                    }
                                ),
                                "target_comparison": {
                                    name: value
                                    for name, value in day.target_comparison.items()
                                    if name in request.nutrients
                                },
                            }
                        )
                        for day in days
                    ]
                    if request.granularity == "day"
                    else days
                )
            rows: list[DailyAnalysis | WeeklyAnalysis] = (
                list(days) if request.granularity == "day" else list(weekly_analysis(days))
            )
            if request.granularity == "week" and request.nutrients:
                rows = [
                    row.model_copy(
                        update={
                            "nutrients": {name: row.nutrients[name] for name in request.nutrients},
                        }
                    )
                    if isinstance(row, WeeklyAnalysis)
                    else row
                    for row in rows
                ]
            # Aggregate responses have no cursor parameter; never silently trim them.
            return QueryPage[DailyAnalysis | WeeklyAnalysis](
                kind="summary",
                scope=scope,
                as_of=self.clock(),
                diet_epoch=tx.state.diet_epoch,
                items=list(rows),
            )

    async def deviation(self, user_id: int, request: GetDietDeviation) -> QueryPage[DailyDeviation]:
        async with self.store.transaction(user_id, default_timezone=self.default_timezone) as tx:
            scope = request.scope
            plans = await tx.list_plans(scope.start_date, scope.end_date)
            if request.plan_id is not None:
                selected = await tx.get_plan(str(request.plan_id))
                if selected is None or selected.deleted_at:
                    raise DietError("NOT_FOUND", "Plan not found.")
                plans = [selected]
            meals = [
                meal
                for plan in plans
                for meal in await tx.list_planned_meals(plan.id)
                if scope.start_date <= meal.local_date <= scope.end_date
            ]
            logs = await tx.list_logs(scope.start_date, scope.end_date)
            linked = {log.id: log for log in logs}
            for meal in meals:
                for log in await tx.list_source_logs(meal.id):
                    linked[log.id] = log
            days = daily_analysis(
                scope.start_date,
                scope.end_date,
                logs,
                meals,
                await tx.list_day_status(scope.start_date, scope.end_date),
                await tx.list_targets(),
            )
            return QueryPage[DailyDeviation](
                kind="deviation",
                scope=scope,
                as_of=self.clock(),
                diet_epoch=tx.state.diet_epoch,
                items=diet_deviation(
                    days,
                    meals,
                    list(linked.values()),
                    {
                        plan.id: ResourceRef(kind="plan", id=plan.id, version=plan.version)
                        for plan in plans
                    },
                ),
            )

    async def targets(
        self,
        user_id: int,
        request: GetNutritionTargets,
    ) -> QueryPage[TargetPeriod]:
        async with self.store.transaction(user_id, default_timezone=self.default_timezone) as tx:
            periods = target_periods(await tx.list_targets())
            selected = [
                period
                for period in periods
                if period.target.effective_date <= request.scope.end_date
                and (
                    period.end_date_exclusive is None
                    or period.end_date_exclusive > request.scope.start_date
                )
            ]
            return QueryPage[TargetPeriod](
                kind="targets",
                scope=request.scope,
                as_of=self.clock(),
                diet_epoch=tx.state.diet_epoch,
                items=selected,
            )

    async def search(self, user_id: int, request: SearchFoods) -> QueryPage[FoodHit]:
        async with self.store.transaction(user_id, default_timezone=self.default_timezone) as tx:
            hits = await self.foods.search(
                request.query,
                food_kind=request.food_kind,
                food_state=request.food_state,
                limit=request.limit + 1,
                now=self.clock(),
                tx=tx,
            )
            return QueryPage[FoodHit](
                kind="food_hits",
                scope=request,
                as_of=self.clock(),
                diet_epoch=tx.state.diet_epoch,
                items=hits[: request.limit],
                truncated=len(hits) > request.limit,
            )

    def _page(
        self,
        tx: DietTransaction,
        request: GetMealPlans | GetDietLogs | CollectionQuery | PlanLibraryQuery | PlanMealsQuery,
        kind: Literal["plans", "logs", "drafts", "personal_foods", "plan_library"],
        scope: IDScope | RangeScope | CollectionScope | PlanIDScope,
        rows: list[T],
        *,
        key: Callable[[T], str],
        ref: Callable[[T], ResourceRef],
    ) -> QueryPage[T]:
        query_hash = hashlib.sha256(
            json.dumps(
                request.model_dump(mode="json", exclude={"cursor", "limit"}),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        query_hash = hashlib.sha256(f"{kind}:{query_hash}".encode()).hexdigest()
        rows = sorted(rows, key=key)
        if request.cursor:
            cursor = self._decode(request.cursor)
            if cursor.owner != tx.state.user_id or cursor.query_hash != query_hash:
                raise DietError("INVALID_CURSOR", "Cursor does not belong to this query.")
            if cursor.diet_epoch != tx.state.diet_epoch:
                raise DietError("DATA_CHANGED", "Data changed; restart pagination.")
            rows = [row for row in rows if key(row) > cursor.last_key]
        page = QueryPage[T](
            kind=kind,
            scope=scope,
            as_of=self.clock(),
            diet_epoch=tx.state.diet_epoch,
            items=[],
        )
        last_key = None
        for row in rows[: request.limit]:
            item: T | ReferenceOnly = row
            if len(row.model_dump_json().encode()) > 12 * 1024:
                item = ReferenceOnly(ref=ref(row))
            candidate = page.model_copy(update={"items": [*page.items, item]})
            if len(candidate.model_dump_json().encode()) > 14 * 1024:
                break
            page = candidate
            last_key = key(row)
        truncated = len(rows) > len(page.items)
        cursor_value = (
            self._encode(
                _Cursor(
                    owner=tx.state.user_id,
                    query_hash=query_hash,
                    diet_epoch=tx.state.diet_epoch,
                    last_key=last_key,
                )
            )
            if truncated and last_key is not None
            else None
        )
        return page.model_copy(update={"truncated": truncated, "next_cursor": cursor_value})

    def _encode(self, cursor: _Cursor) -> str:
        body = base64.urlsafe_b64encode(cursor.model_dump_json().encode()).decode().rstrip("=")
        signature = hmac.new(self.cursor_secret, body.encode(), hashlib.sha256).hexdigest()
        return f"{body}.{signature}"

    def _decode(self, value: str) -> _Cursor:
        try:
            body, signature = value.rsplit(".", 1)
            expected = hmac.new(self.cursor_secret, body.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError("Invalid signature.")
            return _Cursor.model_validate_json(
                base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
            )
        except (ValueError, ValidationError) as exc:
            raise DietError("INVALID_CURSOR", "Invalid pagination cursor.") from exc
