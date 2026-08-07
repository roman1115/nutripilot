from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from pydantic import BaseModel, ValidationError

from nutripilot.tools.errors import ToolRuntimeError
from nutripilot.tools.mcp.models import MCP_TOOL_CONTRACTS, RemoteToolSpec
from nutripilot.tools.mcp.transport import MCPTransport
from nutripilot.tools.models import ToolAccess, ToolContext, ToolDefinition
from nutripilot.tools.registry import ToolRegistry


class MCPToolAdapter:
    def __init__(
        self,
        transport: MCPTransport,
        *,
        discovery_ttl_seconds: float = 300.0,
        call_timeout_seconds: float = 6.0,
        max_payload_bytes: int = 64_000,
        allowlist: set[str] | None = None,
    ) -> None:
        self.transport = transport
        self.discovery_ttl_seconds = discovery_ttl_seconds
        self.call_timeout_seconds = call_timeout_seconds
        self.max_payload_bytes = max_payload_bytes
        self.allowlist = allowlist or set(MCP_TOOL_CONTRACTS)
        self._cached_specs: dict[str, RemoteToolSpec] = {}
        self._cache_expires_at = 0.0
        self._discovery_lock = asyncio.Lock()

    async def discover(self, *, force: bool = False) -> tuple[RemoteToolSpec, ...]:
        now = time.monotonic()
        if not force and self._cached_specs and now < self._cache_expires_at:
            return tuple(self._cached_specs[name] for name in sorted(self._cached_specs))
        async with self._discovery_lock:
            now = time.monotonic()
            if not force and self._cached_specs and now < self._cache_expires_at:
                return tuple(self._cached_specs[name] for name in sorted(self._cached_specs))
            try:
                specs = await asyncio.wait_for(
                    self.transport.list_tools(), timeout=self.call_timeout_seconds
                )
            except asyncio.TimeoutError as exc:  # noqa: UP041
                raise ToolRuntimeError(
                    "MCP_DISCOVERY_TIMEOUT",
                    "Nutrition tools could not be discovered before the deadline.",
                    retryable=True,
                ) from exc
            except ToolRuntimeError:
                raise
            except Exception as exc:
                raise ToolRuntimeError(
                    "MCP_UNAVAILABLE",
                    "Nutrition tools are temporarily unavailable.",
                    retryable=True,
                ) from exc
            self._cached_specs = {
                spec.name: spec
                for spec in specs
                if spec.name in self.allowlist and spec.name in MCP_TOOL_CONTRACTS
            }
            self._cache_expires_at = time.monotonic() + self.discovery_ttl_seconds
            return tuple(self._cached_specs[name] for name in sorted(self._cached_specs))

    async def definitions(self) -> list[ToolDefinition]:
        specs = await self.discover()
        return [self._definition(spec) for spec in specs]

    def contract_definitions(self) -> list[ToolDefinition]:
        """Build allowlisted contracts without requiring network discovery.

        Application composition is synchronous. Registering known contracts lets the API
        start independently from the MCP server while ``call_tool`` still performs and
        caches protocol discovery before the first remote invocation.
        """

        definitions: list[ToolDefinition] = []
        for name in sorted(self.allowlist.intersection(MCP_TOOL_CONTRACTS)):
            arguments_model, _, description = MCP_TOOL_CONTRACTS[name]
            definitions.append(
                self._definition(
                    RemoteToolSpec(
                        name=name,
                        description=description,
                        input_schema=arguments_model.model_json_schema(),
                    )
                )
            )
        return definitions

    def register_contracts(self, registry: ToolRegistry, *, replace: bool = False) -> ToolRegistry:
        registry.register_many(self.contract_definitions(), replace=replace)
        return registry

    async def register(self, registry: ToolRegistry, *, replace: bool = False) -> ToolRegistry:
        registry.register_many(await self.definitions(), replace=replace)
        return registry

    def _definition(self, spec: RemoteToolSpec) -> ToolDefinition:
        arguments_model, result_model, local_description = MCP_TOOL_CONTRACTS[spec.name]

        async def handler(arguments: BaseModel, _context: ToolContext) -> Any:
            return await self.call_tool(spec.name, arguments.model_dump(mode="json"))

        return ToolDefinition(
            name=spec.name,
            description=spec.description or local_description,
            arguments_model=arguments_model,
            result_model=result_model,
            handler=handler,
            access=ToolAccess.READ,
            timeout_seconds=self.call_timeout_seconds,
            max_retries=1,
            schema_version=spec.version,
            implementation_version=spec.version,
            source="mcp",
        )

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        await self.discover()
        if name not in self._cached_specs:
            raise ToolRuntimeError(
                "MCP_TOOL_NOT_FOUND", "The requested nutrition tool is unavailable."
            )
        arguments_model, result_model, _ = MCP_TOOL_CONTRACTS[name]
        try:
            validated_arguments = arguments_model.model_validate(arguments)
        except ValidationError as exc:
            raise ToolRuntimeError(
                "INVALID_ARGUMENT",
                "The MCP tool arguments did not match the required schema.",
                details={"field_count": len(exc.errors())},
            ) from exc
        try:
            raw = await asyncio.wait_for(
                self.transport.call_tool(name, validated_arguments.model_dump(mode="json")),
                timeout=self.call_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:  # noqa: UP041
            raise ToolRuntimeError(
                "MCP_TIMEOUT",
                "The nutrition tool did not finish before the deadline.",
                retryable=True,
            ) from exc
        except ToolRuntimeError:
            raise
        except Exception as exc:
            raise ToolRuntimeError(
                "MCP_UNAVAILABLE",
                "The nutrition tool is temporarily unavailable.",
                retryable=True,
            ) from exc
        sanitised = _sanitise_json(raw)
        encoded = json.dumps(sanitised, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > self.max_payload_bytes:
            raise ToolRuntimeError(
                "MCP_RESPONSE_TOO_LARGE", "The nutrition tool returned too much data."
            )
        try:
            result = result_model.model_validate(sanitised)
        except ValidationError as exc:
            raise ToolRuntimeError(
                "MCP_INVALID_RESPONSE",
                "The nutrition tool returned a response that failed validation.",
                details={"field_count": len(exc.errors())},
            ) from exc
        return result.model_dump(mode="json")


def _sanitise_json(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        raise ToolRuntimeError("MCP_INVALID_RESPONSE", "The MCP response was nested too deeply.")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:4000]
    if isinstance(value, BaseModel):
        return _sanitise_json(value.model_dump(mode="json"), depth=depth + 1)
    if isinstance(value, list):
        return [_sanitise_json(item, depth=depth + 1) for item in value[:100]]
    if isinstance(value, dict):
        return {
            str(key)[:200]: _sanitise_json(item, depth=depth + 1)
            for key, item in list(value.items())[:100]
        }
    raise ToolRuntimeError("MCP_INVALID_RESPONSE", "The MCP response was not valid JSON data.")
