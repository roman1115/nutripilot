from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from nutripilot.api.app import create_app
from nutripilot.application import build_container
from nutripilot.application.chat_runs import ChatRunService
from nutripilot.application.chat_service import ChatService, ThreadBusyError, ThreadNotFoundError
from nutripilot.application.chat_worker import ChatWorker
from nutripilot.application.history import ConversationMessage
from nutripilot.config import Settings
from nutripilot.infrastructure.chat_tasks import TRASH_RETENTION_SECONDS, ChatTaskStore
from nutripilot.infrastructure.history import InMemoryConversationStore, SQLiteConversationStore
from nutripilot.infrastructure.run_store import SQLiteRunStore
from nutripilot.model_gateway import FakeModelGateway


@pytest.mark.parametrize("persistent", [False, True])
def test_history_search_pagination_and_ownership(tmp_path: Path, persistent: bool) -> None:
    store = (
        SQLiteConversationStore(SQLiteRunStore(tmp_path / "history.sqlite"))
        if persistent
        else InMemoryConversationStore()
    )
    store.append(1, "a", ConversationMessage(id="1", role="user", content="午餐100%"))
    store.append(1, "a", ConversationMessage(id="2", role="assistant", content="答复"))
    store.append(2, "b", ConversationMessage(id="3", role="user", content="另一用户"))
    assert [item.thread_id for item in store.list_threads(1, "午餐", 30, 0)] == ["a"]
    assert not store.list_threads(2, "午餐", 30, 0)
    assert not store.list_threads(1, "_", 30, 0)
    assert len(store.list_threads(1, "%", 30, 0)) == 1
    assert not store.list_threads(1, "", 1, 1)
    assert [item.content for item in store.messages(1, "a", 1, 1)] == ["答复"]
    assert not store.messages(2, "a", 100, 0)
    with pytest.raises(PermissionError):
        store.append(2, "a", ConversationMessage(id="4", role="user", content="越权"))
    store.append(1, "a", ConversationMessage(id="1", role="user", content="不应重复"))
    assert len(store.messages(1, "a", 100, 0)) == 2


