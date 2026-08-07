from fastapi.testclient import TestClient

from nutripilot.api.app import create_app


def test_api_factory_runs_without_model_credentials() -> None:
    with TestClient(create_app()) as client:
        assert client.get("/health/live").json() == {"status": "ok"}

        response = client.post(
            "/api/v1/chat",
            json={"user_id": 1, "thread_id": "api-flow", "message": "推荐清淡晚餐"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["phase"] == "COMPLETED"
        assert body["final_response"]["recommendations"]

        trace = client.get(f"/api/v1/traces/{body['trace_id']}")
        assert trace.status_code == 200
        assert trace.json()["events"]


def test_sse_endpoint_returns_named_completion_event() -> None:
    with (
        TestClient(create_app()) as client,
        client.stream(
            "POST",
            "/api/v1/chat/stream",
            json={"user_id": 1, "message": "午餐推荐减脂餐"},
        ) as response,
    ):
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert "event: status" in body
    assert "event: progress" in body
    assert '"component": "intent"' in body
    assert '"component": "planner"' in body
    assert '"component": "tool"' in body
    assert "event: completed" in body


def test_thread_state_is_not_shared_between_users() -> None:
    with TestClient(create_app()) as client:
        first = client.post(
            "/api/v1/chat",
            json={"user_id": 1, "thread_id": "private-thread", "message": "推荐清淡晚餐"},
        )
        second = client.post(
            "/api/v1/chat",
            json={"user_id": 2, "thread_id": "private-thread", "message": "推荐午餐"},
        )

    assert first.status_code == 200
    assert second.status_code == 404


def test_localhost_and_loopback_frontends_are_allowed_by_cors() -> None:
    with TestClient(create_app()) as client:
        for origin in ("http://localhost:5173", "http://127.0.0.1:5173"):
            response = client.get("/health/ready", headers={"Origin": origin})
            assert response.headers["access-control-allow-origin"] == origin


def test_readiness_reports_the_actual_demo_runtime_components() -> None:
    with TestClient(create_app()) as client:
        body = client.get("/health/ready").json()

    assert body == {
        "status": "ready",
        "app_mode": "demo",
        "model_provider": "fake",
        "model_name": "fake-nutripilot-v1",
        "embedding_provider": "hashing",
        "embedding_model": "BAAI/bge-m3",
        "rerank_provider": "lexical",
        "rerank_model": "BAAI/bge-reranker-v2-m3",
        "storage": "memory",
        "mcp_transport": "in-process",
    }
