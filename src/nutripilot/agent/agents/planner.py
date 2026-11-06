"""Tool-planning Worker Agent."""

from __future__ import annotations

import time
from datetime import date
from typing import Any

from pydantic import ValidationError

from nutripilot.agent.agents.base import (
    compact_tool_results,
    fallback_update,
    record_worker_event,
)
from nutripilot.agent.answers import answer_evidence
from nutripilot.agent.contracts import PlannerOutput, PlanSpec
from nutripilot.agent.planning import PlanBuilder, PlanBuildError, candidate_meals_from_results
from nutripilot.agent.state import AgentState
from nutripilot.domain.enums import Intent, Phase
from nutripilot.domain.execution import ExecutionOwnershipLost
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
    Intent.NUTRITION_QUERY.value: {
        "get_user_profile", "get_nutrition", "query_dietary_guidelines",
    },
    Intent.RECIPE_QUERY.value: {"get_user_profile", "get_recipe"},
    Intent.FOOD_QUESTION.value: {
        "get_user_profile", "get_recipe", "get_nutrition", "query_dietary_guidelines",
    },
    Intent.PROFILE_UPDATE.value: {"update_user_profile"},
}


class PlannerAgent:
    name = "PlannerAgent"
    prompt_version = "planner-v17"
    system_prompt = (
        "你是工具规划 Worker。根据结构化状态选择白名单工具；一次只生成必要调用，"
        "不得虚构工具，不得决定图路由或绕过写操作确认。对于推荐、调整或计划，"
        "必须先读取用户画像和历史反馈，再检索菜谱。餐单按日期和餐次安排整道菜，"
        "不推算人数或个人份量，不判断个人每日营养目标达成；单道菜不代表完整一餐。"
        "计划检索只需一次 search_meals 调用，由工具边界分别覆盖所需餐次。"
        "普通推荐或换菜在本轮成功检索到候选后应返回 complete=true，避免重复检索。"
        "换一批应选择本轮新候选，不必继续寻找已被排除的上一批菜名。"
        "若提供 verified_plan，创建计划必须原样采用它，禁止编造份量与营养数值。"
        "RECIPE_QUERY 必须用 get_recipe 查询 food_name，不能用推荐或营养查询代替做法。"
        "search_meals 必须传递本轮 required_ingredients，必含食材不得替换为相近食材；"
        "可以少于三道，没有匹配不能用其它食材的菜凑数。"
        "同时原样传递ingredient_any_groups，每组任选一项，组间全部满足。"
        "即使仅靠结构化食材条件检索，query也应填用户本轮原话，不得留空。"
        "slots中的数值限制为本轮最终条件，值为null表示已取消，不得从旧消息重新添加；"
        "有新数值时使用新值，不能继续沿用旧值。"
        "food_name 为点名对象，必须传给 search_meals，不得用其他菜代替。"
        "用户明确根据天气选菜时必须查询天气；其他推荐不要为天气额外调用。"
        "首次可以一起读取画像和反馈；已有所需资料就检索，不重复读取。"
        "answer_task表达本轮目标。替换指定菜品时不要排除其原料或无关菜品。"
        "lower_fat表示想比原菜脂肪更少，不是固定低脂标准；保留高蛋白等已有目标，"
        "检索足够候选供回答Agent比较营养，不编造脂肪上限。"
        "answer_task只供理解任务，不把meal_ids、nutrient_preference等任务字段作为工具参数；"
        "调用参数严格使用available_tools的schema。"
        "FOOD_QUESTION先判断conversation_evidence能否支持本轮解释或比较，足够则complete=true。"
        "一般烹饪原理可直接解释，不强制查菜谱。具体配方细节缺失时用get_recipe，"
        "具体营养缺失时用get_nutrition，一般营养资料可query_dietary_guidelines。"
        "比较菜品制作方式时，不能从菜名猜步骤；先get_recipe补齐被比较菜的原始配方。"
        "没有说明比较维度的菜品比较，优先补齐前两道原始配方后再回答。"
        "不为解释、比较或技巧问答检索新推荐，不允许写入资料或餐单；"
        "未知对象不要猜菜名，由回答阶段追问。重复查询无必要，资料足够就结束。"
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
        # A successful search already completes ordinary read-only recommendation planning.
        if intent in {Intent.MEAL_RECOMMENDATION.value, Intent.MEAL_ADJUSTMENT.value} and any(
            result.get("tool_name") == "search_meals" and result.get("status") == "success"
            for result in state.get("tool_results", [])
        ):
            return self._update(state, PlannerOutput(
                complete=True, plan_summary="候选已取得，由回答Agent结合本轮目标选择。",
            ))
        allowed_tools = _TOOLS_BY_INTENT.get(intent, set())
        payload = {
            "intent": intent,
            "slots": state.get("slots", {}),
            "tool_results": compact_tool_results(state.get("tool_results", [])),
            "candidate_ids": state.get("candidate_ids", []),
            "turn_id": state["turn_id"],
            "user_message": state["user_message"],
            "current_date": state["current_date"],
            "available_tools": self.registry.planner_schemas(allowed_tools),
            "answer_task": state.get("answer_task", {}),
            "recent_messages": [
                item for item in state.get("recent_messages", [])[-4:]
                if item.get("role") == "user"
            ],
            "conversation_evidence": answer_evidence(
                state.get("conversation_evidence", []), state, historical=True,
            ),
            "discussed_meals": state.get("discussed_meals", []),
        }
        if (
            intent == Intent.MEAL_PLAN.value
            and any(
                result.get("tool_name") == "search_meals" and result.get("status") == "success"
                for result in state.get("tool_results", [])
            )
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
        result = None
        try:
            result = await self.gateway.generate_structured(
                worker="planner",
                system_prompt=self.system_prompt,
                payload=payload,
                response_model=PlannerOutput,
                prompt_version=self.prompt_version,
            )
            output = result.output
            for call in output.tool_calls:
                query = call.arguments.get("query")
                if call.name == "search_meals" and (
                    query is None or (isinstance(query, str) and not query.strip())
                ):
                    call.arguments["query"] = state["user_message"].strip()[:500] or "菜品推荐"
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
        except (ExecutionLimitExceeded, ExecutionOwnershipLost):
            raise
        except Exception as exc:
            output = _fallback_plan(state)
            usage = result.usage if result is not None else getattr(exc, "usage", ModelUsage())
            record_worker_event(
                self.recorder,
                state,
                worker=self.name,
                event_type="model.fallback",
                payload={
                    "error_type": type(exc).__name__,
                    "error_code": getattr(exc, "code", "MODEL_OR_SCHEMA_ERROR"),
                    "tools": [c.name for c in output.tool_calls],
                    "validation_errors": [
                        {"field": list(error["loc"]), "type": error["type"]}
                        for error in exc.errors(include_input=False)[:10]
                    ] if isinstance(exc, ValidationError) else [],
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
            state.get("intent") == Intent.FOOD_QUESTION.value
            and not state.get("discussed_meals")
        ):
            calls = [
                call for call in output.tool_calls
                if not (
                    call.name in {"get_recipe", "get_nutrition"}
                    and any(call.arguments.get(key) in {
                        "那道菜", "这道菜", "这个", "那个", "它",
                    } for key in ("name", "food_name"))
                )
            ]
            if len(calls) != len(output.tool_calls):
                output = output.model_copy(update={"complete": not calls, "tool_calls": calls})
        if (
            state.get("intent") == Intent.FOOD_QUESTION.value
            and len(state.get("discussed_meals", [])) >= 2
            and "前两" in state["user_message"]
            and any(term in state["user_message"] for term in ("比较", "区别", "对比", "不同"))
            and not any(term in state["user_message"] for term in (
                "营养", "热量", "蛋白", "脂肪", "碳水", "钠",
            ))
            and not output.tool_calls
            and not any(
                result.get("tool_name") == "get_recipe"
                for result in state.get("tool_results", [])
            )
        ):
            known = {
                (result.get("data") or {}).get("meal_id")
                for result in state.get("conversation_evidence", [])
                if result.get("tool_name") == "get_recipe" and result.get("status") == "success"
            }
            calls = [
                ToolCall(
                    call_id=f"{state['turn_id']}:comparison:recipe:{meal['meal_id']}",
                    name="get_recipe", arguments={"meal_id": meal["meal_id"]},
                )
                for meal in state.get("discussed_meals", [])[:2]
                if meal["meal_id"] not in known
            ]
            if calls:
                output = output.model_copy(update={"complete": False, "tool_calls": calls})
        if state.get("intent") in {
            Intent.MEAL_RECOMMENDATION.value,
            Intent.MEAL_ADJUSTMENT.value,
            Intent.MEAL_PLAN.value,
        }:
            succeeded = {
                result.get("tool_name") for result in state.get("tool_results", [])
                if result.get("status") == "success"
            }
            if "search_meals" not in succeeded:
                if not output.tool_calls or any(
                    call.name == "create_meal_plan" for call in output.tool_calls
                ):
                    output = _fallback_plan(state)
                calls = list(output.tool_calls)
                if any(call.name == "search_meals" for call in calls):
                    # Order dependencies before search even when the planner skips or reverses them.
                    dependencies = ["get_user_profile", "get_user_feedback"]
                    if any(term in state["user_message"] for term in (
                        "天气", "下雨", "炎热", "寒冷", "降温", "天热", "天冷",
                    )):
                        dependencies.append("get_weather_context")
                    calls = [
                        *(
                            ToolCall(
                                call_id=f"{state['turn_id']}:required:{name}",
                                name=name,
                                arguments=(
                                    {"location": state.get("slots", {}).get("location", "北京")}
                                    if name == "get_weather_context" else {}
                                ),
                            )
                            for name in dependencies if name not in succeeded
                        ),
                        *(call for call in calls if call.name not in dependencies),
                    ]
                    output = output.model_copy(update={"complete": False, "tool_calls": calls})
        if state.get("intent") == Intent.RECIPE_QUERY.value and not state.get("tool_results"):
            output = _fallback_plan(state)
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
        if state.get("intent") in {
            Intent.RECIPE_QUERY.value, Intent.NUTRITION_QUERY.value, Intent.FOOD_QUESTION.value,
        } and not any(
            result.get("tool_name") == "get_user_profile" and result.get("status") == "success"
            for result in state.get("tool_results", [])
        ):
            output = output.model_copy(update={"tool_calls": [
                ToolCall(
                    call_id=f"{state['turn_id']}:required:get_user_profile",
                    name="get_user_profile", arguments={},
                ),
                *(call for call in output.tool_calls if call.name != "get_user_profile"),
            ]})
        serialized_calls = [call.model_dump(mode="json") for call in output.tool_calls]
        return {
            "phase": Phase.PLANNING.value,
            "execution_plan": [
                *state.get("execution_plan", []),
                {
                    "summary": output.plan_summary,
                    "tools": [call.name for call in output.tool_calls],
                    "complete": output.complete,
                },
            ],
            "pending_tool_calls": serialized_calls,
            "prompt_versions": {
                **state.get("prompt_versions", {}),
                self.name: self.prompt_version,
            },
        }


def _fallback_plan(state: AgentState) -> PlannerOutput:
    called = {
        str(result.get("tool_name")) for result in state.get("tool_results", [])
        if result.get("status") == "success"
    }
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
                    "required_ingredients": state.get("slots", {}).get("required_ingredients", []),
                    "ingredient_any_groups": state.get("slots", {}).get(
                        "ingredient_any_groups", []
                    ),
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
    elif intent == Intent.RECIPE_QUERY.value and "get_recipe" not in called:
        call = ToolCall(
            call_id=f"{state['turn_id']}:get_recipe",
            name="get_recipe",
            arguments={"name": state.get("slots", {}).get("food_name", "")},
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
    return PlanSpec.from_slots(
        state.get("slots", {}),
        start_date=date.fromisoformat(state["current_date"]),
        profile=profile,
    )
