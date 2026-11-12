"""Adapter between graph state and the canonical ToolExecutor."""

from __future__ import annotations

from datetime import date
from typing import Any

from nutripilot.agent.contracts import PlanSpec
from nutripilot.agent.planning import (
    PlanBuilder,
    PlanBuildError,
    candidate_meals_from_results,
)
from nutripilot.agent.state import AgentState
from nutripilot.domain.enums import Intent, Phase
from nutripilot.model_gateway.budget import current_budget
from nutripilot.safety import has_dietary_conflict, merge_dietary_terms
from nutripilot.safety.dietary import meets_ingredient_requirements
from nutripilot.tools import (
    ToolCall,
    ToolContext,
    ToolExecutionState,
    ToolExecutor,
    ToolResult,
    ToolStatus,
)
from nutripilot.tracing import TraceRecorder


class ToolExecutionNode:
    def __init__(self, executor: ToolExecutor, recorder: TraceRecorder) -> None:
        self.executor = executor
        self.recorder = recorder

    async def run(self, state: AgentState) -> dict[str, Any]:
        calls = [ToolCall.model_validate(item) for item in state.get("pending_tool_calls", [])]
        if state.get("intent") == Intent.MEAL_PLAN.value:
            spec = plan_spec_from_results(
                state.get("slots", {}),
                state.get("tool_results", []),
                current_date=date.fromisoformat(state["current_date"]),
            )
            search_calls = [call for call in calls if call.name == "search_meals"]
            if search_calls:
                expanded_searches = []
                for meal_type in spec.meal_types:
                    selected = next(
                        (
                            call
                            for call in search_calls
                            if call.arguments.get("meal_type") == meal_type
                        ),
                        search_calls[0],
                    )
                    expanded_searches.append(
                        selected.model_copy(
                            update={
                                "call_id": f"{selected.call_id}:{meal_type}",
                                "arguments": {
                                    **selected.arguments,
                                    "meal_type": meal_type,
                                    "meal_role": None,
                                    "limit": 8,
                                },
                            }
                        )
                    )
                calls = [
                    expanded
                    for call in calls
                    for expanded in (
                        expanded_searches
                        if call is search_calls[0]
                        else [] if call.name == "search_meals" else [call]
                    )
                ]
        execution_state = ToolExecutionState(
            step_count=state.get("step_count", 0),
            signatures=dict(state.get("tool_signatures", {})),
        )
        results = list(state.get("tool_results", []))
        confirmed_ids = set(state.get("confirmed_call_ids", []))
        requires_confirmation = False
        dietary_constraints = dict(state.get("dietary_constraints", {}))
        plan_validation_error: PlanBuildError | None = None
        plan_quality = state.get("plan_quality")
        pending_calls: list[dict[str, Any]] = []

        for call in calls:
            if (
                (call.name, state.get("intent")) in {
                    ("get_nutrition", Intent.NUTRITION_QUERY.value),
                    ("get_recipe", Intent.RECIPE_QUERY.value),
                }
                and state.get("slots", {}).get("food_name")
            ):
                call = call.model_copy(
                    update={"arguments": {"name": state["slots"]["food_name"]}}
                )
            call = _apply_profile_constraints(call, results, state.get("slots", {}))
            if call.name == "search_meals":
                targets = state.get("answer_task", {}).get("meal_ids", [])
                if state.get("intent") == Intent.MEAL_ADJUSTMENT.value and targets:
                    call = call.model_copy(update={"arguments": {
                        **call.arguments,
                        "excluded_meal_ids": sorted({
                            *call.arguments.get("excluded_meal_ids", []), *targets,
                        }),
                    }})
                elif (
                    state.get("intent") == Intent.MEAL_ADJUSTMENT.value
                    and any(term in state["user_message"] for term in (
                        "换一批", "换一个", "不想吃这个", "再来", "换几道",
                    ))
                ) or state.get(
                    "replacing_plan"
                ):
                    call = call.model_copy(
                        update={
                            "arguments": {
                                **call.arguments,
                                "excluded_meal_ids": sorted(
                                    {
                                        *call.arguments.get("excluded_meal_ids", []),
                                        *state.get("previous_candidate_ids", []),
                                    }
                                ),
                            }
                        }
                    )
                if state.get("intent") == Intent.MEAL_PLAN.value:
                    arguments = dict(call.arguments)
                    slots = state.get("slots", {})
                    for field in (
                        "min_protein_g",
                        "max_calories_kcal",
                        "max_price_cny",
                        "max_prep_minutes",
                    ):
                        if slots.get(field) is None:
                            arguments.pop(field, None)
                    call = call.model_copy(update={"arguments": arguments})
                if not _has_successful_profile(results):
                    execution_state.step_count += 1
                    results.append(
                        ToolResult.failure(
                            call_id=call.call_id,
                            tool_name=call.name,
                            code="PROFILE_REQUIRED",
                            message=(
                                "A verified user profile is required before personalized meal "
                                "retrieval."
                            ),
                        ).model_dump(mode="json")
                    )
                    break
                if not any(
                    result.get("tool_name") == "get_user_feedback"
                    and result.get("status") == "success" for result in results
                ):
                    execution_state.step_count += 1
                    results.append(ToolResult.failure(
                        call_id=call.call_id, tool_name=call.name, code="FEEDBACK_REQUIRED",
                        message="Verified feedback is required before meal retrieval.",
                    ).model_dump(mode="json"))
                    break
                dietary_constraints = {
                    "required_ingredients": call.arguments.get("required_ingredients", []),
                    "ingredient_any_groups": call.arguments.get("ingredient_any_groups", []),
                    "allergens": merge_dietary_terms(call.arguments.get("excluded_allergens")),
                    "excluded_ingredients": merge_dietary_terms(
                        call.arguments.get("excluded_ingredients")
                    ),
                }
            if call.name == "create_meal_plan":
                try:
                    call = _validate_meal_plan_call(
                        call,
                        state=state,
                        results=results,
                        dietary_constraints=dietary_constraints,
                    )
                    plan_quality = PlanBuilder().assess(
                        plan_spec_from_results(
                            state.get("slots", {}),
                            results,
                            current_date=date.fromisoformat(state["current_date"]),
                        ),
                        PlanBuilder.parse_request(call.arguments),
                        candidate_meals_from_results(results),
                    )
                except PlanBuildError as exc:
                    plan_validation_error = exc
                    execution_state.step_count += 1
                    results.append(
                        ToolResult.failure(
                            call_id=call.call_id,
                            tool_name=call.name,
                            code=exc.code,
                            message=exc.safe_message,
                            details=exc.details,
                        ).model_dump(mode="json")
                    )
                    break
            budget = current_budget()
            if budget:
                budget.check()
            context = ToolContext(
                user_id=state["user_id"],
                trace_id=state["trace_id"],
                thread_id=state["thread_id"],
                turn_id=state["turn_id"],
                permissions=frozenset(
                    {"profile:read", "meal_plan:write", "profile:write", "feedback:write"}
                ),
                confirmed=call.call_id in confirmed_ids,
                idempotency_key=f"{state['turn_id']}:{call.call_id}",
                deadline_monotonic=budget.deadline if budget else None,
            )
            result = await self.executor.execute(call, context, execution_state)
            if result.status is ToolStatus.CONFIRMATION_REQUIRED:
                requires_confirmation = True
                pending_calls = [call.model_dump(mode="json")]
                break
            results.append(result.model_dump(mode="json"))
            if result.error and not result.error.retryable:
                break

        dietary_constraints = {
            **dietary_constraints,
            "allergens": merge_dietary_terms(
                dietary_constraints.get("allergens"), _profile_terms(results, "allergens"),
                state.get("slots", {}).get("allergens"),
            ),
            "excluded_ingredients": merge_dietary_terms(
                dietary_constraints.get("excluded_ingredients"),
                _profile_terms(results, "dislikes"),
                state.get("slots", {}).get("dislikes"),
                state.get("slots", {}).get("excluded_ingredients"),
            ),
        }
        candidate_ids, citations, filtered_meal_ids = _extract_grounding(
            results,
            dietary_constraints,
        )
        self.recorder.record(
            trace_id=state["trace_id"],
            thread_id=state["thread_id"],
            turn_id=state["turn_id"],
            user_id=state["user_id"],
            event_type="tools.batch_completed",
            component="ToolExecutor",
            phase_before=state.get("phase"),
            phase_after=(
                Phase.AWAITING_CONFIRMATION.value
                if requires_confirmation
                else Phase.TOOL_EXECUTION.value
            ),
            payload={
                "tools": [call.name for call in calls],
                "requires_confirmation": requires_confirmation,
                "filtered_meal_ids": sorted(filtered_meal_ids),
                "plan_validation_error": (
                    plan_validation_error.code if plan_validation_error else None
                ),
            },
        )
        return {
            "phase": (
                Phase.AWAITING_CONFIRMATION.value
                if requires_confirmation
                else Phase.TOOL_EXECUTION.value
            ),
            "requires_confirmation": requires_confirmation,
            "risk_flags": list(
                dict.fromkeys(
                    [
                        *state.get("risk_flags", []),
                        *(["UNSAFE_CANDIDATE_REMOVED"] if filtered_meal_ids else []),
                        *(["UNSAFE_PLAN_BLOCKED"] if plan_validation_error else []),
                    ]
                )
            ),
            "tool_results": results,
            "pending_tool_calls": pending_calls,
            "plan_quality": plan_quality,
            "candidate_ids": candidate_ids,
            "citations": citations,
            "dietary_constraints": dietary_constraints,
            "step_count": execution_state.step_count,
            "tool_signatures": execution_state.signatures,
        }


