from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import httpx
import pytest

from nutripilot.api.app import create_app
from nutripilot.application import build_container
from nutripilot.application.chat_runs import ChatRunService
from nutripilot.application.chat_worker import ChatWorker
from nutripilot.application.runtime import _postgres_checkpointer
from nutripilot.config import Settings
from nutripilot.domain.execution import ExecutionOwnershipLost
from nutripilot.infrastructure.chat_tasks import ChatTaskStore
from nutripilot.infrastructure.history import SQLiteConversationStore
from nutripilot.infrastructure.run_store import SQLiteRunStore
from nutripilot.model_gateway import FakeModelGateway


class GatedGateway(FakeModelGateway):
    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.started = 0
        self.intent_calls = 0
        self.block_worker = "intent"

    async def generate_structured(self, **kwargs: Any) -> Any:
        if kwargs["worker"] == "intent":
            self.intent_calls += 1
        if kwargs["worker"] == self.block_worker:
            self.started += 1
            await self.gate.wait()
        return await super().generate_structured(**kwargs)


async def until(predicate: Any, seconds: float = 10) -> Any:
    async with asyncio.timeout(seconds):
        while True:
            result = predicate()
            if asyncio.iscoroutine(result):
                result = await result
            if result:
                return result
            await asyncio.sleep(0.02)


async def finished(service: ChatRunService, run: dict[str, Any], user: int = 1) -> dict[str, Any]:
    async def read() -> Any:
        current = await asyncio.to_thread(service.store.get, run["id"], user)
        return current if current["status"] not in ("queued", "running", "cancelling") else None

    return await until(read)


@asynccontextmanager
async def runtime(gateway: FakeModelGateway | None = None, **overrides: Any) -> Any:
    settings = Settings(_env_file=None, **overrides)
    container = build_container(settings=settings, gateway=gateway)
    app = create_app(container)
    async with app.router.lifespan_context(app):
        assert container.chat_runs
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            yield container, client


@pytest.mark.asyncio
async def test_three_conversations_run_concurrently_and_fourth_queues() -> None:
    gateway = GatedGateway()
    async with runtime(gateway) as (container, client):
        service = container.chat_runs
        runs = [
            await service.submit(
                user_id=1, thread_id=uuid4().hex, message="推荐晚餐", request_key=uuid4().hex
            )
            for _ in range(4)
        ]
        await until(lambda: gateway.started == 3)
        assert service.store.get(runs[3]["id"], 1)["status"] == "queued"
        fifth = await service.submit(
            user_id=2, thread_id=uuid4().hex, message="推荐午餐", request_key=uuid4().hex
        )
        await until(lambda: gateway.started == 4)
        assert sum(row[0].done() is False for row in container.chat_worker.active.values()) == 4
        gateway.gate.set()
        for run in runs:
            assert (await finished(service, run))["status"] == "completed"
        assert (await finished(service, fifth, 2))["status"] == "completed"
        snapshot = (await client.get(f"/api/v1/threads/{runs[0]['thread_id']}/snapshot")).json()
        assert len(snapshot["messages"]) == 2
        assert snapshot["run"]["status"] == "completed"


@pytest.mark.asyncio
async def test_request_idempotency_busy_ownership_and_event_replay() -> None:
    gateway = GatedGateway()
    async with runtime(gateway) as (container, client):
        thread, key = uuid4().hex, uuid4().hex
        path = f"/api/v1/threads/{thread}/runs"
        payload = {"message": "推荐晚餐", "request_id": key}
        first, second = await asyncio.gather(
            client.post(path, json=payload), client.post(path, json=payload)
        )
        assert first.status_code == second.status_code == 202
        run = first.json()
        assert second.json()["id"] == run["id"]
        assert (await client.post(path, json={**payload, "message": "推荐早餐"})).status_code == 409
        assert (
            await client.post(path, json={**payload, "request_id": uuid4().hex})
        ).status_code == 409
        assert (
            await client.get(f"/api/v1/runs/{run['id']}", headers={"X-User-ID": "2"})
        ).status_code == 404
        assert (
            await client.post(f"/api/v1/runs/{run['id']}/cancel", headers={"X-User-ID": "2"})
        ).status_code == 404
        # Closing a subscriber must not affect the independent execution.
        assert (
            await client.post(
                path, json={**payload, "request_id": uuid4().hex}, headers={"X-User-ID": "2"}
            )
        ).status_code == 404
        subscription = container.chat_runs.subscribe(run["id"], 1)
        event = await anext(subscription)
        await subscription.aclose()
        await until(lambda: gateway.started == 1)
        assert container.chat_runs.store.get(run["id"], 1)["status"] == "running"
        gateway.gate.set()
        done = await finished(container.chat_runs, run)
        replay = await client.get(f"/api/v1/runs/{run['id']}/events?after={event['id']}")
        assert "event: completed" in replay.text
        assert f"id: {event['id']}\n" not in replay.text
        assert done["last_event"] > event["id"]
        assert len(container.chat_runs.store.messages(1, thread, 100, 0)) == 2


