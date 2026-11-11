"""Per-request execution limits shared by workers, retries, and tool calls."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from nutripilot.domain.execution import current_execution
from nutripilot.model_gateway.base import GatewayResult, ModelGateway, ModelOutputT


class ExecutionLimitExceeded(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class ExecutionBudget:
    deadline: float
    token_limit: int
    input_tokens: int = 0
    output_tokens: int = 0
    observed_input_ratio: float | None = None
    completion_cap: int = 4096
    context_window: int = 32768

    @property
    def remaining_tokens(self) -> int:
        return self.token_limit - self.input_tokens - self.output_tokens

    def check(self) -> None:
        if time.monotonic() >= self.deadline:
            raise ExecutionLimitExceeded("DEADLINE_EXCEEDED")
        if self.remaining_tokens <= 0:
            raise ExecutionLimitExceeded("TOKEN_BUDGET_EXCEEDED")

    def add_usage(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens

    def estimate_input(self, request: object) -> int:
        base = max(1, len(json.dumps(
            request, ensure_ascii=False, default=str, separators=(",", ":"),
        ).encode()) // 2)
        ratio = min(1.0, self.observed_input_ratio * 1.2) if (
            self.observed_input_ratio is not None
        ) else 1.0
        return math.ceil(base * ratio)

    def observe_input(self, request: object, actual_tokens: int) -> None:
        if actual_tokens <= 0:
            return
        base = max(1, len(json.dumps(
            request, ensure_ascii=False, default=str, separators=(",", ":"),
        ).encode()) // 2)
        ratio = actual_tokens / base
        self.observed_input_ratio = max(self.observed_input_ratio or 0, ratio)

    def completion_limit(self, request: object) -> int:
        self.check()
        # Calibrate this request only from provider usage, with headroom; never raise the cap.
        estimated_input = self.estimate_input(request)
        remaining = self.remaining_tokens - estimated_input
        if remaining < 128:
            raise ExecutionLimitExceeded("TOKEN_BUDGET_EXCEEDED")
        window_remaining = self.context_window - estimated_input - 512
        if window_remaining < 128:
            raise ExecutionLimitExceeded("CONTEXT_WINDOW_EXCEEDED")
        return min(self.completion_cap, remaining, window_remaining)


def structured_request(
    worker: str, system_prompt: str, payload: dict[str, Any],
    response_model: type[BaseModel], prompt_version: str,
    schema_feedback: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Use the same serialized envelope for preflight and the provider request."""
    return {
        "messages": [
            {
                "role": "system",
                "content": (
                    f"{system_prompt}\nReturn exactly one valid JSON object that "
                    "matches output_schema. Do not include Markdown fences."
                ),
            },
            {
                "role": "user",
                "content": json.dumps({
                    "worker": worker, "prompt_version": prompt_version, "input": payload,
                    "output_schema": response_model.model_json_schema(),
                    "schema_feedback": schema_feedback or [],
                }, ensure_ascii=False, default=str, separators=(",", ":")),
            },
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
    }


_CURRENT: ContextVar[ExecutionBudget | None] = ContextVar("execution_budget", default=None)


def current_budget() -> ExecutionBudget | None:
    return _CURRENT.get()


@contextmanager
def model_allocation(*, output_tokens: int) -> Iterator[None]:
    budget = current_budget()
    if budget is None:
        yield
        return
    previous = budget.completion_cap
    budget.completion_cap = min(4096, max(128, output_tokens))
    try:
        yield
    finally:
        budget.completion_cap = previous


@contextmanager
def execution_budget(
    *, seconds: float, tokens: int, context_window: int = 32768,
) -> Iterator[ExecutionBudget]:
    budget = ExecutionBudget(
        deadline=time.monotonic() + seconds, token_limit=tokens, context_window=context_window,
    )
    handle = _CURRENT.set(budget)
    try:
        yield budget
    finally:
        _CURRENT.reset(handle)


class BudgetedModelGateway:
    def __init__(self, delegate: ModelGateway) -> None:
        self.delegate = delegate

    async def generate_structured(
        self,
        *,
        worker: str,
        system_prompt: str,
        payload: dict[str, Any],
        response_model: type[ModelOutputT],
        prompt_version: str,
    ) -> GatewayResult[ModelOutputT]:
        budget = current_budget()
        if budget:
            budget.check()
            budget.completion_limit(structured_request(
                worker, system_prompt, payload, response_model, prompt_version,
            ))
        execution = current_execution.get()
        reserved = 0
        if (
            budget
            and execution
            and execution.reserve
            and not getattr(self.delegate, "accounts_usage", False)
        ):
            reserved = min(budget.remaining_tokens, 4096 + len(json.dumps(payload)) // 2)
            await execution.reserve(reserved)
        result = await self.delegate.generate_structured(
            worker=worker,
            system_prompt=system_prompt,
            payload=payload,
            response_model=response_model,
            prompt_version=prompt_version,
        )
        if budget and not getattr(self.delegate, "accounts_usage", False):
            budget.add_usage(result.usage.input_tokens, result.usage.output_tokens)
            if reserved and execution and execution.settle:
                await execution.settle(
                    reserved, result.usage.input_tokens + result.usage.output_tokens
                )
        return result
