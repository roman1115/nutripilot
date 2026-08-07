"""Small model abstraction that preserves structured output validation."""

from __future__ import annotations

from typing import Any, Generic, Protocol, TypeVar

from pydantic import BaseModel, Field

ModelOutputT = TypeVar("ModelOutputT", bound=BaseModel)


class ModelUsage(BaseModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    estimated_cost: float = Field(default=0, ge=0)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class GatewayResult(BaseModel, Generic[ModelOutputT]):  # noqa: UP046
    output: ModelOutputT
    model: str
    usage: ModelUsage = Field(default_factory=ModelUsage)
    attempts: int = Field(default=1, ge=1)
    used_fallback: bool = False


class ModelGatewayError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ModelGateway(Protocol):
    async def generate_structured(
        self,
        *,
        worker: str,
        system_prompt: str,
        payload: dict[str, Any],
        response_model: type[ModelOutputT],
        prompt_version: str,
    ) -> GatewayResult[ModelOutputT]: ...
