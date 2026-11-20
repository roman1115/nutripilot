"""Six scoped queries and four preparation-only tools; no commit capability."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, Field

from nutripilot.application.diet_management import DietManagement
from nutripilot.domain.diet import DietError, DietModel
from nutripilot.domain.diet_records import DietChangePreview, DietItem, IntakeDraft, ResourceRef
from nutripilot.tools.errors import ToolRuntimeError
from nutripilot.tools.local.diet_models import (
    DIET_ARGUMENT_MODELS,
    AddItem,
    CorrectDietLog,
    CreateIntake,
    DiscardIntake,
    EntryPatch,
    GetDietLogs,
    GetRecommendationContext,
    PrepareDietLogChange,
    PrepareIntakeDraft,
    PrepareMealPlanChange,
    PrepareTargetChange,
    ReviseIntake,
)
from nutripilot.tools.models import ToolAccess, ToolContext, ToolDefinition
from nutripilot.tools.registry import ToolRegistry


def require_unambiguous_items(items: list[DietItem], patch: EntryPatch, message: str) -> None:
    edits = patch.item_edits or []
    edited_ids = {edit.item_id for edit in edits if not isinstance(edit, AddItem)}
    for edit in edits:
        if isinstance(edit, AddItem):
            continue
        selected = next((item for item in items if item.id == edit.item_id), None)
        if selected is None:
            continue
        matches = [
            item for item in items
            if item.name_snapshot.casefold() == selected.name_snapshot.casefold()
        ]
        if len(matches) < 2 or str(selected.id) in message:
            continue
        if edited_ids.issuperset(item.id for item in matches) and any(
            word in message for word in ("都", "全部", "每份")
        ):
            continue
        ordinals = re.findall(r"第([一二三四五六七八九十]|[1-9]\d*)([个份项])", message)
        selections = [
            (
                int(value) if value.isdigit() else "一二三四五六七八九十".index(value) + 1,
                selected.position + 1 if unit == "项" else matches.index(selected) + 1,
            )
            for value, unit in ordinals
        ]
        if selections and all(number == expected for number, expected in selections):
            continue
        grams = getattr(selected.quantity, "grams", None)
        if (
            grams is not None
            and sum(getattr(item.quantity, "grams", None) == grams for item in matches) == 1
            and re.search(rf"{re.escape(str(grams))}\s*(?:克|g)(?:的|那份|那一份)", message)
        ):
            continue
        raise DietError(
            "AMBIGUOUS_ITEM",
            f"有{len(matches)}项同名的{selected.name_snapshot}，请说明要修改第几份；"
            "当前内容保持不变。",
        )


class PreparedPreview(DietModel):
    ref: ResourceRef
    content_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    state: str
    blocking_fields: list[str]
    expires_at: AwareDatetime


class PreparedWarning(DietModel):
    code: str
    field: str | None = None
    message: str


class PreparedData(DietModel):
    kind: Literal["draft", "change_preview"]
    scope: ResourceRef
    as_of: AwareDatetime
    diet_epoch: int
    items: list[ResourceRef] = Field(default_factory=list)
    next_cursor: None = None
    truncated: bool = False
    warnings: list[PreparedWarning] = Field(default_factory=list)
    preview: PreparedPreview
    job: None = None


class DietDraftOperations:
    """Bound preparation functions only, deliberately excluding DietManagement.confirm."""

    def __init__(self, diet: DietManagement) -> None:
        self.prepare_intake = diet.intake.prepare
        self.revise_intake = diet.intake.revise
        self.discard_intake = diet.intake.discard
        self.correct_log = diet.records.prepare_correction
        self.delete_restore_log = diet.records.prepare_delete_restore
        self.prepare_plan = diet.plans.prepare
        self.prepare_target = diet.records.prepare_target
        self.foods = diet.foods

    async def prepare(
        self,
        name: str,
        arguments: BaseModel,
        context: ToolContext,
    ) -> IntakeDraft | DietChangePreview:
        owner = context.user_id
        assert owner is not None
        assert context.idempotency_key is not None
        key = context.idempotency_key
        if isinstance(arguments, PrepareIntakeDraft):
            request = arguments.root
            if isinstance(request, CreateIntake):
                if context.submission_anchor is None:
                    raise DietError("ANCHOR_REQUIRED", "Submission anchor is required.")
                return await self.prepare_intake(
                    owner,
                    request,
                    idempotency_key=key,
                    anchor=context.submission_anchor,
                    channel="chat",
                    thread_id=context.thread_id,
                    source_turn_id=context.turn_id,
                )
            if isinstance(request, ReviseIntake):
                return await self.revise_intake(owner, request, idempotency_key=key, actor="model")
            return await self.discard_intake(owner, request, idempotency_key=key)
        if isinstance(arguments, PrepareDietLogChange):
            change = arguments.root
            if isinstance(change, CorrectDietLog):
                return await self.correct_log(
                    owner,
                    change,
                    idempotency_key=key,
                    foods=self.foods,
                    thread_id=context.thread_id,
                    source_turn_id=context.turn_id,
                )
            return await self.delete_restore_log(
                owner,
                change.record_id,
                base_version=change.base_version,
                action=change.action,
                idempotency_key=key,
                thread_id=context.thread_id,
                source_turn_id=context.turn_id,
            )
        if isinstance(arguments, PrepareMealPlanChange):
            return await self.prepare_plan(
                owner,
                arguments.root,
                idempotency_key=key,
                thread_id=context.thread_id,
                source_turn_id=context.turn_id,
            )
        if isinstance(arguments, PrepareTargetChange):
            return await self.prepare_target(
                owner,
                arguments.root,
                idempotency_key=key,
                source="user_message",
                source_ref=str(context.turn_id),
                thread_id=context.thread_id,
                source_turn_id=context.turn_id,
            )
        raise DietError("INVALID_ARGUMENT", "Unsupported preparation request.")


class DietToolHandler:
    database_idempotency = True

    def __init__(self, name: str, diet: DietManagement) -> None:
        self.name = name
        self.recommendation_context = diet.queries.recommendation_context
        self.operations = DietDraftOperations(diet)
        self.store, self.default_timezone, self.clock = (
            diet.store,
            diet.default_timezone,
            diet.clock,
        )
        self.queries: dict[str, Callable[..., Awaitable[BaseModel]]] = {
            "search_foods": diet.queries.search,
            "get_meal_plans": diet.queries.plans,
            "get_diet_logs": diet.queries.logs,
            "get_nutrition_summary": diet.queries.summary,
            "get_nutrition_targets": diet.queries.targets,
            "get_diet_deviation": diet.queries.deviation,
        }

    async def __call__(self, arguments: BaseModel, context: ToolContext) -> dict[str, Any]:
        if context.user_id is None:
            raise ToolRuntimeError("USER_ID_REQUIRED", "An authenticated identity is required.")
        try:
            if isinstance(arguments, GetRecommendationContext):
                if context.submission_anchor is None:
                    raise DietError("ANCHOR_REQUIRED", "Submission anchor is required.")
                return await self.recommendation_context(
                    context.user_id, arguments, context.submission_anchor,
                )
            if self.name in self.queries:
                if (
                    isinstance(arguments, GetDietLogs)
                    and arguments.include_deleted
                    and (
                        not context.source_message
                        or not any(
                            term in context.source_message
                            for term in ("恢复", "回收站", "删除", "撤销")
                        )
                    )
                ):
                    raise DietError("EXPLICIT_ACTION_REQUIRED", "未授权查询已删除记录。")
                result = await self.queries[self.name](context.user_id, arguments)
                data = result.model_dump(mode="json")
                if len(result.model_dump_json().encode()) > 15 * 1024:
                    data.update(
                        items=[],
                        truncated=True,
                        next_cursor=None,
                        warnings=[
                            {
                                "code": "SCOPE_TOO_LARGE",
                                "field": None,
                                "message": "结果过长，未读完整；请缩小范围或在页面查看。",
                            }
                        ],
                    )
                return data
            if not isinstance(
                arguments,
                (
                    PrepareIntakeDraft,
                    PrepareDietLogChange,
                    PrepareMealPlanChange,
                    PrepareTargetChange,
                ),
            ):
                raise DietError("INVALID_ARGUMENT", "Unsupported preparation request.")
            request = arguments.root
            if not context.source_message or not context.turn_id:
                raise DietError("SOURCE_REQUIRED", "Preparation needs the current user message.")
            for index, evidence in enumerate(request.evidence):
                if (
                    evidence.source_kind != "user_message"
                    or evidence.source_ref != context.turn_id
                    or not evidence.original_text.strip()
                    or evidence.original_text not in context.source_message
                ):
                    raise DietError(
                        "SOURCE_REQUIRED",
                        f"evidence[{index}]必须使用source_kind=user_message、"
                        "source_ref=当前turn_id、original_text=当前user_message中的逐字片段；"
                        "删项或放弃只引用本轮操作原话，不复制原餐证据。",
                    )
            if isinstance(request, (ReviseIntake, DiscardIntake)):
                async with self.store.transaction(
                    context.user_id, default_timezone=self.default_timezone
                ) as tx:
                    current = await tx.get_draft(request.draft_id)
                    if current is None or current.thread_id != context.thread_id:
                        raise DietError("NOT_FOUND", "请先在当前会话选择草稿。")
                    if isinstance(request, ReviseIntake):
                        require_unambiguous_items(
                            current.content.items, request.patch, context.source_message
                        )
            if isinstance(request, CorrectDietLog):
                async with self.store.transaction(
                    context.user_id, default_timezone=self.default_timezone
                ) as tx:
                    record = await tx.get_log(request.record_id)
                    if record is None:
                        raise DietError("NOT_FOUND", "Record not found.")
                    require_unambiguous_items(record.items, request.patch, context.source_message)
            preview = await self.operations.prepare(self.name, arguments, context)
            if preview.thread_id != context.thread_id:
                raise DietError("NOT_FOUND", "预览不属于当前会话。")
            async with self.store.transaction(
                context.user_id, default_timezone=self.default_timezone
            ) as tx:
                epoch = tx.state.diet_epoch
            ref = ResourceRef(
                kind="draft" if isinstance(preview, IntakeDraft) else "preview",
                id=preview.id,
                version=preview.version,
            )
            return PreparedData(
                kind="draft" if isinstance(preview, IntakeDraft) else "change_preview",
                scope=ref,
                as_of=self.clock(),
                diet_epoch=epoch,
                preview=PreparedPreview(
                    ref=ref,
                    state=preview.state,
                    expires_at=preview.expires_at,
                    content_hash=(
                        preview.preview_hash
                        if isinstance(preview, IntakeDraft)
                        else preview.content_hash
                    ),
                    blocking_fields=(
                        preview.content.entry.blocking_fields(
                            today=preview.content.anchor.anchor_local_date
                        )
                        if isinstance(preview, IntakeDraft)
                        else []
                    ),
                ),
            ).model_dump(mode="json")
        except DietError as exc:
            raise ToolRuntimeError(exc.code, str(exc)) from exc


def register_diet_tools(registry: ToolRegistry, diet: DietManagement) -> None:
    for name, arguments_model in DIET_ARGUMENT_MODELS.items():
        preparing = name.startswith("prepare_")
        registry.register(
            ToolDefinition(
                name=name,
                arguments_model=arguments_model,
                handler=DietToolHandler(name, diet),
                description=(
                    "Prepare a persistent diet preview only; success does not mean formally saved."
                    if preparing
                    else "Query only the authenticated user's scoped diet facts."
                ),
                access=ToolAccess.WRITE if preparing else ToolAccess.READ,
                required_permission="diet:draft" if preparing else "diet:read",
                idempotency_backend="database" if preparing else "redis",
                private_arguments=True,
            )
        )
