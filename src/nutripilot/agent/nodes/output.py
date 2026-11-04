"""Final grounding and medical-boundary check."""

from __future__ import annotations

from typing import Any

from nutripilot.agent.answers import apply_approved_answer, guard_answer_facts
from nutripilot.agent.grounding import ground_recommendation_facts
from nutripilot.agent.state import AgentState
from nutripilot.domain.enums import Intent, Phase
from nutripilot.domain.models import FinalResponse
from nutripilot.safety import OutputGuard
from nutripilot.tracing import TraceRecorder


class OutputGuardNode:
    def __init__(self, guard: OutputGuard, recorder: TraceRecorder) -> None:
        self.guard = guard
        self.recorder = recorder

    async def run(self, state: AgentState) -> dict[str, Any]:
        raw = state.get("final_response") or {
            "status": "degraded",
            "message": "系统未生成有效回复。",
        }
        response = FinalResponse.model_validate(raw)
        if state.get("intent") in {
            Intent.MEAL_RECOMMENDATION.value, Intent.MEAL_ADJUSTMENT.value,
            Intent.MEAL_PLAN.value,
        }:
            response = ground_recommendation_facts(
                response, state.get("tool_results", []), state.get("slots", {}),
                user_message=state.get("user_message", ""), require_meal_evidence=True,
            )
            response.warnings = list(dict.fromkeys([
                *response.warnings, *(state.get("plan_quality") or {}).get("warnings", []),
            ]))
        guarded, flags, forbidden_meal_ids = guard_answer_facts(response, state, self.guard)
        # Prose is reusable only while its reviewed facts survive the final safety filter.
        prose_applied = False
        if guarded.status != "blocked":
            approved = apply_approved_answer(guarded, state.get("approved_answer"))
            prose_applied = approved is not guarded
            guarded = approved
            guarded, prose_flags = self.guard.validate(
                guarded, allowed_meal_ids={item.meal_id for item in guarded.recommendations},
                forbidden_meal_ids=forbidden_meal_ids,
            )
            flags = list(dict.fromkeys([*flags, *prose_flags]))
        phase = Phase.BLOCKED if guarded.status == "blocked" else Phase.COMPLETED
        self.recorder.record(
            trace_id=state["trace_id"],
            thread_id=state["thread_id"],
            turn_id=state["turn_id"],
            user_id=state["user_id"],
            event_type="safety.output_checked",
            component="OutputGuard",
            phase_before=state.get("phase"),
            phase_after=phase.value,
            payload={
                "flags": flags, "removed_meal_ids": sorted(forbidden_meal_ids),
                "approved_prose_applied": prose_applied and guarded.status != "blocked",
            },
        )
        return {
            "phase": phase.value,
            "risk_flags": list(dict.fromkeys([*state.get("risk_flags", []), *flags])),
            "final_response": guarded.model_dump(mode="json"),
        }
