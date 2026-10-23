"""Per-request execution limits shared by workers, retries, and tool calls."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

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

    def completion_limit(self, request: object) -> int:
        self.check()
        # This is a conservative estimate, not a provider-specific tokenizer.
        estimated_input = len(json.dumps(request, ensure_ascii=False, default=str).encode()) // 2
        remaining = self.remaining_tokens - estimated_input
        if remaining < 128:
            raise ExecutionLimitExceeded("TOKEN_BUDGET_EXCEEDED")
        return min(4096, remaining)


_CURRENT: ContextVar[ExecutionBudget | None] = ContextVar("execution_budget", default=None)


def current_budget() -> ExecutionBudget | None:
    return _CURRENT.get()


@contextmanager
def execution_budget(*, seconds: float, tokens: int) -> Iterator[ExecutionBudget]:
    budget = ExecutionBudget(deadline=time.monotonic() + seconds, token_limit=tokens)
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
        result = await self.delegate.generate_structured(
            worker=worker,
            system_prompt=system_prompt,
            payload=payload,
            response_model=response_model,
            prompt_version=prompt_version,
        )
        if budget and not getattr(self.delegate, "accounts_usage", False):
            budget.add_usage(result.usage.input_tokens, result.usage.output_tokens)
        return result
