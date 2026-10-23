from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nutripilot.api.app import create_app
from nutripilot.application import build_container
from nutripilot.application.chat_service import ChatService
from nutripilot.application.history import ConversationMessage
from nutripilot.config import Settings
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
        user_id=1, thread_id="saved", message="鸡胸肉藜麦能量碗有多少热量？"
    )
    recreated = ChatService(container.graph, SQLiteConversationStore(SQLiteRunStore(path)))
    messages = await recreated.messages(user_id=1, thread_id="saved")
    assert [item.role for item in messages] == ["user", "assistant", "user", "assistant"]
    assert "516" in messages[-1].content
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
