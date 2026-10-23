"""Pure routing policies owned by the deterministic Supervisor."""

from nutripilot.agent.state import AgentState
from nutripilot.domain.enums import Intent, Phase

DEFAULT_MAX_TOOL_STEPS = 8


def route_entry(state: AgentState) -> str:
    if (
        state.get("phase") == Phase.AWAITING_CONFIRMATION.value
        and state.get("confirmation_decision") is not None
    ):
        return "confirmation"
    return "risk"


def route_after_risk(state: AgentState) -> str:
    return "blocked" if state.get("blocked") else "intent"


def route_after_intent(state: AgentState) -> str:
    if state.get("intent") in {Intent.HEALTH_RISK.value, Intent.OTHER.value}:
        return "scope"
    if state.get("missing_slots"):
        return "clarify"
    return "planner"


def route_after_planner(
    state: AgentState,
    *,
    max_tool_steps: int = DEFAULT_MAX_TOOL_STEPS,
) -> str:
    if state.get("error"):
        return "tool_failure"
    if state.get("step_count", 0) >= max_tool_steps and state.get("pending_tool_calls"):
        return "budget"
    return "tool" if state.get("pending_tool_calls") else "recommendation"


def route_after_tool(
    state: AgentState,
    *,
    max_tool_steps: int = DEFAULT_MAX_TOOL_STEPS,
) -> str:
    if state.get("requires_confirmation"):
        return "confirmation"
    results = state.get("tool_results", [])
    if results:
        error = results[-1].get("error")
        if isinstance(error, dict) and not error.get("retryable", False):
            return "tool_failure"
    if state.get("step_count", 0) >= max_tool_steps:
        return "budget"
    return "planner"


def route_after_confirmation(state: AgentState) -> str:
    if state.get("confirmation_decision") is True:
        return "tool"
    return "end"
