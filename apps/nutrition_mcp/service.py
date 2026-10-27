from __future__ import annotations

import asyncio
import os
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from nutripilot.infrastructure import PostgresDataStore, QdrantMealIndex
from nutripilot.infrastructure.memory import load_seed_store
from nutripilot.paths import project_root
from nutripilot.retrieval.embedding import BGEEmbedder, HashingEmbedder
from nutripilot.retrieval.models import Meal, MealSearchFilters, MealSearchRequest
from nutripilot.retrieval.ports import GuidelineRepository, MealRepository
from nutripilot.retrieval.rerank import BGEReranker, LexicalReranker
from nutripilot.retrieval.service import GuidelineSearchService, RetrievalService
from nutripilot.tools.errors import ToolRuntimeError
from nutripilot.tools.mcp.models import (
    MCP_TOOL_CONTRACTS,
    GetNutritionMCPArgs,
    GetNutritionMCPResult,
    MCPGuidelineHit,
    MCPMealHit,
    QueryDietaryGuidelinesMCPArgs,
    QueryDietaryGuidelinesMCPResult,
    RemoteToolSpec,
    SearchMealsMCPArgs,
    SearchMealsMCPResult,
)


class NutritionDataStore(MealRepository, GuidelineRepository, Protocol):
    """Authoritative methods required by the nutrition MCP boundary."""


class NutritionService(Protocol):
    async def list_tools(self) -> list[RemoteToolSpec]: ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


