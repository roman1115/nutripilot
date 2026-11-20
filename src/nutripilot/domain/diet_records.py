"""Versioned diet resources and their confirmation state transitions."""

from __future__ import annotations

import hmac
from datetime import date, timedelta
from typing import Annotated, Literal, Self
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AwareDatetime,
    Field,
    ValidationInfo,
    ValidatorFunctionWrapHandler,
    field_validator,
    model_validator,
)

from nutripilot.domain.diet import (
    CALCULATION_VERSION,
    DRAFT_LIFETIME,
    TRASH_LIFETIME,
    ClientKey,
    DayStatus,
    DietError,
    DietModel,
    DraftEntry,
    FieldEvidence,
    FoodEvidenceSnapshot,
    FoodRef,
    FoodState,
    Grams,
    MealType,
    Note,
    Nutrient,
    NutritionGoals,
    NutritionSnapshot,
    Quantity,
    ShortText,
    SubmissionAnchor,
    TemporalFields,
    Version,
    calculate_nutrition,
    content_hash,
    resolve_occurred_at,
)
from nutripilot.domain.diet_images import ImageEstimation
from nutripilot.domain.diet_labels import ParsedLabel


class ResourceRef(DietModel):
    kind: Literal[
        "log",
        "plan",
        "planned_meal",
        "target",
        "personal_food",
        "draft",
        "preview",
        "day_status",
        "media",
        "job",
    ]
    id: UUID | ShortText
    version: Version

    @field_validator("id", mode="wrap")
    @classmethod
    def normalize_uuid(
        cls,
        value: object,
        handler: ValidatorFunctionWrapHandler,
        info: ValidationInfo,
    ) -> UUID | str:
        if info.data.get("kind") == "plan":
            if not isinstance(value, (UUID, str)) or not 1 <= len(str(value)) <= 120:
                raise ValueError("Invalid plan ID.")
            return str(value)
        if info.data.get("kind") == "day_status":
            if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                raise ValueError("A day declaration reference must use its ISO local date.")
            return value
        parsed: UUID | str = handler(value)
        return UUID(parsed) if isinstance(parsed, str) else parsed

    @property
    def uuid(self) -> UUID:
        if not isinstance(self.id, UUID):
            raise ValueError("This resource uses a legacy plan ID.")
        return self.id

    @model_validator(mode="after")
    def legacy_plan_only(self) -> Self:
        if isinstance(self.id, str) and self.kind not in ("plan", "day_status"):
            try:
                UUID(self.id)
            except ValueError as exc:
                raise ValueError("Only legacy plan IDs may be non-UUID strings.") from exc
        return self


class FoodHit(DietModel):
    food_ref: FoodRef
    name: str
    food_state: FoodState
    basis: Literal["per_100g", "per_serving", "recipe_batch"]
    source_ref: str
    available_nutrients: list[Nutrient]
    serving_evidence_id: str | None


class DietItem(DietModel):
    id: UUID
    version: Version = 1
    client_item_key: ClientKey
    position: Annotated[int, Field(strict=True, ge=0)]
    food_ref: FoodRef
    name_snapshot: ShortText
    quantity: Quantity
    evidence_snapshot: FoodEvidenceSnapshot | None
    nutrition_snapshot: NutritionSnapshot
    calculation_version: str = CALCULATION_VERSION
    field_provenance: list[FieldEvidence] = Field(default_factory=list)
    source_planned_item_id: UUID | None = None
    legacy_food_id: Annotated[int, Field(strict=True, gt=0)] | None = None
    note: Note | None = None

    @model_validator(mode="after")
    def calculation_matches(self) -> Self:
        if self.calculation_version != CALCULATION_VERSION:
            raise ValueError("Unsupported nutrition calculation version.")
        if self.nutrition_snapshot != calculate_nutrition(self.quantity, self.evidence_snapshot):
            raise ValueError("Item nutrition must match its frozen quantity and evidence.")
        return self


class VersionedDietResource(DietModel):
    id: UUID
    user_id: Annotated[int, Field(strict=True, gt=0)]
    version: Version = 1
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def timestamps_ordered(self) -> Self:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at.")
        return self


