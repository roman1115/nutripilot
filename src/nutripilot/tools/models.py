from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ToolAccess(StrEnum):
    READ = "read"
    WRITE = "write"


class ToolStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"
    CONFIRMATION_REQUIRED = "confirmation_required"
    CANCELLED = "cancelled"


class ToolCitation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    title: str
    uri: str | None = None
    excerpt: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolErrorInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    """Canonical result returned by every local and MCP tool."""

    model_config = ConfigDict(extra="forbid")

    call_id: str
    tool_name: str
    status: ToolStatus
    data: Any | None = None
    error: ToolErrorInfo | None = None
    citations: list[ToolCitation] = Field(default_factory=list)
    retry_count: int = 0
    duration_ms: float = 0.0
    implementation_version: str = "1.0.0"
    schema_version: str = "1.0.0"
    cached: bool = False

    @property
    def ok(self) -> bool:
        return self.status is ToolStatus.SUCCESS

    @classmethod
    def success(
        cls,
        *,
        call_id: str,
        tool_name: str,
        data: Any,
        citations: list[ToolCitation] | None = None,
        duration_ms: float = 0.0,
        retry_count: int = 0,
        implementation_version: str = "1.0.0",
        schema_version: str = "1.0.0",
        cached: bool = False,
    ) -> ToolResult:
        return cls(
            call_id=call_id,
            tool_name=tool_name,
            status=ToolStatus.SUCCESS,
            data=data,
            citations=citations or [],
            duration_ms=duration_ms,
            retry_count=retry_count,
            implementation_version=implementation_version,
            schema_version=schema_version,
            cached=cached,
        )

    @classmethod
    def failure(
        cls,
        *,
        call_id: str,
        tool_name: str,
        code: str,
        message: str,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
        duration_ms: float = 0.0,
        retry_count: int = 0,
        implementation_version: str = "1.0.0",
        schema_version: str = "1.0.0",
        status: ToolStatus = ToolStatus.ERROR,
    ) -> ToolResult:
        return cls(
            call_id=call_id,
            tool_name=tool_name,
            status=status,
            error=ToolErrorInfo(
                code=code,
                message=message,
                retryable=retryable,
                details=details or {},
            ),
            duration_ms=duration_ms,
            retry_count=retry_count,
            implementation_version=implementation_version,
            schema_version=schema_version,
        )


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)

    def signature(self) -> str:
        canonical = json.dumps(
            {"name": self.name, "arguments": self.arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class ToolContext:
    user_id: int | None = None
    trace_id: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    permissions: frozenset[str] = field(default_factory=frozenset)
    confirmed: bool = False
    idempotency_key: str | None = None
    deadline_monotonic: float | None = None
    cancellation_event: asyncio.Event | None = None

    @classmethod
    def with_timeout(cls, timeout_seconds: float, **kwargs: Any) -> ToolContext:
        return cls(deadline_monotonic=time.monotonic() + timeout_seconds, **kwargs)

    def is_cancelled(self) -> bool:
        return self.cancellation_event is not None and self.cancellation_event.is_set()

    def remaining_seconds(self) -> float | None:
        if self.deadline_monotonic is None:
            return None
        return max(0.0, self.deadline_monotonic - time.monotonic())


@dataclass(slots=True)
class ToolExecutionState:
    step_count: int = 0
    signatures: dict[str, int] = field(default_factory=dict)


ToolHandler = Callable[[BaseModel, ToolContext], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    arguments_model: type[BaseModel]
    handler: ToolHandler
    access: ToolAccess = ToolAccess.READ
    result_model: type[BaseModel] | None = None
    required_permission: str | None = None
    requires_confirmation: bool = False
    timeout_seconds: float = 5.0
    max_retries: int = 0
    schema_version: str = "1.0.0"
    implementation_version: str = "1.0.0"
    source: str = "local"

    def json_schema(self) -> dict[str, Any]:
        return self.arguments_model.model_json_schema()
