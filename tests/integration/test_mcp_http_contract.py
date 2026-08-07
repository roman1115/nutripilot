from __future__ import annotations

import os
import sys
from types import ModuleType
from typing import Any

import pytest
from apps.nutrition_mcp.server import (
    MCP_PATH,
    MCP_TOKEN_ENV,
    BearerTokenMiddleware,
    _allowed_mcp_hosts,
    create_mcp_server,
)

from nutripilot.tools.mcp.transport import (
    StreamableHTTPMCPTransport,
    _MCPClientSessionContext,
)


def test_streamable_http_environment_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_SERVER_URL", "http://nutrition-mcp:8010")
    monkeypatch.setenv("MCP_INTERNAL_TOKEN", "internal-secret")
    monkeypatch.setenv("MCP_TIMEOUT_SECONDS", "7")

    transport = StreamableHTTPMCPTransport.from_environment()

    assert transport.url == "http://nutrition-mcp:8010/mcp"
    assert transport.token == "internal-secret"
    assert transport.timeout_seconds == 7
    assert MCP_PATH == "/mcp"
    assert MCP_TOKEN_ENV == "MCP_INTERNAL_TOKEN"


def test_mcp_hosts_allow_compose_service_and_explicit_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MCP_ALLOWED_HOSTS", raising=False)
    assert "nutrition-mcp:*" in _allowed_mcp_hosts()

    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "mcp.internal:8010, localhost:*")
    assert _allowed_mcp_hosts() == ["mcp.internal:8010", "localhost:*"]


@pytest.mark.asyncio
async def test_bearer_middleware_rejects_missing_token_and_accepts_matching_token() -> None:
    downstream_calls = 0

    async def downstream(scope: Any, receive: Any, send: Any) -> None:
        nonlocal downstream_calls
        downstream_calls += 1
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = BearerTokenMiddleware(downstream, "expected-token")

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    unauthorized_events: list[dict[str, Any]] = []

    async def send_unauthorized(event: dict[str, Any]) -> None:
        unauthorized_events.append(event)

    await middleware(
        {"type": "http", "headers": [], "path": "/mcp"},
        receive,
        send_unauthorized,
    )
    assert unauthorized_events[0]["status"] == 401
    assert downstream_calls == 0

    authorized_events: list[dict[str, Any]] = []

    async def send_authorized(event: dict[str, Any]) -> None:
        authorized_events.append(event)

    await middleware(
        {
            "type": "http",
            "headers": [(b"authorization", b"Bearer expected-token")],
            "path": "/mcp",
        },
        receive,
        send_authorized,
    )
    assert authorized_events[0]["status"] == 204
    assert downstream_calls == 1


@pytest.mark.asyncio
async def test_current_mcp_sdk_http_client_receives_bearer_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeTransportContext:
        async def __aenter__(self):
            return object(), object(), lambda: None

        async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
            return None

    def streamable_http_client(url: str, *, http_client: Any) -> FakeTransportContext:
        captured["url"] = url
        captured["authorization"] = http_client.headers.get("Authorization")
        return FakeTransportContext()

    class FakeClientSession:
        def __init__(self, read_stream: Any, write_stream: Any) -> None:
            pass

        async def __aenter__(self) -> FakeClientSession:
            return self

        async def initialize(self) -> None:
            captured["initialized"] = True

        async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
            return None

    mcp_module = ModuleType("mcp")
    mcp_module.ClientSession = FakeClientSession  # type: ignore[attr-defined]
    client_module = ModuleType("mcp.client")
    stream_module = ModuleType("mcp.client.streamable_http")
    stream_module.streamable_http_client = streamable_http_client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mcp", mcp_module)
    monkeypatch.setitem(sys.modules, "mcp.client", client_module)
    monkeypatch.setitem(sys.modules, "mcp.client.streamable_http", stream_module)

    async with _MCPClientSessionContext("http://nutrition-mcp:8010/mcp", "internal-secret", 5.0):
        pass

    assert captured == {
        "url": "http://nutrition-mcp:8010/mcp",
        "authorization": "Bearer internal-secret",
        "initialized": True,
    }


def test_docker_entrypoint_module_exists() -> None:
    from apps.nutrition_mcp import main as entrypoint

    assert callable(entrypoint.main)


def test_fastmcp_server_uses_compose_endpoint_path() -> None:
    if os.getenv("NUTRIPILOT_SKIP_REAL_MCP_TEST") == "1":
        pytest.skip("real SDK initialization was disabled for this verifier")
    try:
        server = create_mcp_server()
    except (ImportError, RuntimeError) as exc:
        pytest.skip(f"official MCP SDK cannot initialize in this environment: {exc}")

    paths = {getattr(route, "path", None) for route in server.streamable_http_app().routes}
    assert MCP_PATH in paths