class DietLog(VersionedDietResource):
    temporal: TemporalFields
    timezone: ShortText
    occurred_at: AwareDatetime | None
    channel: Literal["manual", "text", "image", "chat", "plan"]
    items: Annotated[list[DietItem], Field(min_length=1, max_length=50)]
    source_planned_meal_id: UUID | None = None
    source_event_kind: Literal["default_plan", "explicit_repeat", "copy"] | None = None
    source_event_key: str | None = None
    deleted_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def valid_record(self) -> Self:
        if self.temporal.local_date is None or self.temporal.meal_type is None:
            raise ValueError("An actual record requires a local date and meal type.")
        if self.occurred_at != resolve_occurred_at(self.temporal, self.timezone):
            raise ValueError("occurred_at must match the original local time and timezone.")
        ids = [item.id for item in self.items]
        keys = [item.client_item_key for item in self.items]
        positions = [item.position for item in self.items]
        if (
            len(set(ids)) != len(ids)
            or len(set(positions)) != len(positions)
            or len(set(keys)) != len(keys)
        ):
            raise ValueError("Diet item identities and positions must be unique.")
        if self.source_event_kind == "default_plan" and self.source_planned_meal_id is None:
            raise ValueError("Default plan events require a planned meal.")
        return self

    def soft_deleted(self, now: AwareDatetime) -> DietLog:
        if self.deleted_at is not None:
            raise DietError("NO_CHANGE", "Record is already deleted.")
        return DietLog.model_validate(
            {
                **self.model_dump(),
                "deleted_at": now,
                "updated_at": now,
                "version": self.version + 1,
            }
        )

    def restored(self, now: AwareDatetime) -> DietLog:
        if self.deleted_at is None:
            raise DietError("NO_CHANGE", "Record is not deleted.")
        if now >= self.deleted_at + TRASH_LIFETIME:
            raise DietError("RESOURCE_EXPIRED", "The seven-day restore window has expired.")
        return DietLog.model_validate(
            {
                **self.model_dump(),
                "deleted_at": None,
                "updated_at": now,
                "version": self.version + 1,
            }
        )


class PlannedMeal(VersionedDietResource):
    plan_id: str
    local_date: date
    meal_type: MealType
    items: Annotated[list[DietItem], Field(min_length=1, max_length=50)]
    note: Note | None = None
    intake_generation: Annotated[int, Field(strict=True, ge=1)] = 1
    legacy_item_refs: dict[str, UUID] = Field(default_factory=dict)
    deleted_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def unique_items(self) -> Self:
        for values in (
            [item.id for item in self.items],
            [item.client_item_key for item in self.items],
            [item.position for item in self.items],
        ):
            if len(set(values)) != len(values):
                raise ValueError("Planned item identities, keys and positions must be unique.")
        return self


class DietPlan(DietModel):
    id: ShortText
    user_id: Annotated[int, Field(strict=True, gt=0)]
    version: Version = 1
    title: ShortText
    start_date: date
    created_at: AwareDatetime
    updated_at: AwareDatetime
    deleted_at: AwareDatetime | None = None
    migrated_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def ordered_timestamps(self) -> Self:
        if self.updated_at < self.created_at:
            raise ValueError("Plan update cannot precede creation.")
        return self


class AllergenFact(DietModel):
    name: ShortText
    state: Literal["known_present", "known_absent", "unknown"]
    evidence_ref: ShortText | None
    explanation: Note

    @model_validator(mode="after")
    def evidence_required(self) -> Self:
        if self.state != "unknown" and not self.evidence_ref:
            raise ValueError("Known allergen facts require evidence.")
        return self


class PersonalFood(VersionedDietResource):
    name: ShortText
    brand: ShortText | None = None
    evidence: FoodEvidenceSnapshot
    allergens: list[AllergenFact] = Field(default_factory=list)
    deleted_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def personal_evidence(self) -> Self:
        if self.evidence.source_kind not in ("label", "user_report"):
            raise ValueError("Personal foods require a label or explicit user report.")
        if self.evidence.basis_quantity not in ("per_100g", "per_serving"):
            raise ValueError("Personal foods use per-100g or per-serving data.")
        if (
            self.evidence.source_kind == "label"
            and self.evidence.media_id is None
            and not self.evidence.original_excerpt.strip()
        ):
            raise ValueError("Label evidence requires an owned image or manual label notes.")
        if self.evidence.source_version != str(self.version):
            raise ValueError("Personal food evidence must use its resource version.")
        if len({fact.name.casefold() for fact in self.allergens}) != len(self.allergens):
            raise ValueError("Do not duplicate allergen facts.")
        return self


