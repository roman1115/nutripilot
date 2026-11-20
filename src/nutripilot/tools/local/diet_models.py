"""Strict, discriminated inputs for the six diet queries and four draft tools."""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, Field, RootModel, StringConstraints, model_validator

from nutripilot.domain.diet import (
    ClientKey,
    DietModel,
    EvidenceList,
    FoodRef,
    FoodState,
    LocalDate,
    MealType,
    Note,
    Nutrient,
    NutritionGoals,
    ProposedEntry,
    ProposedItem,
    Quantity,
    ShortText,
    TemporalFields,
    Version,
)

Cursor = Annotated[str, StringConstraints(min_length=1, max_length=2048)]
Limit = Annotated[int, Field(strict=True, ge=1, le=50)]


class RangeScope(DietModel):
    kind: Literal["range"]
    start_date: LocalDate
    end_date: LocalDate

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.start_date > self.end_date:
            raise ValueError("start_date must not exceed end_date.")
        return self

    def check_limit(self, days: int) -> None:
        if (self.end_date - self.start_date).days + 1 > days:
            raise ValueError(f"Date range must not exceed {days} days.")


class IDScope(DietModel):
    kind: Literal["id"]
    id: UUID


Scope = Annotated[RangeScope | IDScope, Field(discriminator="kind")]


class SearchFoods(DietModel):
    nonnullable_optional_fields = frozenset({"food_state"})
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    food_kind: Literal["any", "ingredient", "recipe", "personal_food"] = "any"
    food_state: FoodState | None = None
    limit: Annotated[int, Field(strict=True, ge=1, le=20)] = 10


class GetRecommendationContext(DietModel):
    nonnullable_optional_fields = frozenset({"local_date", "meal_type"})
    local_date: LocalDate | None = None
    meal_type: MealType | None = None


class GetMealPlans(DietModel):
    nonnullable_optional_fields = frozenset({"meal_type", "cursor"})
    scope: Scope
    meal_type: MealType | None = None
    cursor: Cursor | None = None
    limit: Limit = 20

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if isinstance(self.scope, RangeScope):
            self.scope.check_limit(31)
        return self


class GetDietLogs(GetMealPlans):
    nonnullable_optional_fields = GetMealPlans.nonnullable_optional_fields | {"food_query"}
    food_query: ShortText | None = None
    include_deleted: Annotated[bool, Field(strict=True)] = False


class GetNutritionTargets(DietModel):
    scope: RangeScope

    @model_validator(mode="after")
    def bounded(self) -> Self:
        self.scope.check_limit(366)
        return self


class GetNutritionSummary(GetNutritionTargets):
    nonnullable_optional_fields = frozenset({"nutrients"})
    granularity: Literal["day", "week"]
    nutrients: Annotated[list[Nutrient], Field(min_length=1, max_length=6)] | None = None

    @model_validator(mode="after")
    def unique_nutrients(self) -> Self:
        if self.nutrients is not None and len(set(self.nutrients)) != len(self.nutrients):
            raise ValueError("Nutrient selection must not contain duplicates.")
        return self


class GetDietDeviation(GetNutritionTargets):
    nonnullable_optional_fields = frozenset({"plan_id"})
    plan_id: UUID | None = None


