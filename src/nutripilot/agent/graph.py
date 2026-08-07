"""The single LangGraph Supervisor for the online Agent workflow."""

from __future__ import annotations

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
from nutripilot.agent.state import AgentState
from nutripilot.model_gateway import ModelGateway
from nutripilot.safety import OutputGuard, RiskGuard
from nutripilot.tools import ToolExecutor
from nutripilot.tracing import TraceRecorder


class AgentGraph:
    """Typed facade around LangGraph's compiled runnable."""

    def __init__(self, compiled: Any) -> None:
        self.compiled = compiled

    async def ainvoke(self, state: AgentState, config: dict[str, Any]) -> AgentState:
        result = await self.compiled.ainvoke(state, config=config)
        return cast("AgentState", result)

    async def aget_state(self, config: dict[str, Any]) -> AgentState | None:
        snapshot = await self.compiled.aget_state(config)
        values = getattr(snapshot, "values", None)
        return cast("AgentState", values) if values else None

    async def astream_updates(
        self,
        state: AgentState,
        config: dict[str, Any],
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        async for chunk in self.compiled.astream(state, config=config, stream_mode="updates"):
            if not isinstance(chunk, dict):
                continue
            for node, update in chunk.items():
                if isinstance(node, str) and isinstance(update, dict):
                    yield node, cast("dict[str, Any]", update)


def create_agent_graph(
    *,
    gateway: ModelGateway,
    tool_executor: ToolExecutor,
    recorder: TraceRecorder,
    checkpointer: Any | None = None,
    max_tool_steps: int = 8,
) -> AgentGraph:
    """Build the deterministic graph with three online LLM Worker Agents."""

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
        {"tool": "tool", "recommendation": "recommendation", "budget": "budget"},
    )
    graph.add_conditional_edges(
        "tool",
        partial(route_after_tool, max_tool_steps=max_tool_steps),
        {"confirmation": "confirmation", "planner": "planner", "budget": "budget"},
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

    return AgentGraph(graph.compile(checkpointer=checkpointer or InMemorySaver()))
