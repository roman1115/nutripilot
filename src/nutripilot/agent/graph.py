"""The single LangGraph Supervisor for the online Agent workflow."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from functools import partial
from typing import Any, cast

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from nutripilot.agent.agents import IntentAgent, PlannerAgent, RecommendationAgent
from nutripilot.agent.nodes import (
    BlockedResponseNode,
    BudgetExceededNode,
    ClarifyNode,
    ConfirmationNode,
    OutputGuardNode,
    RiskNode,
    ScopeResponseNode,
    ToolExecutionNode,
)
from nutripilot.agent.routing import (
    route_after_confirmation,
    route_after_intent,
    route_after_planner,
    route_after_risk,
    route_after_tool,
    route_entry,
)
from nutripilot.agent.state import AgentState, state_identity
from nutripilot.domain.enums import Phase
from nutripilot.domain.models import FinalResponse
from nutripilot.model_gateway import ModelGateway
from nutripilot.model_gateway.budget import (
    BudgetedModelGateway,
    ExecutionLimitExceeded,
    execution_budget,
)
from nutripilot.safety import OutputGuard, RiskGuard
from nutripilot.tools import ToolExecutor
from nutripilot.tracing import TraceRecorder


class AgentGraph:
    """Typed facade around LangGraph's compiled runnable."""

    def __init__(
        self,
        compiled: Any,
        recorder: TraceRecorder,
        deadline_seconds: float,
        token_budget: int,
    ) -> None:
        self.compiled = compiled
        self.recorder = recorder
        self.deadline_seconds = deadline_seconds
        self.token_budget = token_budget

    async def ainvoke(self, state: AgentState, config: dict[str, Any]) -> AgentState:
        with execution_budget(seconds=self.deadline_seconds, tokens=self.token_budget):
            try:
                async with asyncio.timeout(self.deadline_seconds):
                    result = await self.compiled.ainvoke(state, config=config)
                return cast("AgentState", result)
            except (TimeoutError, ExecutionLimitExceeded) as exc:
                code = exc.code if isinstance(exc, ExecutionLimitExceeded) else "DEADLINE_EXCEEDED"
                return await self._terminate(state, config, code)
            except asyncio.CancelledError:
                await self._terminate(state, config, "CANCELLED")
                raise

    async def aget_state(self, config: dict[str, Any]) -> AgentState | None:
        snapshot = await self.compiled.aget_state(config)
        values = getattr(snapshot, "values", None)
        return cast("AgentState", values) if values else None

    async def astream_updates(
        self,
        state: AgentState,
        config: dict[str, Any],
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        with execution_budget(seconds=self.deadline_seconds, tokens=self.token_budget):
            try:
                async with asyncio.timeout(self.deadline_seconds):
                    async for chunk in self.compiled.astream(
                        state, config=config, stream_mode="updates"
                    ):
                        if not isinstance(chunk, dict):
                            continue
                        for node, update in chunk.items():
                            if isinstance(node, str) and isinstance(update, dict):
                                yield node, cast("dict[str, Any]", update)
            except (TimeoutError, ExecutionLimitExceeded) as exc:
                code = exc.code if isinstance(exc, ExecutionLimitExceeded) else "DEADLINE_EXCEEDED"
                terminated = await self._terminate(state, config, code)
                yield "budget", cast("dict[str, Any]", terminated)
            except asyncio.CancelledError:
                await self._terminate(state, config, "CANCELLED")
                raise

    def _record_termination(self, state: AgentState, code: str) -> None:
        self.recorder.record(
            trace_id=state["trace_id"],
            thread_id=state["thread_id"],
            turn_id=state["turn_id"],
            user_id=state["user_id"],
            event_type=f"execution.{code.lower()}",
            component="Supervisor",
            phase_after=Phase.FAILED.value,
            payload={"code": code},
        )

    async def _terminate(self, state: AgentState, config: dict[str, Any], code: str) -> AgentState:
        self._record_termination(state, code)
        response = FinalResponse(
            status="degraded",
            message=(
                "已停止本轮请求。已经完成的数据写入不会因停止而撤销，请核对已保存计划。"
                if code == "CANCELLED"
                else "本次请求达到总时限，已停止继续调用。"
                if code == "DEADLINE_EXCEEDED"
                else "本次请求达到 Token 预算，已停止继续调用。"
            ),
            suggested_actions=["缩小需求范围后重试"],
        )
        update: dict[str, Any] = {
            **state_identity(state),
            "phase": Phase.FAILED.value,
            "pending_tool_calls": [],
            "requires_confirmation": False,
            "error": {"code": code, "retryable": False},
            "final_response": response.model_dump(mode="json"),
        }
        await self.compiled.aupdate_state(config, update, as_node="budget")
        final = await self.aget_state(config)
        assert final is not None
        return final


def create_agent_graph(
    *,
    gateway: ModelGateway,
    tool_executor: ToolExecutor,
    recorder: TraceRecorder,
    checkpointer: Any | None = None,
    max_tool_steps: int = 8,
    deadline_seconds: float = 90,
    token_budget: int = 24000,
) -> AgentGraph:
    """Build the deterministic graph with three online LLM Worker Agents."""

    gateway = BudgetedModelGateway(gateway)
    intent = IntentAgent(gateway, recorder)
    planner = PlannerAgent(gateway, recorder, tool_executor.registry)
    recommendation = RecommendationAgent(gateway, recorder)
    risk = RiskNode(RiskGuard(), recorder)
    blocked = BlockedResponseNode(recorder)
    scope = ScopeResponseNode(recorder)
    clarify = ClarifyNode(recorder)
    tool = ToolExecutionNode(tool_executor, recorder)
    confirmation = ConfirmationNode(recorder)
    output = OutputGuardNode(OutputGuard(), recorder)
    budget = BudgetExceededNode(recorder)

    graph = StateGraph(AgentState)
    graph.add_node("risk", risk.run)
    graph.add_node("blocked", blocked.run)
    graph.add_node("intent", intent.run)
    graph.add_node("scope", scope.run)
    graph.add_node("clarify", clarify.run)
    graph.add_node("planner", planner.run)
    graph.add_node("tool", tool.run)
    graph.add_node("confirmation", confirmation.run)
    graph.add_node("recommendation", recommendation.run)
    graph.add_node("output_guard", output.run)
    graph.add_node("budget", budget.run)
    graph.add_node("tool_failure", tool_failure)

    graph.add_conditional_edges(
        START, route_entry, {"risk": "risk", "confirmation": "confirmation"}
    )
    graph.add_conditional_edges(
        "risk", route_after_risk, {"blocked": "blocked", "intent": "intent"}
    )
    graph.add_conditional_edges(
        "intent",
        route_after_intent,
        {"scope": "scope", "clarify": "clarify", "planner": "planner"},
    )
    graph.add_conditional_edges(
        "planner",
        partial(route_after_planner, max_tool_steps=max_tool_steps),
        {
            "tool": "tool",
            "recommendation": "recommendation",
            "budget": "budget",
            "tool_failure": "tool_failure",
        },
    )
    graph.add_conditional_edges(
        "tool",
        partial(route_after_tool, max_tool_steps=max_tool_steps),
        {
            "confirmation": "confirmation",
            "planner": "planner",
            "budget": "budget",
            "tool_failure": "tool_failure",
        },
    )
    graph.add_conditional_edges(
        "confirmation",
        route_after_confirmation,
        {"tool": "tool", "end": END},
    )
    graph.add_edge("recommendation", "output_guard")
    graph.add_edge("blocked", END)
    graph.add_edge("scope", END)
    graph.add_edge("clarify", END)
    graph.add_edge("output_guard", END)
    graph.add_edge("budget", END)
    graph.add_edge("tool_failure", END)

    return AgentGraph(
        graph.compile(checkpointer=checkpointer or InMemorySaver()),
        recorder,
        deadline_seconds,
        token_budget,
    )


async def tool_failure(state: AgentState) -> dict[str, Any]:
    results = state.get("tool_results", [])
    error = state.get("error") or (results[-1].get("error") if results else None) or {}
    code = error.get("code")
    candidates = error.get("details", {}).get("candidates", [])
    clarification = code in {"MEAL_NAME_AMBIGUOUS", "MEAL_NOT_FOUND"}
    if code == "MEAL_NAME_AMBIGUOUS":
        names = [item["name"] for item in candidates if isinstance(item, dict) and "name" in item]
        message = f"请确认你要查询的是哪一道餐食：{'、'.join(names)}。"
    elif code == "MEAL_NOT_FOUND":
        message = "没有找到该餐食的可靠营养记录，请提供完整菜名后再试。"
    elif str(code).startswith("PLAN_NUTRITION_"):
        message = str(error.get("message") or "当前餐食无法满足每日营养目标，已阻止写入。")
    else:
        message = "本次工具调用未能完成，已停止重复尝试，请调整需求后重试。"
    response = FinalResponse(
        status="needs_clarification" if clarification else "degraded",
        message=message,
        suggested_actions=[
            item["name"] for item in candidates if isinstance(item, dict) and "name" in item
        ],
    )
    return {
        "phase": Phase.CLARIFY.value if clarification else Phase.FAILED.value,
        "pending_tool_calls": [],
        "requires_confirmation": False,
        "error": error,
        "final_response": response.model_dump(mode="json"),
    }