def _extract_grounding(
    results: list[dict[str, Any]],
    dietary_constraints: dict[str, Any],
) -> tuple[list[int], list[dict[str, Any]], set[int]]:
    candidate_ids: list[int] = []
    citations: list[dict[str, Any]] = []
    filtered_meal_ids: set[int] = set()
    declared_allergens = merge_dietary_terms(dietary_constraints.get("allergens"))
    excluded_ingredients = merge_dietary_terms(dietary_constraints.get("excluded_ingredients"))
    required = dietary_constraints.get("required_ingredients", [])
    groups = dietary_constraints.get("ingredient_any_groups", [])
    constraints_active = bool(declared_allergens or excluded_ingredients or required or groups)

    for result in results:
        if result.get("status") != ToolStatus.SUCCESS.value:
            continue
        if result.get("tool_name") != "search_meals":
            for citation in result.get("citations", []):
                if isinstance(citation, dict):
                    citations.append(citation)
            continue
        data = result.get("data") or {}
        hits = data.get("hits", []) if isinstance(data, dict) else []
        for hit in hits:
            meal = hit.get("meal", hit) if isinstance(hit, dict) else {}
            meal_id = meal.get("id") or meal.get("meal_id")
            if not isinstance(meal_id, int):
                continue
            meal_allergens = meal.get("allergens")
            meal_ingredients = meal.get("ingredients")
            metadata_missing = not isinstance(meal_allergens, list) or not isinstance(
                meal_ingredients, list
            )
            if constraints_active and (
                metadata_missing
                or has_dietary_conflict(
                    meal_allergens=meal_allergens,
                    meal_ingredients=meal_ingredients,
                    declared_allergens=declared_allergens,
                    excluded_ingredients=excluded_ingredients,
                    ingredient_records=meal.get("ingredient_records", []),
                )
                or not meets_ingredient_requirements(meal, required, groups)
            ):
                filtered_meal_ids.add(meal_id)
                continue
            candidate_ids.append(meal_id)
            if isinstance(hit, dict) and isinstance(hit.get("citation"), dict):
                citations.append(hit["citation"])
    return list(dict.fromkeys(candidate_ids)), citations, filtered_meal_ids


