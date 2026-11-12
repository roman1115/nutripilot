"""Opt-in tests against local services; never use production database URLs."""

from __future__ import annotations

import asyncio
import os
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest
from anyio import create_task_group
from apps.nutrition_mcp.service import ExternalNutritionMCPService
from sqlalchemy import text

from nutripilot.agent.slots import today_in_timezone
from nutripilot.api.app import create_app
from nutripilot.application import build_container
from nutripilot.application.chat_worker import ChatWorker
from nutripilot.application.runtime import _postgres_checkpointer
from nutripilot.config import Settings
from nutripilot.infrastructure.chat_tasks import TRASH_RETENTION_SECONDS
from nutripilot.infrastructure.postgres import PostgresDataStore
from nutripilot.infrastructure.qdrant import QdrantMealIndex
from nutripilot.infrastructure.redis import RedisTTLStore
from nutripilot.model_gateway import FakeModelGateway
from nutripilot.paths import project_root
from nutripilot.retrieval.embedding import HashingEmbedder
from nutripilot.tools.errors import ToolRuntimeError
from nutripilot.tools.local.models import CreateMealPlanArgs, MealPlan

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("NUTRIPILOT_TEST_EXTERNAL") != "1",
        reason="Set NUTRIPILOT_TEST_EXTERNAL=1 to verify local PostgreSQL/Redis/Qdrant",
    ),
]
DATABASE_URL = "postgresql+asyncpg://nutripilot:nutripilot@127.0.0.1:5432/nutripilot"


@pytest.fixture
async def external_worker():  # type: ignore[no-untyped-def]
    workers = []

    async def start(app):  # type: ignore[no-untyped-def]
        container = app.state.container
        assert container.chat_runs is not None
        worker = ChatWorker(container.chat_service, container.chat_runs.store, container.settings)
        task = asyncio.create_task(worker.run())
        await asyncio.wait_for(worker.ready.wait(), 5)
        workers.append((worker, task))
        return worker, task

    yield start
    for worker, task in reversed(workers):
        await worker.close()
        await task


@pytest.mark.asyncio
async def test_real_checkpoint_pool_replaces_broken_connection() -> None:
    container = build_container(
        settings=Settings(_env_file=None, database_url=DATABASE_URL)
    )
    async with _postgres_checkpointer(container) as checkpointer:
        await checkpointer.setup()
        async with checkpointer.conn.connection() as connection:
            await connection.close()
        assert await checkpointer.aget_tuple(
            {"configurable": {"thread_id": uuid4().hex}}
        ) is None
    assert checkpointer.conn.closed


