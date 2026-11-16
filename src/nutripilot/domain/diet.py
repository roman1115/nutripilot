"""Diet quantities, immutable evidence, local dates and deterministic nutrition rules."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from typing import Annotated, Any, ClassVar, Literal, Self, cast
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    SerializerFunctionWrapHandler,
    StringConstraints,
    WithJsonSchema,
    model_serializer,
    model_validator,
)

CALCULATION_VERSION = "diet-1"
DRAFT_LIFETIME = timedelta(hours=24)
TRASH_LIFETIME = timedelta(days=7)
DECIMAL_PATTERN = r"^(0|[1-9][0-9]{0,8})(\.[0-9]{1,6})?$"
SIGNED_DECIMAL_PATTERN = r"^-?(0|[1-9][0-9]{0,8})(\.[0-9]{1,6})?$"
DECIMAL_STEP = Decimal("0.000001")
type Nutrient = Literal[
    "calories_kcal", "protein_g", "fat_g", "carbohydrates_g", "fiber_g", "sodium_mg"
]
NUTRIENTS: tuple[Nutrient, ...] = (
    "calories_kcal",
    "protein_g",
    "fat_g",
    "carbohydrates_g",
    "fiber_g",
    "sodium_mg",
)
type GoalNutrient = Literal["calories_kcal", "protein_g", "fat_g", "carbohydrates_g"]
GOAL_NUTRIENTS: tuple[GoalNutrient, ...] = (
    "calories_kcal",
    "protein_g",
    "fat_g",
    "carbohydrates_g",
)
type MealType = Literal["breakfast", "lunch", "dinner", "snack"]
type FoodState = Literal["raw", "cooked", "as_sold", "unknown"]
type DayStatus = Literal["open", "complete", "no_intake"]
type NutritionBasis = Literal[
    "catalog_value",
    "label_reported",
    "user_reported",
    "calculated_estimate",
    "model_estimate",
    "unknown",
]


def decimal_text(value: Decimal) -> str:
    if not value:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _decimal(value: object, *, signed: bool = False) -> Decimal:
    if not isinstance(value, (str, Decimal)):
        raise ValueError("Decimals must be strings, not JSON numbers.")
    pattern = SIGNED_DECIMAL_PATTERN if signed else DECIMAL_PATTERN
    text = decimal_text(value) if isinstance(value, Decimal) and value.is_finite() else str(value)
    if re.fullmatch(pattern, text) is None:
        raise ValueError("Expected a finite decimal with at most 9 integer and 6 fraction digits.")
    return Decimal(text)


def _unsigned_decimal(value: object) -> Decimal:
    return _decimal(value)


def _signed_decimal(value: object) -> Decimal:
    return _decimal(value, signed=True)


DecimalValue = Annotated[
    Decimal,
    BeforeValidator(_unsigned_decimal),
    PlainSerializer(decimal_text, return_type=str, when_used="json"),
    WithJsonSchema({"type": "string", "pattern": DECIMAL_PATTERN}),
]
SignedDecimal = Annotated[
    Decimal,
    BeforeValidator(_signed_decimal),
    PlainSerializer(decimal_text, return_type=str, when_used="json"),
    WithJsonSchema({"type": "string", "pattern": SIGNED_DECIMAL_PATTERN}),
]
PositiveDecimal = Annotated[DecimalValue, Field(gt=0)]
Grams = Annotated[PositiveDecimal, Field(le=100_000)]
Count = Annotated[PositiveDecimal, Field(le=1_000)]
Version = Annotated[int, Field(strict=True, ge=1)]
ClientKey = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_-]{1,32}$")]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Note = Annotated[str, StringConstraints(max_length=500)]


def _date(value: object) -> date:
    if type(value) is date:
        return value
    if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        raise ValueError("Expected a local date in YYYY-MM-DD format.")
    return date.fromisoformat(value)


def _time(value: object) -> time:
    if isinstance(value, time) and value.tzinfo is None and value.microsecond == 0:
        return value
    if (
        not isinstance(value, str)
        or re.fullmatch(r"([01][0-9]|2[0-3]):[0-5][0-9](:[0-5][0-9])?", value) is None
    ):
        raise ValueError("Expected a local time in HH:MM or HH:MM:SS format.")
    return time.fromisoformat(value)


LocalDate = Annotated[date, BeforeValidator(_date)]
LocalTime = Annotated[time, BeforeValidator(_time)]


def _diet_json_schema(schema: dict[str, Any], model: type[BaseModel]) -> None:
    for key in getattr(model, "nonnullable_optional_fields", ()):
        field = schema.get("properties", {}).get(key, {})
        choices = [value for value in field.get("anyOf", []) if value.get("type") != "null"]
        if len(choices) == 1:
            field.pop("anyOf", None)
            field.update(choices[0])
        elif choices:
            field["anyOf"] = choices
        if field.get("default") is None:
            field.pop("default", None)


class DietModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, json_schema_extra=_diet_json_schema
    )
    nonnullable_optional_fields: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode="before")
    @classmethod
    def omitted_is_not_null(cls, value: object) -> object:
        if isinstance(value, dict) and any(
            key in value and value[key] is None for key in cls.nonnullable_optional_fields
        ):
            raise ValueError("Omit optional fields instead of setting them to null.")
        return value

    @model_serializer(mode="wrap")
    def serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data = cast("dict[str, Any]", handler(self))
        for key in self.nonnullable_optional_fields:
            if data.get(key) is None:
                data.pop(key, None)
        return data


class DietError(ValueError):
    def __init__(self, code: str, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.field = field


class NutrientRange(DietModel):
    lower: DecimalValue
    upper: DecimalValue

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.lower > self.upper:
            raise ValueError("Range lower must not exceed upper.")
        return self

    @classmethod
    def exact(cls, value: Decimal) -> Self:
        return cls(lower=value, upper=value)

    def scaled(self, lower: Decimal, upper: Decimal | None = None) -> NutrientRange:
        if lower < 0 or (upper is not None and upper < lower):
            raise ValueError("Nutrition scale must be a nonnegative ordered interval.")
        # Outward rounding retains uncertainty when ratios have recurring decimals.
        with localcontext() as context:
            context.prec = 60
            return NutrientRange(
                lower=(self.lower * lower).quantize(DECIMAL_STEP, rounding=ROUND_FLOOR),
                upper=(self.upper * (upper if upper is not None else lower)).quantize(
                    DECIMAL_STEP, rounding=ROUND_CEILING
                ),
            )

    def divided(self, divisor: Decimal) -> NutrientRange:
        if divisor <= 0 or not divisor.is_finite():
            raise ValueError("A positive finite divisor is required.")
        with localcontext() as context:
            context.prec = 60
            return NutrientRange(
                lower=(self.lower / divisor).quantize(DECIMAL_STEP, rounding=ROUND_FLOOR),
                upper=(self.upper / divisor).quantize(DECIMAL_STEP, rounding=ROUND_CEILING),
            )


class DifferenceRange(DietModel):
    lower: SignedDecimal
    upper: SignedDecimal

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.lower > self.upper:
            raise ValueError("Difference lower must not exceed upper.")
        return self


def convert_label_range(
    value: NutrientRange,
    *,
    from_unit: Literal["kJ", "kcal", "g", "mg", "ml"],
    to_unit: Literal["kJ", "kcal", "g", "mg", "ml"],
    density_grams_per_ml: NutrientRange | None = None,
) -> NutrientRange:
    if from_unit == to_unit:
        return value
    if (from_unit, to_unit) == ("kJ", "kcal"):
        return value.divided(Decimal("4.184"))
    factors = {
        ("kcal", "kJ"): Decimal("4.184"),
        ("g", "mg"): Decimal(1000),
        ("mg", "g"): Decimal("0.001"),
    }
    if (factor := factors.get((from_unit, to_unit))) is not None:
        return value.scaled(factor)
    if from_unit == "ml" and to_unit == "g":
        if density_grams_per_ml is None or density_grams_per_ml.lower <= 0:
            raise DietError(
                "DENSITY_REQUIRED", "Volume conversion requires positive density evidence."
            )
        return value.scaled(density_grams_per_ml.lower, density_grams_per_ml.upper)
    raise DietError("UNSUPPORTED_UNIT", "These units cannot be converted.")


class UnknownFood(DietModel):
    kind: Literal["unknown"]
    name: ShortText


class IngredientFood(DietModel):
    kind: Literal["ingredient"]
    id: ShortText
    version: ShortText


class RecipeFood(DietModel):
    kind: Literal["recipe"]
    id: Annotated[int, Field(strict=True, ge=1000)]
    version: ShortText


class PersonalFoodRef(DietModel):
    kind: Literal["personal_food"]
    id: UUID
    version: Version


FoodRef = Annotated[
    UnknownFood | IngredientFood | RecipeFood | PersonalFoodRef, Field(discriminator="kind")
]


class UnknownQuantity(DietModel):
    kind: Literal["unknown"]
    description: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
    ]


class MassQuantity(DietModel):
    kind: Literal["mass"]
    grams: Grams
    food_state: FoodState


class MassRangeQuantity(DietModel):
    kind: Literal["mass_range"]
    grams_lower: Grams
    grams_upper: Grams
    food_state: FoodState

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.grams_lower > self.grams_upper:
            raise ValueError("grams_lower must not exceed grams_upper.")
        return self


class ServingsQuantity(DietModel):
    kind: Literal["servings"]
    count: Count
    serving_evidence_id: Annotated[str, StringConstraints(min_length=1, max_length=160)]


class RecipeFractionQuantity(DietModel):
    kind: Literal["recipe_fraction"]
    batch_multiplier: Count
    share: Annotated[PositiveDecimal, Field(le=1)]


class CookedMassQuantity(DietModel):
    kind: Literal["cooked_mass"]
    batch_multiplier: Count
    consumed_grams: Grams
    batch_cooked_grams: Grams

    @model_validator(mode="after")
    def fits_batch(self) -> Self:
        if self.consumed_grams > self.batch_cooked_grams:
            raise ValueError("Consumed weight must not exceed cooked batch weight.")
        return self


Quantity = Annotated[
    UnknownQuantity
    | MassQuantity
    | MassRangeQuantity
    | ServingsQuantity
    | RecipeFractionQuantity
    | CookedMassQuantity,
    Field(discriminator="kind"),
]


class FieldEvidence(DietModel):
    nonnullable_optional_fields = frozenset({"entry_key", "item_key"})
    field: Literal[
        "actuality",
        "subject",
        "date",
        "time",
        "meal_type",
        "food",
        "quantity",
        "food_state",
        "note",
        "goal",
        "effective_date",
        "operation",
    ]
    entry_key: ClientKey | None = None
    item_key: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = None
    source_kind: Literal[
        "user_message",
        "user_form",
        "active_draft",
        "catalog",
        "label",
        "model_estimate",
        "server_default",
    ]
    source_ref: Annotated[str, StringConstraints(min_length=1, max_length=160)]
    original_text: Annotated[str, StringConstraints(max_length=2000)]
    operation: Literal["set", "clear", "keep"]
    status: Literal["explicit", "derived", "proposed", "unresolved"]


EvidenceList = Annotated[list[FieldEvidence], Field(min_length=1, max_length=400)]


class ProposedItem(DietModel):
    nonnullable_optional_fields = frozenset({"note"})
    client_item_key: ClientKey
    food: FoodRef
    quantity: Quantity
    note: Note | None = None

    @model_validator(mode="after")
    def compatible_quantity(self) -> Self:
        if isinstance(self.food, (IngredientFood, PersonalFoodRef)) and isinstance(
            self.quantity, (RecipeFractionQuantity, CookedMassQuantity)
        ):
            raise ValueError("Recipe quantities require a recipe reference.")
        return self


class TemporalFields(DietModel):
    local_date: LocalDate | None = None
    local_time: LocalTime | None = None
    utc_offset_minutes: Annotated[int, Field(strict=True, ge=-840, le=840)] | None = None
    meal_type: MealType | None = None
    note: Note | None = None


class DraftEntry(DietModel):
    actuality: Literal["actual", "planned", "hypothetical", "unknown"]
    subject: Literal["self", "third_party", "unknown"]
    temporal: TemporalFields
    items: Annotated[list[ProposedItem], Field(max_length=50)]

    @model_validator(mode="after")
    def unique_keys(self) -> Self:
        keys = [item.client_item_key for item in self.items]
        if len(set(keys)) != len(keys):
            raise ValueError("client_item_key must be unique within an entry.")
        return self

    def blocking_fields(self, *, planned: bool = False, today: date) -> list[str]:
        missing = []
        if self.subject != "self":
            missing.append("subject")
        if self.actuality != ("planned" if planned else "actual"):
            missing.append("actuality")
        if self.temporal.local_date is None:
            missing.append("local_date")
        elif not planned and self.temporal.local_date > today:
            missing.append("future_actual_date")
        if self.temporal.meal_type is None:
            missing.append("meal_type")
        if not self.items:
            missing.append("items")
        return missing


class ProposedEntry(DraftEntry):
    items: Annotated[list[ProposedItem], Field(min_length=1, max_length=50)]


class SubmissionAnchor(DietModel):
    submitted_at: AwareDatetime
    timezone: ShortText
    anchor_local_date: LocalDate

    @model_validator(mode="after")
    def matches_timezone(self) -> Self:
        try:
            zone = ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Expected an IANA timezone.") from exc
        if self.submitted_at.astimezone(zone).date() != self.anchor_local_date:
            raise ValueError("Anchor date must match submission time in its timezone.")
        return self

    @classmethod
    def capture(cls, submitted_at: datetime, timezone: str) -> SubmissionAnchor:
        if submitted_at.tzinfo is None:
            raise ValueError("Submission time must be timezone-aware.")
        return cls(
            submitted_at=submitted_at,
            timezone=timezone,
            anchor_local_date=submitted_at.astimezone(ZoneInfo(timezone)).date(),
        )

    def relative_date(self, expression: Literal["today", "yesterday", "tomorrow"]) -> date:
        return self.anchor_local_date + timedelta(
            days={"today": 0, "yesterday": -1, "tomorrow": 1}[expression]
        )

    def calendar_hint(self) -> dict[str, int | str]:
        monday = self.anchor_local_date - timedelta(days=self.anchor_local_date.weekday())
        return {
            "today_weekday": self.anchor_local_date.isoweekday(),
            "this_week_monday": monday.isoformat(),
            "next_week_monday": (monday + timedelta(days=7)).isoformat(),
        }


def resolve_occurred_at(temporal: TemporalFields, timezone: str) -> datetime | None:
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise DietError("INVALID_TIMEZONE", "Expected an IANA timezone.") from exc
    if temporal.local_time is None:
        if temporal.utc_offset_minutes is not None:
            raise DietError("INVALID_LOCAL_TIME", "An offset requires a local time.")
        return None
    if temporal.local_date is None:
        raise DietError("INVALID_LOCAL_TIME", "A local time requires a local date.")
    wall = datetime.combine(temporal.local_date, temporal.local_time)
    candidates = {
        candidate.astimezone(UTC)
        for fold in (0, 1)
        if (candidate := wall.replace(tzinfo=zone, fold=fold))
        .astimezone(UTC)
        .astimezone(zone)
        .replace(tzinfo=None)
        == wall
    }
    if temporal.utc_offset_minutes is not None:
        candidates = {
            candidate
            for candidate in candidates
            if candidate.astimezone(zone).utcoffset()
            == timedelta(minutes=temporal.utc_offset_minutes)
        }
    if len(candidates) != 1:
        raise DietError(
            "INVALID_LOCAL_TIME", "Local time does not exist or requires an explicit UTC offset."
        )
    return candidates.pop()


class NutrientSnapshot(DietModel):
    value: NutrientRange | None = None
    basis: NutritionBasis = "unknown"
    evidence_ids: list[str] = Field(default_factory=list)
    missing_reasons: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def traceable(self) -> Self:
        if self.value is not None and (self.basis == "unknown" or not self.evidence_ids):
            raise ValueError("Known nutrition requires a source category and evidence.")
        if self.value is None and not self.missing_reasons:
            raise ValueError("Unknown nutrition requires a reason.")
        if self.value is not None and self.missing_reasons:
            raise ValueError("Known nutrition cannot also have missing reasons.")
        return self


class NutritionSnapshot(DietModel):
    calories_kcal: NutrientSnapshot
    protein_g: NutrientSnapshot
    fat_g: NutrientSnapshot
    carbohydrates_g: NutrientSnapshot
    fiber_g: NutrientSnapshot
    sodium_mg: NutrientSnapshot

    def nutrient(self, name: Nutrient) -> NutrientSnapshot:
        value: NutrientSnapshot = getattr(self, name)
        return value

    @classmethod
    def unknown(cls, reason: str) -> NutritionSnapshot:
        return cls(**{name: NutrientSnapshot(missing_reasons=[reason]) for name in NUTRIENTS})


class FoodEvidenceSnapshot(DietModel):
    evidence_id: ShortText
    source_kind: Literal["catalog", "label", "user_report", "model_estimate"]
    source_ref: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    source_version: ShortText
    retrieved_at: AwareDatetime
    food_state: FoodState
    basis_quantity: Literal["per_100g", "per_serving", "recipe_batch"]
    nutrients: NutritionSnapshot
    original_excerpt: Annotated[str, StringConstraints(max_length=2000)] = ""
    calculation_note: Annotated[str, StringConstraints(max_length=2000)]
    serving_evidence_id: str | None = None
    serving_grams: Grams | None = None
    model_name: str | None = None
    prompt_version: str | None = None
    media_id: UUID | None = None
    # Empty additions must not invalidate hashes of existing frozen evidence.
    media_ids: Annotated[list[UUID], Field(max_length=4)] = Field(
        default_factory=list, exclude_if=lambda value: not value
    )
    source_uri: str | None = None
    license: str | None = None

    @model_validator(mode="after")
    def source_consistency(self) -> Self:
        if self.source_kind == "model_estimate" and not (self.model_name and self.prompt_version):
            raise ValueError("Model estimates require model and prompt versions.")
        for name in NUTRIENTS:
            nutrient = self.nutrients.nutrient(name)
            if nutrient.value is not None and self.evidence_id not in nutrient.evidence_ids:
                raise ValueError("Nutrition must reference this evidence snapshot.")
            if (
                nutrient.value is not None
                and self.source_kind == "model_estimate"
                and nutrient.basis != "model_estimate"
            ):
                raise ValueError("Model evidence must remain marked as an estimate.")
        return self


def calculate_nutrition(
    quantity: Quantity, evidence: FoodEvidenceSnapshot | None
) -> NutritionSnapshot:
    if isinstance(quantity, UnknownQuantity):
        return NutritionSnapshot.unknown("quantity_unknown")
    if evidence is None:
        return NutritionSnapshot.unknown("food_evidence_unknown")
    with localcontext() as context:
        context.prec = 60
        scale: tuple[Decimal, Decimal]
        if isinstance(quantity, (MassQuantity, MassRangeQuantity)):
            if evidence.basis_quantity != "per_100g":
                return NutritionSnapshot.unknown("mass_basis_unavailable")
            if quantity.food_state == "unknown" or evidence.food_state == "unknown":
                return NutritionSnapshot.unknown("food_state_unknown")
            if quantity.food_state != evidence.food_state:
                return NutritionSnapshot.unknown("food_state_mismatch")
            low = quantity.grams if isinstance(quantity, MassQuantity) else quantity.grams_lower
            high = quantity.grams if isinstance(quantity, MassQuantity) else quantity.grams_upper
            scale = (low / 100, high / 100)
        elif isinstance(quantity, ServingsQuantity):
            if quantity.serving_evidence_id != evidence.serving_evidence_id:
                return NutritionSnapshot.unknown("serving_evidence_mismatch")
            if evidence.basis_quantity == "per_serving":
                scale = (quantity.count, quantity.count)
            elif evidence.basis_quantity == "per_100g" and evidence.serving_grams is not None:
                factor = quantity.count * evidence.serving_grams / 100
                scale = (factor, factor)
            else:
                return NutritionSnapshot.unknown("serving_basis_unavailable")
        else:
            if evidence.basis_quantity != "recipe_batch":
                return NutritionSnapshot.unknown("recipe_batch_unavailable")
            factor = quantity.batch_multiplier * (
                quantity.share
                if isinstance(quantity, RecipeFractionQuantity)
                else quantity.consumed_grams / quantity.batch_cooked_grams
            )
            scale = (factor, factor)
        values = {}
        for name in NUTRIENTS:
            nutrient = evidence.nutrients.nutrient(name)
            values[name] = (
                nutrient
                if nutrient.value is None
                else NutrientSnapshot(
                    value=nutrient.value.scaled(*scale),
                    basis=nutrient.basis,
                    evidence_ids=list(nutrient.evidence_ids),
                )
            )
        return NutritionSnapshot(**values)


class NutrientTotal(DietModel):
    total: NutrientRange | None
    known_subtotal: NutrientRange
    complete: bool
    known_item_count: Annotated[int, Field(ge=0)]
    total_item_count: Annotated[int, Field(ge=0)]
    missing_reasons: list[str]


class NutritionSummary(DietModel):
    state: Literal["no_records", "partial", "recorded", "complete", "no_intake"]
    day_status: DayStatus
    record_count: Annotated[int, Field(ge=0)]
    nutrients: dict[Nutrient, NutrientTotal]


def summarize_nutrition(
    snapshots: Iterable[NutritionSnapshot], *, record_count: int, day_status: DayStatus = "open"
) -> NutritionSummary:
    values = list(snapshots)
    if record_count < 0 or bool(record_count) != bool(values):
        raise ValueError("Record count must match the presence of diet items.")
    if day_status == "no_intake" and record_count:
        raise DietError("DAY_HAS_RECORDS", "A no-intake day cannot have active records.")
    totals: dict[Nutrient, NutrientTotal] = {}
    for name in NUTRIENTS:
        known = [item.nutrient(name).value for item in values if item.nutrient(name).value]
        subtotal = NutrientRange(
            lower=sum((item.lower for item in known if item is not None), Decimal(0)),
            upper=sum((item.upper for item in known if item is not None), Decimal(0)),
        )
        complete = (bool(values) and len(known) == len(values)) or day_status == "no_intake"
        totals[name] = NutrientTotal(
            total=subtotal if complete else None,
            known_subtotal=subtotal,
            complete=complete,
            known_item_count=len(known),
            total_item_count=len(values),
            missing_reasons=sorted(
                {reason for item in values for reason in item.nutrient(name).missing_reasons}
            )
            if values
            else ([] if complete else ["no_records"]),
        )
    state: Literal["no_records", "partial", "recorded", "complete", "no_intake"]
    if day_status == "no_intake":
        state = "no_intake"
    elif not values:
        state = "no_records"
    elif not all(item.complete for item in totals.values()):
        state = "partial"
    else:
        state = "complete" if day_status == "complete" else "recorded"
    return NutritionSummary(
        state=state, day_status=day_status, record_count=record_count, nutrients=totals
    )


class NutritionGoals(DietModel):
    calories_kcal: NutrientRange | None = None
    protein_g: NutrientRange | None = None
    fat_g: NutrientRange | None = None
    carbohydrates_g: NutrientRange | None = None

    @model_validator(mode="after")
    def technical_limits(self) -> Self:
        if self.calories_kcal is not None and self.calories_kcal.lower <= 0:
            raise ValueError("Calorie goals must be positive.")
        for name in GOAL_NUTRIENTS:
            value = self.nutrient(name)
            if value is not None and value.upper > (100000 if name == "calories_kcal" else 10000):
                raise ValueError(f"{name} exceeds the technical limit.")
        return self

    def nutrient(self, name: GoalNutrient) -> NutrientRange | None:
        value: NutrientRange | None = getattr(self, name)
        return value

    def merged(self, patch: NutritionGoals) -> NutritionGoals:
        return NutritionGoals.model_validate(
            {
                **self.model_dump(),
                **patch.model_dump(exclude_unset=True),
            }
        )


class GoalComparison(DietModel):
    status: Literal["within", "below", "above", "overlap", "not_comparable"]
    difference: DifferenceRange | None = None
    reason: str | None = None


def compare_goal(
    actual: NutrientTotal, goal: NutrientRange | None, *, day_status: DayStatus
) -> GoalComparison:
    if goal is None:
        return GoalComparison(status="not_comparable", reason="target_unknown")
    if day_status == "open":
        return GoalComparison(status="not_comparable", reason="day_not_complete")
    if not actual.complete or actual.total is None:
        return GoalComparison(status="not_comparable", reason="nutrition_incomplete")
    value = actual.total
    status: Literal["within", "below", "above", "overlap"]
    if value.lower >= goal.lower and value.upper <= goal.upper:
        status = "within"
    elif value.upper < goal.lower:
        status = "below"
    elif value.lower > goal.upper:
        status = "above"
    else:
        status = "overlap"
    return GoalComparison(
        status=status,
        difference=DifferenceRange(lower=value.lower - goal.upper, upper=value.upper - goal.lower),
    )


def content_hash(value: BaseModel) -> str:
    canonical = json.dumps(
        value.model_dump(mode="json", exclude_unset=False),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()
