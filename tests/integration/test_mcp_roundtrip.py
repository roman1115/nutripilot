from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from apps.nutrition_mcp.service import NutritionMCPService

from nutripilot.tools import ToolCall, ToolContext, ToolExecutor, ToolRegistry
from nutripilot.tools.errors import ToolRuntimeError
from nutripilot.tools.mcp import InProcessMCPTransport, MCPToolAdapter
from nutripilot.tools.mcp.models import RemoteToolSpec

DATA_DIR = Path(__file__).resolve().parents[2] / "data"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mcp_dynamic_discovery_and_tool_round_trip() -> None:
    service = NutritionMCPService.from_seed_data(DATA_DIR)
    adapter = MCPToolAdapter(InProcessMCPTransport(service), discovery_ttl_seconds=60)
    registry = ToolRegistry()
    await adapter.register(registry)
    executor = ToolExecutor(registry)

    assert set(registry.names()) == {
        "get_nutrition",
        "query_dietary_guidelines",
        "search_meals",
    }
    result = await executor.execute(
        ToolCall(
            call_id="mcp-search",
            name="search_meals",
            arguments={
                "query": "清淡高蛋白午餐",
                "meal_type": "lunch",
                "excluded_allergens": ["鱼", "甲壳类"],
                "limit": 3,
            },
        ),
        ToolContext(),
    )

    assert result.ok
    assert result.data["hits"]
    assert all(not {"鱼", "甲壳类"}.intersection(hit["allergens"]) for hit in result.data["hits"])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mcp_filters_meal_role_before_truncating_candidates() -> None:
    service = NutritionMCPService.from_seed_data(DATA_DIR)
    adapter = MCPToolAdapter(InProcessMCPTransport(service))
    result = await adapter.call_tool(
        "search_meals", {"query": "米粥", "meal_role": "complete_meal", "limit": 20}
    )
    assert result["hits"]
    assert all(hit["meal_role"] == "complete_meal" for hit in result["hits"])
    assert result["total_candidates"] == 18


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mcp_preserves_recipe_nutrition_ranges() -> None:
    service = NutritionMCPService.from_seed_data(DATA_DIR)
    adapter = MCPToolAdapter(InProcessMCPTransport(service))
    result = await adapter.call_tool("get_nutrition", {"meal_id": 1008})

    nutrition = result["nutrition"]
    bounds = nutrition["nutrient_ranges"]["calories_kcal"]
    assert bounds["minimum"] == pytest.approx(398.828)
    assert bounds["maximum"] == nutrition["calories_kcal"] == pytest.approx(480.156)
    assert nutrition["serving_size"] == "整道菜（整份配方）"
    assert nutrition["input_weight_range_g"]["minimum"] < nutrition["serving_weight_g"]


class CountingTransport:
    identity = "counting"

    def __init__(self, service: NutritionMCPService) -> None:
        self.service = service
        self.discovery_calls = 0

    async def list_tools(self) -> list[RemoteToolSpec]:
        self.discovery_calls += 1
        return await self.service.list_tools()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return await self.service.call_tool(name, arguments)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mcp_contract_registration_defers_discovery_until_first_call() -> None:
    service = NutritionMCPService.from_seed_data(DATA_DIR)
    transport = CountingTransport(service)
    adapter = MCPToolAdapter(transport, discovery_ttl_seconds=60)
    registry = ToolRegistry()

    adapter.register_contracts(registry)

    assert transport.discovery_calls == 0
    assert set(registry.names()) == {
        "get_nutrition",
        "query_dietary_guidelines",
        "search_meals",
    }
    result = await ToolExecutor(registry).execute(
        ToolCall(
            call_id="lazy-mcp-call",
            name="get_nutrition",
            arguments={"meal_id": 1},
        ),
        ToolContext(),
    )
    assert result.ok
    assert transport.discovery_calls == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mcp_discovery_uses_ttl_cache() -> None:
    service = NutritionMCPService.from_seed_data(DATA_DIR)
    transport = CountingTransport(service)
    adapter = MCPToolAdapter(transport, discovery_ttl_seconds=60)

    await adapter.discover()
    await adapter.discover()

    assert transport.discovery_calls == 1


class InvalidResponseTransport(CountingTransport):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return {"unexpected": "untrusted remote data"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mcp_adapter_rejects_untrusted_invalid_response() -> None:
    service = NutritionMCPService.from_seed_data(DATA_DIR)
    adapter = MCPToolAdapter(InvalidResponseTransport(service))

    with pytest.raises(ToolRuntimeError) as caught:
        await adapter.call_tool("get_nutrition", {"meal_id": 1})

    assert caught.value.code == "MCP_INVALID_RESPONSE"


class SlowDiscoveryTransport(CountingTransport):
    async def list_tools(self) -> list[RemoteToolSpec]:
        await asyncio.sleep(0.05)
        return await super().list_tools()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mcp_discovery_timeout_is_structured() -> None:
    service = NutritionMCPService.from_seed_data(DATA_DIR)
    adapter = MCPToolAdapter(SlowDiscoveryTransport(service), call_timeout_seconds=0.005)

    with pytest.raises(ToolRuntimeError) as caught:
        await adapter.discover()

    assert caught.value.code == "MCP_DISCOVERY_TIMEOUT"
    assert caught.value.retryable is True


class SlowCallTransport(CountingTransport):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        await asyncio.sleep(0.05)
        return await super().call_tool(name, arguments)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mcp_call_timeout_is_structured() -> None:
    service = NutritionMCPService.from_seed_data(DATA_DIR)
    adapter = MCPToolAdapter(SlowCallTransport(service), call_timeout_seconds=0.005)

    with pytest.raises(ToolRuntimeError) as caught:
        await adapter.call_tool("get_nutrition", {"food_name": "燕麦蓝莓酸奶杯"})

    assert caught.value.code == "MCP_TIMEOUT"
    assert caught.value.retryable is True