@pytest.mark.asyncio
async def test_real_trash_survives_restart_restores_context_and_purges(
    tmp_path, external_worker,
) -> None:
    user_id = 6_000_000_000 + uuid4().int % 1_000_000_000
    settings = Settings(
        _env_file=None, app_mode="docker", model_provider="fake",
        embedding_provider="hashing", rerank_provider="lexical",
        database_url=DATABASE_URL, redis_url="redis://127.0.0.1:6379/0",
        mcp_enabled=False, chat_queue=f"trash-{uuid4().hex}",
        run_store_path=str(tmp_path / "trash.sqlite3"),
    )
    thread = uuid4().hex
    headers = {"X-User-ID": str(user_id)}
    app = create_app(build_container(settings=settings))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test", headers=headers,
        ) as client,
    ):
        worker, task = await external_worker(app)
        created = await client.post(
            "/api/v1/chat",
            json={"user_id": user_id, "thread_id": thread, "message": "推荐午餐"},
        )
        assert created.status_code == 200
        original = (await client.get(f"/api/v1/threads/{thread}/snapshot")).json()
        assert (await client.delete(f"/api/v1/threads/{thread}")).status_code == 204
        assert (await client.get("/api/v1/threads")).json()["items"] == []
        await worker.close()
        await task
    app = create_app(build_container(settings=settings))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test", headers=headers,
        ) as client,
    ):
        worker, task = await external_worker(app)
        service = app.state.container.chat_runs
        try:
            trash = (await client.get("/api/v1/threads/trash")).json()["items"]
            assert len(trash) == 1 and trash[0]["thread_id"] == thread
            assert (await client.get(f"/api/v1/threads/{thread}")).status_code == 404
            assert (await client.post(f"/api/v1/threads/{thread}/restore")).status_code == 204
            assert (await client.get(f"/api/v1/threads/{thread}/snapshot")).json() == original
            response = await client.post(
                "/api/v1/chat",
                json={"user_id": user_id, "thread_id": thread, "message": "第一道怎么做？"},
            )
            assert response.status_code == 200 and response.json()["intent"] == "RECIPE_QUERY"
            messages = (await client.get(f"/api/v1/threads/{thread}/messages")).json()["items"]
            assert len(messages) == 4
            assert (await client.delete(f"/api/v1/threads/{thread}")).status_code == 204
            with service.store.transaction() as connection:
                connection.execute(text(
                    "UPDATE chat_thread_trash SET deleted_at=:expired WHERE thread_id=:id"
                ), {"expired": time.time() - TRASH_RETENTION_SECONDS - 1, "id": thread})
            assert (await client.post(f"/api/v1/threads/{thread}/restore")).status_code == 404
            await service.purge_expired_threads()
            assert not await app.state.container.graph.aget_state(
                app.state.container.chat_service._config(thread)
            )
            with service.store.transaction() as connection:
                for table in ("chat_threads", "chat_messages", "chat_runs", "chat_thread_trash"):
                    assert connection.execute(
                        text(f"SELECT COUNT(*) FROM {table} WHERE thread_id=:id"), {"id": thread},
                    ).scalar() == 0
                assert connection.execute(
                    text("SELECT COUNT(*) FROM chat_events WHERE run_id=:id"),
                    {"id": original["run"]["id"]},
                ).scalar() == 0
        finally:
            await worker.close()
            await task


@pytest.mark.asyncio
async def test_real_stream_disconnect_restoration_and_next_turn(tmp_path, monkeypatch) -> None:
    user_id = 2_000_000_000 + uuid4().int % 1_000_000_000
    settings = Settings(
        _env_file=None,
        app_mode="docker",
        model_provider="fake",
        database_url=DATABASE_URL,
        redis_url="redis://127.0.0.1:6379/0",
        mcp_enabled=False,
        run_store_path=str(tmp_path / "disconnect.sqlite3"),
    )
    gateway = FakeModelGateway()
    generate = gateway.generate_structured
    started = asyncio.Event()

    async def slow_generate(**kwargs: Any) -> Any:
        started.set()
        await asyncio.Event().wait()

    app = create_app(build_container(gateway=gateway, settings=settings))
    thread_id = uuid4().hex
    async with app.router.lifespan_context(app):
        service = app.state.container.chat_service

        async def run() -> None:
            async for _ in service.stream(
                user_id=user_id, message="推荐晚餐", thread_id=thread_id
            ):
                pass

        for _ in range(3):
            started.clear()
            monkeypatch.setattr(gateway, "generate_structured", slow_generate)
            async with create_task_group() as group:
                group.start_soon(run)
                await asyncio.wait_for(started.wait(), timeout=5)
                state, messages = await asyncio.gather(
                    service.get_thread(thread_id=thread_id, user_id=user_id),
                    service.messages(thread_id=thread_id, user_id=user_id),
                )
                assert state["user_id"] == user_id
                assert messages[-1].role == "user"
                group.cancel_scope.cancel()
            restored = await service.get_thread(thread_id=thread_id, user_id=user_id)
            assert restored["error"]["code"] == "CANCELLED"
            messages = await service.messages(thread_id=thread_id, user_id=user_id)
            assert "已停止本轮请求" in messages[-1].content
        monkeypatch.setattr(gateway, "generate_structured", generate)
        result = await service.chat(
            user_id=user_id, message="糖拌西红柿有多少热量", thread_id=thread_id
        )
        assert result["phase"] == "COMPLETED"