@pytest.mark.asyncio
async def test_cancel_is_persisted_and_only_stops_requested_conversation() -> None:
    gateway = GatedGateway()
    async with runtime(gateway) as (container, client):
        service = container.chat_runs
        first, second = [
            await service.submit(
                user_id=1, thread_id=uuid4().hex, message="推荐午餐", request_key=uuid4().hex
            )
            for _ in range(2)
        ]
        await until(lambda: gateway.started == 2)
        response = await client.post(f"/api/v1/runs/{first['id']}/cancel")
        assert response.json()["status"] == "cancelling"
        assert (await finished(service, first))["status"] == "cancelled"
        assert service.store.get(second["id"], 1)["status"] == "running"
        gateway.gate.set()
        assert (await finished(service, second))["status"] == "completed"
        assert (await client.post(f"/api/v1/runs/{first['id']}/cancel")).json()[
            "status"
        ] == "cancelled"


@pytest.mark.asyncio
async def test_worker_shutdown_resumes_checkpoint_without_repeating_completed_intent() -> None:
    gateway = GatedGateway()
    gateway.block_worker = "planner"
    async with runtime(gateway) as (container, _client):
        service = container.chat_runs
        run = await service.submit(
            user_id=1, thread_id=uuid4().hex, message="推荐午餐", request_key=uuid4().hex
        )
        await until(lambda: gateway.started == 1)
        await container.chat_worker.close()
        await container.chat_worker_task
        assert service.store.get(run["id"], 1)["status"] == "running"
        assert gateway.intent_calls == 1
        gateway.gate.set()
        worker = ChatWorker(container.chat_service, service.store, container.settings)
        container.chat_worker = worker
        container.chat_worker_task = asyncio.create_task(worker.run())
        await worker.ready.wait()
        done = await finished(service, run)
        assert done["status"] == "completed"
        assert done["attempts"] == 2
        assert gateway.intent_calls == 1
        assert len(service.store.messages(1, run["thread_id"], 100, 0)) == 2


@pytest.mark.asyncio
async def test_confirmation_replay_and_stale_preview_cannot_write_twice() -> None:
    async with runtime() as (container, client):
        service = container.chat_runs
        thread = uuid4().hex
        run = await service.submit(
            user_id=1, thread_id=thread, message="制定明天的一日三餐餐单", request_key=uuid4().hex
        )
        pending = await finished(service, run)
        assert pending["status"] == "awaiting_confirmation"
        payload = {
            "approved": True,
            "confirmation_id": pending["result"]["pending_action"]["id"],
            "request_id": uuid4().hex,
        }
        path = f"/api/v1/threads/{thread}/confirm"
        first = await client.post(path, json=payload)
        assert first.status_code == 202, first.text
        await finished(service, first.json())
        replay = await client.post(path, json=payload)
        assert replay.json()["id"] == first.json()["id"]
        assert (
            await client.post(path, json={**payload, "request_id": uuid4().hex})
        ).status_code == 409
        assert len(await container.data_store.list_plans(1)) == 1


def test_legacy_import_preserves_identity_content_and_is_repeatable(tmp_path: Any) -> None:
    from nutripilot.application.history import ConversationMessage

    source = SQLiteRunStore(tmp_path / "legacy.sqlite")
    legacy = SQLiteConversationStore(source)
    for user in (1, 2):
        legacy.append(
            user,
            f"thread-{user}",
            ConversationMessage(id=f"message-{user}", role="user", content=f"消息 {user}"),
        )
    store = ChatTaskStore.create()
    try:
        store.initialize()
        for _ in range(2):
            assert store.import_history(source) == {"threads": 2, "messages": 2}
        assert store.messages(1, "thread-1", 100, 0) == legacy.messages(1, "thread-1", 100, 0)
        with pytest.raises(LookupError):
            store.messages(2, "thread-1", 100, 0)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_queue_bound_and_obsolete_owner_fence() -> None:
    gateway = GatedGateway()
    async with runtime(gateway, chat_max_queued=1) as (container, client):
        first = await client.post(
            "/api/v1/threads/one/runs", json={"message": "推荐午餐", "request_id": "one"}
        )
        assert first.status_code == 202
        assert (
            await client.post(
                "/api/v1/threads/two/runs", json={"message": "推荐晚餐", "request_id": "two"}
            )
        ).status_code == 429
        await until(lambda: gateway.started == 1)
        store = container.chat_runs.store
        with pytest.raises(ExecutionOwnershipLost), store.fence(first.json()["id"], 999):
            pytest.fail("Obsolete owner acquired the fence")
        gateway.gate.set()
        await finished(container.chat_runs, first.json())