class ServingEvidence(DietModel):
    id: str | None
    grams: Grams | None


class NutritionTarget(VersionedDietResource):
    effective_date: date
    goals: NutritionGoals
    source: Literal["user_form", "user_message", "legacy_profile"]
    source_ref: str


class TargetPeriod(DietModel):
    target: NutritionTarget
    end_date_exclusive: date | None


def target_periods(targets: list[NutritionTarget]) -> list[TargetPeriod]:
    ordered = sorted(targets, key=lambda target: target.effective_date)
    dates = [target.effective_date for target in ordered]
    if len(set(dates)) != len(dates):
        raise DietError("TARGET_DATE_CONFLICT", "Two goals cannot start on the same local date.")
    if len({target.user_id for target in targets}) > 1:
        raise DietError("TARGET_OWNER_CONFLICT", "Target timelines must belong to one user.")
    return [
        TargetPeriod(
            target=target,
            end_date_exclusive=ordered[index + 1].effective_date
            if index + 1 < len(ordered)
            else None,
        )
        for index, target in enumerate(ordered)
    ]


def target_on(targets: list[NutritionTarget], local_date: date) -> NutritionTarget | None:
    periods = target_periods(targets)
    return next(
        (
            period.target
            for period in reversed(periods)
            if period.target.effective_date <= local_date
        ),
        None,
    )


class PreviewContent(DietModel):
    operation: Literal["log_create"]
    entry: DraftEntry
    items: list[DietItem]
    anchor: SubmissionAnchor
    field_locks: list[str] = Field(default_factory=list)
    source_refs: list[ResourceRef] = Field(default_factory=list)
    source_event_kind: Literal["default_plan", "explicit_repeat", "copy"] | None = None
    source_event_key: str | None = None
    calculation_version: str = CALCULATION_VERSION

    @model_validator(mode="after")
    def resolved_items_match(self) -> Self:
        proposed = {item.client_item_key: item for item in self.entry.items}
        if len(self.items) != len(proposed):
            raise ValueError("Resolved items must match proposed items.")
        keys = [item.client_item_key for item in self.items]
        if len(set(keys)) != len(keys) or set(keys) != set(proposed):
            raise ValueError("Resolved item keys must match proposed items.")
        if len({item.id for item in self.items}) != len(self.items) or len(
            {item.position for item in self.items}
        ) != len(self.items):
            raise ValueError("Resolved item identities and positions must be unique.")
        for item in self.items:
            value = proposed[item.client_item_key]
            if (
                value.food != item.food_ref
                or value.quantity != item.quantity
                or value.note != item.note
            ):
                raise ValueError("Resolved item content must match the approved proposal.")
        return self


class IntakeDraft(VersionedDietResource):
    thread_id: str | None = None
    source_turn_id: str | None = None
    channel: Literal["manual", "text", "image", "chat", "plan"]
    state: Literal["needs_input", "ready", "committed", "discarded", "expired"]
    content: PreviewContent
    evidence: list[FieldEvidence]
    field_locks_version: Version = 1
    preview_hash: str
    expires_at: AwareDatetime
    receipt_id: UUID | None = None
    source_text: Annotated[str, Field(max_length=4000)] | None = None
    media_ids: Annotated[list[UUID], Field(max_length=4)] = Field(default_factory=list)
    job_id: UUID | None = None
    parse_suggestion: DraftEntry | None = None
    parse_candidates: Annotated[list[DraftEntry], Field(max_length=10)] = Field(
        default_factory=list
    )
    parse_message: Annotated[str, Field(max_length=1000)] = ""
    label_suggestion: ParsedLabel | None = None
    label_model_name: str | None = None
    label_prompt_version: str | None = None
    image_estimation: ImageEstimation | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.expires_at != self.created_at + DRAFT_LIFETIME:
            raise ValueError("Draft lifetime is fixed at 24 hours from creation.")
        if not hmac.compare_digest(self.preview_hash, content_hash(self.content)):
            raise ValueError("Preview hash must match the frozen content.")
        if (self.state == "committed") != (self.receipt_id is not None):
            raise ValueError("Committed drafts require a receipt, other drafts must not have one.")
        if self.state == "ready" and self.content.entry.blocking_fields(
            today=self.content.anchor.anchor_local_date
        ):
            raise ValueError("A ready draft must not have blocking fields.")
        return self

    def assert_confirmable(
        self,
        *,
        user_id: int,
        version: int,
        approved_hash: str,
        now: AwareDatetime,
    ) -> None:
        if self.user_id != user_id:
            raise DietError("NOT_FOUND", "Draft not found.")
        if self.version != version or not hmac.compare_digest(self.preview_hash, approved_hash):
            raise DietError("VERSION_CONFLICT", "The draft has changed; review it again.")
        if now >= self.expires_at:
            raise DietError("PREVIEW_EXPIRED", "The preview has expired.")
        if self.state != "ready":
            raise DietError("INVALID_PREVIEW_STATE", "The preview is not ready for confirmation.")

    def discarded(self, now: AwareDatetime) -> IntakeDraft:
        if self.state == "committed":
            raise DietError("INVALID_PREVIEW_STATE", "A saved record requires a separate change.")
        if now >= self.expires_at:
            raise DietError("PREVIEW_EXPIRED", "The preview has expired.")
        if self.state == "discarded":
            return self
        return IntakeDraft.model_validate(
            {
                **self.model_dump(),
                "state": "discarded",
                "version": self.version + 1,
                "updated_at": now,
            }
        )


