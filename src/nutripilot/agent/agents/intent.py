"""Intent and slot extraction Worker Agent."""

from __future__ import annotations

import time
from datetime import date
from typing import Any

from nutripilot.agent.agents.base import fallback_update, record_worker_event
from nutripilot.agent.contracts import IntentOutput
from nutripilot.agent.slots import extract_plan_scope
from nutripilot.agent.state import AgentState
from nutripilot.domain.enums import Intent, Phase
from nutripilot.memory.context import merge_slots
from nutripilot.model_gateway import ModelGateway, ModelUsage
from nutripilot.model_gateway.budget import ExecutionLimitExceeded
from nutripilot.tracing import TraceRecorder


class IntentAgent:
    name = "IntentAgent"
    prompt_version = "intent-v3"
    system_prompt = (
        "你是饮食意图识别 Worker。仅依据用户消息和已有状态识别意图、抽取槽位；"
        "不得调用工具或提供医疗结论，必须返回符合给定 Schema 的结构化结果。"
        "extracted_slots 仅允许 meal_type、plan_range、start_date（YYYY-MM-DD）、food_name、"
        "location、tastes、preferences、cuisines、health_goals、dietary_tags、allergens、"
        "dislikes、excluded_ingredients、max_calories_kcal、min_protein_g、max_price_cny、"
        "max_prep_minutes、daily_calorie_target、daily_protein_target_g。"
        "每日/全天热量和蛋白质目标必须放入 daily_* 字段，不得误用为单餐检索上下限。"
        "依据 current_date 解析相对日期。"
        "计划默认一周三餐，不要把可选的饮食限制作为必填项。"
    )
    reads = frozenset({"user_message", "slots", "conversation_summary"})
    writes = frozenset({"intent", "intent_confidence", "slots", "missing_slots"})

    def __init__(self, gateway: ModelGateway, recorder: TraceRecorder) -> None:
        self.gateway = gateway
        self.recorder = recorder

    async def run(self, state: AgentState) -> dict[str, Any]:
        started = time.monotonic()
        current_date = date.fromisoformat(state["current_date"])
        explicit, invalid = extract_plan_scope(
            state["user_message"], current_date=current_date
        )
        if invalid and any(term in state["user_message"] for term in ("计划", "餐单")):
            record_worker_event(
                self.recorder,
                state,
                worker=self.name,
                event_type="intent.scope_validated",
                payload={"invalid_fields": invalid, "deterministic": True},
            )
            return {
                "phase": Phase.INTENT.value,
                "intent": Intent.MEAL_PLAN.value,
                "intent_confidence": 1.0,
                "slots": merge_slots(state.get("slots", {}), explicit),
                "missing_slots": invalid,
                "prompt_versions": {
                    **state.get("prompt_versions", {}),
                    self.name: self.prompt_version,
                },
            }
        payload = {
            "message": state["user_message"],
            "slots": state.get("slots", {}),
            "conversation_summary": state.get("conversation_summary"),
            "current_date": state["current_date"],
            "clarification_intent": state.get("clarification_intent"),
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
            slots, missing = _resolved_slots(state, output)
            return {
                "phase": Phase.INTENT.value,
                "intent": output.intent.value,
                "intent_confidence": output.confidence,
                "slots": slots,
                "missing_slots": missing,
                "prompt_versions": {
                    **state.get("prompt_versions", {}),
                    self.name: self.prompt_version,
                },
            }
        except ExecutionLimitExceeded:
            raise
        except Exception as exc:
            output = _fallback_intent(state["user_message"], state.get("slots", {}))
            usage = getattr(exc, "usage", ModelUsage())
            record_worker_event(
                self.recorder,
                state,
                worker=self.name,
                event_type="model.fallback",
                payload={
                    "error_type": type(exc).__name__,
                    "intent": output.intent.value,
                    "error_code": getattr(exc, "code", "MODEL_OR_SCHEMA_ERROR"),
                },
                duration_ms=(time.monotonic() - started) * 1000,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
            )
            slots, missing = _resolved_slots(state, output)
            return {
                "phase": Phase.INTENT.value,
                "intent": output.intent.value,
                "intent_confidence": output.confidence,
                "slots": slots,
                "missing_slots": missing,
                "fallback_events": fallback_update(state, self.name, "MODEL_OR_SCHEMA_ERROR"),
                "prompt_versions": {
                    **state.get("prompt_versions", {}),
                    self.name: self.prompt_version,
                },
            }


def _required_missing(output: IntentOutput, slots: dict[str, Any]) -> list[str]:
    if output.intent is Intent.MEAL_RECOMMENDATION:
        return [] if slots.get("meal_type") else ["meal_type"]
    if output.intent is Intent.NUTRITION_QUERY:
        return [] if slots.get("food_name") else ["food_name"]
    if output.intent is Intent.PROFILE_UPDATE:
        fields = {"preferences", "allergens", "dislikes", "health_goals", "dietary_tags"}
        return [] if any(slots.get(field) for field in fields) else ["preference"]
    return []


def _resolved_slots(state: AgentState, output: IntentOutput) -> tuple[dict[str, Any], list[str]]:
    slots = merge_slots(state.get("slots", {}), output.extracted_slots)
    if output.intent is Intent.MEAL_PLAN:
        explicit, missing = extract_plan_scope(
            state["user_message"],
            current_date=date.fromisoformat(state["current_date"]),
        )
        slots.update(explicit)
        if slots.get("max_calories_kcal") == explicit.get("daily_calorie_target"):
            slots.pop("max_calories_kcal", None)
        if slots.get("min_protein_g") == explicit.get("daily_protein_target_g"):
            slots.pop("min_protein_g", None)
        return slots, missing
    return slots, _required_missing(output, slots)


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
    elif any(word in message for word in ("一周", "计划", "餐单")):
        intent = Intent.MEAL_PLAN
    elif any(word in message for word in ("营养", "热量", "蛋白质")):
        intent = Intent.NUTRITION_QUERY
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
