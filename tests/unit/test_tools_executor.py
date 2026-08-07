from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from nutripilot.tools import (
    ToolAccess,
    ToolCall,
    ToolContext,
    ToolDefinition,
    ToolExecutionState,
    ToolExecutor,
    ToolRegistry,
    ToolStatus,
)
from nutripilot.tools.errors import ToolRuntimeError


class ValueArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: int


@pytest.mark.asyncio
async def test_executor_validates_arguments_without_calling_handler() -> None:
    called = False

    async def handler(arguments: BaseModel, context: Any) -> dict[str, int]:
        nonlocal called
        called = True
        return {"value": 1}

    executor = ToolExecutor(ToolRegistry([ToolDefinition("value", "value", ValueArgs, handler)]))
    result = await executor.execute(
        ToolCall(call_id="1", name="value", arguments={"value": "not-an-int"}),
        ToolContext(),
    )

    assert result.error is not None
    assert result.error.code == "INVALID_ARGUMENT"
    assert called is False


@pytest.mark.asyncio
async def test_write_requires_permission_confirmation_and_idempotency() -> None:
    calls = 0

    async def handler(arguments: BaseModel, context: Any) -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"value": ValueArgs.model_validate(arguments).value}

    definition = ToolDefinition(
        "write_value",
        "write",
        ValueArgs,
        handler,
        access=ToolAccess.WRITE,
        required_permission="value:write",
        requires_confirmation=True,
    )
    executor = ToolExecutor(ToolRegistry([definition]))
    call = ToolCall(call_id="1", name="write_value", arguments={"value": 7})

    denied = await executor.execute(call, ToolContext(user_id=1))
    assert denied.error and denied.error.code == "PERMISSION_DENIED"

    needs_key = await executor.execute(
        call, ToolContext(user_id=1, permissions=frozenset({"value:write"}))
    )
    assert needs_key.error and needs_key.error.code == "IDEMPOTENCY_KEY_REQUIRED"

    needs_confirmation = await executor.execute(
        call,
        ToolContext(
            user_id=1,
            permissions=frozenset({"value:write"}),
            idempotency_key="same-command",
        ),
    )
    assert needs_confirmation.status is ToolStatus.CONFIRMATION_REQUIRED

    context = ToolContext(
        user_id=1,
        permissions=frozenset({"value:write"}),
        idempotency_key="same-command",
        confirmed=True,
    )
    first = await executor.execute(call, context)
    repeated = await executor.execute(call.model_copy(update={"call_id": "2"}), context)

    assert first.ok
    assert repeated.ok and repeated.cached
    assert calls == 1


@pytest.mark.asyncio
async def test_executor_retries_only_controlled_retryable_failures() -> None:
    attempts = 0

    async def flaky(arguments: BaseModel, context: Any) -> dict[str, int]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ToolRuntimeError("TEMPORARY", "Temporary failure.", retryable=True)
        return {"value": 9}

    definition = ToolDefinition("flaky", "flaky", ValueArgs, flaky, max_retries=1)
    executor = ToolExecutor(ToolRegistry([definition]))

    result = await executor.execute(
        ToolCall(call_id="retry", name="flaky", arguments={"value": 9}),
        ToolContext(),
    )

    assert result.ok
    assert result.retry_count == 1
    assert attempts == 2


@pytest.mark.asyncio
async def test_executor_stops_identical_call_loop() -> None:
    async def handler(arguments: BaseModel, context: Any) -> dict[str, int]:
        return {"value": ValueArgs.model_validate(arguments).value}

    executor = ToolExecutor(
        ToolRegistry([ToolDefinition("value", "value", ValueArgs, handler)]),
        max_identical_calls=2,
    )
    state = ToolExecutionState()
    context = ToolContext()
    call = ToolCall(call_id="1", name="value", arguments={"value": 1})

    assert (await executor.execute(call, context, state)).ok
    assert (await executor.execute(call.model_copy(update={"call_id": "2"}), context, state)).ok
    stopped = await executor.execute(call.model_copy(update={"call_id": "3"}), context, state)

    assert stopped.error and stopped.error.code == "LOOP_DETECTED"


@pytest.mark.asyncio
async def test_executor_hides_unexpected_exception_details() -> None:
    async def broken(arguments: BaseModel, context: Any) -> dict[str, int]:
        raise RuntimeError("database-password-must-not-leak")

    executor = ToolExecutor(ToolRegistry([ToolDefinition("broken", "broken", ValueArgs, broken)]))
    result = await executor.execute(
        ToolCall(call_id="broken", name="broken", arguments={"value": 1}), ToolContext()
    )

    assert result.error and result.error.code == "TOOL_INTERNAL_ERROR"
    assert "password" not in result.error.message
