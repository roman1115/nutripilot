"""Final grounding and medical-boundary check."""

from __future__ import annotations

from typing import Any

from nutripilot.agent.state import AgentState
from nutripilot.domain.enums import Phase
from nutripilot.domain.models import FinalResponse
from nutripilot.safety import OutputGuard, has_dietary_conflict, merge_dietary_terms
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
        forbidden_meal_ids = _find_constraint_violations(state)
        guarded, flags = self.guard.validate(
            response,
            allowed_meal_ids=set(state.get("candidate_ids", [])),
            forbidden_meal_ids=forbidden_meal_ids,
        )
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
            payload={"flags": flags, "removed_meal_ids": sorted(forbidden_meal_ids)},
        )
        return {
            "phase": phase.value,
            "risk_flags": list(dict.fromkeys([*state.get("risk_flags", []), *flags])),
            "final_response": guarded.model_dump(mode="json"),
        }


def _find_constraint_violations(state: AgentState) -> set[int]:
    constraints = state.get("dietary_constraints", {})
    declared_allergens = merge_dietary_terms(constraints.get("allergens"))
    excluded_ingredients = merge_dietary_terms(constraints.get("excluded_ingredients"))
    if not declared_allergens and not excluded_ingredients:
        return set()

    forbidden: set[int] = set()
    for result in state.get("tool_results", []):
        if result.get("status") != "success" or result.get("tool_name") != "search_meals":
            continue
        data = result.get("data")
        hits = data.get("hits", []) if isinstance(data, dict) else []
        for hit in hits:
            meal = hit.get("meal", hit) if isinstance(hit, dict) else {}
            if not isinstance(meal, dict):
                continue
            meal_id = meal.get("id") or meal.get("meal_id")
            if not isinstance(meal_id, int):
                continue
            meal_allergens = meal.get("allergens")
            meal_ingredients = meal.get("ingredients")
            if (
                not isinstance(meal_allergens, list)
                or not isinstance(meal_ingredients, list)
                or has_dietary_conflict(
                    meal_allergens=meal_allergens,
                    meal_ingredients=meal_ingredients,
                    declared_allergens=declared_allergens,
                    excluded_ingredients=excluded_ingredients,
                )
            ):
                forbidden.add(meal_id)
    return forbidden
