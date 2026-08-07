"""Intent and slot extraction Worker Agent."""

from __future__ import annotations

import time
from typing import Any

from nutripilot.agent.agents.base import fallback_update, record_worker_event
from nutripilot.agent.contracts import IntentOutput
from nutripilot.agent.state import AgentState
from nutripilot.domain.enums import Intent, Phase
from nutripilot.memory.context import merge_slots
from nutripilot.model_gateway import ModelGateway
from nutripilot.tracing import TraceRecorder


class IntentAgent:
    name = "IntentAgent"
    prompt_version = "intent-v1"
    system_prompt = (
        "你是饮食意图识别 Worker。仅依据用户消息和已有状态识别意图、抽取槽位；"
        "不得调用工具或提供医疗结论，必须返回符合给定 Schema 的结构化结果。"
    )
    reads = frozenset({"user_message", "slots", "conversation_summary"})
    writes = frozenset({"intent", "intent_confidence", "slots", "missing_slots"})

    def __init__(self, gateway: ModelGateway, recorder: TraceRecorder) -> None:
        self.gateway = gateway
        self.recorder = recorder

    async def run(self, state: AgentState) -> dict[str, Any]:
        started = time.monotonic()
        payload = {
            "message": state["user_message"],
            "slots": state.get("slots", {}),
            "conversation_summary": state.get("conversation_summary"),
        }
        try:
            result = await self.gateway.generate_structured(
                worker="intent",
                system_prompt=self.system_prompt,
                payload=payload,
                response_model=IntentOutput,
                prompt_version=self.prompt_version,
            )
            output = result.output
            record_worker_event(
                self.recorder,
                state,
                worker=self.name,
                event_type="model.completed",
                payload={
                    "intent": output.intent.value,
                    "missing_slots": output.missing_slots,
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
                "phase": Phase.INTENT.value,
                "intent": output.intent.value,
                "intent_confidence": output.confidence,
                "slots": merge_slots(state.get("slots", {}), output.extracted_slots),
                "missing_slots": output.missing_slots,
                "prompt_versions": {
                    **state.get("prompt_versions", {}),
                    self.name: self.prompt_version,
                },
            }
        except Exception as exc:
            output = _fallback_intent(state["user_message"], state.get("slots", {}))
            record_worker_event(
                self.recorder,
                state,
                worker=self.name,
                event_type="model.fallback",
                payload={"error_type": type(exc).__name__, "intent": output.intent.value},
                duration_ms=(time.monotonic() - started) * 1000,
            )
            return {
                "phase": Phase.INTENT.value,
                "intent": output.intent.value,
                "intent_confidence": output.confidence,
                "slots": merge_slots(state.get("slots", {}), output.extracted_slots),
                "missing_slots": output.missing_slots,
                "fallback_events": fallback_update(state, self.name, "MODEL_OR_SCHEMA_ERROR"),
                "prompt_versions": {
                    **state.get("prompt_versions", {}),
                    self.name: self.prompt_version,
                },
            }


def _fallback_intent(message: str, slots: dict[str, Any]) -> IntentOutput:
    extracted = dict(slots)
    for keyword, value in {
        "早餐": "breakfast",
        "午餐": "lunch",
        "晚餐": "dinner",
        "夜宵": "late_snack",
    }.items():
        if keyword in message:
            extracted["meal_type"] = value
            break
    if any(word in message for word in ("诊断", "药物", "绝食", "催吐")):
        intent = Intent.HEALTH_RISK
    elif any(word in message for word in ("营养", "热量", "蛋白质")):
        intent = Intent.NUTRITION_QUERY
    elif any(word in message for word in ("一周", "计划", "餐单")):
        intent = Intent.MEAL_PLAN
    elif any(word in message for word in ("吃", "推荐", "早餐", "午餐", "晚餐")):
        intent = Intent.MEAL_RECOMMENDATION
    else:
        intent = Intent.OTHER
    missing = (
        ["meal_type"]
        if intent is Intent.MEAL_RECOMMENDATION and not extracted.get("meal_type")
        else []
    )
    return IntentOutput(
        intent=intent,
        confidence=0.55,
        extracted_slots=extracted,
        missing_slots=missing,
        notes="rule fallback",
    )
