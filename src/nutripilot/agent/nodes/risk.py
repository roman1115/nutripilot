"""Risk checks and controlled terminal responses."""

from __future__ import annotations

from typing import Any

from nutripilot.agent.state import AgentState
from nutripilot.domain.enums import Intent, Phase
from nutripilot.domain.models import FinalResponse
from nutripilot.memory.context import merge_slots
from nutripilot.safety import RiskGuard
from nutripilot.tracing import TraceRecorder


class RiskNode:
    def __init__(self, guard: RiskGuard, recorder: TraceRecorder) -> None:
        self.guard = guard
        self.recorder = recorder

    async def run(self, state: AgentState) -> dict[str, Any]:
        assessment = self.guard.assess(state["user_message"])
        allergens = [
            flag.split(":", 1)[1] for flag in assessment.flags if flag.startswith("ALLERGEN:")
        ]
        slots = merge_slots(state.get("slots", {}), {"allergens": allergens})
        self.recorder.record(
            trace_id=state["trace_id"],
            thread_id=state["thread_id"],
            turn_id=state["turn_id"],
            user_id=state["user_id"],
            event_type="safety.risk_assessed",
            component="RiskGuard",
            phase_before=state.get("phase"),
            phase_after=Phase.RISK_CHECK.value,
            payload={"flags": assessment.flags, "blocked": assessment.blocked},
        )
        return {
            "phase": Phase.RISK_CHECK.value,
            "risk_flags": assessment.flags,
            "blocked": assessment.blocked,
            "block_reason": assessment.safe_message,
            "slots": slots,
        }


class BlockedResponseNode:
    def __init__(self, recorder: TraceRecorder) -> None:
        self.recorder = recorder

    async def run(self, state: AgentState) -> dict[str, Any]:
        response = FinalResponse(
            status="blocked",
            message=state.get("block_reason") or "该请求无法在安全范围内处理。",
            warnings=state.get("risk_flags", []),
        )
        self.recorder.record(
            trace_id=state["trace_id"],
            thread_id=state["thread_id"],
            turn_id=state["turn_id"],
            user_id=state["user_id"],
            event_type="request.blocked",
            component="RiskGuard",
            phase_before=state.get("phase"),
            phase_after=Phase.BLOCKED.value,
            payload={"flags": state.get("risk_flags", [])},
        )
        return {"phase": Phase.BLOCKED.value, "final_response": response.model_dump(mode="json")}


class ScopeResponseNode:
    def __init__(self, recorder: TraceRecorder) -> None:
        self.recorder = recorder

    async def run(self, state: AgentState) -> dict[str, Any]:
        if state.get("intent") == Intent.HEALTH_RISK.value:
            message = "我不能提供诊断或治疗建议，但可以帮助查询一般营养信息。"
            warnings = ["如有健康风险，请咨询医生或注册营养师。"]
        else:
            message = "我目前可以帮助推荐餐食、查询营养信息或制定饮食计划。"
            warnings = []
        response = FinalResponse(status="completed", message=message, warnings=warnings)
        self.recorder.record(
            trace_id=state["trace_id"],
            thread_id=state["thread_id"],
            turn_id=state["turn_id"],
            user_id=state["user_id"],
            event_type="request.out_of_scope",
            component="Supervisor",
            phase_before=state.get("phase"),
            phase_after=Phase.COMPLETED.value,
            payload={"intent": state.get("intent")},
        )
        return {"phase": Phase.COMPLETED.value, "final_response": response.model_dump(mode="json")}