@pytest.mark.asyncio
async def test_cancel_queued_run_creates_terminal_checkpoint_without_model_calls() -> None:
    gateway = GatedGateway()
    async with runtime(gateway, chat_concurrency=1, chat_user_concurrency=1) as (container, _):
        service = container.chat_runs
        running, queued = [
            await service.submit(
                user_id=1, thread_id=uuid4().hex, message="推荐午餐", request_key=uuid4().hex
            )
            for _ in range(2)
        ]
        await until(lambda: gateway.started == 1)
        service.store.cancel(queued["id"], 1)
        gateway.gate.set()
        await finished(service, running)
        stopped = await finished(service, queued)
        assert stopped["status"] == "cancelled"
        assert gateway.intent_calls == 1
        assert stopped["result"]["phase"] == "FAILED"
        assert len(service.store.messages(1, queued["thread_id"], 100, 0)) == 2


@pytest.mark.asyncio
async def test_evaluation_uses_the_same_queue_as_interactive_chats() -> None:
    from nutripilot.evaluation.models import EvalCase

    gateway = GatedGateway()
    async with runtime(gateway, chat_concurrency=1, chat_user_concurrency=1) as (container, _):
        service = container.chat_runs
        run = await service.submit(
            user_id=1, thread_id=uuid4().hex, message="推荐午餐", request_key=uuid4().hex
        )
        await until(lambda: gateway.started == 1)
        report_task = asyncio.create_task(
            container.evaluation_service.run(
                [
                    EvalCase(
                        id="durable", user_message="推荐晚餐", expected_intent="MEAL_RECOMMENDATION"
                    )
                ],
                dataset_version="durable-test",
            )
        )
        await until(lambda: len(service.store.list_threads(1, "", 100, 0)) == 2)
        assert gateway.started == 1
        gateway.gate.set()
        await finished(service, run)
        report = await report_task
        assert report.total_cases == 1
        assert gateway.started > 1


@pytest.mark.asyncio
async def test_durable_deadline_and_token_budget_are_terminal_not_fallbacks() -> None:
    async with runtime(GatedGateway(), agent_deadline_seconds=0.2) as (container, _):
        run = await container.chat_runs.submit(
            user_id=1, thread_id=uuid4().hex, message="推荐午餐", request_key=uuid4().hex
        )
        assert (await finished(container.chat_runs, run))["error"] == "DEADLINE_EXCEEDED"
    async with runtime(agent_token_budget=1) as (container, _):
        run = await container.chat_runs.submit(
            user_id=1, thread_id=uuid4().hex, message="推荐午餐", request_key=uuid4().hex
        )
        assert (await finished(container.chat_runs, run))["error"] == "TOKEN_BUDGET_EXCEEDED"


@pytest.mark.asyncio
async def test_wait_returns_its_own_result_after_thread_has_moved_to_next_turn() -> None:
    async with runtime() as (container, _):
        service = container.chat_runs
        thread = uuid4().hex
        first = await service.submit(
            user_id=1, thread_id=thread, message="推荐午餐", request_key=uuid4().hex
        )
        await finished(service, first)
        second = await service.submit(
            user_id=1, thread_id=thread, message="糖拌西红柿有多少热量", request_key=uuid4().hex
        )
        await finished(service, second)
        result = await service.wait(first, 1)
        assert result["run_id"] == first["id"]
        assert result["turn_id"] == first["turn_id"]
        assert result["user_message"] == "推荐午餐"


