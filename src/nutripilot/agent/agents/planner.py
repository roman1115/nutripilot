"""Tool-planning Worker Agent."""

from __future__ import annotations

import time
from typing import Any

from nutripilot.agent.agents.base import fallback_update, record_worker_event
from nutripilot.agent.contracts import PlannerOutput, PlanSpec
from nutripilot.agent.planning import PlanBuilder, PlanBuildError, candidate_meals_from_results
from nutripilot.agent.state import AgentState
from nutripilot.domain.enums import Intent, Phase
from nutripilot.model_gateway import ModelGateway, ModelUsage
from nutripilot.model_gateway.budget import ExecutionLimitExceeded
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
    prompt_version = "planner-v2"
    system_prompt = (
        "你是工具规划 Worker。根据结构化状态选择白名单工具；一次只生成必要调用，"
        "不得虚构工具，不得决定图路由或绕过写操作确认。对于推荐、调整或计划，"
        "必须先读取用户画像和历史反馈，再检索餐食。计划检索 limit=20，"
        "三餐计划不得把检索 meal_type 限定为某一餐。"
        "若提供 verified_plan，创建计划必须原样采用它，禁止编造份量与营养数值。"
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
            "tool_results": compact_tool_results(state.get("tool_results", [])),
            "candidate_ids": state.get("candidate_ids", []),
            "turn_id": state["turn_id"],
            "user_message": state["user_message"],
            "available_tools": self.registry.planner_schemas(allowed_tools),
        }
        if (
            intent == Intent.MEAL_PLAN.value
            and candidate_meals_from_results(state.get("tool_results", []))
            and not any(
                result.get("tool_name") == "create_meal_plan"
                for result in state.get("tool_results", [])
            )
        ):
            try:
                plan = PlanBuilder().build(
                    _plan_spec(state), candidate_meals_from_results(state.get("tool_results", []))
                )
            except PlanBuildError as exc:
                return {
                    "phase": Phase.PLANNING.value,
                    "pending_tool_calls": [],
                    "error": {"code": exc.code, "message": exc.safe_message, "retryable": False},
                }
            payload["verified_plan"] = plan.model_dump(mode="json")
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
        except ExecutionLimitExceeded:
            raise
        except Exception as exc:
            output = _fallback_plan(state)
            usage = getattr(exc, "usage", ModelUsage())
            record_worker_event(
                self.recorder,
                state,
                worker=self.name,
                event_type="model.fallback",
                payload={
                    "error_type": type(exc).__name__,
                    "error_code": getattr(exc, "code", "MODEL_OR_SCHEMA_ERROR"),
                    "tools": [c.name for c in output.tool_calls],
                },
                duration_ms=(time.monotonic() - started) * 1000,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
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
        if (
            state.get("intent") == Intent.MEAL_PLAN.value
            and not output.tool_calls
            and candidate_meals_from_results(state.get("tool_results", []))
            and not any(
                result.get("tool_name") == "create_meal_plan"
                for result in state.get("tool_results", [])
            )
        ):
            plan = PlanBuilder().build(
                _plan_spec(state), candidate_meals_from_results(state.get("tool_results", []))
            )
            output = output.model_copy(
                update={
                    "complete": False,
                    "tool_calls": [
                        ToolCall(
                            call_id=f"{state['turn_id']}:verified-plan",
                            name="create_meal_plan",
                            arguments=plan.model_dump(mode="json"),
                        )
                    ],
                }
            )
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
    if intent in {
        Intent.MEAL_RECOMMENDATION.value,
        Intent.MEAL_ADJUSTMENT.value,
        Intent.MEAL_PLAN.value,
    }:
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
                    "excluded_meal_ids": _feedback_excluded_ids(state.get("tool_results", [])),
                    "limit": 20 if intent == Intent.MEAL_PLAN.value else 3,
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


def _plan_spec(state: AgentState) -> PlanSpec:
    profile: dict[str, Any] = next(
        (
            result["data"]
            for result in reversed(state.get("tool_results", []))
            if result.get("tool_name") == "get_user_profile"
            and result.get("status") == "success"
            and isinstance(result.get("data"), dict)
        ),
        {},
    )
    return PlanSpec.from_slots(state.get("slots", {}), profile=profile)


def compact_tool_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Preserve planning facts without repeatedly sending full retrieval documents."""

    compact = []
    for result in results:
        item = {key: result[key] for key in ("tool_name", "status", "error") if key in result}
        data = result.get("data")
        if isinstance(data, dict) and result.get("tool_name") == "search_meals":
            item["data"] = {
                "hits": [
                    {
                        key: meal[key]
                        for key in (
                            "id",
                            "meal_id",
                            "name",
                            "meal_types",
                            "ingredients",
                            "allergens",
                            "nutrition",
                        )
                        if key in meal
                    }
                    for hit in data.get("hits", [])
                    if isinstance(hit, dict) and isinstance(meal := hit.get("meal", hit), dict)
                ]
            }
        elif isinstance(data, dict) and result.get("tool_name") == "get_user_feedback":
            item["data"] = {"excluded_meal_ids": data.get("excluded_meal_ids", [])}
        else:
            item["data"] = data
        compact.append(item)
    return compact
