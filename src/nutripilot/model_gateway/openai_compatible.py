"""OpenAI-compatible structured model gateway with classified retries."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    RateLimitError,
)
from pydantic import ValidationError

from nutripilot.domain.execution import ExecutionOwnershipLost, current_execution
from nutripilot.model_gateway.base import (
    GatewayResult,
    ModelGatewayError,
    ModelOutputT,
    ModelUsage,
)
from nutripilot.model_gateway.budget import (
    ExecutionLimitExceeded,
    current_budget,
    structured_request,
)


class OpenAICompatibleModelGateway:
    """Uses the official SDK while keeping routing and validation explicit."""

    accounts_usage = True

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        fallback_model: str | None = None,
        timeout_seconds: float = 25,
        max_retries: int = 2,
        client: Any | None = None,
    ) -> None:
        self.primary_model = model
        self.fallback_model = fallback_model
        self.max_retries = max_retries
        self.client = client or AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=0,
        )

    async def generate_structured(
        self,
        *,
        worker: str,
        system_prompt: str,
        payload: dict[str, Any],
        response_model: type[ModelOutputT],
        prompt_version: str,
    ) -> GatewayResult[ModelOutputT]:
        models = [self.primary_model]
        if self.fallback_model and self.fallback_model != self.primary_model:
            models.append(self.fallback_model)

        total_attempts = 0
        last_error: ModelGatewayError | None = None
        total_input_tokens = 0
        total_output_tokens = 0
        schema_feedback: list[dict[str, Any]] = []
        for model_index, model in enumerate(models):
            for attempt in range(self.max_retries + 1):
                total_attempts += 1
                try:
                    budget = current_budget()
                    completion_options: dict[str, Any] = {}
                    model_request = structured_request(
                        worker, system_prompt, payload, response_model, prompt_version,
                        schema_feedback,
                    )
                    if budget:
                        completion_options["max_tokens"] = budget.completion_limit(model_request)
                    execution = current_execution.get()
                    reserved = 0
                    if budget and execution and execution.reserve:
                        estimated = budget.estimate_input(model_request)
                        reserved = min(
                            budget.remaining_tokens,
                            estimated + int(completion_options.get("max_tokens", 4096)),
                        )
                        await execution.reserve(reserved)
                    completion = await self.client.chat.completions.create(
                        model=model,
                        **model_request,
                        **completion_options,
                    )
                    usage = getattr(completion, "usage", None)
                    input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
                    output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
                    total_input_tokens += input_tokens
                    total_output_tokens += output_tokens
                    if budget:
                        budget.add_usage(input_tokens, output_tokens)
                        budget.observe_input(model_request, input_tokens)
                    if reserved and usage is not None and execution and execution.settle:
                        await execution.settle(reserved, input_tokens + output_tokens)
                    content = completion.choices[0].message.content
                    if not content:
                        raise ModelGatewayError(
                            "EMPTY_RESPONSE",
                            "The model returned no structured content.",
                            retryable=True,
                        )
                    output = response_model.model_validate(
                        json.loads(content), context={"payload": payload},
                    )
                    return GatewayResult(
                        output=output,
                        model=model,
                        usage=ModelUsage(
                            input_tokens=total_input_tokens,
                            output_tokens=total_output_tokens,
                        ),
                        attempts=total_attempts,
                        used_fallback=model_index > 0,
                    )
                except (ExecutionLimitExceeded, ExecutionOwnershipLost):
                    raise
                except (json.JSONDecodeError, ValidationError) as exc:
                    schema_feedback = (
                        [
                            {
                                "field": list(error["loc"]), "type": error["type"],
                                "message": error["msg"],
                            }
                            for error in exc.errors()[:10]
                        ]
                        if isinstance(exc, ValidationError)
                        else [{"type": "invalid_json"}]
                    )
                    last_error = ModelGatewayError(
                        "INVALID_STRUCTURED_OUTPUT",
                        "The model response did not match the required schema.",
                        retryable=True,
                    )
                    last_error.__cause__ = exc
                except ModelGatewayError as exc:
                    last_error = exc
                except Exception as exc:
                    last_error = _classify_error(exc)

                if not last_error.retryable:
                    last_error.usage = ModelUsage(
                        input_tokens=total_input_tokens, output_tokens=total_output_tokens
                    )
                    raise last_error
                if attempt < self.max_retries:
                    await asyncio.sleep(min(0.1 * (2**attempt), 1.0))

        error = last_error or ModelGatewayError(
            "MODEL_UNAVAILABLE", "No configured model produced a response.", retryable=True
        )
        error.usage = ModelUsage(input_tokens=total_input_tokens, output_tokens=total_output_tokens)
        raise error


def _classify_error(exc: Exception) -> ModelGatewayError:
    if isinstance(exc, RateLimitError):
        return ModelGatewayError(
            "RATE_LIMITED", "The model provider rate limit was reached.", retryable=True
        )
    if isinstance(exc, (APITimeoutError, TimeoutError, asyncio.TimeoutError)):
        return ModelGatewayError("MODEL_TIMEOUT", "The model request timed out.", retryable=True)
    if isinstance(exc, APIConnectionError):
        return ModelGatewayError(
            "MODEL_CONNECTION_ERROR", "The model provider is unavailable.", retryable=True
        )
    if isinstance(exc, InternalServerError):
        return ModelGatewayError("MODEL_SERVER_ERROR", "The model provider failed.", retryable=True)
    if isinstance(exc, APIStatusError):
        body = exc.body if isinstance(exc.body, dict) else {}
        error = body.get("error", body)
        if (
            isinstance(error, dict)
            and (error.get("code") or error.get("type")) == "AllocationQuota.FreeTierOnly"
        ):
            return ModelGatewayError(
                "MODEL_QUOTA_EXHAUSTED",
                "模型免费额度已耗尽，请在提供方控制台确认可用额度和计费设置。",
            )
        if exc.status_code == 401:
            return ModelGatewayError("MODEL_AUTHENTICATION_FAILED", "模型访问凭据无效。")
        if exc.status_code == 403:
            return ModelGatewayError("MODEL_PERMISSION_DENIED", "当前账号无权调用该模型。")
    return ModelGatewayError("MODEL_REQUEST_REJECTED", "The model request was rejected.")
