"""Independent structured-output contracts for each Worker Agent."""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from nutripilot.domain.enums import Intent
from nutripilot.domain.models import IngredientGroup, IngredientTerm
from nutripilot.tools.models import ToolCall


class IntentSlots(BaseModel):
    """Only supported, type-checked values may enter the shared state."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    meal_type: Literal["breakfast", "lunch", "dinner", "snack", "late_snack"] | None = None
    plan_range: Literal["one_day", "one_week"] | None = None
    start_date: date | None = None
    food_name: str | None = Field(default=None, min_length=1, max_length=120)
    location: str | None = Field(default=None, max_length=100)
    tastes: list[str] = Field(default_factory=list)
    preferences: list[str] = Field(default_factory=list)
    cuisines: list[str] = Field(default_factory=list)
    health_goals: list[str] = Field(default_factory=list)
    dietary_tags: list[str] = Field(default_factory=list)
    allergens: list[str] = Field(default_factory=list)
    dislikes: list[str] = Field(default_factory=list)
    excluded_ingredients: list[str] = Field(default_factory=list)
    required_ingredients: list[IngredientTerm] = Field(default_factory=list, max_length=10)
    ingredient_any_groups: list[IngredientGroup] = Field(default_factory=list, max_length=10)
    ingredient_action: Literal["replace", "append", "keep", "clear"] | None = None
    max_calories_kcal: float | None = Field(default=None, gt=0)
    min_protein_g: float | None = Field(default=None, ge=0)
    max_price_cny: float | None = Field(default=None, ge=0)
    max_prep_minutes: int | None = Field(default=None, ge=0, le=1440)
    clear_constraints: list[
        Literal["max_calories_kcal", "min_protein_g", "max_price_cny", "max_prep_minutes"]
    ] = Field(default_factory=list, max_length=4)
    daily_calorie_target: float | None = Field(default=None, gt=0, le=10_000)
    daily_protein_target_g: float | None = Field(default=None, ge=0, le=1_000)


class AnswerBoundaryCheck(BaseModel):
    """Compact online fact/safety feedback; no sentence-level proof contract."""

    model_config = ConfigDict(extra="forbid")

    facts_match: bool = Field(strict=True)
    objects_match: bool = Field(strict=True)
    portion_basis_preserved: bool = Field(strict=True)
    cooking_conditions_preserved: bool = Field(strict=True)
    issues: list[str] = Field(max_length=8)


class AnswerTask(BaseModel):
    """Turn-local meaning; never merged into persistent dietary constraints."""

    model_config = ConfigDict(extra="forbid")

    objective: str = Field(default="", max_length=300)
    context: Literal["auto", "general", "meals"] = "auto"
    meal_ids: list[int] = Field(default_factory=list, max_length=3)
    recommendation_count: int | None = Field(default=None, ge=1, le=3)
    nutrient_preference: Literal["lower_fat", "higher_protein"] | None = None
    needs_nutrition: bool = False

    @field_validator("meal_ids", mode="before")
    @classmethod
    def empty_reference_is_not_an_object(cls, value: Any) -> Any:
        return [] if value is None else value


class IntentOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Intent
    confidence: float = Field(ge=0, le=1)
    extracted_slots: dict[str, Any] = Field(
        default_factory=dict, json_schema_extra=IntentSlots.model_json_schema()
    )
    missing_slots: list[str] = Field(default_factory=list)
    notes: str = ""
    answer_task: AnswerTask = Field(default_factory=AnswerTask)

    @model_validator(mode="before")
    @classmethod
    def separate_turn_metadata_from_dietary_slots(cls, value: Any) -> Any:
        if not isinstance(value, dict) or not isinstance(value.get("extracted_slots"), dict):
            return value
        slots = dict(value["extracted_slots"])
        task = dict(value.get("answer_task") or {})
        for key in AnswerTask.model_fields:
            if key in slots:
                if key in task and task[key] != slots[key]:
                    raise ValueError(f"Conflicting answer_task field: {key}")
                task[key] = slots.pop(key)
        return {**value, "extracted_slots": slots, "answer_task": task}

    @field_validator("extracted_slots")
    @classmethod
    def validate_slots(cls, value: dict[str, Any]) -> dict[str, Any]:
        return IntentSlots.model_validate(value).model_dump(
            mode="json", exclude_none=True, exclude_defaults=True
        )


class PlannerOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    complete: bool
    tool_calls: list[ToolCall] = Field(default_factory=list)
    plan_summary: str

    @model_validator(mode="after")
    def validate_available_tool_arguments(self, info: ValidationInfo) -> PlannerOutput:
        payload = (info.context or {}).get("payload", {})
        if "available_tools" not in payload:
            return self
        schemas = {
            item["function"]["name"]: item["function"]["parameters"]
            for item in payload["available_tools"]
        }
        for call in self.tool_calls:
            if call.name not in schemas:
                raise ValueError(f"工具{call.name}不在当前白名单；允许工具：{','.join(schemas)}")
            schema = schemas[call.name]
            properties = schema.get("properties", {})
            if schema.get("additionalProperties") is False:
                unknown = set(call.arguments) - set(properties)
                if unknown:
                    raise ValueError(
                        f"{call.name}不支持参数：{','.join(sorted(unknown))}；"
                        f"仅允许：{','.join(properties)}。删除未知参数，不自行扩展工具。"
                    )
        return self


class PlanSpec(BaseModel):
    """Deterministic scope used to build and validate a meal plan."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    plan_range: Literal["one_day", "one_week"] = "one_week"
    meal_type: Literal["breakfast", "lunch", "dinner", "snack"] | None = None
    start_date: date = Field(default_factory=date.today)
    daily_calorie_target: float | None = Field(default=None, gt=0, le=10_000)
    daily_protein_target_g: float | None = Field(default=None, ge=0, le=1_000)

    @property
    def day_count(self) -> int:
        return 1 if self.plan_range == "one_day" else 7

    @property
    def meal_types(self) -> tuple[str, ...]:
        if self.meal_type is not None:
            return (self.meal_type,)
        return ("breakfast", "lunch", "dinner")

    @classmethod
    def from_slots(
        cls,
        slots: dict[str, Any],
        *,
        start_date: date | None = None,
        profile: dict[str, Any] | None = None,
    ) -> PlanSpec:
        raw_range = str(slots.get("plan_range") or "one_week").casefold()
        plan_range = "one_day" if raw_range == "one_day" else "one_week"
        raw_meal_type = slots.get("meal_type")
        if raw_meal_type == "late_snack":
            raw_meal_type = "snack"
        meal_type = (
            raw_meal_type if raw_meal_type in {"breakfast", "lunch", "dinner", "snack"} else None
        )
        return cls(
            plan_range=plan_range,
            meal_type=meal_type,
            start_date=slots.get("start_date") or start_date or date.today(),
            daily_calorie_target=slots.get("daily_calorie_target")
            or (profile or {}).get("daily_calorie_target"),
            daily_protein_target_g=slots.get("daily_protein_target_g")
            if slots.get("daily_protein_target_g") is not None
            else (profile or {}).get("daily_protein_target_g"),
        )