class OperationReceipt(DietModel):
    id: UUID
    user_id: Annotated[int, Field(strict=True, gt=0)]
    operation: ShortText
    idempotency_key: Annotated[str, Field(min_length=1, max_length=100)]
    request_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    preview_id: UUID | None = None
    preview_version: Version | None = None
    result_refs: list[ResourceRef]
    result_status: Literal["prepared", "committed", "discarded", "deleted", "purged"]
    created_at: AwareDatetime


class DietDayDeclaration(DietModel):
    local_date: date
    status: DayStatus
    version: Version


DietRevisionResource = (
    DietLog | NutritionTarget | PlannedMeal | DietPlan | PersonalFood | DietDayDeclaration
)


class DietUserState(DietModel):
    user_id: Annotated[int, Field(strict=True, gt=0)]
    timezone: ShortText
    diet_epoch: Annotated[int, Field(strict=True, ge=0)] = 0
    target_epoch: Annotated[int, Field(strict=True, ge=0)] = 0
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def valid_timezone(self) -> Self:
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Expected an IANA timezone.") from exc
        return self


class ChangeContent(DietModel):
    operation: Literal[
        "log_correct",
        "log_delete",
        "log_restore",
        "target_set",
        "target_correct",
        "target_stop",
    ]
    before: DietLog | NutritionTarget | None
    after: DietLog | NutritionTarget
    base_refs: list[ResourceRef]
    base_target_epoch: Annotated[int, Field(strict=True, ge=0)] | None = None
    calculation_version: str = CALCULATION_VERSION

    @model_validator(mode="after")
    def same_resource(self) -> Self:
        if self.before is not None:
            if type(self.before) is not type(self.after):
                raise ValueError("Changes must keep their resource type.")
            if self.before.id != self.after.id or self.before.user_id != self.after.user_id:
                raise ValueError("Changes must keep their resource identity and owner.")
            if self.after.version != self.before.version + 1:
                raise ValueError("Changes must advance the resource version once.")
        if self.operation.startswith("log_") != isinstance(self.after, DietLog):
            raise ValueError("Operation does not match its resource type.")
        if isinstance(self.after, NutritionTarget) and self.base_target_epoch is None:
            raise ValueError("Target previews require a timeline epoch.")
        return self


