"""Authenticated diet HTTP entry points; model tools never receive this commit capability."""

from __future__ import annotations

import hmac
import re
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.routing import APIRoute
from pydantic import Field, ValidationError, model_validator
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.background import BackgroundTask
from starlette.datastructures import UploadFile
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from nutripilot.application.container import ApplicationContainer
from nutripilot.application.diet_management import DayStatusRequest, DietManagement
from nutripilot.application.diet_parsing import (
    DietParsingService,
    ImageDraft,
    ParseRequest,
    TextDraft,
)
from nutripilot.application.diet_privacy import DietPrivacyService, PurgeRequest
from nutripilot.application.diet_queries import (
    CollectionQuery,
    CollectionScope,
    PlanIDScope,
    PlanLibraryQuery,
    PlanMealsQuery,
    PlanMealView,
)
from nutripilot.application.intake_service import IntakeAlreadyRecorded, UserDraftEdit
from nutripilot.application.personal_food_service import PersonalFoodMutation
from nutripilot.domain.diet import (
    DietError,
    DietModel,
    FieldEvidence,
    SubmissionAnchor,
    Version,
    content_hash,
)
from nutripilot.domain.diet_jobs import BackgroundJob, ExportRequest
from nutripilot.domain.diet_records import IntakeDraft, OperationReceipt
from nutripilot.infrastructure.diet_store import PostgresDietStore
from nutripilot.tools.local.diet_models import (
    CopyMealPlan,
    CopyPlanWeek,
    CorrectDietLog,
    CorrectTarget,
    CreateIntake,
    CreateMealPlan,
    DeleteMealPlan,
    DeleteRestoreDietLog,
    DiscardIntake,
    EntryPatch,
    GetDietDeviation,
    GetDietLogs,
    GetMealPlans,
    GetNutritionSummary,
    GetNutritionTargets,
    ReviseIntake,
    SearchFoods,
    SetTarget,
    StopTarget,
    UpdateMealPlan,
)


class DietRoute(APIRoute):
    def get_route_handler(self) -> Any:
        handler = super().get_route_handler()

        async def protected(request: Request) -> Response:
            try:
                return await handler(request)
            except ValidationError as exc:
                return JSONResponse(
                    status_code=422,
                    content={
                        "detail": {
                            "code": "INVALID_FIELDS",
                            "message": "字段不符合饮食契约。",
                            "field_errors": [
                                {"field": ".".join(map(str, error["loc"])), "message": error["msg"]}
                                for error in exc.errors(include_input=False, include_context=False)
                            ],
                            "retryable": False,
                        }
                    },
                )
            except SQLAlchemyError:
                return JSONResponse(
                    status_code=503,
                    content={
                        "detail": {
                            "code": "DEPENDENCY_UNAVAILABLE",
                            "message": "饮食数据库暂不可用。",
                            "retryable": True,
                        }
                    },
                )

        return protected


