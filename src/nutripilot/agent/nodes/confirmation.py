"""Checkpoint-friendly Human-in-the-loop decision node."""

from __future__ import annotations

from typing import Any

from nutripilot.agent.state import AgentState
from nutripilot.domain.enums import Phase
from nutripilot.domain.models import FinalResponse
from nutripilot.tracing import TraceRecorder


class ConfirmationNode:
    def __init__(self, recorder: TraceRecorder) -> None:
        self.recorder = recorder

    async def run(self, state: AgentState) -> dict[str, Any]:
        decision = state.get("confirmation_decision")
        calls = state.get("pending_tool_calls", [])
        call_ids = [str(call.get("call_id")) for call in calls]
        if decision is True:
            confirmed = list(dict.fromkeys([*state.get("confirmed_call_ids", []), *call_ids]))
            self._trace(state, "confirmation.approved", Phase.TOOL_EXECUTION.value)
            return {
                "phase": Phase.TOOL_EXECUTION.value,
                "requires_confirmation": False,
                "confirmed_call_ids": confirmed,
                "final_response": None,
            }
        if decision is False:
            response = FinalResponse(
                status="cancelled",
                message="已取消本次写入操作，不会修改你的计划或长期偏好。",
            )
            self._trace(state, "confirmation.rejected", Phase.COMPLETED.value)
            return {
                "phase": Phase.COMPLETED.value,
                "requires_confirmation": False,
                "pending_tool_calls": [],
                "final_response": response.model_dump(mode="json"),
            }

        names = "、".join(str(call.get("name")) for call in calls)
        response = FinalResponse(
            status="confirmation_required",
            message=f"即将执行写操作：{names}。确认后才会保存数据。",
            suggested_actions=["确认", "取消"],
        )
        self._trace(state, "confirmation.requested", Phase.AWAITING_CONFIRMATION.value)
        return {
            "phase": Phase.AWAITING_CONFIRMATION.value,
            "requires_confirmation": True,
            "final_response": response.model_dump(mode="json"),
        }

    def _trace(self, state: AgentState, event_type: str, phase_after: str) -> None:
        self.recorder.record(
            trace_id=state["trace_id"],
            thread_id=state["thread_id"],
            turn_id=state["turn_id"],
            user_id=state["user_id"],
            event_type=event_type,
            component="ConfirmationNode",
            phase_before=state.get("phase"),
            phase_after=phase_after,
            payload={"tool_calls": state.get("pending_tool_calls", [])},
        )