def _apply_profile_constraints(
    call: ToolCall,
    results: list[dict[str, Any]],
    slots: dict[str, Any],
) -> ToolCall:
    """Inject persisted hard constraints even when a model omits them."""

    if call.name != "search_meals":
        return call

    arguments = dict(call.arguments)
    profile_allergens = _profile_terms(results, "allergens")
    profile_dislikes = _profile_terms(results, "dislikes")
    declared_allergens = merge_dietary_terms(
        profile_allergens,
        slots.get("allergens"),
    )
    excluded_ingredients = merge_dietary_terms(
        profile_dislikes,
        profile_allergens,
        slots.get("dislikes"),
        slots.get("excluded_ingredients"),
        slots.get("allergens"),
    )
    excluded_meal_ids = sorted(
        {
            *(
                meal_id
                for meal_id in arguments.get("excluded_meal_ids", [])
                if isinstance(meal_id, int)
            ),
            *_feedback_excluded_ids(results),
        }
    )
    arguments.update(
        food_name=slots.get("food_name"),
        excluded_allergens=declared_allergens,
        excluded_ingredients=excluded_ingredients,
        excluded_meal_ids=excluded_meal_ids,
        required_ingredients=slots.get("required_ingredients", []),
        ingredient_any_groups=slots.get("ingredient_any_groups", []),
    )
    if slots.get("meal_type"):
        arguments["meal_type"] = (
            "snack" if slots["meal_type"] == "late_snack" else slots["meal_type"]
        )
    for field in ("max_calories_kcal", "max_price_cny", "max_prep_minutes", "min_protein_g"):
        if field not in slots:
            continue
        value = slots.get(field)
        if value is None:
            arguments.pop(field, None)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            arguments[field] = value
    return call.model_copy(update={"arguments": arguments})