class ItemChanges(DietModel):
    nonnullable_optional_fields = frozenset({"food", "quantity"})
    food: FoodRef | None = None
    quantity: Quantity | None = None
    note: Note | None = None

    @model_validator(mode="after")
    def nonempty(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("An item patch must change at least one field.")
        if "food" in self.model_fields_set and self.food is None:
            raise ValueError("Food cannot be cleared.")
        if "quantity" in self.model_fields_set and self.quantity is None:
            raise ValueError("Use an explicit unknown quantity instead of null.")
        return self


class AddItem(DietModel):
    action: Literal["add"]
    item: ProposedItem


class UpdateItem(DietModel):
    action: Literal["update"]
    item_id: UUID
    changes: ItemChanges


class RemoveItem(DietModel):
    action: Literal["remove"]
    item_id: UUID


ItemEdit = Annotated[AddItem | UpdateItem | RemoveItem, Field(discriminator="action")]


class EntryPatch(DietModel):
    nonnullable_optional_fields = frozenset({"temporal", "item_edits"})
    temporal: TemporalFields | None = None
    item_edits: Annotated[list[ItemEdit], Field(min_length=1, max_length=50)] | None = None

    @model_validator(mode="after")
    def meaningful(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("An entry patch must change at least one field.")
        if "temporal" in self.model_fields_set and (
            self.temporal is None or not self.temporal.model_fields_set
        ):
            raise ValueError("Temporal patches cannot be empty.")
        if "item_edits" in self.model_fields_set and self.item_edits is None:
            raise ValueError("Item edits cannot be null.")
        if self.item_edits:
            ids = [edit.item_id for edit in self.item_edits if not isinstance(edit, AddItem)]
            keys = [
                edit.item.client_item_key for edit in self.item_edits if isinstance(edit, AddItem)
            ]
            if len(ids) != len(set(ids)) or len(keys) != len(set(keys)):
                raise ValueError("Do not edit the same item twice in one patch.")
        return self


class PlanSource(DietModel):
    kind: Literal["plan"]
    planned_meal_id: UUID
    base_version: Version
    record_again: Annotated[bool, Field(strict=True)]


class RecordCopySource(DietModel):
    kind: Literal["record_copy"]
    record_id: UUID
    base_version: Version


IntakeSource = Annotated[PlanSource | RecordCopySource, Field(discriminator="kind")]


class CreateIntake(DietModel):
    nonnullable_optional_fields = frozenset({"media_ids", "source"})
    action: Literal["create"]
    entry: ProposedEntry
    evidence: EvidenceList
    media_ids: Annotated[list[UUID], Field(max_length=4)] | None = None
    source: IntakeSource | None = None

    @model_validator(mode="after")
    def unique_media(self) -> Self:
        if self.media_ids and len(set(self.media_ids)) != len(self.media_ids):
            raise ValueError("Media references must be unique.")
        return self


class ReviseIntake(DietModel):
    action: Literal["revise"]
    draft_id: UUID
    base_version: Version
    patch: EntryPatch
    evidence: EvidenceList


class DiscardIntake(DietModel):
    action: Literal["discard"]
    draft_id: UUID
    base_version: Version
    evidence: EvidenceList


class PrepareIntakeDraft(
    RootModel[Annotated[CreateIntake | ReviseIntake | DiscardIntake, Field(discriminator="action")]]
):
    pass


class CorrectDietLog(DietModel):
    action: Literal["correct"]
    record_id: UUID
    base_version: Version
    patch: EntryPatch
    evidence: EvidenceList


class DeleteRestoreDietLog(DietModel):
    action: Literal["delete", "restore"]
    record_id: UUID
    base_version: Version
    evidence: EvidenceList


class PrepareDietLogChange(
    RootModel[Annotated[CorrectDietLog | DeleteRestoreDietLog, Field(discriminator="action")]]
):
    pass


class NewPlan(DietModel):
    kind: Literal["new"]
    title: ShortText
    start_date: LocalDate


class ExistingPlan(DietModel):
    kind: Literal["existing"]
    plan_id: UUID
    base_version: Version


class PlannedEntry(DietModel):
    client_entry_key: ClientKey
    entry: ProposedEntry


class CreateMealPlan(DietModel):
    action: Literal["create"]
    parent: Annotated[NewPlan | ExistingPlan, Field(discriminator="kind")]
    entries: Annotated[list[PlannedEntry], Field(min_length=1, max_length=28)]
    evidence: EvidenceList

    @model_validator(mode="after")
    def valid_batch(self) -> Self:
        keys = [entry.client_entry_key for entry in self.entries]
        if len(set(keys)) != len(keys):
            raise ValueError("client_entry_key must be unique.")
        if sum(len(entry.entry.items) for entry in self.entries) > 100:
            raise ValueError("A plan batch must not exceed 100 food items.")
        for planned in self.entries:
            entry = planned.entry
            if entry.actuality != "planned" or entry.subject != "self":
                raise ValueError("Plan entries must describe the user's planned meals.")
            if entry.temporal.local_date is None or entry.temporal.meal_type is None:
                raise ValueError("Plan entries require a local date and meal type.")
            if isinstance(self.parent, NewPlan) and not (
                self.parent.start_date
                <= entry.temporal.local_date
                < self.parent.start_date + timedelta(days=7)
            ):
                raise ValueError("Plan entries must fall within the new plan's seven days.")
        return self


class UpdateMealPlan(DietModel):
    action: Literal["update"]
    planned_meal_id: UUID
    base_version: Version
    patch: EntryPatch
    evidence: EvidenceList


class DeleteMealPlan(DietModel):
    action: Literal["delete"]
    planned_meal_id: UUID
    base_version: Version
    evidence: EvidenceList


class CopyMealPlan(DietModel):
    action: Literal["copy"]
    planned_meal_id: UUID
    base_version: Version
    target_date: LocalDate
    target_meal_type: MealType
    evidence: EvidenceList


class CopyPlanWeek(DietModel):
    """Page-only copy contract; the model tool remains the frozen ten-tool schema."""

    action: Literal["copy_week"]
    source_plan_id: ShortText
    base_version: Version
    parent: Annotated[NewPlan | ExistingPlan, Field(discriminator="kind")]
    target_start_date: LocalDate
    conflict_policy: Literal["reject", "append_items", "replace_meals"]
    evidence: EvidenceList

    @model_validator(mode="after")
    def week_start(self) -> Self:
        if self.target_start_date.weekday() != 0:
            raise ValueError("Week copies must start on Monday.")
        if isinstance(self.parent, NewPlan) and self.parent.start_date != self.target_start_date:
            raise ValueError("Destination plan and target week must have the same start.")
        return self


class PrepareMealPlanChange(
    RootModel[
        Annotated[
            CreateMealPlan | UpdateMealPlan | DeleteMealPlan | CopyMealPlan,
            Field(discriminator="action"),
        ]
    ]
):
    pass


class GoalsPatch(NutritionGoals):
    @model_validator(mode="after")
    def nonempty(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("A goal change must include at least one field.")
        return self


class SetTarget(DietModel):
    action: Literal["set"]
    effective_date: LocalDate
    goals: GoalsPatch
    evidence: EvidenceList


class CorrectTarget(DietModel):
    action: Literal["correct"]
    target_id: UUID
    base_version: Version
    effective_date: LocalDate
    goals: GoalsPatch
    evidence: EvidenceList


class StopTarget(DietModel):
    action: Literal["stop"]
    target_id: UUID
    base_version: Version
    effective_date: LocalDate
    evidence: EvidenceList


class PrepareTargetChange(
    RootModel[Annotated[SetTarget | CorrectTarget | StopTarget, Field(discriminator="action")]]
):
    pass


DIET_ARGUMENT_MODELS: dict[str, type[BaseModel]] = {
    "search_foods": SearchFoods,
    "get_meal_plans": GetMealPlans,
    "get_diet_logs": GetDietLogs,
    "get_nutrition_summary": GetNutritionSummary,
    "get_nutrition_targets": GetNutritionTargets,
    "get_diet_deviation": GetDietDeviation,
    "get_recommendation_context": GetRecommendationContext,
    "prepare_intake_draft": PrepareIntakeDraft,
    "prepare_diet_log_change": PrepareDietLogChange,
    "prepare_meal_plan_change": PrepareMealPlanChange,
    "prepare_target_change": PrepareTargetChange,
}
