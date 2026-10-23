from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any

from pydantic import BaseModel, ValidationError

from nutripilot.tools.errors import ToolRuntimeError
from nutripilot.tools.models import (
    ToolAccess,
    ToolCall,
    ToolCitation,
    ToolContext,
    ToolDefinition,
    ToolExecutionState,
    ToolResult,
    ToolStatus,
)
from nutripilot.tools.ports import IdempotencyStore
from nutripilot.tools.registry import ToolRegistry


class InMemoryIdempotencyStore:
    def __init__(self) -> None:
        self._values: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> dict[str, Any] | None:
        async with self._lock:
            item = self._values.get(key)
            if item is None:
                return None
            expires_at, value = item
            if expires_at <= time.monotonic():
                self._values.pop(key, None)
                return None
            return value.copy()

    async def set(self, key: str, value: dict[str, Any], ttl_seconds: int) -> None:
        async with self._lock:
            self._values[key] = (time.monotonic() + ttl_seconds, value.copy())


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        idempotency_store: IdempotencyStore | None = None,
        trace_sink: Any | None = None,
        max_steps: int = 8,
        max_identical_calls: int = 2,
        idempotency_ttl_seconds: int = 86_400,
    ) -> None:
        self.registry = registry
        self.idempotency_store = idempotency_store or InMemoryIdempotencyStore()
        self.trace_sink = trace_sink
        self.max_steps = max_steps
        self.max_identical_calls = max_identical_calls
        self.idempotency_ttl_seconds = idempotency_ttl_seconds

    async def execute(
        self,
        call: ToolCall,
        context: ToolContext,
        state: ToolExecutionState | None = None,
    ) -> ToolResult:
        state = state or ToolExecutionState()
        started = time.monotonic()
        definition = self.registry.get(call.name)
        if definition is None:
            return self._error(call, "TOOL_NOT_FOUND", "The requested tool is not available.")

        preflight = self._preflight(call, context, state, definition)
        if preflight is not None:
            await self._trace(call, context, definition, preflight)
            return preflight

        try:
            arguments = definition.arguments_model.model_validate(call.arguments)
        except ValidationError as exc:
            result = self._error(
                call,
                "INVALID_ARGUMENT",
                "The tool arguments did not match the required schema.",
                details={"fields": self._validation_details(exc)},
                definition=definition,
            )
            await self._trace(call, context, definition, result)
            return result

        idempotency_storage_key: str | None = None
        if definition.access is ToolAccess.WRITE:
            idempotency_storage_key = self._idempotency_key(call, context)
            cached = await self.idempotency_store.get(idempotency_storage_key)
            if cached is not None:
                result = ToolResult.model_validate(cached).model_copy(
                    update={"call_id": call.call_id, "cached": True}
                )
                await self._trace(call, context, definition, result)
                return result

        timeout = definition.timeout_seconds
        remaining = context.remaining_seconds()
        if remaining is not None:
            timeout = min(timeout, remaining)
        if timeout <= 0:
            result = self._error(
                call,
                "DEADLINE_EXCEEDED",
                "The request deadline was exceeded before the tool started.",
                retryable=True,
                definition=definition,
            )
            await self._trace(call, context, definition, result)
            return result

        retries = 0
        tool_deadline = time.monotonic() + timeout
        while True:
            try:
                remaining = tool_deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                raw = await asyncio.wait_for(
                    definition.handler(arguments, context), timeout=remaining
                )
                result = self._normalise_success(call, definition, raw, retries)
                break
            except asyncio.TimeoutError:  # noqa: UP041 - supports the local Python 3.10 verifier
                error = ToolRuntimeError(
                    "TOOL_TIMEOUT", "The tool did not finish within its deadline.", retryable=True
                )
            except ToolRuntimeError as exc:
                error = exc
            except asyncio.CancelledError:
                result = self._error(
                    call,
                    "CANCELLED",
                    "The tool call was cancelled.",
                    definition=definition,
                    status=ToolStatus.CANCELLED,
                )
                await self._trace(call, context, definition, result)
                raise
            except Exception:
                error = ToolRuntimeError(
                    "TOOL_INTERNAL_ERROR",
                    "The tool failed without exposing internal details.",
                    retryable=False,
                )

            if not error.retryable or retries >= definition.max_retries:
                result = self._error(
                    call,
                    error.code,
                    error.safe_message,
                    retryable=error.retryable,
                    details=error.details,
                    definition=definition,
                    retry_count=retries,
                )
                break
            retries += 1
            await asyncio.sleep(min(0.05 * (2 ** (retries - 1)), 0.5))

        result = result.model_copy(
            update={"duration_ms": round((time.monotonic() - started) * 1000, 3)}
        )
        if idempotency_storage_key is not None and result.ok:
            await self.idempotency_store.set(
                idempotency_storage_key,
                result.model_dump(mode="json"),
                self.idempotency_ttl_seconds,
            )
        await self._trace(call, context, definition, result)
        return result

    async def execute_plan(
        self,
        calls: list[ToolCall],
        context: ToolContext,
        state: ToolExecutionState | None = None,
    ) -> list[ToolResult]:
        execution_state = state or ToolExecutionState()
        results: list[ToolResult] = []
        for call in calls:
            result = await self.execute(call, context, execution_state)
            results.append(result)
            if result.status in {
                ToolStatus.CONFIRMATION_REQUIRED,
                ToolStatus.CANCELLED,
            }:
                break
            if result.error and result.error.code in {"STEP_LIMIT_EXCEEDED", "LOOP_DETECTED"}:
                break
        return results

    def _preflight(
        self,
        call: ToolCall,
        context: ToolContext,
        state: ToolExecutionState,
        definition: ToolDefinition,
    ) -> ToolResult | None:
        if context.is_cancelled():
            return self._error(
                call,
                "CANCELLED",
                "The tool call was cancelled.",
                definition=definition,
                status=ToolStatus.CANCELLED,
            )
        state.step_count += 1
        if state.step_count > self.max_steps:
            return self._error(
                call,
                "STEP_LIMIT_EXCEEDED",
                "The maximum number of tool steps was reached.",
                definition=definition,
            )
        signature = call.signature()
        state.signatures[signature] = state.signatures.get(signature, 0) + 1
        if state.signatures[signature] > self.max_identical_calls:
            return self._error(
                call,
                "LOOP_DETECTED",
                "A repeated tool-call loop was stopped.",
                definition=definition,
            )
        if (
            definition.required_permission
            and definition.required_permission not in context.permissions
        ):
            return self._error(
                call,
                "PERMISSION_DENIED",
                "The caller is not allowed to use this tool.",
                definition=definition,
            )
        if definition.access is ToolAccess.WRITE:
            if context.user_id is None:
                return self._error(
                    call,
                    "USER_ID_REQUIRED",
                    "A verified user is required for write tools.",
                    definition=definition,
                )
            if not context.idempotency_key:
                return self._error(
                    call,
                    "IDEMPOTENCY_KEY_REQUIRED",
                    "An idempotency key is required for write tools.",
                    definition=definition,
                )
        if definition.requires_confirmation and not context.confirmed:
            return self._error(
                call,
                "CONFIRMATION_REQUIRED",
                "User confirmation is required before this change is applied.",
                definition=definition,
                status=ToolStatus.CONFIRMATION_REQUIRED,
            )
        return None

    @staticmethod
    def _idempotency_key(call: ToolCall, context: ToolContext) -> str:
        return f"tool:{context.user_id}:{call.name}:{context.idempotency_key}"

    @staticmethod
    def _validation_details(exc: ValidationError) -> list[dict[str, Any]]:
        return [
            {
                "location": [str(part) for part in error["loc"]],
                "type": error["type"],
                "message": error["msg"],
            }
            for error in exc.errors(include_url=False, include_input=False)
        ]

    def _normalise_success(
        self,
        call: ToolCall,
        definition: ToolDefinition,
        raw: Any,
        retries: int,
    ) -> ToolResult:
        if isinstance(raw, ToolResult):
            return raw.model_copy(
                update={
                    "call_id": call.call_id,
                    "tool_name": definition.name,
                    "retry_count": retries,
                    "implementation_version": definition.implementation_version,
                    "schema_version": definition.schema_version,
                }
            )
        citations: list[ToolCitation] = []
        data = raw
        if isinstance(raw, tuple) and len(raw) == 2:
            data, citations = raw
        if definition.result_model is not None:
            validated = definition.result_model.model_validate(data)
            data = validated.model_dump(mode="json")
        elif isinstance(data, BaseModel):
            data = data.model_dump(mode="json")
        return ToolResult.success(
            call_id=call.call_id,
            tool_name=definition.name,
            data=data,
            citations=citations,
            retry_count=retries,
            implementation_version=definition.implementation_version,
            schema_version=definition.schema_version,
        )

    @staticmethod
    def _error(
        call: ToolCall,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
        definition: ToolDefinition | None = None,
        status: ToolStatus = ToolStatus.ERROR,
        retry_count: int = 0,
    ) -> ToolResult:
        return ToolResult.failure(
            call_id=call.call_id,
            tool_name=call.name,
            code=code,
            message=message,
            retryable=retryable,
            details=details,
            status=status,
            retry_count=retry_count,
            implementation_version=(definition.implementation_version if definition else "unknown"),
            schema_version=(definition.schema_version if definition else "unknown"),
        )

    async def _trace(
        self,
        call: ToolCall,
        context: ToolContext,
        definition: ToolDefinition,
        result: ToolResult,
    ) -> None:
        if self.trace_sink is None:
            return
        event = {
            "trace_id": context.trace_id,
            "thread_id": context.thread_id,
            "turn_id": context.turn_id,
            "user_id": context.user_id,
            "call_id": call.call_id,
            "tool_name": definition.name,
            "source": definition.source,
            "access": definition.access.value,
            "arguments": call.arguments,
            "status": result.status.value,
            "error_code": result.error.code if result.error else None,
            "duration_ms": result.duration_ms,
            "retry_count": result.retry_count,
            "cached": result.cached,
            "schema_version": definition.schema_version,
            "implementation_version": definition.implementation_version,
        }
        if hasattr(self.trace_sink, "record_tool_event"):
            trace_output = self.trace_sink.record_tool_event(event)
        else:
            trace_output = self.trace_sink.record(
                trace_id=context.trace_id or "untraced",
                thread_id=context.thread_id or "untraced",
                turn_id=context.turn_id or call.call_id,
                user_id=context.user_id or 0,
                event_type="tool.completed" if result.ok else "tool.failed",
                component=definition.name,
                payload=event,
                duration_ms=result.duration_ms,
            )
        if inspect.isawaitable(trace_output):
            await trace_output
