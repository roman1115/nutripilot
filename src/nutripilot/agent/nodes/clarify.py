"""Deterministic, one-slot-at-a-time clarification."""

from __future__ import annotations

from typing import Any

from nutripilot.agent.state import AgentState
from nutripilot.domain.enums import Phase
from nutripilot.domain.models import FinalResponse
from nutripilot.tracing import TraceRecorder

_QUESTIONS = {
    "meal_type": "你想安排早餐、午餐、晚餐还是夜宵？",
    "food_name": "你想查询哪一种食物或餐食的营养信息？",
    "preference": "请告诉我需要长期记录的偏好、忌口或过敏原。",
    "plan_range": "你想制定一天还是一周的饮食计划？",
    "start_date": "这个日期不存在，请提供有效的计划开始日期，例如 2026-10-22。",
}


class ClarifyNode:
    def __init__(self, recorder: TraceRecorder) -> None:
        self.recorder = recorder

    async def run(self, state: AgentState) -> dict[str, Any]:
        missing = state.get("missing_slots", [])
        slot = missing[0] if missing else "preference"
        message = _QUESTIONS.get(slot, f"请补充 {slot} 信息。")
        response = FinalResponse(
            status="needs_clarification",
            message=message,
            suggested_actions=["继续补充条件"],
        )
        self.recorder.record(
            trace_id=state["trace_id"],
            thread_id=state["thread_id"],
            turn_id=state["turn_id"],
            user_id=state["user_id"],
            event_type="conversation.clarification_requested",
            component="ClarifyNode",
            phase_before=state.get("phase"),
            phase_after=Phase.CLARIFY.value,
            payload={"slot": slot},
        )
        return {"phase": Phase.CLARIFY.value, "final_response": response.model_dump(mode="json")}
