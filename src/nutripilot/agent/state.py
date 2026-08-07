"""Serializable state shared by the deterministic supervisor and workers."""

from __future__ import annotations

from typing import Any, TypedDict
from uuid import uuid4

from nutripilot.domain.enums import Phase


class AgentState(TypedDict, total=False):
    trace_id: str
    thread_id: str
    turn_id: str
    user_id: int
    phase: str
    user_message: str
    intent: str | None
    intent_confidence: float | None
    slots: dict[str, Any]
    missing_slots: list[str]
    conversation_summary: str | None
    execution_plan: list[dict[str, Any]]
    pending_tool_calls: list[dict[str, Any]]
    tool_results: list[dict[str, Any]]
    candidate_ids: list[int]
    citations: list[dict[str, Any]]
    dietary_constraints: dict[str, list[str]]
    risk_flags: list[str]
    blocked: bool
    block_reason: str | None
    requires_confirmation: bool
    confirmation_decision: bool | None
    confirmed_call_ids: list[str]
    step_count: int
    tool_signatures: dict[str, int]
    fallback_events: list[dict[str, Any]]
    prompt_versions: dict[str, str]
    final_response: dict[str, Any] | None
    error: dict[str, Any] | None


def new_agent_state(
    *,
    message: str,
    user_id: int,
    thread_id: str | None = None,
    turn_id: str | None = None,
    trace_id: str | None = None,
) -> AgentState:
    """Create explicit defaults so every checkpoint is inspectable."""

    return AgentState(
        trace_id=trace_id or str(uuid4()),
        thread_id=thread_id or str(uuid4()),
        turn_id=turn_id or str(uuid4()),
        user_id=user_id,
        phase=Phase.RECEIVED.value,
        user_message=message.strip(),
        intent=None,
        intent_confidence=None,
        slots={},
        missing_slots=[],
        conversation_summary=None,
        execution_plan=[],
        pending_tool_calls=[],
        tool_results=[],
        candidate_ids=[],
        citations=[],
        dietary_constraints={"allergens": [], "excluded_ingredients": []},
        risk_flags=[],
        blocked=False,
        block_reason=None,
        requires_confirmation=False,
        confirmation_decision=None,
        confirmed_call_ids=[],
        step_count=0,
        tool_signatures={},
        fallback_events=[],
        prompt_versions={},
        final_response=None,
        error=None,
    )


def state_identity(state: AgentState) -> dict[str, str | int]:
    return {
        "trace_id": state["trace_id"],
        "thread_id": state["thread_id"],
        "turn_id": state["turn_id"],
        "user_id": state["user_id"],
    }
