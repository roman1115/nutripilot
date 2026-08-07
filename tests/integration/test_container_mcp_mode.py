from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import pytest
import uvicorn
from apps.nutrition_mcp.server import create_app
from apps.nutrition_mcp.service import NutritionMCPService

from nutripilot.application import build_container
from nutripilot.config import Settings
from nutripilot.tools import ToolCall, ToolContext

DATA_DIR = Path(__file__).resolve().parents[2] / "data"


def test_docker_container_registers_remote_mcp_without_network_discovery() -> None:
    container = build_container(
        settings=Settings(
            app_mode="docker",
            mcp_enabled=True,
            mcp_server_url="http://nutrition-mcp:8010/mcp",
            mcp_internal_token="test-token",
        )
    )

    definitions = {
        definition.name: definition for definition in container.tool_executor.registry.definitions()
    }
    assert definitions["search_meals"].source == "mcp"
    assert definitions["get_nutrition"].source == "mcp"
    assert definitions["query_dietary_guidelines"].source == "mcp"


def test_container_can_disable_mcp_tools() -> None:
    container = build_container(settings=Settings(app_mode="demo", mcp_enabled=False))

    names = set(container.tool_executor.registry.names())
    assert "search_meals" not in names
    assert "get_nutrition" not in names
    assert "query_dietary_guidelines" not in names


@pytest.mark.integration
@pytest.mark.asyncio
async def test_docker_container_calls_mcp_over_authenticated_http() -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    app = create_app(NutritionMCPService.from_seed_data(DATA_DIR), token="test-token")
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="critical", lifespan="on", access_log=False)
    )
    server_task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started

        container = build_container(
            settings=Settings(
                app_mode="docker",
                mcp_enabled=True,
                mcp_server_url=f"http://127.0.0.1:{port}/mcp",
                mcp_internal_token="test-token",
                mcp_timeout_seconds=5,
            )
        )
        result = await container.tool_executor.execute(
            ToolCall(
                call_id="remote-container-call",
                name="get_nutrition",
                arguments={"meal_id": 1},
            ),
            ToolContext(),
        )

        assert result.ok
        assert result.data["meal_id"] == 1
    finally:
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=5)