class PlanChangeContent(DietModel):
    operation: Literal[
        "plan_create",
        "plan_update",
        "plan_delete_meal",
        "plan_copy_meal",
        "plan_copy_week",
        "plan_archive",
        "plan_restore",
    ]
    before_plan: DietPlan | None
    after_plan: DietPlan
    before_meals: list[PlannedMeal]
    after_meals: Annotated[list[PlannedMeal], Field(max_length=28)]
    base_refs: list[ResourceRef]
    calculation_version: str = CALCULATION_VERSION

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.before_plan is not None and (
            self.after_plan.id != self.before_plan.id
            or self.after_plan.user_id != self.before_plan.user_id
            or self.after_plan.version != self.before_plan.version + 1
        ):
            raise ValueError("A plan change must keep identity and advance its version once.")
        ids = [meal.id for meal in self.after_meals]
        slots = [
            (meal.local_date, meal.meal_type)
            for meal in self.after_meals
            if meal.deleted_at is None
        ]
        if len(set(ids)) != len(ids) or len(set(slots)) != len(slots):
            raise ValueError("Plan meals and active slots must be unique.")
        for meal in [*self.before_meals, *self.after_meals]:
            if meal.user_id != self.after_plan.user_id or meal.plan_id != self.after_plan.id:
                raise ValueError("Plan changes cannot cross owners or parents.")
        if any(
            not self.after_plan.start_date
            <= meal.local_date
            < self.after_plan.start_date + timedelta(days=7)
            for meal in self.after_meals
        ):
            raise ValueError("Plan meal dates must fall within the plan's seven days.")
        return self


class PersonalFoodChangeContent(DietModel):
    operation: Literal[
        "personal_food_create",
        "personal_food_update",
        "personal_food_delete",
        "personal_food_restore",
    ]
    before: PersonalFood | None
    after: PersonalFood
    base_refs: list[ResourceRef]
    calculation_version: str = CALCULATION_VERSION

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.before is not None and (
            self.before.id != self.after.id
            or self.before.user_id != self.after.user_id
            or self.after.version != self.before.version + 1
        ):
            raise ValueError("A food change must preserve identity and advance its version once.")
        return self


class DayStatusChangeContent(DietModel):
    operation: Literal["day_status_set"]
    before: DietDayDeclaration
    after: DietDayDeclaration
    base_refs: list[ResourceRef]
    base_diet_epoch: Annotated[int, Field(strict=True, ge=0)]
    owner: Annotated[int, Field(strict=True, gt=0)]
    calculation_version: str = CALCULATION_VERSION

    @model_validator(mode="after")
    def valid_change(self) -> Self:
        if self.before.local_date != self.after.local_date:
            raise ValueError("Day declarations cannot move dates.")
        if self.after.version != self.before.version + 1:
            raise ValueError("Day changes must advance their version once.")
        return self


class PurgeChangeContent(DietModel):
    operation: Literal["purge"] = "purge"
    owner: int
    base_diet_epoch: int
    refs: Annotated[list[ResourceRef], Field(min_length=1, max_length=10000)]
    related_thread_ids: list[str]
    impact_notices: list[str] = Field(default_factory=list, exclude_if=lambda value: not value)
    history_notice: str = (
        "原始聊天与记录独立，历史聊天可能仍有原文；此操作不擦除离线备份，请按运维留存策略销毁。"
    )
    calculation_version: str = CALCULATION_VERSION

    @property
    def base_refs(self) -> list[ResourceRef]:
        return self.refs


ChangePayload = Annotated[
    ChangeContent
    | PlanChangeContent
    | PersonalFoodChangeContent
    | DayStatusChangeContent
    | PurgeChangeContent,
    Field(discriminator="operation"),
]


class DietChangePreview(VersionedDietResource):
    thread_id: str | None = None
    source_turn_id: str | None = None
    recommendation_epoch: Annotated[int, Field(strict=True, ge=0)] | None = None
    recommendation_fingerprint: str | None = None
    recommendation_future: bool = False
    recommendation_basis_changed: bool = False
    recommendation_note: str | None = None
    content: ChangePayload
    state: Literal["ready", "committed", "discarded", "expired"] = "ready"
    content_hash: str
    expires_at: AwareDatetime
    receipt_id: UUID | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.expires_at != self.created_at + DRAFT_LIFETIME:
            raise ValueError("Preview lifetime is fixed at 24 hours.")
        if not hmac.compare_digest(self.content_hash, content_hash(self.content)):
            raise ValueError("Preview hash must match frozen changes.")
        if (self.state == "committed") != (self.receipt_id is not None):
            raise ValueError("Committed previews must have a receipt.")
        if isinstance(self.content, (DayStatusChangeContent, PurgeChangeContent)):
            owner = self.content.owner
        elif isinstance(self.content, PlanChangeContent):
            owner = self.content.after_plan.user_id
        else:
            owner = self.content.after.user_id
        if owner != self.user_id:
            raise ValueError("Preview resources must belong to the preview owner.")
        return self
