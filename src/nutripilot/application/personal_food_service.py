"""Owned food references share the same frozen preview and atomic receipt rules."""

from __future__ import annotations

import hmac
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Literal, Self
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from nutripilot.domain.diet import (
    DRAFT_LIFETIME,
    NUTRIENTS,
    TRASH_LIFETIME,
    DietError,
    DietModel,
    FoodEvidenceSnapshot,
    FoodState,
    Grams,
    Nutrient,
    NutrientRange,
    NutrientSnapshot,
    NutritionSnapshot,
    ShortText,
    content_hash,
)
from nutripilot.domain.diet_records import (
    AllergenFact,
    DietChangePreview,
    OperationReceipt,
    PersonalFood,
    PersonalFoodChangeContent,
    ResourceRef,
)
from nutripilot.tools.local.ports import DietStore


class PersonalFoodInput(DietModel):
    name: ShortText
    brand: ShortText | None = None
    food_state: FoodState
    basis: Literal["per_100g", "per_serving"]
    serving_grams: Grams | None = None
    nutrients: dict[Nutrient, NutrientRange | None]
    source: Literal["manual_label", "user_report"]
    source_note: Annotated[str, Field(min_length=1, max_length=2000)]
    media_id: UUID | None = None
    media_ids: Annotated[list[UUID], Field(max_length=4)] = Field(default_factory=list)
    allergens: list[AllergenFact] = Field(default_factory=list)

    @model_validator(mode="after")
    def explicit_fields(self) -> Self:
        if set(self.nutrients) != set(NUTRIENTS):
            raise ValueError("All six nutrients must be explicitly known or unknown.")
        if not self.source_note.strip():
            raise ValueError("Food evidence needs a source description.")
        return self


class PersonalFoodMutation(DietModel):
    action: Literal["create", "update", "delete", "restore"]
    food_id: UUID | None = None
    base_version: Annotated[int, Field(strict=True, ge=1)] | None = None
    data: PersonalFoodInput | None = None
    label_draft_id: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    label_draft_version: Annotated[int, Field(strict=True, ge=1)] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    label_reviewed: bool = Field(default=False, exclude_if=lambda value: not value)

    @model_validator(mode="after")
    def action_fields(self) -> Self:
        if self.action == "create":
            if self.food_id is not None or self.base_version is not None or self.data is None:
                raise ValueError("Create needs food data and no existing resource reference.")
        elif self.food_id is None or self.base_version is None:
            raise ValueError("Existing food changes require ID and base version.")
        if (self.action in ("create", "update")) != (self.data is not None):
            raise ValueError("Only create and update accept food data.")
        if any((self.label_draft_id, self.label_draft_version, self.label_reviewed)) and not (
            self.action == "create"
            and self.label_draft_id is not None
            and self.label_draft_version is not None
            and self.label_reviewed
            and self.data is not None
            and self.data.source == "manual_label"
        ):
            raise ValueError("Label conversion requires an explicitly reviewed draft version.")
        return self