@pytest.mark.asyncio
async def test_real_checkpoint_confirmation_feedback_and_identity_survive_rebuild(
    tmp_path, external_worker
) -> None:
    user_id = 2_000_000_000 + uuid4().int % 1_000_000_000
    owner_token, other_token = uuid4().hex, uuid4().hex
    settings = Settings(
        _env_file=None,
        app_mode="docker",
        model_provider="fake",
        embedding_provider="hashing",
        rerank_provider="lexical",
        database_url=DATABASE_URL,
        redis_url="redis://127.0.0.1:6379/0",
        mcp_enabled=False,
        user_timezone="Asia/Shanghai",
        run_store_path=str(tmp_path / "history.sqlite3"),
        chat_queue=f"test-{uuid4().hex}",
        api_access_tokens={owner_token: user_id, other_token: user_id + 1},
    )
    headers = {"Authorization": f"Bearer {owner_token}"}
    other_headers = {"Authorization": f"Bearer {other_token}"}
    day = today_in_timezone("Asia/Shanghai") + timedelta(days=1)
    app = create_app(build_container(settings=settings))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test", headers=headers
        ) as client,
    ):
        first_worker, first_task = await external_worker(app)
        response = await client.put(
            "/api/v1/profile", json={"display_name": "持久化验收", "avoidances": ["香菇"]}
        )
        assert response.status_code == 200
        profile = response.json()
        response = await client.post(
            "/api/v1/chat",
            json={"user_id": user_id, "message": f"制定{day}的一日三餐餐单"},
        )
        assert response.status_code == 200
        pending = response.json()
        assert pending["requires_confirmation"], pending
        assert await app.state.container.data_store.list_plans(user_id) == []
        thread = pending["thread_id"]
        response = await client.get(f"/api/v1/threads/{thread}/messages")
        messages = response.json()
        feedback_payload = {"meal_id": 1001, "feedback_type": "dislike", "reason": "验收"}
        response = await client.post(
            "/api/v1/feedback", json=feedback_payload, headers={"Idempotency-Key": thread}
        )
        assert response.status_code == 200
        feedback = response.json()
        await first_worker.close()
        await first_task

    app = create_app(build_container(settings=settings))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test", headers=headers
        ) as client,
    ):
        second_worker, second_task = await external_worker(app)
        assert (await client.get("/api/v1/profile")).json() == profile
        assert (await client.get(f"/api/v1/threads/{thread}/messages")).json() == messages
        response = await client.get(f"/api/v1/threads/{thread}", params={"user_id": user_id})
        assert response.json()["pending_action"] == pending["pending_action"]
        response = await client.get(
            f"/api/v1/threads/{thread}", params={"user_id": user_id + 1}, headers=other_headers
        )
        assert response.status_code == 404
        assert (
            await client.get(f"/api/v1/threads/{thread}/messages", headers=other_headers)
        ).status_code == 404
        assert (await client.get("/api/v1/meal-plans", headers=other_headers)).json() == []
        spoofed = {**other_headers, "X-User-ID": str(user_id)}
        spoofed_profile = (await client.get("/api/v1/profile", headers=spoofed)).json()
        assert spoofed_profile["user_id"] == user_id + 1
        response = await client.post(
            "/api/v1/feedback", json=feedback_payload, headers={"Idempotency-Key": thread}
        )
        assert response.json() == feedback
        assert len(await app.state.container.data_store.list_feedback(user_id)) == 1
        decision = {
            "user_id": user_id,
            "approved": True,
            "confirmation_id": pending["pending_action"]["id"],
        }
        response = await client.post(f"/api/v1/chat/{thread}/confirm", json=decision)
        assert response.status_code == 200
        assert response.json()["phase"] == "COMPLETED", response.json()
        assert len((await client.get("/api/v1/meal-plans")).json()) == 1
        assert (
            await client.post(f"/api/v1/chat/{thread}/confirm", json=decision)
        ).status_code == 409
        await second_worker.close()
        await second_task

    app = create_app(build_container(settings=settings))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test", headers=headers
        ) as client,
    ):
        plans = (await client.get("/api/v1/meal-plans")).json()
        assert len(plans) == 1
        assert len(plans[0]["items"]) == 3
        assert plans[0]["quality"]["nutrition_basis"] == "whole_recipe"
        assert (await client.get("/api/v1/profile")).json() == profile
        assert len(await app.state.container.data_store.list_feedback(user_id)) == 1


