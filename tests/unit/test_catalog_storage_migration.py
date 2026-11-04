from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from apps.nutrition_mcp.service import ExternalNutritionMCPService
from qdrant_client import AsyncQdrantClient

from nutripilot.application.runtime import _seed_postgres
from nutripilot.infrastructure.memory import load_seed_store
from nutripilot.infrastructure.postgres import PostgresDataStore
from nutripilot.infrastructure.qdrant import QdrantMealIndex
from nutripilot.tools.errors import ToolRuntimeError
from nutripilot.tools.local.models import CreateMealPlanArgs, MealPlan


@pytest.mark.asyncio
async def test_postgres_seed_contains_retirement_flags_as_well_as_active_recipes() -> None:
    store = AsyncMock()
    await _seed_postgres(store, Path("data"))
    meals, _ = store.seed.call_args.args
    assert len(meals) == 118
    assert {meal.id for meal in meals if not meal.is_active} == set(range(1, 19))
    assert len([meal for meal in meals if meal.is_active]) == 100


@pytest.mark.asyncio
async def test_external_mcp_seeds_history_but_indexes_only_active_recipes(monkeypatch) -> None:
    store = AsyncMock()
    index = AsyncMock()
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://test")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "hashing")
    monkeypatch.setenv("RERANK_PROVIDER", "lexical")
    monkeypatch.setattr(PostgresDataStore, "from_url", lambda _: store)
    monkeypatch.setattr(QdrantMealIndex, "from_url", lambda *args, **kwargs: index)
    service = ExternalNutritionMCPService(Path("data"))
    try:
        delegate = await service._get_delegate()
        assert await service._get_delegate() is delegate
        assert len(store.seed.call_args.args[0]) == 118
        index.delete.assert_awaited_once_with(list(range(1, 19)))
        records = index.upsert.call_args.args[0]
        assert len(records) == 100
        assert {meal_id for meal_id, _, _ in records} == set(range(1001, 1101))
        index.ensure_collection.assert_awaited_once()
    finally:
        await service.close()


class _Result:
    def __init__(self, values):
        self.values = values

    def __iter__(self):
        return iter(self.values)

    def first(self):
        return next(iter(self.values), None)


class _Engine:
    def __init__(self, rows):
        self.queries = []
        self.rows = rows

    @asynccontextmanager
    async def connect(self):
        yield self

    async def execute(self, statement, parameters):
        self.queries.append((str(statement), parameters))
        return _Result(self.rows)


@pytest.mark.asyncio
async def test_postgres_history_batch_lookup_does_not_filter_archived_records() -> None:
    meal = await load_seed_store("data").get_meal(2)
    engine = _Engine([(meal.model_dump(mode="json"),)])
    store = PostgresDataStore(engine)
    assert [meal.id for meal in await store.get_meals([2, 9999])] == [2]
    assert "is_active" not in engine.queries[0][0]
    assert not (await store.get_meal(2)).is_active


@pytest.mark.asyncio
async def test_postgres_old_idempotent_plan_is_returned_before_active_validation() -> None:
    request = CreateMealPlanArgs.model_validate(
        {
            "title": "旧餐单",
            "start_date": "2026-10-20",
            "items": [{"meal_id": 2, "meal_type": "lunch", "meal_date": "2026-10-20"}],
        }
    )
    plan = MealPlan.model_validate(
        {
            **request.model_dump(mode="json"),
            "id": "old",
            "user_id": 2,
            "created_at": "2026-10-20T00:00:00Z",
        }
    )
    engine = _Engine([(json.dumps(plan.model_dump(mode="json")),)])
    replay = await PostgresDataStore(engine).create_meal_plan(2, request, "same")
    assert replay == plan
    assert len(engine.queries) == 1
    assert "meal_plans" in engine.queries[0][0]


@pytest.mark.asyncio
async def test_postgres_rejects_new_plan_with_a_retired_id() -> None:
    request = CreateMealPlanArgs.model_validate(
        {
            "title": "旧餐单",
            "start_date": "2026-10-20",
            "items": [{"meal_id": 2, "meal_type": "lunch", "meal_date": "2026-10-20"}],
        }
    )
    meal = await load_seed_store("data").get_meal(2)
    store = PostgresDataStore(_Engine([]))
    store.get_meals = AsyncMock(return_value=[meal])
    with pytest.raises(ToolRuntimeError) as caught:
        await store.create_meal_plan(2, request, "new")
    assert caught.value.code == "MEAL_RETIRED"


@pytest.mark.asyncio
async def test_qdrant_retirement_removes_old_points_without_losing_source_recipe_points() -> None:
    index = QdrantMealIndex(AsyncQdrantClient(location=":memory:"))
    try:
        await index.ensure_collection(2)
        await index.upsert([(2, [1.0, 0.0], {}), (1004, [1.0, 0.0], {})])
        assert {hit.id for hit in await index.search([1, 0], limit=20)} == {2, 1004}
        await index.delete([2])
        await index.delete([2])
        assert [hit.id for hit in await index.search([1, 0], limit=20)] == [1004]
    finally:
        await index.close()