class PersonalFoodService:
    def __init__(
        self,
        store: DietStore,
        *,
        default_timezone: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store, self.default_timezone = store, default_timezone
        self.clock = clock or (lambda: datetime.now(UTC))

    async def prepare(
        self,
        user_id: int,
        request: PersonalFoodMutation,
        *,
        idempotency_key: str,
    ) -> DietChangePreview:
        async with self.store.transaction(user_id, default_timezone=self.default_timezone) as tx:
            request_hash = content_hash(request)
            previous = await tx.get_receipt("prepare_personal_food", idempotency_key)
            if previous:
                if previous.request_hash != request_hash:
                    raise DietError("IDEMPOTENCY_CONFLICT", "Request key has different content.")
                preview = await tx.get_preview(previous.result_refs[0].uuid)
                if preview is None:
                    raise DietError("RESOURCE_PURGED", "Food preview is no longer available.")
                return preview
            before = await tx.get_personal_food(request.food_id) if request.food_id else None
            if request.action != "create":
                if before is None:
                    raise DietError("NOT_FOUND", "Personal food not found.")
                if before.version != request.base_version:
                    raise DietError("VERSION_CONFLICT", "Personal food has changed.")
            now = self.clock()
            label_draft = None
            if request.label_draft_id is not None:
                label_draft = await tx.get_draft(request.label_draft_id)
                if label_draft is None or label_draft.label_suggestion is None:
                    raise DietError("NOT_FOUND", "Label draft not found.")
                if label_draft.version != request.label_draft_version:
                    raise DietError("VERSION_CONFLICT", "Label proposal has changed.")
                if (
                    label_draft.state not in ("needs_input", "ready")
                    or now >= label_draft.expires_at
                ):
                    raise DietError("PREVIEW_EXPIRED", "Label proposal is no longer editable.")
                assert request.data is not None
                supplied_media = set(request.data.media_ids) | (
                    {request.data.media_id} if request.data.media_id else set()
                )
                if supplied_media != set(label_draft.media_ids):
                    raise DietError("INVALID_FIELDS", "Label conversion must retain its images.")
            food_id = before.id if before else uuid4()
            version = before.version + 1 if before else 1
            if request.data is not None:
                if before is not None and before.deleted_at is not None:
                    raise DietError("INVALID_RECORD_STATE", "Restore this food before editing it.")
                data = request.data
                for media_id in set(data.media_ids) | ({data.media_id} if data.media_id else set()):
                    asset = await tx.get_media(media_id)
                    if asset is None or asset.kind != "label_image" or asset.state != "ready":
                        raise DietError("NOT_FOUND", "Label image not found.")
                    if (
                        asset.expires_at is not None
                        and asset.expires_at <= now
                        and not (await tx.media_referenced(asset.id))
                    ):
                        raise DietError("RESOURCE_EXPIRED", "Label image expired.")
                    if data.source != "manual_label":
                        raise DietError("INVALID_FIELDS", "Only label evidence accepts an image.")
                if before is not None and data == self.input_from_food(before):
                    raise DietError("NO_CHANGE", "The food has no changes.")
                evidence_id = f"personal:{food_id}:{version}"
                evidence = FoodEvidenceSnapshot(
                    evidence_id=evidence_id,
                    source_kind=("label" if data.source == "manual_label" else "user_report"),
                    source_ref=f"personal-food:{food_id}",
                    source_version=str(version),
                    retrieved_at=now,
                    food_state=data.food_state,
                    basis_quantity=data.basis,
                    nutrients=NutritionSnapshot(
                        **{
                            name: NutrientSnapshot(
                                value=data.nutrients[name],
                                basis="label_reported"
                                if data.source == "manual_label"
                                else "user_reported",
                                evidence_ids=[evidence_id],
                            )
                            if data.nutrients[name] is not None
                            else NutrientSnapshot(
                                missing_reasons=["not_reported"],
                            )
                            for name in NUTRIENTS
                        }
                    ),
                    original_excerpt=data.source_note,
                    calculation_note=(
                        "Explicitly supplied food composition; not independently verified."
                    ),
                    serving_evidence_id=f"serving:{food_id}:{version}",
                    serving_grams=data.serving_grams,
                    media_id=data.media_id,
                    media_ids=data.media_ids,
                    model_name=label_draft.label_model_name
                    if label_draft
                    else (before.evidence.model_name if before else None),
                    prompt_version=label_draft.label_prompt_version
                    if label_draft
                    else (before.evidence.prompt_version if before else None),
                )
                after = PersonalFood(
                    id=food_id,
                    user_id=user_id,
                    version=version,
                    name=data.name,
                    brand=data.brand,
                    evidence=evidence,
                    allergens=data.allergens,
                    created_at=before.created_at if before else now,
                    updated_at=now,
                )
            else:
                assert before is not None
                if request.action == "delete" and before.deleted_at is not None:
                    raise DietError("NO_CHANGE", "Personal food is already deleted.")
                if request.action == "restore":
                    self._restorable(before, now)
                evidence = before.evidence.model_copy(update={"source_version": str(version)})
                after = PersonalFood.model_validate(
                    {
                        **before.model_dump(),
                        "version": version,
                        "updated_at": now,
                        "evidence": evidence,
                        "deleted_at": now if request.action == "delete" else None,
                    }
                )
            content = PersonalFoodChangeContent(
                operation={
                    "create": "personal_food_create",
                    "update": "personal_food_update",
                    "delete": "personal_food_delete",
                    "restore": "personal_food_restore",
                }[request.action],
                before=before,
                after=after,
                base_refs=[ResourceRef(kind="personal_food", id=before.id, version=before.version)]
                if before
                else [],
            )
            if label_draft:
                content.base_refs.append(
                    ResourceRef(kind="draft", id=label_draft.id, version=label_draft.version)
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
                    operation="prepare_personal_food",
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    result_status="prepared",
                    result_refs=[
                        ResourceRef(kind="preview", id=preview.id, version=preview.version)
                    ],
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
            if not isinstance(preview.content, PersonalFoodChangeContent):
                raise DietError("INVALID_PREVIEW_STATE", "Expected personal-food preview.")
            content = preview.content
            operation, key = f"confirm:{content.operation}", f"{preview_id}:{version}"
            previous = await tx.get_receipt(operation, key)
            if previous:
                if not hmac.compare_digest(previous.request_hash, approved_hash):
                    raise DietError("IDEMPOTENCY_CONFLICT", "Approved content is different.")
                current = await tx.get_personal_food(content.after.id)
                return previous.model_copy(
                    update={
                        "result_status": "purged"
                        if current is None
                        else (
                            "deleted" if current.deleted_at is not None else previous.result_status
                        ),
                    }
                )
            if preview.version != version or not hmac.compare_digest(
                preview.content_hash, approved_hash
            ):
                raise DietError("VERSION_CONFLICT", "Preview has changed.")
            now = self.clock()
            if now >= preview.expires_at:
                raise DietError("PREVIEW_EXPIRED", "Preview has expired.")
            if preview.state != "ready":
                raise DietError("INVALID_PREVIEW_STATE", "Preview is not ready.")
            if content.operation in ("personal_food_create", "personal_food_update"):
                evidence = content.after.evidence
                for media_id in set(evidence.media_ids) | (
                    {evidence.media_id} if evidence.media_id else set()
                ):
                    asset = await tx.get_media(media_id)
                    if asset is None or asset.state != "ready" or asset.kind != "label_image":
                        raise DietError("SOURCE_CHANGED", "Label image is no longer available.")
            if content.before:
                current = await tx.get_personal_food(content.before.id)
                if current is None:
                    raise DietError("NOT_FOUND", "Personal food not found.")
                if current.version != content.before.version:
                    raise DietError("VERSION_CONFLICT", "Personal food has changed.")
                if content.operation == "personal_food_restore":
                    self._restorable(current, now)
            for reference in content.base_refs:
                if reference.kind != "draft":
                    continue
                draft = await tx.get_draft(reference.uuid)
                if draft is None:
                    raise DietError("RESOURCE_PURGED", "Label proposal is no longer available.")
                if draft.version != reference.version:
                    raise DietError("VERSION_CONFLICT", "Label proposal has changed or was used.")
                if draft.state not in ("needs_input", "ready") or now >= draft.expires_at:
                    raise DietError("PREVIEW_EXPIRED", "Label proposal is no longer editable.")
                await tx.save_draft(
                    draft.model_copy(
                        update={
                            "state": "discarded",
                            "version": draft.version + 1,
                            "updated_at": now,
                        }
                    )
                )
            receipt = OperationReceipt(
                id=uuid4(),
                user_id=user_id,
                operation=operation,
                idempotency_key=key,
                request_hash=approved_hash,
                preview_id=preview.id,
                preview_version=version,
                result_refs=[
                    ResourceRef(
                        kind="personal_food", id=content.after.id, version=content.after.version
                    )
                ],
                result_status="committed",
                created_at=now,
            )
            await tx.save_personal_food(content.after)
            await tx.add_revision(content.before, content.after, receipt)
            await tx.save_receipt(receipt)
            await tx.save_preview(
                DietChangePreview.model_validate(
                    {
                        **preview.model_dump(),
                        "state": "committed",
                        "updated_at": now,
                        "receipt_id": receipt.id,
                    }
                )
            )
            await tx.advance_epoch()
            return receipt

    @staticmethod
    def input_from_food(food: PersonalFood) -> PersonalFoodInput:
        basis = food.evidence.basis_quantity
        assert basis in ("per_100g", "per_serving")
        return PersonalFoodInput(
            name=food.name,
            brand=food.brand,
            food_state=food.evidence.food_state,
            basis=basis,
            serving_grams=food.evidence.serving_grams,
            nutrients={name: food.evidence.nutrients.nutrient(name).value for name in NUTRIENTS},
            source="manual_label" if food.evidence.source_kind == "label" else "user_report",
            source_note=food.evidence.original_excerpt,
            allergens=food.allergens,
            media_id=food.evidence.media_id,
            media_ids=food.evidence.media_ids,
        )

    @staticmethod
    def _restorable(food: PersonalFood, now: datetime) -> None:
        if food.deleted_at is None:
            raise DietError("NO_CHANGE", "Personal food is not deleted.")
        if now >= food.deleted_at + TRASH_LIFETIME:
            raise DietError("RESOURCE_EXPIRED", "Personal food restore window has expired.")
