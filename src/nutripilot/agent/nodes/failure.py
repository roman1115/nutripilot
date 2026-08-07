"""Controlled termination when an execution budget is exhausted."""

from __future__ import annotations

from typing import Any

from nutripilot.agent.state import AgentState
from nutripilot.domain.enums import Phase
from nutripilot.domain.models import FinalResponse
from nutripilot.tracing import TraceRecorder


class BudgetExceededNode:
    def __init__(self, recorder: TraceRecorder) -> None:
        self.recorder = recorder

    async def run(self, state: AgentState) -> dict[str, Any]:
        response = FinalResponse(
            status="degraded",
            message="本次任务达到最大执行步数，已停止继续调用工具。",
            warnings=["请缩小需求范围后重试。"],
        )
        self.recorder.record(
            trace_id=state["trace_id"],
            thread_id=state["thread_id"],
            turn_id=state["turn_id"],
            user_id=state["user_id"],
            event_type="budget.step_limit",
            component="Supervisor",
            phase_before=state.get("phase"),
            phase_after=Phase.FAILED.value,
            payload={"step_count": state.get("step_count", 0)},
        )
        return {
            "phase": Phase.FAILED.value,
            "error": {"code": "STEP_LIMIT_EXCEEDED", "retryable": False},
            "final_response": response.model_dump(mode="json"),
        }
