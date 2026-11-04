from argparse import Namespace
from unittest.mock import Mock

import httpx
import pytest
from scripts import demo_launcher
from scripts.verify_persistence import snapshot


@pytest.fixture(autouse=True)
def isolate_launcher_environment(monkeypatch) -> None:
    monkeypatch.setattr(demo_launcher.os, "environ", dict(demo_launcher.os.environ))


@pytest.mark.parametrize("mode", ["persistent", "real"])
def test_external_launcher_configures_loopback_and_separate_indexes(monkeypatch, mode) -> None:
    monkeypatch.setenv("MODEL_PROVIDER", "fake")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://local:local@postgres:5432/nutripilot")
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("QDRANT_URL", "http://qdrant:6333")
    monkeypatch.setattr(demo_launcher, "_check_port", lambda _: None)
    run = Mock()
    monkeypatch.setattr(demo_launcher.subprocess, "run", run)
    args = Namespace(
        mode=mode, mcp_port=8011, api_port=8005, web_port=5177, no_infrastructure=False
    )

    demo_launcher._configure_infrastructure(args)

    assert demo_launcher.os.environ["APP_MODE"] == "docker"
    assert demo_launcher.os.environ["MODEL_PROVIDER"] == "fake"
    assert demo_launcher.os.environ["DATABASE_URL"].endswith("@127.0.0.1:5432/nutripilot")
    assert demo_launcher.os.environ["REDIS_URL"] == "redis://127.0.0.1:6379/0"
    assert demo_launcher.os.environ["QDRANT_URL"] == "http://127.0.0.1:6333"
    assert demo_launcher.os.environ["QDRANT_COLLECTION"] == (
        "meal_search_bge_m3" if mode == "real" else "meal_search"
    )
    assert len(demo_launcher.os.environ["MCP_INTERNAL_TOKEN"]) >= 32
    assert run.call_args_list[0].args[0][:3] == ["docker", "compose", "up"]
    assert run.call_args_list[1].args[0][-3:] == ["alembic", "upgrade", "head"]


def test_no_infrastructure_still_runs_migrations(monkeypatch) -> None:
    monkeypatch.setenv("MODEL_PROVIDER", "fake")
    monkeypatch.setattr(demo_launcher, "_check_port", lambda _: None)
    run = Mock()
    monkeypatch.setattr(demo_launcher.subprocess, "run", run)
    demo_launcher._configure_infrastructure(
        Namespace(
            mode="persistent",
            mcp_port=8011,
            api_port=8005,
            web_port=5177,
            no_infrastructure=True,
        )
    )
    assert run.call_count == 1
    assert run.call_args.args[0][-3:] == ["alembic", "upgrade", "head"]


@pytest.mark.parametrize("failure", [None, "missing_plans", "wrong_user"])
def test_persistence_snapshot_requires_saved_plan_and_matching_profile(failure) -> None:
    values = {
        "/api/v1/threads": {"items": [{"thread_id": "persisted"}]},
        "/api/v1/threads/persisted/messages": {"items": [{"content": "hello"}]},
        "/api/v1/threads/persisted": {"phase": "COMPLETED"},
        "/api/v1/meal-plans": [] if failure == "missing_plans" else [{"id": "plan"}],
        "/api/v1/profile": {"user_id": 2 if failure == "wrong_user" else 1},
    }
    with httpx.Client(
        base_url="http://test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=values[request.url.path])
        ),
    ) as client:
        if failure:
            with pytest.raises(AssertionError):
                snapshot(client, 1)
        else:
            assert snapshot(client, 1)["profile"] == {"user_id": 1}
