"""Shared worker instrumentation without sharing worker prompts or contracts."""

from __future__ import annotations

from typing import Any

from nutripilot.agent.state import AgentState
from nutripilot.tracing import TraceRecorder


def record_worker_event(
    recorder: TraceRecorder,
    state: AgentState,
    *,
    worker: str,
    event_type: str,
    payload: dict[str, Any],
    duration_ms: float = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> None:
    recorder.record(
        trace_id=state["trace_id"],
        thread_id=state["thread_id"],
        turn_id=state["turn_id"],
        user_id=state["user_id"],
        event_type=event_type,
        component=worker,
        phase_before=state.get("phase"),
        payload=payload,
        duration_ms=duration_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def fallback_update(state: AgentState, component: str, code: str) -> list[dict[str, Any]]:
    return [
        *state.get("fallback_events", []),
        {"component": component, "code": code, "turn_id": state["turn_id"]},
    ]