@pytest.mark.asyncio
async def test_history_survives_adapter_recreation(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite"
    container = build_container(settings=Settings(_env_file=None, run_store_path=str(path)))
    await container.chat_service.chat(user_id=1, thread_id="saved", message="推荐午餐")
    await container.chat_service.chat(
        user_id=1, thread_id="saved", message="糖拌西红柿有多少热量？"
    )
    recreated = ChatService(container.graph, SQLiteConversationStore(SQLiteRunStore(path)))
    messages = await recreated.messages(user_id=1, thread_id="saved")
    assert [item.role for item in messages] == ["user", "assistant", "user", "assistant"]
    assert "113.4" in messages[-1].content
    assert messages[1].response["recommendations"]


def test_history_api_is_user_scoped_and_restores_confirmation_messages() -> None:
    container = build_container(settings=Settings(_env_file=None))
    with TestClient(create_app(container)) as client:
        first = client.post(
            "/api/v1/chat",
            json={
                "user_id": 1,
                "thread_id": "plan-history",
                "message": "一周晚餐饮食计划",
            },
        )
        assert first.json()["requires_confirmation"]
        preview = first.json()["pending_action"]["meal_preview"]
        assert len(preview) == 7
        assert all(item["meal_name"] and item["calories"] is not None for item in preview)
        assert client.get("/api/v1/threads?q=晚餐").json()["items"][0]["message_count"] == 2
        assert (
            client.get(
                "/api/v1/threads",
                headers={"X-User-ID": "2"},
            ).json()["items"]
            == []
        )
        assert (
            client.get(
                "/api/v1/threads/plan-history/messages",
                headers={"X-User-ID": "2"},
            ).status_code
            == 404
        )
        result = client.post(
            "/api/v1/chat/plan-history/confirm",
            json={
                "user_id": 1,
                "approved": False,
            },
        )
        assert result.status_code == 200
        messages = client.get("/api/v1/threads/plan-history/messages").json()["items"]
        assert [item["role"] for item in messages] == ["user", "assistant", "system", "assistant"]
        assert "拒绝" in messages[2]["content"]


def test_history_api_moves_to_trash_and_restores_messages_and_checkpoint() -> None:
    container = build_container(settings=Settings(_env_file=None))
    with TestClient(create_app(container)) as client:
        created = client.post(
            "/api/v1/chat",
            json={"user_id": 1, "thread_id": "delete-history", "message": "推荐午餐"},
        )
        assert created.status_code == 200
        snapshot = client.get("/api/v1/threads/delete-history/snapshot").json()
        run = snapshot["run"]

        assert client.delete(
            "/api/v1/threads/delete-history", headers={"X-User-ID": "2"}
        ).status_code == 404
        deleted = client.delete("/api/v1/threads/delete-history")
        assert deleted.status_code == 204
        assert client.get("/api/v1/threads").json()["items"] == []
        assert client.get("/api/v1/threads/delete-history/messages").status_code == 404
        assert client.get("/api/v1/threads/delete-history/snapshot").status_code == 404
        assert client.get("/api/v1/threads/delete-history").status_code == 404
        assert client.get(f"/api/v1/runs/{run['id']}").status_code == 404
        assert client.get(f"/api/v1/runs/{run['id']}/events").status_code == 404
        assert client.post(
            "/api/v1/threads/delete-history/runs",
            json={"message": "继续", "request_id": uuid4().hex},
        ).status_code == 404

        trash = client.get("/api/v1/threads/trash").json()["items"]
        assert len(trash) == 1 and trash[0]["thread_id"] == "delete-history"
        assert trash[0]["deleted_at"] and trash[0]["expires_at"]
        assert client.delete("/api/v1/threads/delete-history").status_code == 204
        assert client.get("/api/v1/threads/trash").json()["items"] == trash
        assert not client.get(
            "/api/v1/threads/trash", headers={"X-User-ID": "2"}
        ).json()["items"]
        assert client.post(
            "/api/v1/threads/delete-history/restore", headers={"X-User-ID": "2"}
        ).status_code == 404
        assert client.post("/api/v1/threads/delete-history/restore").status_code == 204
        assert not client.get("/api/v1/threads/trash").json()["items"]
        assert client.get("/api/v1/threads/delete-history/snapshot").json() == snapshot
        assert client.get("/api/v1/threads").json()["items"][0]["message_count"] == 2
        continued = client.post(
            "/api/v1/chat",
            json={"user_id": 1, "thread_id": "delete-history", "message": "第一道怎么做？"},
        )
        assert continued.status_code == 200
        assert continued.json()["intent"] == "RECIPE_QUERY"
        assert len(client.get("/api/v1/threads/delete-history/messages").json()["items"]) == 4


def test_trash_search_pagination_expiry_and_redelete_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "trash.sqlite"
    store = ChatTaskStore.create(f"sqlite:///{path}")
    store.initialize()
    now = 1_800_000_000.0
    monkeypatch.setattr(
        "nutripilot.infrastructure.chat_tasks.time", SimpleNamespace(time=lambda: now)
    )
    for thread_id in ("a", "b", "c"):
        store.append(1, thread_id, ConversationMessage(
            id=thread_id, role="user", content=f"午餐100% {thread_id}"
        ))
        store.delete_thread(1, thread_id)
        now += 1
    assert [item.thread_id for item in store.list_threads(1, "%", 1, 1, trashed=True)] == ["b"]
    assert not store.list_threads(1, "_", 30, 0, trashed=True)
    assert not store.list_threads(2, "", 30, 0, trashed=True)
    store.close()
    store = ChatTaskStore.create(f"sqlite:///{path}")
    store.initialize()
    original = store.list_threads(1, "", 30, 0, trashed=True)
    now = 1_800_000_000.0 + TRASH_RETENTION_SECONDS - 1
    store.delete_thread(1, "a")
    assert store.list_threads(1, "100%", 30, 0, trashed=True) == original
    assert not store.claim_expired_threads()
    now += 1
    with pytest.raises(ThreadNotFoundError):
        store.restore_thread(1, "a")
    assert [item.thread_id for item in store.list_threads(1, "", 30, 0, trashed=True)] == ["c", "b"]
    assert store.claim_expired_threads() == ["a"]
    store.purge_thread("b")
    store.restore_thread(1, "b")
    now += TRASH_RETENTION_SECONDS
    store.delete_thread(1, "b")
    restored = store.list_threads(1, "", 30, 0, trashed=True)
    assert len(restored) == 1 and restored[0].deleted_at.timestamp() == now
    store.close()


@pytest.mark.asyncio
async def test_expired_trash_purge_retries_checkpoint_failure_and_clears_all_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = build_container(settings=Settings(_env_file=None))
    store = ChatTaskStore.create()
    store.initialize()
    container.chat_service.history = store
    service = ChatRunService(container.chat_service, store)
    worker = ChatWorker(container.chat_service, store, container.settings)
    task = asyncio.create_task(worker.run())
    await worker.ready.wait()
    try:
        run = await service.submit(
            user_id=1, thread_id="expired", message="推荐午餐", request_key="expired-key"
        )
        await service.wait(run, 1)
        await service.delete_thread(1, "expired")
        assert await container.graph.aget_state(container.chat_service._config("expired"))
        with pytest.raises(ThreadNotFoundError):
            await service.submit(
                user_id=1, thread_id="expired", message="推荐午餐", request_key="expired-key"
            )
        with store.transaction() as connection:
            connection.execute(text(
                "UPDATE chat_thread_trash SET deleted_at=:expired WHERE thread_id='expired'"
            ), {"expired": time.time() - TRASH_RETENTION_SECONDS - 1})
        checkpointer = container.graph.compiled.checkpointer
        delete_checkpoint = checkpointer.adelete_thread

        async def unavailable(thread_id: str) -> None:
            raise OSError("checkpoint unavailable")

        monkeypatch.setattr(checkpointer, "adelete_thread", unavailable)
        await service.purge_expired_threads()
        with store.transaction() as connection:
            assert connection.execute(text("SELECT COUNT(*) FROM chat_messages")).scalar() == 2
        with pytest.raises(ThreadNotFoundError):
            store.restore_thread(1, "expired")
        monkeypatch.setattr(checkpointer, "adelete_thread", delete_checkpoint)
        await service.purge_expired_threads()
        assert not await container.graph.aget_state(container.chat_service._config("expired"))
        with pytest.raises(ThreadNotFoundError):
            await service.submit(
                user_id=1, thread_id="expired", message="继续", request_key="reuse-purged-id"
            )
        with store.transaction() as connection:
            for table in ("chat_messages", "chat_runs", "chat_events", "chat_thread_trash",
                          "chat_threads"):
                assert connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() == 0
    finally:
        await worker.close()
        await task
        store.close()


@pytest.mark.asyncio
async def test_worker_cleans_expired_trash_on_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    container = build_container(settings=Settings(_env_file=None))
    store = ChatTaskStore.create()
    store.initialize()
    container.chat_service.history = store
    await container.chat_service.chat(user_id=1, thread_id="old-trash", message="你好")
    store.delete_thread(1, "old-trash")
    with store.transaction() as connection:
        connection.execute(text(
            "UPDATE chat_thread_trash SET deleted_at=:expired"
        ), {"expired": time.time() - TRASH_RETENTION_SECONDS - 1})
    worker = ChatWorker(container.chat_service, store, container.settings)
    cleaned = asyncio.Event()
    loop = asyncio.get_running_loop()
    purge = store.purge_thread

    def observe_purge(thread_id: str) -> None:
        purge(thread_id)
        loop.call_soon_threadsafe(cleaned.set)

    monkeypatch.setattr(store, "purge_thread", observe_purge)
    task = asyncio.create_task(worker.run())
    try:
        await asyncio.wait_for(cleaned.wait(), 5)
        assert not await container.graph.aget_state(container.chat_service._config("old-trash"))
    finally:
        await worker.close()
        await task
        store.close()


@pytest.mark.asyncio
async def test_trashing_finished_run_before_worker_returns_keeps_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = build_container(settings=Settings(_env_file=None))
    store = ChatTaskStore.create()
    store.initialize()
    container.chat_service.history = store
    service = ChatRunService(container.chat_service, store)
    worker = ChatWorker(container.chat_service, store, container.settings)
    finished = asyncio.Event()
    release = asyncio.Event()
    finish = worker._finish

    async def pause_after_finish(row):  # type: ignore[no-untyped-def]
        await finish(row)
        if row["thread_id"] == "finishing":
            finished.set()
            await release.wait()

    monkeypatch.setattr(worker, "_finish", pause_after_finish)
    task = asyncio.create_task(worker.run())
    await worker.ready.wait()
    try:
        await service.submit(
            user_id=1, thread_id="finishing", message="你好", request_key="finishing"
        )
        await asyncio.wait_for(finished.wait(), 5)
        await service.delete_thread(1, "finishing")
        next_run = await service.submit(
            user_id=1, thread_id="next", message="你好", request_key="next"
        )
        result = await asyncio.wait_for(service.wait(next_run, 1), 5)
        assert result["phase"] == "COMPLETED"
        assert not task.done()
    finally:
        release.set()
        await worker.close()
        await task
        store.close()


def test_legacy_import_does_not_resurrect_trashed_or_purged_history(tmp_path: Path) -> None:
    source = SQLiteRunStore(tmp_path / "legacy.sqlite")
    legacy = SQLiteConversationStore(source)
    legacy.append(1, "legacy", ConversationMessage(id="legacy", role="user", content="旧消息"))
    store = ChatTaskStore.create()
    store.initialize()
    try:
        store.import_history(source)
        store.delete_thread(1, "legacy")
        store.import_history(source)
        assert len(store.list_threads(1, "", 30, 0, trashed=True)) == 1
        with store.transaction() as connection:
            connection.execute(text(
                "UPDATE chat_thread_trash SET deleted_at=:expired"
            ), {"expired": time.time() - TRASH_RETENTION_SECONDS - 1})
        assert store.claim_expired_threads() == ["legacy"]
        store.purge_thread("legacy")
        store.import_history(source)
        assert not store.list_threads(1, "", 30, 0)
        assert not store.list_threads(1, "", 30, 0, trashed=True)
    finally:
        store.close()


def test_active_thread_cannot_be_deleted(tmp_path: Path) -> None:
    store = ChatTaskStore.create(f"sqlite:///{tmp_path / 'tasks.sqlite'}")
    store.initialize()
    store.submit(
        user_id=1,
        thread_id="active-thread",
        request_key="request",
        request_hash="hash",
        state={"run_id": "run", "turn_id": "turn", "trace_id": "trace"},
        kind="chat",
        message=ConversationMessage(id="message", role="user", content="正在执行"),
        max_queued=5,
    )
    with pytest.raises(ThreadBusyError):
        store.delete_thread(1, "active-thread")
    assert store.bundle("active-thread", 1)["messages"]
    store.close()


def test_access_token_identity_cannot_read_another_users_history() -> None:
    first_token = "a" * 40
    second_token = "b" * 40
    container = build_container(
        settings=Settings(
            _env_file=None,
            api_access_tokens={first_token: 1, second_token: 2},
        )
    )
    with TestClient(create_app(container)) as client:
        assert (
            client.post(
                "/api/v1/chat",
                headers={
                    "Authorization": f"Bearer {first_token}",
                },
                json={
                    "user_id": 1,
                    "thread_id": "authenticated-history",
                    "message": "推荐午餐",
                },
            ).status_code
            == 200
        )
        headers = {"Authorization": f"Bearer {second_token}", "X-User-ID": "1"}
        assert not client.get("/api/v1/threads", headers=headers).json()["items"]
        assert (
            client.get(
                "/api/v1/threads/authenticated-history/messages",
                headers=headers,
            ).status_code
            == 404
        )


@pytest.mark.asyncio
async def test_cancelled_turn_has_terminal_checkpoint_and_history_then_can_continue() -> None:
    class SlowGateway(FakeModelGateway):
        async def generate_structured(self, **kwargs):  # type: ignore[no-untyped-def]
            if kwargs["worker"] == "intent" and not self.call_history:
                await asyncio.sleep(30)
            return await super().generate_structured(**kwargs)

    gateway = SlowGateway()
    container = build_container(gateway=gateway, settings=Settings(_env_file=None))
    task = asyncio.create_task(
        container.chat_service.chat(
            user_id=1,
            thread_id="cancel-history",
            message="推荐午餐",
        )
    )
    await asyncio.sleep(0.05)
    assert container.chat_service.cancel(thread_id="cancel-history", user_id=1)
    with pytest.raises(asyncio.CancelledError):
        await task
    state = await container.chat_service.get_thread(thread_id="cancel-history", user_id=1)
    assert state["phase"] == "FAILED"
    assert state["error"]["code"] == "CANCELLED"
    messages = await container.chat_service.messages(user_id=1, thread_id="cancel-history")
    assert len(messages) == 2
    assert "已停止" in messages[-1].content
    gateway.call_history.append({"worker": "cancelled"})
    resumed = await container.chat_service.chat(
        user_id=1,
        thread_id="cancel-history",
        message="推荐晚餐",
    )
    assert resumed["phase"] == "COMPLETED"
