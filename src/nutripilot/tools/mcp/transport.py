from __future__ import annotations

import inspect
import json
import os
from types import TracebackType
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

from nutripilot.tools.errors import ToolRuntimeError
from nutripilot.tools.mcp.models import RemoteToolSpec


class MCPTransport(Protocol):
    identity: str

    async def list_tools(self) -> list[RemoteToolSpec]: ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...


class InProcessMCPService(Protocol):
    async def list_tools(self) -> list[RemoteToolSpec]: ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...


class InProcessMCPTransport:
    """Protocol-faithful test/demo transport without network or MCP SDK."""

    identity = "in-process:nutrition-mcp"

    def __init__(self, service: InProcessMCPService) -> None:
        self.service = service

    async def list_tools(self) -> list[RemoteToolSpec]:
        return await self.service.list_tools()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return await self.service.call_tool(name, arguments)


class StreamableHTTPMCPTransport:
    """Official MCP SDK client for Streamable HTTP deployments."""

    def __init__(
        self,
        url: str,
        *,
        token: str | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        self.url = _normalise_mcp_url(url)
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.identity = f"streamable-http:{self.url}"

    @classmethod
    def from_environment(cls) -> StreamableHTTPMCPTransport:
        return cls(
            os.getenv("MCP_SERVER_URL", "http://localhost:8010/mcp"),
            token=os.getenv("MCP_INTERNAL_TOKEN"),
            timeout_seconds=float(os.getenv("MCP_TIMEOUT_SECONDS", "5")),
        )

    async def list_tools(self) -> list[RemoteToolSpec]:
        async with self._session() as session:
            response = await session.list_tools()
        return [
            RemoteToolSpec(
                name=tool.name,
                description=tool.description or "",
                input_schema=getattr(tool, "inputSchema", None)
                or getattr(tool, "input_schema", {}),
            )
            for tool in response.tools
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        async with self._session() as session:
            result = await session.call_tool(name, arguments=arguments)
        if getattr(result, "isError", False) or getattr(result, "is_error", False):
            raise ToolRuntimeError(
                "MCP_REMOTE_ERROR", "The nutrition tool reported a controlled error."
            )
        structured = getattr(result, "structuredContent", None)
        if structured is None:
            structured = getattr(result, "structured_content", None)
        if structured is not None:
            if isinstance(structured, dict) and set(structured) == {"result"}:
                return structured["result"]
            return structured
        content = getattr(result, "content", [])
        texts = [getattr(item, "text", None) for item in content]
        combined = "".join(text for text in texts if text)
        try:
            return json.loads(combined)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ToolRuntimeError(
                "MCP_INVALID_RESPONSE",
                "The nutrition tool returned an invalid response.",
            ) from exc

    def _session(self) -> _MCPClientSessionContext:
        return _MCPClientSessionContext(self.url, self.token, self.timeout_seconds)


class _MCPClientSessionContext:
    def __init__(self, url: str, token: str | None, timeout_seconds: float) -> None:
        self.url = url
        self.headers = {"Authorization": f"Bearer {token}"} if token else None
        self.timeout_seconds = timeout_seconds
        self._transport_context: Any | None = None
        self._session_context: Any | None = None
        self._http_client_context: Any | None = None

    async def __aenter__(self) -> Any:
        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamable_http_client
        except ImportError as exc:
            raise RuntimeError(
                "The official mcp package is required for StreamableHTTPMCPTransport"
            ) from exc
        client_factory: Any = streamable_http_client
        parameters = inspect.signature(client_factory).parameters
        if "http_client" in parameters:
            try:
                import httpx
            except ImportError as exc:
                raise RuntimeError(
                    "httpx is required for authenticated Streamable HTTP MCP calls"
                ) from exc
            self._http_client_context = httpx.AsyncClient(
                headers=self.headers,
                timeout=httpx.Timeout(
                    self.timeout_seconds,
                    connect=min(5.0, self.timeout_seconds),
                ),
            )
            http_client = await self._http_client_context.__aenter__()
            self._transport_context = client_factory(self.url, http_client=http_client)
        else:
            # Compatibility with early 1.x MCP SDK releases.
            self._transport_context = client_factory(self.url, headers=self.headers)
        streams = await self._transport_context.__aenter__()
        read_stream, write_stream = streams[0], streams[1]
        self._session_context = ClientSession(read_stream, write_stream)
        session = await self._session_context.__aenter__()
        await session.initialize()
        return session

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            if self._session_context is not None:
                await self._session_context.__aexit__(exc_type, exc, traceback)
        finally:
            try:
                if self._transport_context is not None:
                    await self._transport_context.__aexit__(exc_type, exc, traceback)
            finally:
                if self._http_client_context is not None:
                    await self._http_client_context.__aexit__(exc_type, exc, traceback)


def _normalise_mcp_url(url: str) -> str:
    value = url.strip()
    if not value:
        raise ValueError("MCP server URL must not be empty")
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("MCP server URL must use http or https")
    path = parts.path.rstrip("/")
    if not path:
        path = "/mcp"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))