class RecommendationChoice(BaseModel):
    model_config = ConfigDict(extra="ignore")

    meal_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=120)
    reason: str = Field(default="", max_length=300)


class NutritionCalculation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meal_id: int = Field(gt=0)
    reference_meal_id: int = Field(gt=0)
    nutrient: Literal["calories_kcal", "protein_g", "fat_g", "carbohydrates_g"]


class RecommendationDraft(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Literal["completed", "degraded"] = "completed"
    message: str = Field(min_length=1, max_length=6000)
    recommendations: list[RecommendationChoice] = Field(default_factory=list, max_length=3)
    source_ids: list[str] = Field(default_factory=list, max_length=12)
    calculations: list[NutritionCalculation] = Field(default_factory=list, max_length=6)


class RecommendationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response: RecommendationDraft


class AnswerEvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    record_id: str = Field(min_length=1, max_length=100)
    quote: str = Field(min_length=1, max_length=1200)


class AnswerConditionReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source: AnswerEvidenceReference
    answer_quote: str = Field(max_length=1200)


class AnswerObjectCount(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    quote: str = Field(min_length=1, max_length=300)
    scope: Literal["recommendations", "display"]
    meal_ids: list[int] = Field(default_factory=list, max_length=100)
    minimum: int = Field(ge=0, le=1000)
    maximum: int | None = Field(ge=0, le=1000)


class AnswerUnitReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    unit_id: str = Field(min_length=1, max_length=100)
    kind: Literal["source_fact", "general_advice", "ranking", "health_effect", "conversation"]
    has_object_count: bool
    object_counts: list[AnswerObjectCount] = Field(max_length=10)
    conditions: list[AnswerConditionReview] = Field(max_length=12)
    conditions_preserved: bool
    evidence: list[AnswerEvidenceReference] = Field(max_length=8)
    explanation: str = Field(min_length=1)
    supported: bool


class AnswerReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    checks: list[AnswerUnitReview] = Field(max_length=80)
    coherent: bool
    issues: list[str] = Field(default_factory=list, max_length=8)
    supported: bool

    @model_validator(mode="after")
    def validate_review_contract(self, info: ValidationInfo) -> AnswerReview:
        for check in self.checks:
            if check.has_object_count != bool(check.object_counts):
                raise ValueError("数量判断与object_counts不一致；有数量表述须列出核对对象。")
        # Rejected drafts go to revision; repairing their citations cannot approve them.
        if not self.supported or not self.coherent or self.issues or any(
            not check.supported or not check.conditions_preserved for check in self.checks
        ):
            return self
        payload = (info.context or {}).get("payload")
        if not payload:
            return self
        units = payload.get("review_units", {})
        records = payload.get("evidence_records", {})
        ids = [check.unit_id for check in self.checks]
        if len(set(ids)) != len(ids) or set(ids) != set(units):
            raise ValueError("checks必须恰好覆盖每个review_units编号，不得遗漏或重复。")
        for check in self.checks:
            for reference in [
                *check.evidence, *(condition.source for condition in check.conditions),
            ]:
                if reference.record_id not in records or not contains_evidence_quote(
                    records[reference.record_id], reference.quote,
                ):
                    raise ValueError(
                        f"引用不存在：record_id={reference.record_id}，"
                        f"quote={reference.quote[:180]!r}。"
                        f"允许编号={','.join(records)}。"
                        "quote只摘录实际原文值（如鸡胸肉），不要自行构造字段路径、"
                        "冒号或JSON。澄清/通用建议无需引用时用evidence=[]。"
                    )
        return self


def contains_evidence_quote(record: str, quote: str) -> bool:
    if not quote.strip():
        return False
    # Compact candidate JSON and its exact field paths identify the same source facts.
    expanded = []
    for line in record.splitlines():
        path, separator, text = line.partition(": ")
        if separator and re.fullmatch(r"data\.hits\[\d+\](?:\.[a-z_]+)?", path):
            try:
                expanded.extend(evidence_field_lines(json.loads(text), path))
            except json.JSONDecodeError:
                continue
    record = record + "\n" + "\n".join(expanded)
    cursor = 0
    for fragment in quote.splitlines():
        if not fragment.strip():
            continue
        position = record.find(fragment, cursor)
        if position < 0:
            path, separator, text = fragment.partition(": ")
            if not separator or not re.fullmatch(r"data\.hits\[\d+\]\.[a-z_]+", path):
                return False
            try:
                quoted = json.loads(text)
            except json.JSONDecodeError:
                return False
            offset = 0
            for source_line in record.splitlines(keepends=True):
                source_path, _, source_text = source_line.rstrip("\n").partition(": ")
                if offset >= cursor and source_path == path:
                    try:
                        if _json_evidence_subset(quoted, json.loads(source_text)):
                            position = offset
                            break
                    except json.JSONDecodeError:
                        pass
                offset += len(source_line)
            if position < 0:
                return False
            cursor = position + len(source_line.rstrip("\n"))
        else:
            cursor = position + len(fragment)
    return True


def _json_evidence_subset(quote: Any, source: Any) -> bool:
    if isinstance(quote, dict):
        return bool(quote) and isinstance(source, dict) and all(
            key in source and _json_evidence_subset(value, source[key])
            for key, value in quote.items()
        )
    if isinstance(quote, list):
        return isinstance(source, list) and len(quote) == len(source) and all(
            _json_evidence_subset(item, original)
            for item, original in zip(quote, source, strict=True)
        )
    return type(quote) is type(source) and quote == source


def evidence_field_lines(value: Any, path: str) -> list[str]:
    if isinstance(value, dict) and value:
        return [
            line for key, item in value.items()
            for line in evidence_field_lines(item, f"{path}.{key}")
        ]
    if isinstance(value, list) and value:
        return [
            line for index, item in enumerate(value)
            for line in evidence_field_lines(item, f"{path}[{index}]")
        ]
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return [f"{path}: {text}"]


class EvaluationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: float = Field(ge=0, le=1)
    reason: str
