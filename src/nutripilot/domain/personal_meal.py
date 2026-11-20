"""Personal quantities and nutrition derived from versioned food evidence."""

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AliasChoices, Field

from nutripilot.domain.diet import (
    DietModel,
    FoodEvidenceSnapshot,
    IngredientFood,
    MassQuantity,
    MealType,
    NutritionSummary,
    PersonalFoodRef,
    Quantity,
    RecipeFood,
    RecipeFractionQuantity,
    ServingsQuantity,
    UnknownQuantity,
    calculate_nutrition,
    summarize_nutrition,
)


class PersonalMealItem(DietModel):
    meal_id: int | None
    name: str
    food: Annotated[RecipeFood | IngredientFood | PersonalFoodRef, Field(discriminator="kind")]
    quantity: Quantity
    evidence: FoodEvidenceSnapshot


class PersonalMealOption(DietModel):
    id: str
    turn_id: str
    local_date: date
    meal_type: MealType | None
    diet_epoch: int | None
    context_fingerprint: str | None = None
    context_future: bool = False
    items: Annotated[list[PersonalMealItem], Field(min_length=1, max_length=3)]
    nutrition: NutritionSummary
    reason: str


class PortionChoice(DietModel):
    candidate_id: int = Field(validation_alias=AliasChoices("candidate_id", "meal_id"))
    share: Literal["0.25", "0.5", "0.75", "1"]


class PersonalMealChoices(DietModel):
    options: Annotated[
        list[Annotated[list[PortionChoice], Field(min_length=1, max_length=3)]], Field(max_length=3)
    ]


def personal_nutrition(items: list[PersonalMealItem]) -> NutritionSummary:
    return summarize_nutrition(
        (calculate_nutrition(item.quantity, item.evidence) for item in items),
        record_count=1,
    )


def portion_amount(quantity: Quantity) -> Decimal | None:
    if isinstance(quantity, RecipeFractionQuantity):
        return quantity.share
    if isinstance(quantity, MassQuantity):
        return quantity.grams
    if isinstance(quantity, ServingsQuantity):
        return quantity.count
    return None


def with_portion(item: PersonalMealItem, amount: Decimal | None) -> PersonalMealItem:
    quantity = item.quantity
    field = {
        "recipe_fraction": "share",
        "mass": "grams",
        "servings": "count",
    }.get(quantity.kind)
    if field and amount is not None:
        quantity = type(quantity).model_validate({**quantity.model_dump(), field: amount})
    elif amount is not None or not isinstance(quantity, UnknownQuantity):
        raise ValueError("Amount does not match the food's portion basis.")
    return item.model_copy(update={"quantity": quantity})