def _validate_meal_plan_call(
    call: ToolCall,
    *,
    state: AgentState,
    results: list[dict[str, Any]],
    dietary_constraints: dict[str, Any],
) -> ToolCall:
    request = PlanBuilder.parse_request(call.arguments)
    spec = plan_spec_from_results(
        state.get("slots", {}),
        results,
        current_date=date.fromisoformat(state["current_date"]),
    )
    PlanBuilder().validate_prewrite(
        spec=spec,
        request=request,
        candidates=candidate_meals_from_results(results),
        allowed_meal_ids=set(state.get("candidate_ids", [])),
        declared_allergens=merge_dietary_terms(dietary_constraints.get("allergens")),
        excluded_ingredients=merge_dietary_terms(dietary_constraints.get("excluded_ingredients")),
    )
    # Safe model selections are canonicalised before confirmation, not after the user approves.
    safe_candidates = [
        meal
        for meal in candidate_meals_from_results(results)
        if (meal.get("meal_id") or meal.get("id")) in set(state.get("candidate_ids", []))
    ]
    canonical = PlanBuilder().build(spec, safe_candidates)
    PlanBuilder().validate_prewrite(
        spec=spec,
        request=canonical,
        candidates=safe_candidates,
        allowed_meal_ids=set(state.get("candidate_ids", [])),
        declared_allergens=merge_dietary_terms(dietary_constraints.get("allergens")),
        excluded_ingredients=merge_dietary_terms(dietary_constraints.get("excluded_ingredients")),
    )
    canonical.quality = PlanBuilder().assess(spec, canonical, safe_candidates)
    return call.model_copy(update={"arguments": canonical.model_dump(mode="json")})


def plan_spec_from_results(
    slots: dict[str, Any], results: list[dict[str, Any]], *, current_date: date
) -> PlanSpec:
    profile: dict[str, Any] = next(
        (
            result["data"]
            for result in reversed(results)
            if result.get("tool_name") == "get_user_profile"
            and result.get("status") == "success"
            and isinstance(result.get("data"), dict)
        ),
        {},
    )
    return PlanSpec.from_slots(slots, start_date=current_date, profile=profile)


def _profile_terms(results: list[dict[str, Any]], field: str) -> list[str]:
    for result in reversed(results):
        if result.get("tool_name") != "get_user_profile":
            continue
        if result.get("status") != ToolStatus.SUCCESS.value:
            continue
        data = result.get("data")
        if isinstance(data, dict):
            return merge_dietary_terms(data.get(field))
    return []


def _has_successful_profile(results: list[dict[str, Any]]) -> bool:
    return any(
        result.get("tool_name") == "get_user_profile"
        and result.get("status") == ToolStatus.SUCCESS.value
        and isinstance(result.get("data"), dict)
        for result in results
    )


def _feedback_excluded_ids(results: list[dict[str, Any]]) -> set[int]:
    for result in reversed(results):
        if result.get("tool_name") != "get_user_feedback":
            continue
        if result.get("status") != ToolStatus.SUCCESS.value:
            continue
        data = result.get("data")
        if isinstance(data, dict) and isinstance(data.get("excluded_meal_ids"), list):
            return {meal_id for meal_id in data["excluded_meal_ids"] if isinstance(meal_id, int)}
    return set()
