from __future__ import annotations

from typing import Any, Protocol


class IdempotencyStore(Protocol):
    async def get(self, key: str) -> dict[str, Any] | None: ...

    async def set(self, key: str, value: dict[str, Any], ttl_seconds: int) -> None: ...


class ToolTraceSink(Protocol):
    async def record_tool_event(self, event: dict[str, Any]) -> None: ...