class NutritionMCPService:
    """Application service behind both real MCP and in-process transports."""

    version = "1.0.0"

    def __init__(self, store: NutritionDataStore, retrieval: RetrievalService) -> None:
        self.store = store
        self.retrieval = retrieval
        self.guidelines = GuidelineSearchService(store)

    @classmethod
    def from_seed_data(cls, data_directory: str | Path) -> NutritionMCPService:
        store = load_seed_store(data_directory)
        return cls(store, RetrievalService(store))

    async def list_tools(self) -> list[RemoteToolSpec]:
        return [
            RemoteToolSpec(
                name=name,
                description=description,
                input_schema=arguments_model.model_json_schema(),
                version=self.version,
            )
            for name, (arguments_model, _result_model, description) in sorted(
                MCP_TOOL_CONTRACTS.items()
            )
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            if name == "search_meals":
                result: BaseModel = await self.search_meals(
                    SearchMealsMCPArgs.model_validate(arguments)
                )
            elif name == "get_nutrition":
                result = await self.get_nutrition(GetNutritionMCPArgs.model_validate(arguments))
            elif name == "query_dietary_guidelines":
                result = await self.query_dietary_guidelines(
                    QueryDietaryGuidelinesMCPArgs.model_validate(arguments)
                )
            else:
                raise ToolRuntimeError(
                    "MCP_TOOL_NOT_FOUND", "The requested nutrition tool is unavailable."
                )
        except ValidationError as exc:
            raise ToolRuntimeError(
                "INVALID_ARGUMENT",
                "The MCP tool arguments did not match the required schema.",
                details={"field_count": len(exc.errors())},
            ) from exc
        return result.model_dump(mode="json")

    async def search_meals(self, request: SearchMealsMCPArgs) -> SearchMealsMCPResult:
        response = await self.retrieval.search_meals(
            MealSearchRequest(
                query=request.query,
                filters=MealSearchFilters(
                    meal_type=request.meal_type,
                    cuisines=request.cuisines,
                    tastes=request.tastes,
                    health_goals=request.health_goals,
                    dietary_tags=request.dietary_tags,
                    excluded_allergens=request.excluded_allergens,
                    excluded_ingredients=request.excluded_ingredients,
                    excluded_meal_ids=request.excluded_meal_ids,
                    max_calories_kcal=request.max_calories_kcal,
                    min_protein_g=request.min_protein_g,
                    max_prep_minutes=request.max_prep_minutes,
                    max_price_cny=request.max_price_cny,
                ),
                limit=request.limit,
            )
        )
        return SearchMealsMCPResult(
            query=response.query,
            hits=[
                MCPMealHit(
                    meal_id=hit.meal.id,
                    name=hit.meal.name,
                    description=hit.meal.description,
                    meal_types=hit.meal.meal_types,
                    score=hit.score,
                    nutrition=hit.meal.nutrition,
                    ingredients=hit.meal.ingredients,
                    allergens=hit.meal.allergens,
                    prep_minutes=hit.meal.prep_minutes,
                    price_cny=hit.meal.price_cny,
                    recipe_id=hit.meal.recipe_id,
                    meal_role=hit.meal.meal_role,
                    tastes=hit.meal.tastes,
                    cuisines=hit.meal.cuisines,
                    dietary_tags=hit.meal.dietary_tags,
                    reasons=hit.reasons,
                    citation=hit.citation,
                )
                for hit in response.hits
            ],
            total_candidates=response.total_candidates,
            retrieval_version=response.retrieval_version,
        )

    async def get_nutrition(self, request: GetNutritionMCPArgs) -> GetNutritionMCPResult:
        meal = None
        if request.meal_id is not None:
            meal = await self.store.get_meal(request.meal_id)
        if request.meal_id is not None and meal is None:
            raise ToolRuntimeError("MEAL_NOT_FOUND", "没有找到该餐食编号的营养记录。")
        if meal is None and request.name:
            meal = _resolve_meal_name(request.name, await self.store.list_meals())
        if meal is None:
            raise ToolRuntimeError(
                "MEAL_NOT_FOUND", "No authoritative nutrition record matched the request."
            )
        return GetNutritionMCPResult(
            meal_id=meal.id,
            name=meal.name,
            nutrition=meal.nutrition,
            ingredients=meal.ingredients,
            allergens=meal.allergens,
            recipe_id=meal.recipe_id,
            meal_role=meal.meal_role,
            serving_note=meal.nutrition.calculation_note
            or "营养值按记录中的标准份量估算，实际值会随食材和烹调方式变化。",
            citation={
                "source_id": f"meal:{meal.id}",
                "title": meal.name,
                "source": meal.source,
                "uri": meal.source_uri,
                "data_version": meal.data_version,
            },
        )

    async def query_dietary_guidelines(
        self, request: QueryDietaryGuidelinesMCPArgs
    ) -> QueryDietaryGuidelinesMCPResult:
        hits = await self.guidelines.search(request.query, request.limit)
        disclaimer = "仅供一般饮食信息参考，不构成医疗诊断或治疗建议。"
        return QueryDietaryGuidelinesMCPResult(
            query=request.query,
            hits=[
                MCPGuidelineHit(
                    source_id=hit.guideline.id,
                    title=hit.guideline.title,
                    content=hit.guideline.content,
                    source=hit.guideline.source,
                    source_uri=hit.guideline.source_uri,
                    score=round(hit.score, 6),
                    disclaimer=hit.guideline.disclaimer,
                    version=hit.guideline.version,
                )
                for hit in hits
            ],
            disclaimer=disclaimer,
        )


def _normalise_meal_name(value: str) -> str:
    return re.sub(r"[\s\W_]+", "", unicodedata.normalize("NFKC", value).casefold())


def _resolve_meal_name(name: str, meals: list[Meal]) -> Meal:
    requested = _normalise_meal_name(name)
    exact = [
        meal
        for meal in meals
        if requested
        and requested in {_normalise_meal_name(value) for value in (meal.name, *meal.aliases)}
    ]
    if len(exact) == 1:
        return exact[0]
    candidates = exact or [
        meal
        for meal in meals
        if requested
        and any(
            requested in _normalise_meal_name(value)
            or SequenceMatcher(None, requested, _normalise_meal_name(value)).ratio() >= 0.72
            for value in (meal.name, *meal.aliases)
        )
    ]
    if candidates:
        raise ToolRuntimeError(
            "MEAL_NAME_AMBIGUOUS",
            "请确认你要查询的餐食名称。",
            details={
                "candidates": [{"meal_id": meal.id, "name": meal.name} for meal in candidates[:5]]
            },
        )
    raise ToolRuntimeError("MEAL_NOT_FOUND", "没有找到该菜名的可靠营养记录，请提供完整菜名。")


class ExternalNutritionMCPService:
    """Lazily initializes PostgreSQL and Qdrant for the deployed MCP process."""

    version = "1.0.0"

    def __init__(self, data_directory: Path) -> None:
        self.data_directory = data_directory
        self._delegate: NutritionMCPService | None = None
        self._store: PostgresDataStore | None = None
        self._index: QdrantMealIndex | None = None
        self._lock = asyncio.Lock()

    async def list_tools(self) -> list[RemoteToolSpec]:
        return _tool_specs(self.version)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        delegate = await self._get_delegate()
        return await delegate.call_tool(name, arguments)

    async def close(self) -> None:
        if self._index is not None:
            await self._index.close()
        if self._store is not None:
            await self._store.close()
        self._delegate = None
        self._index = None
        self._store = None

    async def _get_delegate(self) -> NutritionMCPService:
        if self._delegate is not None:
            return self._delegate
        async with self._lock:
            if self._delegate is not None:
                return self._delegate
            database_url = os.environ["DATABASE_URL"]
            store = PostgresDataStore.from_url(database_url)
            await store.initialize()
            seed = load_seed_store(self.data_directory)
            meals = await seed.list_meals()
            await store.seed(meals, await seed.list_guidelines())

            embedder = _build_embedder()
            reranker = _build_reranker()
            index = QdrantMealIndex.from_url(
                os.getenv("QDRANT_URL", "http://localhost:6333"),
                api_key=os.getenv("QDRANT_API_KEY"),
                collection_name=os.getenv("QDRANT_COLLECTION", "meal_search"),
            )
            vectors = await embedder.embed([meal.searchable_text() for meal in meals])
            await index.ensure_collection(embedder.dimension)
            await index.upsert(
                [
                    (
                        meal.id,
                        vector,
                        {
                            "name": meal.name,
                            "meal_types": meal.meal_types,
                            "allergens": meal.allergens,
                            "data_version": meal.data_version,
                        },
                    )
                    for meal, vector in zip(meals, vectors, strict=True)
                ]
            )
            self._store = store
            self._index = index
            self._delegate = NutritionMCPService(
                store,
                RetrievalService(
                    store,
                    vector_index=index,
                    embedder=embedder,
                    reranker=reranker,
                ),
            )
            return self._delegate


def default_data_directory() -> Path:
    return project_root() / "data"


def build_default_service() -> NutritionService:
    if os.getenv("APP_MODE", "demo").casefold() in {"docker", "production"}:
        return ExternalNutritionMCPService(default_data_directory())
    return NutritionMCPService.from_seed_data(default_data_directory())


def _tool_specs(version: str) -> list[RemoteToolSpec]:
    return [
        RemoteToolSpec(
            name=name,
            description=description,
            input_schema=arguments_model.model_json_schema(),
            version=version,
        )
        for name, (arguments_model, _result_model, description) in sorted(
            MCP_TOOL_CONTRACTS.items()
        )
    ]


def _build_embedder() -> HashingEmbedder | BGEEmbedder:
    if os.getenv("EMBEDDING_PROVIDER", "hashing").casefold() == "bge":
        return BGEEmbedder(
            os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3"),
            device=os.getenv("EMBEDDING_DEVICE") or None,
        )
    return HashingEmbedder()


def _build_reranker() -> LexicalReranker | BGEReranker:
    if os.getenv("RERANK_PROVIDER", "lexical").casefold() == "bge":
        return BGEReranker(
            os.getenv("RERANK_MODEL", "BAAI/bge-reranker-v2-m3"),
            device=os.getenv("RERANK_DEVICE") or None,
        )
    return LexicalReranker()
