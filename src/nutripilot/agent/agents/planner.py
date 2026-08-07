"""Tool-planning Worker Agent."""

from __future__ import annotations

import time
from typing import Any

from nutripilot.agent.agents.base import fallback_update, record_worker_event
from nutripilot.agent.contracts import PlannerOutput
from nutripilot.agent.state import AgentState
from nutripilot.domain.enums import Intent, Phase
from nutripilot.model_gateway import ModelGateway
from nutripilot.safety import merge_dietary_terms
from nutripilot.tools import ToolCall, ToolRegistry
from nutripilot.tracing import TraceRecorder

_TOOLS_BY_INTENT: dict[str, set[str]] = {
    Intent.MEAL_RECOMMENDATION.value: {
        "get_user_profile",
        "get_user_feedback",
        "get_weather_context",
        "search_meals",
    },
    Intent.MEAL_ADJUSTMENT.value: {
        "get_user_profile",
        "get_user_feedback",
        "get_weather_context",
        "search_meals",
    },
    Intent.MEAL_PLAN.value: {
        "get_user_profile",
        "get_user_feedback",
        "get_weather_context",
        "search_meals",
        "create_meal_plan",
    },
    Intent.NUTRITION_QUERY.value: {"get_nutrition", "query_dietary_guidelines"},
    Intent.PROFILE_UPDATE.value: {"update_user_profile"},
}


class PlannerAgent:
    name = "PlannerAgent"
    prompt_version = "planner-v1"
    system_prompt = (
        "你是工具规划 Worker。根据结构化状态选择白名单工具；一次只生成必要调用，"
        "不得虚构工具，不得决定图路由或绕过写操作确认。对于推荐、调整或计划，"
        "必须先读取用户画像和历史反馈，再检索餐食。"
    )
    reads = frozenset({"intent", "slots", "tool_results", "candidate_ids"})
    writes = frozenset({"execution_plan", "pending_tool_calls"})

    def __init__(
        self,
        gateway: ModelGateway,
        recorder: TraceRecorder,
        registry: ToolRegistry,
    ) -> None:
        self.gateway = gateway
        self.recorder = recorder
        self.registry = registry

    async def run(self, state: AgentState) -> dict[str, Any]:
        started = time.monotonic()
        intent = str(state.get("intent") or "")
        allowed_tools = _TOOLS_BY_INTENT.get(intent, set())
        payload = {
            "intent": intent,
            "slots": state.get("slots", {}),
            "tool_results": state.get("tool_results", []),
            "candidate_ids": state.get("candidate_ids", []),
            "turn_id": state["turn_id"],
            "user_message": state["user_message"],
            "available_tools": self.registry.planner_schemas(allowed_tools),
        }
        try:
            result = await self.gateway.generate_structured(
                worker="planner",
                system_prompt=self.system_prompt,
                payload=payload,
                response_model=PlannerOutput,
                prompt_version=self.prompt_version,
            )
            output = result.output
            self._validate_tool_plan(output, allowed_tools)
            record_worker_event(
                self.recorder,
                state,
                worker=self.name,
                event_type="model.completed",
                payload={
                    "complete": output.complete,
                    "tools": [call.name for call in output.tool_calls],
                    "model": result.model,
                    "attempts": result.attempts,
                    "used_fallback_model": result.used_fallback,
                    "prompt_version": self.prompt_version,
                },
                duration_ms=(time.monotonic() - started) * 1000,
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
            )
            return self._update(state, output)
        except Exception as exc:
            output = _fallback_plan(state)
            record_worker_event(
                self.recorder,
                state,
                worker=self.name,
                event_type="model.fallback",
                payload={
                    "error_type": type(exc).__name__,
                    "tools": [c.name for c in output.tool_calls],
                },
                duration_ms=(time.monotonic() - started) * 1000,
            )
            update = self._update(state, output)
            update["fallback_events"] = fallback_update(state, self.name, "MODEL_OR_SCHEMA_ERROR")
            return update

    def _validate_tool_plan(
        self,
        output: PlannerOutput,
        allowed_tools: set[str],
    ) -> None:
        for call in output.tool_calls:
            if call.name not in allowed_tools or call.name not in self.registry:
                raise ValueError(
                    f"Planner selected a tool outside the intent allowlist: {call.name}"
                )
            definition = self.registry.require(call.name)
            definition.arguments_model.model_validate(call.arguments)

    def _update(self, state: AgentState, output: PlannerOutput) -> dict[str, Any]:
        calls = [call.model_dump(mode="json") for call in output.tool_calls]
        return {
            "phase": Phase.PLANNING.value,
            "execution_plan": [
                *state.get("execution_plan", []),
                {
                    "summary": output.plan_summary,
                    "tools": [call.name for call in output.tool_calls],
                },
            ],
            "pending_tool_calls": calls,
            "prompt_versions": {
                **state.get("prompt_versions", {}),
                self.name: self.prompt_version,
            },
        }


def _fallback_plan(state: AgentState) -> PlannerOutput:
    called = {str(result.get("tool_name")) for result in state.get("tool_results", [])}
    intent = state.get("intent")
    call: ToolCall | None = None
    if (
        intent
        in {
            Intent.MEAL_RECOMMENDATION.value,
            Intent.MEAL_ADJUSTMENT.value,
            Intent.MEAL_PLAN.value,
        }
    ):
        if "get_user_profile" not in called:
            call = ToolCall(
                call_id=f"{state['turn_id']}:fallback:get_user_profile",
                name="get_user_profile",
                arguments={},
            )
        elif "get_user_feedback" not in called:
            call = ToolCall(
                call_id=f"{state['turn_id']}:fallback:get_user_feedback",
                name="get_user_feedback",
                arguments={},
            )
        elif "search_meals" not in called:
            call = ToolCall(
                call_id=f"{state['turn_id']}:fallback:search_meals",
                name="search_meals",
                arguments={
                    "query": state["user_message"],
                    "meal_type": state.get("slots", {}).get("meal_type"),
                    "excluded_allergens": state.get("slots", {}).get("allergens", []),
                    "excluded_ingredients": merge_dietary_terms(
                        state.get("slots", {}).get("excluded_ingredients"),
                        state.get("slots", {}).get("dislikes"),
                        state.get("slots", {}).get("allergens"),
                    ),
                    "excluded_meal_ids": _feedback_excluded_ids(
                        state.get("tool_results", [])
                    ),
                    "limit": 10 if intent == Intent.MEAL_PLAN.value else 3,
                },
            )
    elif intent == Intent.NUTRITION_QUERY.value and "get_nutrition" not in called:
        call = ToolCall(
            call_id=f"{state['turn_id']}:fallback:get_nutrition",
            name="get_nutrition",
            arguments={"food_name": state.get("slots", {}).get("food_name", "")},
        )
    return PlannerOutput(
        complete=call is None,
        tool_calls=[call] if call else [],
        plan_summary="规则降级计划",
    )


def _feedback_excluded_ids(results: list[dict[str, Any]]) -> list[int]:
    for result in reversed(results):
        if result.get("tool_name") != "get_user_feedback" or result.get("status") != "success":
            continue
        data = result.get("data")
        if isinstance(data, dict) and isinstance(data.get("excluded_meal_ids"), list):
            return sorted(
                {meal_id for meal_id in data["excluded_meal_ids"] if isinstance(meal_id, int)}
            )
    return []