@pytest.mark.asyncio
async def test_real_postgres_keeps_retired_history_and_rejects_new_retired_plans() -> None:
    store = PostgresDataStore.from_url(DATABASE_URL)
    try:
        active = await store.list_meals()
        assert {meal.id for meal in active} == set(range(1001, 1101))
        history = await store.get_meals(list(range(1, 19)))
        assert len(history) == 18
        assert all(not meal.is_active for meal in history)
        request = CreateMealPlanArgs.model_validate(
            {
                "title": "迁移前历史验收夹具",
                "start_date": "2026-10-20",
                "items": [{"meal_id": 2, "meal_type": "lunch", "meal_date": "2026-10-20"}],
            }
        )
        key = uuid4().hex
        legacy = MealPlan(
            **request.model_dump(),
            id=key,
            user_id=2_000_000_000 + uuid4().int % 1_000_000_000,
            created_at=datetime.now(UTC),
        )
        async with store.engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO meal_plans (id, user_id, idempotency_key, payload) "
                    "VALUES (:id, :user, :key, CAST(:payload AS JSONB))"
                ),
                {
                    "id": key,
                    "user": legacy.user_id,
                    "key": key,
                    "payload": legacy.model_dump_json(),
                },
            )
        replay = await store.create_meal_plan(legacy.user_id, request, key)
        assert replay == legacy
        with pytest.raises(ToolRuntimeError) as caught:
            await store.create_meal_plan(legacy.user_id, request, uuid4().hex)
        assert caught.value.code == "MEAL_RETIRED"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_real_redis_keeps_idempotency_keys_across_reconnect() -> None:
    prefix = f"nutripilot-test:{uuid4().hex}"
    store = RedisTTLStore.from_url("redis://127.0.0.1:6379/0", key_prefix=prefix)
    try:
        assert await store.add_if_absent("receipt", {"saved": True}, 120)
    finally:
        await store.close()
    store = RedisTTLStore.from_url("redis://127.0.0.1:6379/0", key_prefix=prefix)
    try:
        assert await store.get("receipt") == {"saved": True}
        assert not await store.add_if_absent("receipt", {"saved": False}, 120)
        assert 0 < await store.client.ttl(store._key("receipt")) <= 120
    finally:
        await store.delete("receipt")
        await store.close()


@pytest.mark.asyncio
async def test_real_qdrant_migration_deletes_only_retired_points(monkeypatch) -> None:
    collection = f"nutripilot_test_{uuid4().hex}"
    index = QdrantMealIndex.from_url("http://127.0.0.1:6333", collection_name=collection)
    embedder = HashingEmbedder()
    vector = (await embedder.embed(["迁移验收"]))[0]
    await index.ensure_collection(embedder.dimension)
    await index.upsert([(meal_id, vector, {}) for meal_id in [*range(1, 19), 9999]])
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("QDRANT_URL", "http://127.0.0.1:6333")
    monkeypatch.setenv("QDRANT_COLLECTION", collection)
    monkeypatch.setenv("EMBEDDING_PROVIDER", "hashing")
    monkeypatch.setenv("RERANK_PROVIDER", "lexical")
    service = ExternalNutritionMCPService(project_root() / "data")
    try:
        await service.call_tool("search_meals", {"query": "午餐", "limit": 3})
        assert (await index.client.count(collection, exact=True)).count == 101
        assert not await index.client.retrieve(collection, ids=list(range(1, 19)))
        assert len(await index.client.retrieve(collection, ids=[1001, 1100, 9999])) == 3
        await service.close()
        await service.call_tool("get_nutrition", {"meal_id": 1001})
        assert (await index.client.count(collection, exact=True)).count == 101
    finally:
        await service.close()
        await index.client.delete_collection(collection)
        await index.close()
