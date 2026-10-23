"""Grounded response-generation Worker Agent."""

from __future__ import annotations

import time
from typing import Any

from nutripilot.agent.agents.base import fallback_update, record_worker_event
from nutripilot.agent.contracts import RecommendationOutput
from nutripilot.agent.grounding import ground_recommendation_facts
from nutripilot.agent.state import AgentState
from nutripilot.domain.enums import Phase
from nutripilot.domain.models import FinalResponse
from nutripilot.model_gateway import ModelGateway, ModelUsage
from nutripilot.model_gateway.budget import ExecutionLimitExceeded
from nutripilot.tracing import TraceRecorder


class RecommendationAgent:
    name = "RecommendationAgent"
    prompt_version = "recommendation-v2"
    system_prompt = (
        "你是推荐生成 Worker。只能使用工具结果中的餐食、营养数据和引用，"
        "不得虚构 meal_id，不得给出医疗诊断或治疗建议，必须返回结构化响应。"
        "用户的过敏原只能来自 get_user_profile；餐食 allergens 描述食物本身，"
        "不能推断用户过敏。没有画像事实时不得声称用户有某种过敏或疾病。"
    )
    reads = frozenset({"intent", "slots", "tool_results", "candidate_ids", "citations"})
    writes = frozenset({"final_response"})

    def __init__(self, gateway: ModelGateway, recorder: TraceRecorder) -> None:
        self.gateway = gateway
        self.recorder = recorder

    async def run(self, state: AgentState) -> dict[str, Any]:
        started = time.monotonic()
        payload = {
            "intent": state.get("intent"),
            "slots": state.get("slots", {}),
            "tool_results": state.get("tool_results", []),
            "candidate_ids": state.get("candidate_ids", []),
            "citations": state.get("citations", []),
        }
        try:
            result = await self.gateway.generate_structured(
                worker="recommendation",
                system_prompt=self.system_prompt,
                payload=payload,
                response_model=RecommendationOutput,
                prompt_version=self.prompt_version,
            )
            response = ground_recommendation_facts(
                result.output.response,
                state.get("tool_results", []),
                state.get("slots", {}),
            )
            if state.get("plan_quality"):
                response.warnings = list(
                    dict.fromkeys(
                        [
                            *response.warnings,
                            *(state.get("plan_quality") or {}).get("warnings", []),
                        ]
                    )
                )
            record_worker_event(
                self.recorder,
                state,
                worker=self.name,
                event_type="model.completed",
                payload={
                    "recommendation_ids": [item.meal_id for item in response.recommendations],
                    "model": result.model,
                    "attempts": result.attempts,
                    "used_fallback_model": result.used_fallback,
                    "prompt_version": self.prompt_version,
                },
                duration_ms=(time.monotonic() - started) * 1000,
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
            )
            return {
                "phase": Phase.RECOMMENDATION.value,
                "final_response": response.model_dump(mode="json"),
                "prompt_versions": {
                    **state.get("prompt_versions", {}),
                    self.name: self.prompt_version,
                },
            }
        except ExecutionLimitExceeded:
            raise
        except Exception as exc:
            response = _fallback_response(state)
            usage = getattr(exc, "usage", ModelUsage())
            record_worker_event(
                self.recorder,
                state,
                worker=self.name,
                event_type="model.fallback",
                payload={
                    "error_type": type(exc).__name__,
                    "error_code": getattr(exc, "code", "MODEL_OR_SCHEMA_ERROR"),
                },
                duration_ms=(time.monotonic() - started) * 1000,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
            )
            return {
                "phase": Phase.RECOMMENDATION.value,
                "final_response": response.model_dump(mode="json"),
                "fallback_events": fallback_update(state, self.name, "MODEL_OR_SCHEMA_ERROR"),
                "prompt_versions": {
                    **state.get("prompt_versions", {}),
                    self.name: self.prompt_version,
                },
            }


def _fallback_response(state: AgentState) -> FinalResponse:
    succeeded = [
        result for result in state.get("tool_results", []) if result.get("status") == "success"
    ]
    if succeeded:
        return FinalResponse(
            status="degraded",
            message="已取得部分可靠数据，但生成服务暂时不可用，请稍后重试。",
            warnings=["当前为受控降级响应，未生成个性化解释。"],
        )
    return FinalResponse(
        status="degraded",
        message="服务暂时无法完成推荐，请稍后重试。",
        warnings=["未返回未经验证的餐食信息。"],
    )