@pytest.mark.asyncio
async def test_completed_checkpoint_is_finalized_even_after_recovery_limit() -> None:
    async with runtime(chat_max_attempts=1) as (container, _):
        service = container.chat_runs
        blocked = asyncio.Event()

        async def delay_finish(_row: dict[str, Any]) -> None:
            blocked.set()
            await asyncio.Event().wait()

        container.chat_worker._finish = delay_finish
        run = await service.submit(
            user_id=1, thread_id=uuid4().hex, message="推荐午餐", request_key=uuid4().hex
        )
        await asyncio.wait_for(blocked.wait(), 10)
        await container.chat_worker.close()
        await container.chat_worker_task
        worker = ChatWorker(container.chat_service, service.store, container.settings)
        container.chat_worker = worker
        container.chat_worker_task = asyncio.create_task(worker.run())
        await asyncio.wait_for(worker.ready.wait(), 5)
        result = await finished(service, run)
        assert result["status"] == "completed"
        assert result["attempts"] == 2
        assert len(service.store.messages(1, run["thread_id"], 100, 0)) == 2


@pytest.mark.asyncio
async def test_imported_messages_restore_without_a_graph_checkpoint() -> None:
    from nutripilot.application.history import ConversationMessage

    async with runtime() as (container, client):
        thread = uuid4().hex
        container.chat_runs.store.append(
            1, thread, ConversationMessage(id=uuid4().hex, role="user", content="迁移前的消息")
        )
        snapshot = await client.get(f"/api/v1/threads/{thread}/snapshot")
        assert snapshot.status_code == 200
        assert snapshot.json()["messages"][0]["content"] == "迁移前的消息"
        assert snapshot.json()["run"] is None


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("NUTRIPILOT_TEST_EXTERNAL") != "1", reason="Local PostgreSQL required"
)
async def test_postgres_queue_single_worker_fence_and_process_recreation() -> None:
    from nutripilot.agent import create_agent_graph
    from nutripilot.application.chat_service import ChatService

    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://nutripilot:nutripilot@127.0.0.1:5432/nutripilot",
        chat_queue=f"test-{uuid4().hex}",
    )
    gateway = GatedGateway()
    gateway.block_worker = "planner"
    container = build_container(gateway=gateway, settings=settings)
    store = ChatTaskStore.create(settings.database_url, settings.chat_queue)
    store.initialize()
    async with _postgres_checkpointer(container) as saver:
        await saver.setup()

        def build_chat() -> Any:
            graph = create_agent_graph(
                gateway=gateway,
                tool_executor=container.tool_executor,
                recorder=container.recorder,
                checkpointer=saver,
            )
            return ChatService(graph, store)

        chat = build_chat()
        service = ChatRunService(chat, store)
        worker = ChatWorker(chat, store, settings)
        task = asyncio.create_task(worker.run())
        try:
            await worker.ready.wait()
            assert store.worker_lock() is None
            run = await service.submit(
                user_id=1, thread_id=uuid4().hex, message="推荐午餐", request_key=uuid4().hex
            )
            await until(lambda: gateway.started == 1)
            await worker.close()
            await task
            recreated_chat = build_chat()
            worker = ChatWorker(recreated_chat, store, settings)
            task = asyncio.create_task(worker.run())
            await worker.ready.wait()
            gateway.gate.set()
            done = await finished(service, run)
            assert done["status"] == "completed"
            assert done["attempts"] == 2
            assert gateway.intent_calls == 1
        finally:
            await worker.close()
            await task
            store.close()


@pytest.mark.asyncio
async def test_final_archive_retry_keeps_success_and_does_not_regenerate(monkeypatch) -> None:
    from sqlalchemy.exc import OperationalError

    async with runtime() as (container, _):
        service = container.chat_runs
        finish = service.store.finish
        attempts = 0

        def transient_failure(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OperationalError("commit", {}, RuntimeError("temporary outage"))
            return finish(*args, **kwargs)

        monkeypatch.setattr(service.store, "finish", transient_failure)
        run = await service.submit(
            user_id=1, thread_id=uuid4().hex, message="你好", request_key=uuid4().hex,
        )
        done = await finished(service, run)
        assert done["status"] == "completed"
        assert attempts == 2
        state = service.store.final_state(run["id"], 1)
        assert state["memory_finalized_run_id"] == run["id"]
        assert all(isinstance(message, dict) for message in state["messages"])
        assert len(service.store.messages(1, run["thread_id"], 10, 0)) == 2
        calls = container.gateway.call_history
        assert [call["worker"] for call in calls] == ["intent", "recommendation"]
        events = service.store.events(run["id"], 1, 0)
        assert sum(event["type"] == "completed" for event in events) == 1
