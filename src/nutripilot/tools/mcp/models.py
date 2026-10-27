from __future__ import annotations

from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from nutripilot.retrieval.models import NutritionFacts


class SearchMealsMCPArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=500)
    meal_type: str | None = None
    cuisines: list[str] = Field(default_factory=list)
    tastes: list[str] = Field(default_factory=list)
    health_goals: list[str] = Field(default_factory=list)
    dietary_tags: list[str] = Field(default_factory=list)
    excluded_allergens: list[str] = Field(default_factory=list)
    excluded_ingredients: list[str] = Field(default_factory=list)
    excluded_meal_ids: list[int] = Field(default_factory=list)
    max_calories_kcal: float | None = Field(default=None, gt=0)
    min_protein_g: float | None = Field(default=None, ge=0)
    max_prep_minutes: int | None = Field(default=None, ge=0)
    max_price_cny: float | None = Field(default=None, ge=0)
    limit: int = Field(default=5, ge=1, le=20)


class MCPMealHit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meal_id: int
    name: str
    description: str
    meal_types: list[str] = Field(min_length=1)
    score: float
    nutrition: NutritionFacts
    ingredients: list[str]
    allergens: list[str]
    prep_minutes: int | None
    price_cny: float | None
    recipe_id: int | None = None
    meal_role: Literal["complete_meal", "dish", "staple", "soup", "snack"] = "complete_meal"
    tastes: list[str] = Field(default_factory=list)
    cuisines: list[str] = Field(default_factory=list)
    dietary_tags: list[str] = Field(default_factory=list)
    reasons: list[str]
    citation: dict[str, Any]


class SearchMealsMCPResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    hits: list[MCPMealHit] = Field(max_length=20)
    total_candidates: int
    retrieval_version: str


class GetNutritionMCPArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    meal_id: int | None = Field(default=None, gt=0)
    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=120,
        validation_alias=AliasChoices("name", "food_name"),
    )

    @model_validator(mode="after")
    def require_identifier(self) -> GetNutritionMCPArgs:
        if self.meal_id is None and self.name is None:
            raise ValueError("meal_id or name is required")
        return self


class GetNutritionMCPResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meal_id: int
    name: str
    nutrition: NutritionFacts
    ingredients: list[str]
    allergens: list[str]
    recipe_id: int | None = None
    meal_role: Literal["complete_meal", "dish", "staple", "soup", "snack"] = "complete_meal"
    serving_note: str
    citation: dict[str, Any]


class QueryDietaryGuidelinesMCPArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=3, ge=1, le=5)


class MCPGuidelineHit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    title: str
    content: str = Field(max_length=2000)
    source: str
    source_uri: str | None = None
    score: float
    disclaimer: str
    version: str


class QueryDietaryGuidelinesMCPResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    hits: list[MCPGuidelineHit] = Field(max_length=5)
    disclaimer: str


class RemoteToolSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    description: str
    input_schema: dict[str, Any] = Field(alias="inputSchema")
    version: str = "1.0.0"


MCP_TOOL_CONTRACTS: dict[str, tuple[type[BaseModel], type[BaseModel], str]] = {
    "search_meals": (
        SearchMealsMCPArgs,
        SearchMealsMCPResult,
        "Search meal candidates using structured safety filters and hybrid retrieval.",
    ),
    "get_nutrition": (
        GetNutritionMCPArgs,
        GetNutritionMCPResult,
        "Read the nutrition estimate and ingredients for one authoritative meal record.",
    ),
    "query_dietary_guidelines": (
        QueryDietaryGuidelinesMCPArgs,
        QueryDietaryGuidelinesMCPResult,
        "Search sourced dietary guidance and return the relevant disclaimer.",
    ),
}