class DietHTTPMiddleware:
    """Bound JSON before parsing; private diet responses are never publicly cacheable."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not (
            path == "/api/v1/diet" or path.startswith("/api/v1/diet/")
        ):
            await self.app(scope, receive, send)
            return

        async def private_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != b"cache-control"
                ]
                message = {**message, "headers": [*headers, (b"cache-control", b"no-store")]}
            await send(message)

        if scope["method"] in ("POST", "PATCH", "PUT"):
            if path == "/api/v1/diet/media":
                received = 0

                async def media_receive() -> Message:
                    nonlocal received
                    message = await receive()
                    received += len(message.get("body", b""))
                    if received > 20 * 1024 * 1024 + 64 * 1024:
                        raise HTTPException(
                            413,
                            detail={
                                "code": "MEDIA_TOO_LARGE",
                                "message": "图片合计不能超过20MiB。",
                            },
                        )
                    return message

                await self.app(scope, media_receive, private_send)
                return
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > 256 * 1024:
                    await JSONResponse(
                        status_code=413,
                        content={
                            "detail": {
                                "code": "REQUEST_TOO_LARGE",
                                "message": "饮食请求不能超过256KiB。",
                            }
                        },
                    )(scope, receive, private_send)
                    return
                if not message.get("more_body", False):
                    break
            consumed = False

            async def bounded_receive() -> Message:
                nonlocal consumed
                if not consumed:
                    consumed = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()

            await self.app(scope, bounded_receive, private_send)
        else:
            await self.app(scope, receive, private_send)


router = APIRouter(prefix="/api/v1/diet", tags=["diet"], route_class=DietRoute)


def container(request: Request) -> ApplicationContainer:
    return cast("ApplicationContainer", request.app.state.container)


def identity(request: Request, active: ApplicationContainer = Depends(container)) -> int:
    owner = getattr(request.state, "user_id", None)
    if type(owner) is int and owner > 0:
        return owner
    if active.settings.app_mode in ("demo", "test"):
        return 1
    raise HTTPException(401, detail={"code": "IDENTITY_REQUIRED", "message": "需要认证身份。"})


async def service(active: ApplicationContainer = Depends(container)) -> DietManagement:
    if isinstance(active.diet.store, PostgresDietStore):
        try:
            async with active.diet.store.engine.connect() as connection:
                exists = (
                    await connection.execute(
                        text(
                            "SELECT to_regclass('diet_user_state') IS NOT NULL "
                            "AND to_regclass('diet_purge_tombstones') IS NOT NULL "
                            "AND EXISTS(SELECT 1 FROM information_schema.columns "
                            "WHERE table_schema=current_schema() "
                            "AND table_name='diet_purge_tombstones' "
                            "AND column_name='entity_id' AND data_type='text') "
                            "AND EXISTS(SELECT 1 FROM information_schema.columns "
                            "WHERE table_schema=current_schema() AND table_name='meal_plans' "
                            "AND column_name='updated_at')"
                        )
                    )
                ).scalar_one()
            if not exists:
                raise HTTPException(
                    503,
                    detail={
                        "code": "DIET_MIGRATION_REQUIRED",
                        "message": "饮食数据库迁移尚未完成。",
                    },
                )
        except SQLAlchemyError as exc:
            raise HTTPException(
                503, detail={"code": "DEPENDENCY_UNAVAILABLE", "message": "饮食数据库暂不可用。"}
            ) from exc
    return active.diet


def parsing(active: ApplicationContainer = Depends(container)) -> DietParsingService:
    if active.diet_parsing is None:
        raise HTTPException(
            503, detail={"code": "DIET_MIGRATION_REQUIRED", "message": "饮食任务存储未就绪。"}
        )
    return active.diet_parsing


def privacy(parser: DietParsingService = Depends(parsing)) -> DietPrivacyService:
    return DietPrivacyService(parser)


def public_job(job: BackgroundJob) -> dict[str, Any]:
    return job.model_dump(
        mode="json",
        exclude={
            "request_hash": True,
            "idempotency_key": True,
            "lease_owner": True,
            "artifact": {"storage_key"},
        },
    )


def request_key(value: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> str:
    if value is None or not 1 <= len(value) <= 100 or not value.strip():
        raise HTTPException(
            422, detail={"code": "IDEMPOTENCY_KEY_REQUIRED", "message": "需要幂等键。"}
        )
    return value


def base_version(value: Annotated[str | None, Header(alias="If-Match")] = None) -> int:
    if value is None:
        raise HTTPException(428, detail={"code": "VERSION_REQUIRED", "message": "需要对象版本。"})
    if re.fullmatch(r'"(0|[1-9][0-9]*)"', value) is None:
        raise HTTPException(422, detail={"code": "INVALID_VERSION", "message": "版本格式无效。"})
    return int(value[1:-1])


def etag(response: Response, version: int) -> None:
    response.headers["ETag"] = f'"{version}"'
    response.headers["Cache-Control"] = "no-store"


def form_evidence(values: list[FieldEvidence], key: str) -> list[FieldEvidence]:
    return [
        value.model_copy(
            update={
                "source_kind": "user_form",
                "source_ref": f"form:{key}",
            }
        )
        for value in values
    ]


class StructuredDraft(CreateIntake):
    mode: Literal["structured"]
    action: Literal["create"] = "create"
    channel: Literal["manual", "plan"] = "manual"
    record_again_authorized: Annotated[bool, Field(strict=True)] = False


class ConfirmPreview(DietModel):
    approved: Annotated[bool, Field(strict=True)]
    preview_version: Version
    content_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class PlanLifecycle(DietModel):
    action: Literal["archive", "restore"]
    plan_id: Annotated[str, Field(min_length=1, max_length=120)]
    base_version: Version


class PreviewRequest(DietModel):
    timezone: Annotated[str, Field(min_length=1, max_length=120)] | None = None
    operation: Literal[
        "log_correct",
        "log_delete",
        "log_restore",
        "plan_create",
        "plan_update",
        "plan_delete_meal",
        "plan_copy_meal",
        "plan_copy_week",
        "plan_archive",
        "plan_restore",
        "target_set",
        "target_correct",
        "target_stop",
        "personal_food_create",
        "personal_food_update",
        "personal_food_delete",
        "personal_food_restore",
        "day_status_set",
    ]
    arguments: (
        CorrectDietLog
        | DeleteRestoreDietLog
        | CreateMealPlan
        | CopyMealPlan
        | CopyPlanWeek
        | DeleteMealPlan
        | SetTarget
        | CorrectTarget
        | StopTarget
        | PersonalFoodMutation
        | DayStatusRequest
        | UpdateMealPlan
        | PlanLifecycle
    )

    @model_validator(mode="after")
    def matching_operation(self) -> PreviewRequest:
        if self.timezone is not None and self.operation != "log_correct":
            raise ValueError("Only explicit record corrections may change its timezone.")
        expected = {
            "log_correct": (CorrectDietLog, "correct"),
            "log_delete": (DeleteRestoreDietLog, "delete"),
            "log_restore": (DeleteRestoreDietLog, "restore"),
            "plan_create": (CreateMealPlan, "create"),
            "plan_delete_meal": (DeleteMealPlan, "delete"),
            "plan_copy_meal": (CopyMealPlan, "copy"),
            "plan_copy_week": (CopyPlanWeek, "copy_week"),
            "plan_update": (UpdateMealPlan, "update"),
            "plan_archive": (PlanLifecycle, "archive"),
            "plan_restore": (PlanLifecycle, "restore"),
            "target_set": (SetTarget, "set"),
            "target_correct": (CorrectTarget, "correct"),
            "target_stop": (StopTarget, "stop"),
            "personal_food_create": (PersonalFoodMutation, "create"),
            "personal_food_update": (PersonalFoodMutation, "update"),
            "personal_food_delete": (PersonalFoodMutation, "delete"),
            "personal_food_restore": (PersonalFoodMutation, "restore"),
            "day_status_set": (DayStatusRequest, None),
        }.get(self.operation)
        if expected is None or not isinstance(self.arguments, expected[0]):
            raise ValueError("Operation and arguments do not match.")
        if expected[1] is not None and getattr(self.arguments, "action", None) != expected[1]:
            raise ValueError("Operation and action do not match.")
        return self


class TimezoneUpdate(DietModel):
    timezone: Annotated[str, Field(min_length=1, max_length=120)]


@router.get("/context")
async def get_context(
    response: Response,
    user_id: int = Depends(identity),
    diet: DietManagement = Depends(service),
    active: ApplicationContainer = Depends(container),
) -> dict[str, Any]:
    async with diet.store.transaction(user_id, default_timezone=diet.default_timezone) as tx:
        anchor = SubmissionAnchor.capture(diet.clock(), tx.state.timezone)
        etag(response, tx.state.diet_epoch)
        return {
            **tx.state.model_dump(mode="json"),
            "anchor": anchor.model_dump(mode="json"),
            "week_start": (
                anchor.anchor_local_date - timedelta(days=anchor.anchor_local_date.weekday())
            ).isoformat(),
            "capabilities": {
                "structured": True,
                "text": active.diet_parsing is not None,
                "image": active.diet_parsing is not None and active.settings.diet_image_enabled,
            },
        }


@router.patch("/context")
async def update_context(
    payload: TimezoneUpdate,
    response: Response,
    user_id: int = Depends(identity),
    diet: DietManagement = Depends(service),
    version: int = Depends(base_version),
    key: str = Depends(request_key),
) -> Any:
    async with diet.store.transaction(user_id, default_timezone=diet.default_timezone) as tx:
        previous = await tx.get_receipt("timezone_set", key)
        request_hash = content_hash(payload)
        if previous:
            if not hmac.compare_digest(previous.request_hash, request_hash):
                raise DietError("IDEMPOTENCY_CONFLICT", "Request key has different content.")
            etag(response, tx.state.diet_epoch)
            return tx.state
        if tx.state.diet_epoch != version:
            raise DietError("VERSION_CONFLICT", "User context has changed.")
        state = await tx.set_timezone(payload.timezone)
        from uuid import uuid4

        await tx.save_receipt(
            OperationReceipt(
                id=uuid4(),
                user_id=user_id,
                operation="timezone_set",
                idempotency_key=key,
                request_hash=request_hash,
                result_refs=[],
                result_status="committed",
                created_at=diet.clock(),
            )
        )
        etag(response, state.diet_epoch)
        return state


@router.get("/logs")
async def get_logs(
    start_date: date,
    end_date: date,
    include_deleted: bool = False,
    cursor: str | None = None,
    limit: int = Query(20, ge=1, le=50),
    user_id: int = Depends(identity),
    diet: DietManagement = Depends(service),
) -> Any:
    return await diet.queries.logs(
        user_id,
        GetDietLogs.model_validate(
            {
                "scope": {"kind": "range", "start_date": start_date, "end_date": end_date},
                "include_deleted": include_deleted,
                "limit": limit,
                **({"cursor": cursor} if cursor else {}),
            }
        ),
    )


@router.get("/logs/{record_id}")
async def get_log(
    record_id: UUID,
    response: Response,
    user_id: int = Depends(identity),
    diet: DietManagement = Depends(service),
) -> Any:
    log = await diet.records.get_log(user_id, record_id)
    etag(response, log.version)
    return log


@router.get("/plans")
async def get_plans(
    start_date: date,
    end_date: date,
    cursor: str | None = None,
    limit: int = Query(20, ge=1, le=50),
    user_id: int = Depends(identity),
    diet: DietManagement = Depends(service),
) -> Any:
    return await diet.queries.plans(
        user_id,
        GetMealPlans.model_validate(
            {
                "scope": {"kind": "range", "start_date": start_date, "end_date": end_date},
                "limit": limit,
                **({"cursor": cursor} if cursor else {}),
            }
        ),
    )


@router.get("/plans/{plan_id}")
async def get_plan(
    plan_id: str,
    response: Response,
    user_id: int = Depends(identity),
    diet: DietManagement = Depends(service),
) -> Any:
    async with diet.store.transaction(user_id, default_timezone=diet.default_timezone) as tx:
        plan = await tx.get_plan(plan_id)
        if plan is None:
            raise DietError("NOT_FOUND", "Plan not found.")
        etag(response, plan.version)
        return plan


@router.get("/plan-library")
async def get_plan_library(
    start_date: date,
    end_date: date,
    include_deleted: bool = False,
    cursor: str | None = None,
    limit: int = Query(20, ge=1, le=50),
    user_id: int = Depends(identity),
    diet: DietManagement = Depends(service),
) -> Any:
    return await diet.queries.plan_library(
        user_id,
        PlanLibraryQuery.model_validate(
            {
                "scope": {"kind": "range", "start_date": start_date, "end_date": end_date},
                "include_deleted": include_deleted,
                "cursor": cursor,
                "limit": limit,
            }
        ),
    )


@router.get("/plans/{plan_id}/meals")
async def get_plan_meals(
    plan_id: str,
    cursor: str | None = None,
    limit: int = Query(20, ge=1, le=50),
    user_id: int = Depends(identity),
    diet: DietManagement = Depends(service),
) -> Any:
    return await diet.queries.plan_meals(
        user_id,
        PlanMealsQuery(
            scope=PlanIDScope(id=plan_id),
            cursor=cursor,
            limit=limit,
        ),
    )


@router.get("/summary")
async def get_summary(
    start_date: date,
    end_date: date,
    granularity: Literal["day", "week"] = "day",
    user_id: int = Depends(identity),
    diet: DietManagement = Depends(service),
) -> Any:
    return await diet.queries.summary(
        user_id,
        GetNutritionSummary.model_validate(
            {
                "scope": {"kind": "range", "start_date": start_date, "end_date": end_date},
                "granularity": granularity,
            }
        ),
    )


@router.get("/deviation")
async def get_deviation(
    start_date: date,
    end_date: date,
    plan_id: UUID | None = None,
    user_id: int = Depends(identity),
    diet: DietManagement = Depends(service),
) -> Any:
    return await diet.queries.deviation(
        user_id,
        GetDietDeviation.model_validate(
            {
                "scope": {"kind": "range", "start_date": start_date, "end_date": end_date},
                **({"plan_id": plan_id} if plan_id else {}),
            }
        ),
    )


@router.get("/planned-meals/{meal_id}")
async def get_planned_meal(
    meal_id: UUID,
    response: Response,
    user_id: int = Depends(identity),
    diet: DietManagement = Depends(service),
) -> PlanMealView:
    async with diet.store.transaction(user_id, default_timezone=diet.default_timezone) as tx:
        meal = await tx.get_planned_meal(meal_id)
        if meal is None:
            raise DietError("NOT_FOUND", "Planned meal not found.")
        plan = await tx.get_plan(meal.plan_id)
        if plan is None:
            raise DietError("NOT_FOUND", "Plan not found.")
        logs = await tx.list_source_logs(meal_id)
        from nutripilot.domain.diet_records import ResourceRef

        etag(response, meal.version)
        return PlanMealView(
            plan=plan,
            meal=meal,
            actual_refs=[
                ResourceRef(kind="log", id=log.id, version=log.version)
                for log in logs
                if log.deleted_at is None
            ],
        )


@router.get("/targets")
async def get_targets(
    start_date: date,
    end_date: date,
    user_id: int = Depends(identity),
    diet: DietManagement = Depends(service),
) -> Any:
    return await diet.queries.targets(
        user_id,
        GetNutritionTargets.model_validate(
            {
                "scope": {"kind": "range", "start_date": start_date, "end_date": end_date},
            }
        ),
    )


@router.get("/foods")
async def search_foods(
    query: str,
    limit: int = Query(10, ge=1, le=20),
    user_id: int = Depends(identity),
    diet: DietManagement = Depends(service),
) -> Any:
    return await diet.queries.search(user_id, SearchFoods(query=query, limit=limit))


@router.get("/foods/{food_id}")
async def get_food(
    food_id: UUID,
    response: Response,
    user_id: int = Depends(identity),
    diet: DietManagement = Depends(service),
) -> Any:
    async with diet.store.transaction(user_id, default_timezone=diet.default_timezone) as tx:
        food = await tx.get_personal_food(food_id)
        if food is None:
            raise DietError("NOT_FOUND", "Personal food not found.")
        etag(response, food.version)
        return food


@router.get("/personal-foods")
async def list_personal_foods(
    include_deleted: bool = False,
    cursor: str | None = None,
    limit: int = Query(20, ge=1, le=50),
    user_id: int = Depends(identity),
    diet: DietManagement = Depends(service),
) -> Any:
    return await diet.queries.personal_foods(
        user_id,
        CollectionQuery(
            scope=CollectionScope(resource="personal_foods"),
            cursor=cursor,
            limit=limit,
            include_deleted=include_deleted,
        ),
    )


@router.get("/days/{local_date}")
async def get_day_declaration(
    local_date: date,
    response: Response,
    user_id: int = Depends(identity),
    diet: DietManagement = Depends(service),
) -> Any:
    async with diet.store.transaction(user_id, default_timezone=diet.default_timezone) as tx:
        declaration = await tx.get_day_status(local_date)
        etag(response, declaration.version)
        return declaration


@router.post("/drafts", status_code=201)
async def create_draft(
    payload: Annotated[StructuredDraft | TextDraft | ImageDraft, Field(discriminator="mode")],
    response: Response,
    user_id: int = Depends(identity),
    diet: DietManagement = Depends(service),
    key: str = Depends(request_key),
    active: ApplicationContainer = Depends(container),
) -> Any:
    if not isinstance(payload, StructuredDraft):
        parser = parsing(active)
        draft = await parser.create(user_id, payload, key=key)
        etag(response, draft.version)
        return draft
    async with diet.store.transaction(user_id, default_timezone=diet.default_timezone) as tx:
        anchor = SubmissionAnchor.capture(diet.clock(), tx.state.timezone)
    request = CreateIntake.model_validate(
        payload.model_dump(
            exclude={
                "mode",
                "channel",
                "record_again_authorized",
            }
        )
    )
    request = request.model_copy(update={"evidence": form_evidence(request.evidence, key)})
    draft = await diet.intake.prepare(
        user_id,
        request,
        idempotency_key=key,
        anchor=anchor,
        channel=payload.channel,
        explicit_repeat_authorized=payload.record_again_authorized,
    )
    etag(response, draft.version)
    return draft


@router.get("/drafts")
async def list_drafts(
    cursor: str | None = None,
    limit: int = Query(20, ge=1, le=50),
    user_id: int = Depends(identity),
    diet: DietManagement = Depends(service),
) -> Any:
    return await diet.queries.drafts(
        user_id,
        CollectionQuery(scope=CollectionScope(resource="drafts"), cursor=cursor, limit=limit),
    )


@router.get("/drafts/{draft_id}")
async def get_draft(
    draft_id: UUID,
    response: Response,
    user_id: int = Depends(identity),
    diet: DietManagement = Depends(service),
) -> Any:
    preview = await diet.preview(user_id, draft_id)
    if not isinstance(preview, IntakeDraft):
        raise DietError("NOT_FOUND", "Draft not found.")
    etag(response, preview.version)
    return preview


class DraftPatch(DietModel):
    patch: EntryPatch
    evidence: list[FieldEvidence]
    user_edit: UserDraftEdit | None = None


@router.patch("/drafts/{draft_id}")
async def patch_draft(
    draft_id: UUID,
    payload: DraftPatch,
    response: Response,
    user_id: int = Depends(identity),
    diet: DietManagement = Depends(service),
    key: str = Depends(request_key),
    version: int = Depends(base_version),
) -> Any:
    draft = await diet.intake.revise(
        user_id,
        ReviseIntake(
            action="revise",
            draft_id=draft_id,
            base_version=version,
            patch=payload.patch,
            evidence=form_evidence(payload.evidence, key),
        ),
        idempotency_key=key,
        user_edit=payload.user_edit,
    )
    etag(response, draft.version)
    return draft


@router.get("/drafts/{draft_id}/label")
async def label_proposal(
    draft_id: UUID,
    user_id: int = Depends(identity),
    diet: DietManagement = Depends(service),
) -> Any:
    draft = await diet.preview(user_id, draft_id)
    if not isinstance(draft, IntakeDraft) or draft.label_suggestion is None:
        raise DietError("NOT_FOUND", "Label proposal not found.")
    if draft.state not in ("needs_input", "ready"):
        raise DietError("PREVIEW_EXPIRED", "Label draft is no longer editable.")
    label = draft.label_suggestion
    return {
        "name": label.name or "",
        "brand": label.brand,
        "basis": label.basis,
        "serving_grams": str(label.serving_grams) if label.serving_grams is not None else None,
        "nutrients": label.normalized(),
        "source_note": label.original_excerpt,
        "media_id": draft.media_ids[0],
        "media_ids": draft.media_ids,
        "label_draft_id": str(draft.id),
        "label_draft_version": draft.version,
    }


@router.post("/drafts/{draft_id}/parse", status_code=202)
async def parse_draft(
    draft_id: UUID,
    payload: ParseRequest,
    user_id: int = Depends(identity),
    parser: DietParsingService = Depends(parsing),
    key: str = Depends(request_key),
    version: int = Depends(base_version),
) -> Any:
    draft = await parser.enqueue(user_id, draft_id, version, payload, key=key)
    assert draft.job_id is not None
    import asyncio

    return public_job(await asyncio.to_thread(parser.jobs.get, draft.job_id, user_id))


@router.post("/drafts/{draft_id}/split", status_code=201)
async def split_draft(
    draft_id: UUID,
    user_id: int = Depends(identity),
    parser: DietParsingService = Depends(parsing),
    key: str = Depends(request_key),
    version: int = Depends(base_version),
) -> Any:
    return {"items": await parser.split(user_id, draft_id, version, key=key)}


@router.get("/jobs")
async def list_jobs(
    before: datetime | None = None,
    user_id: int = Depends(identity),
    parser: DietParsingService = Depends(parsing),
) -> Any:
    import asyncio

    if before is not None and before.tzinfo is None:
        raise DietError("INVALID_FIELDS", "任务分页时间必须包含时区。")
    jobs = await asyncio.to_thread(parser.jobs.list_owned, user_id, before)
    return {
        "items": [public_job(job) for job in jobs],
        "next_before": jobs[-1].created_at.isoformat() if len(jobs) == 50 else None,
    }


@router.post("/exports", status_code=202)
async def create_export(
    payload: ExportRequest,
    user_id: int = Depends(identity),
    manager: DietPrivacyService = Depends(privacy),
    key: str = Depends(request_key),
) -> Any:
    return public_job(await manager.submit_export(user_id, payload, key))


@router.get("/exports/{job_id}/content")
async def download_export(
    job_id: UUID,
    user_id: int = Depends(identity),
    manager: DietPrivacyService = Depends(privacy),
) -> Response:
    source = await manager.open_download(user_id, job_id)

    def chunks() -> Iterator[bytes]:
        try:
            while chunk := source.read(1024 * 1024):
                yield chunk
        finally:
            source.close()

    return StreamingResponse(
        chunks(),
        media_type="application/zip",
        background=BackgroundTask(source.close),
        headers={
            "Content-Disposition": 'attachment; filename="nutripilot-diet.zip"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/purge/previews", status_code=201)
async def prepare_purge(
    payload: PurgeRequest,
    user_id: int = Depends(identity),
    manager: DietPrivacyService = Depends(privacy),
    key: str = Depends(request_key),
) -> Any:
    return await manager.prepare_purge(user_id, payload, key)


@router.post("/purge/previews/{preview_id}/confirm", status_code=202)
async def confirm_purge(
    preview_id: UUID,
    payload: ConfirmPreview,
    user_id: int = Depends(identity),
    manager: DietPrivacyService = Depends(privacy),
    version: int = Depends(base_version),
    key: str = Depends(request_key),
) -> Any:
    if not payload.approved:
        raise DietError("PURGE_CONFIRMATION_REQUIRED", "未批准永久清除。")
    if version != payload.preview_version:
        raise DietError("VERSION_CONFLICT", "Header and approved version differ.")
    return public_job(
        await manager.confirm_purge(user_id, preview_id, version, payload.content_hash)
    )


@router.get("/jobs/{job_id}")
async def get_job(
    job_id: UUID, user_id: int = Depends(identity), parser: DietParsingService = Depends(parsing)
) -> Any:
    import asyncio

    job = await asyncio.to_thread(parser.jobs.get, job_id, user_id)
    return public_job(job)


@router.get("/jobs/{job_id}/events")
async def get_job_events(
    job_id: UUID,
    after_sequence: int = Query(0, ge=0),
    user_id: int = Depends(identity),
    parser: DietParsingService = Depends(parsing),
) -> Any:
    import asyncio

    return {"items": await asyncio.to_thread(parser.jobs.events, job_id, user_id, after_sequence)}


@router.post("/jobs/{job_id}/cancel", status_code=202)
async def cancel_job(
    job_id: UUID,
    user_id: int = Depends(identity),
    parser: DietParsingService = Depends(parsing),
    key: str = Depends(request_key),
) -> Any:
    import asyncio

    return public_job(await asyncio.to_thread(parser.jobs.cancel, job_id, user_id))


@router.post("/media", status_code=201)
async def upload_media(
    request: Request,
    user_id: int = Depends(identity),
    parser: DietParsingService = Depends(parsing),
    key: str = Depends(request_key),
) -> Any:
    async with request.form(max_files=4, max_fields=1, max_part_size=10 * 1024 * 1024) as form:
        if set(form) - {"files", "kind"}:
            raise DietError("INVALID_FIELDS", "未知的上传字段。")
        files = form.getlist("files")
        kind = form.get("kind", "intake_image")
        if kind not in ("intake_image", "label_image") or not 1 <= len(files) <= 4:
            raise DietError("INVALID_FIELDS", "每次上传1至4张图片，且用途必须明确。")
        data = []
        total = 0
        for upload in files:
            if not isinstance(upload, UploadFile):
                raise DietError("INVALID_FIELDS", "缺少图片文件。")
            content = await upload.read(10 * 1024 * 1024 + 1)
            total += len(content)
            if len(content) > 10 * 1024 * 1024 or total > 20 * 1024 * 1024:
                raise HTTPException(
                    413, detail={"code": "MEDIA_TOO_LARGE", "message": "图片超出大小限制。"}
                )
            data.append((content, upload.content_type or ""))
        assets = await parser.upload_many(
            user_id, data, cast("Literal['intake_image', 'label_image']", kind), key=key
        )
        return {
            "items": [
                asset.model_dump(mode="json", exclude={"storage_key", "user_id"})
                for asset in assets
            ]
        }


@router.get("/media/{media_id}/content")
async def media_content(
    media_id: UUID, user_id: int = Depends(identity), parser: DietParsingService = Depends(parsing)
) -> Response:
    asset, content = await parser.image(user_id, media_id)
    return Response(
        content,
        media_type=asset.mime,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "inline; filename=image.png",
        },
    )


@router.delete("/media/{media_id}", status_code=202)
async def delete_media(
    media_id: UUID,
    user_id: int = Depends(identity),
    parser: DietParsingService = Depends(parsing),
    key: str = Depends(request_key),
) -> Any:
    await parser.remove_media(user_id, media_id)
    return {"state": "purged"}


@router.post("/previews", status_code=201)
async def prepare_preview(
    payload: PreviewRequest,
    user_id: int = Depends(identity),
    diet: DietManagement = Depends(service),
    key: str = Depends(request_key),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> Any:
    args = payload.arguments
    expected = getattr(args, "base_version", None)
    if expected is None and isinstance(args, CreateMealPlan) and args.parent.kind == "existing":
        expected = args.parent.base_version
    if expected is not None and base_version(if_match) != expected:
        raise DietError("VERSION_CONFLICT", "Header and source version differ.")
    if hasattr(args, "evidence"):
        args = args.model_copy(update={"evidence": form_evidence(args.evidence, key)})
    if isinstance(args, (SetTarget, CorrectTarget, StopTarget)):
        return await diet.records.prepare_target(
            user_id,
            args,
            idempotency_key=key,
            source="user_form",
            source_ref=f"form:{key}",
        )
    if isinstance(args, CorrectDietLog):
        return await diet.records.prepare_correction(
            user_id,
            args,
            idempotency_key=key,
            foods=diet.foods,
            timezone=payload.timezone,
        )
    if isinstance(args, DeleteRestoreDietLog):
        return await diet.records.prepare_delete_restore(
            user_id,
            args.record_id,
            base_version=args.base_version,
            action=args.action,
            idempotency_key=key,
        )
    if isinstance(args, PersonalFoodMutation):
        return await diet.personal_foods.prepare(user_id, args, idempotency_key=key)
    if isinstance(args, DayStatusRequest):
        return await diet.prepare_day(user_id, args, idempotency_key=key)
    if isinstance(args, PlanLifecycle):
        return await diet.plans.prepare_archive_restore(
            user_id,
            args.plan_id,
            base_version=args.base_version,
            action=args.action,
            idempotency_key=key,
        )
    return await diet.plans.prepare(user_id, args, idempotency_key=key)


@router.get("/previews/{preview_id}")
async def get_preview(
    preview_id: UUID,
    response: Response,
    user_id: int = Depends(identity),
    diet: DietManagement = Depends(service),
) -> Any:
    preview = await diet.preview(user_id, preview_id)
    etag(response, preview.version)
    return preview


@router.post("/previews/{preview_id}/confirm")
async def confirm_preview(
    preview_id: UUID,
    payload: ConfirmPreview,
    user_id: int = Depends(identity),
    diet: DietManagement = Depends(service),
    key: str = Depends(request_key),
    version: int = Depends(base_version),
) -> Any:
    if version != payload.preview_version:
        raise DietError("VERSION_CONFLICT", "Header and approved version differ.")
    if not payload.approved:
        preview = await diet.preview(user_id, preview_id)
        if isinstance(preview, IntakeDraft):
            if not hmac.compare_digest(preview.preview_hash, payload.content_hash):
                raise DietError("VERSION_CONFLICT", "Approved content is different.")
            return await diet.intake.discard(
                user_id,
                DiscardIntake(
                    action="discard",
                    draft_id=preview_id,
                    base_version=version,
                    evidence=[
                        FieldEvidence(
                            field="operation",
                            source_kind="user_form",
                            source_ref=f"form:{key}",
                            original_text="discard",
                            operation="clear",
                            status="explicit",
                        )
                    ],
                ),
                idempotency_key=key,
            )
        return await diet.discard_preview(
            user_id,
            preview_id,
            version=version,
            approved_hash=payload.content_hash,
        )
    return await diet.confirm(
        user_id,
        preview_id,
        version=version,
        approved_hash=payload.content_hash,
    )


@router.get("/receipts/{receipt_id}")
async def get_receipt(
    receipt_id: UUID,
    user_id: int = Depends(identity),
    diet: DietManagement = Depends(service),
) -> Any:
    async with diet.store.transaction(user_id, default_timezone=diet.default_timezone) as tx:
        receipt = await tx.get_receipt_by_id(receipt_id)
        if receipt is None:
            raise DietError("NOT_FOUND", "Receipt not found.")
        for ref in receipt.result_refs:
            if ref.kind == "log":
                log = await tx.get_log(ref.uuid)
                if log is None:
                    return receipt.model_copy(update={"result_status": "purged"})
                if log.deleted_at:
                    return receipt.model_copy(update={"result_status": "deleted"})
        return receipt


@router.post("/drafts/{draft_id}/discard")
async def discard_draft(
    draft_id: UUID,
    user_id: int = Depends(identity),
    diet: DietManagement = Depends(service),
    key: str = Depends(request_key),
    version: int = Depends(base_version),
) -> Any:
    return await diet.intake.discard(
        user_id,
        DiscardIntake(
            action="discard",
            draft_id=draft_id,
            base_version=version,
            evidence=[
                FieldEvidence(
                    field="operation",
                    source_kind="user_form",
                    source_ref=f"form:{key}",
                    original_text="discard",
                    operation="clear",
                    status="explicit",
                )
            ],
        ),
        idempotency_key=key,
    )


def register_diet_errors(app: FastAPI) -> None:
    from fastapi.responses import JSONResponse

    @app.exception_handler(DietError)
    async def diet_error(request: Request, exc: DietError) -> JSONResponse:
        status = 422
        if exc.code in ("NOT_FOUND",):
            status = 404
        elif exc.code in (
            "PREVIEW_EXPIRED",
            "RESOURCE_PURGED",
            "RESOURCE_EXPIRED",
            "EXPORT_EXPIRED",
            "EXPORT_REVOKED",
        ):
            status = 410
        elif exc.code in (
            "VERSION_CONFLICT",
            "IDEMPOTENCY_CONFLICT",
            "DATA_CHANGED",
            "NO_CHANGE",
            "SOURCE_CHANGED",
            "ALREADY_RECORDED",
            "PLAN_SLOT_CONFLICT",
            "INVALID_PREVIEW_STATE",
            "MEDIA_REFERENCED",
            "PURGE_IRREVERSIBLE",
            "EXPORT_NOT_READY",
        ):
            status = 409
        elif exc.code in ("DEPENDENCY_UNAVAILABLE", "PERSISTENCE_CONFLICT"):
            status = 503
        elif exc.code == "QUEUE_FULL":
            status = 429
        elif exc.code == "MEDIA_TOO_LARGE":
            status = 413
        elif exc.code in (
            "UNSUPPORTED_MEDIA_TYPE",
            "INVALID_MEDIA_FORMAT",
            "ANIMATED_MEDIA_REJECTED",
        ):
            status = 415
        elif exc.code in ("IMAGE_DISABLED", "IMAGE_CONSENT_REQUIRED"):
            status = 403
        detail: dict[str, Any] = {
            "code": exc.code,
            "message": str(exc),
            "field_errors": [],
            "retryable": status == 503,
        }
        if isinstance(exc, IntakeAlreadyRecorded):
            detail["record_ref"] = {
                "id": str(exc.record.id),
                "version": exc.record.version,
            }
        return JSONResponse(
            status_code=status, content={"detail": detail}, headers={"Cache-Control": "no-store"}
        )
