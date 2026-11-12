"""Batch evaluation over fixed, human-reviewable cases."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pydantic import TypeAdapter

from nutripilot.agent.grounding import _meal_records, ground_recommendation_facts
from nutripilot.domain.enums import Intent, Phase
from nutripilot.domain.models import FinalResponse
from nutripilot.evaluation.metrics import expected_slots_match, rate, response_contains
from nutripilot.evaluation.models import (
    EvalCase,
    EvalReport,
    EvalResult,
    RecommendationExpectation,
)
from nutripilot.retrieval.ports import MealRepository
from nutripilot.safety.dietary import match_ingredient, meets_ingredient_requirements

if TYPE_CHECKING:
    from nutripilot.agent.state import AgentState
    from nutripilot.application.chat_runs import ChatRunService
    from nutripilot.application.chat_service import ChatService


def load_dataset(path: str | Path) -> tuple[str, list[EvalCase]]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, list):
        version, rows = "unversioned", raw
    else:
        version, rows = str(raw.get("version", "unversioned")), raw.get("cases", [])
    return version, TypeAdapter(list[EvalCase]).validate_python(rows)


def recommendation_integrity(state: AgentState) -> dict[str, bool]:
    response = FinalResponse.model_validate(state.get("final_response") or {"message": ""})
    results = state.get("tool_results", [])
    records = _meal_records(results)
    meal_intent = state.get("intent") in {
        Intent.MEAL_RECOMMENDATION.value, Intent.MEAL_ADJUSTMENT.value, Intent.MEAL_PLAN.value,
    }
    searched = any(
        result.get("tool_name") == "search_meals" and result.get("status") == "success"
        for result in results
    )
    dependencies_ok = True
    succeeded: set[str] = set()
    for result in results:
        if result.get("tool_name") == "search_meals":
            dependencies_ok &= {"get_user_profile", "get_user_feedback"} <= succeeded
        if result.get("status") == "success":
            succeeded.add(str(result.get("tool_name")))
    current_search = all(card.meal_id in records for card in response.recommendations)
    if meal_intent and state.get("phase") == Phase.COMPLETED.value:
        current_search &= searched
    canonical = ground_recommendation_facts(
        response, results, state.get("slots", {}), user_message=state.get("user_message", "")
    )
    facts_ok = current_search
    canonical_cards = {card.meal_id: card for card in canonical.recommendations}
    for card in response.recommendations:
        source = canonical_cards.get(card.meal_id)
        facts_ok &= bool(source) and all(
            getattr(card, field) == getattr(source, field)
            for field in ("name", "calories_kcal", "protein_g", "nutrient_ranges")
        )
    return {
        "current_search_evidence": current_search,
        "recommendation_facts": facts_ok,
        "recommendation_sources": not response.recommendations or (
            current_search and response.citations == canonical.citations
        ),
        "tool_preconditions": not meal_intent or dependencies_ok,
    }


class EvalRunner:
    def __init__(
        self, chat_service: ChatService, runs: ChatRunService | None = None,
        *, meal_repository: MealRepository | None = None,
    ) -> None:
        self.chat_service = chat_service
        self.runs = runs
        self.meal_repository = meal_repository

    async def _recommendation_checks(
        self, state: AgentState, expected: RecommendationExpectation
    ) -> dict[str, bool]:
        cards = (state.get("final_response") or {}).get("recommendations", [])
        checks = {}
        if expected.min_recommendations is not None:
            checks["minimum_recommendations"] = len(cards) >= expected.min_recommendations
        if expected.max_recommendations is not None:
            checks["maximum_recommendations"] = len(cards) <= expected.max_recommendations
        required = expected.expected_required_ingredients
        groups = expected.expected_ingredient_any_groups
        excluded = expected.expected_excluded_ingredients
        if not (required or groups or excluded or expected.check_reason_evidence):
            return checks
        ingredients_ok, evidence_ok = True, True
        for card in cards:
            meal: dict[str, Any] = {}
            if self.meal_repository is not None:
                record = await self.meal_repository.get_meal(card["meal_id"])
                meal = record.model_dump() if record else {}
            else:
                meal = _meal_records(state.get("tool_results", [])).get(card["meal_id"], {})
            ingredients_ok &= bool(meal) and meets_ingredient_requirements(meal, required, groups)
            ingredients_ok &= not any(
                match_ingredient(
                    term, meal.get("ingredients", []), meal.get("ingredient_records", []),
                    exclusion=True,
                ) for term in excluded
            )
            if expected.check_reason_evidence:
                reason = card.get("reason", "")
                for group in [[term] for term in required] + groups:
                    evidence_ok &= any(
                        term in reason and bool(match_ingredient(
                            term, meal.get("ingredients", []), meal.get("ingredient_records", [])
                        )) for term in group
                    )
        checks["recipe_ingredients"] = ingredients_ok
        if expected.check_reason_evidence:
            checks["reason_evidence"] = evidence_ok
        return checks

    async def _chat(self, user_id: int, message: str, thread_id: str) -> AgentState:
        if self.runs is None:
            return await self.chat_service.chat(
                user_id=user_id, message=message, thread_id=thread_id
            )
        run = await self.runs.submit(
            user_id=user_id, message=message, thread_id=thread_id, request_key=str(uuid4())
        )
        return await self.runs.wait(run, user_id)

    async def run(self, cases: list[EvalCase], *, dataset_version: str) -> EvalReport:
        run_id = str(uuid4())
        results: list[EvalResult] = []
        prompt_versions: dict[str, str] = {}
        models: set[str] = set()
        for case in cases:
            started = time.monotonic()
            thread_id = f"eval:{run_id}:{dataset_version}:{case.case_id}"
            state = await self._chat(
                user_id=case.user_id,
                message=case.user_message,
                thread_id=thread_id,
            )
            traces = [state["trace_id"]]
            turns = [state]
            for follow_up in case.follow_up_messages:
                state = await self._chat(
                    user_id=case.user_id, message=follow_up, thread_id=thread_id
                )
                traces.append(state["trace_id"])
                turns.append(state)
            elapsed_ms = (time.monotonic() - started) * 1000
            tools = [
                str(item.get("tool_name"))
                for item in state.get("tool_results", [])
                if item.get("tool_name")
            ]
            tools = list(dict.fromkeys(tools))
            checks = {
                "intent": (
                    state.get("phase") == Phase.BLOCKED.value
                    if case.expected_blocked
                    else state.get("intent") == case.expected_intent.value
                ),
                "slots": expected_slots_match(case.expected_slots, state.get("slots", {})),
                "tools": set(case.expected_tools)
                <= set(tools)
                <= set(case.expected_tools) | set(case.optional_tools),
                "confirmation": bool(state.get("requires_confirmation"))
                == case.expected_requires_confirmation,
                "blocked": (state.get("phase") == Phase.BLOCKED.value) == case.expected_blocked,
                "response": response_contains(
                    state.get("final_response"), case.expected_response_contains
                ),
            }
            checks.update(await self._recommendation_checks(state, case))
            expected_phase = case.expected_phase or (
                Phase.BLOCKED.value if case.expected_blocked
                else Phase.AWAITING_CONFIRMATION.value if case.expected_requires_confirmation
                else Phase.COMPLETED.value
            )
            checks["phase"] = state.get("phase") == expected_phase
            for index, turn in enumerate(turns, start=1):
                checks.update({
                    f"turn_{index}_{key}": value
                    for key, value in recommendation_integrity(turn).items()
                })
                if index < len(turns) and turn.get("intent") in {
                    Intent.MEAL_RECOMMENDATION.value, Intent.MEAL_ADJUSTMENT.value,
                }:
                    checks[f"turn_{index}_phase"] = turn.get("phase") == Phase.COMPLETED.value
            if case.turn_expectations:
                checks["turn_expectations_count"] = len(case.turn_expectations) == len(turns)
                for index, (turn, expectation) in enumerate(
                    zip(turns, case.turn_expectations, strict=False), start=1
                ):
                    checks[f"turn_{index}_slots"] = expected_slots_match(
                        expectation.expected_slots, turn.get("slots", {})
                    )
                    checks.update({
                        f"turn_{index}_{key}": value
                        for key, value in (await self._recommendation_checks(
                            turn, expectation
                        )).items()
                    })
            events = [
                event
                for trace_id in traces
                for event in self.chat_service.graph.recorder.get_trace(trace_id)
            ]
            models.update(
                str(event.payload["model"]) for event in events if event.payload.get("model")
            )
            if case.expected_phase:
                checks["phase"] = state.get("phase") == case.expected_phase
            if case.forbidden_response_terms:
                text = json.dumps(state.get("final_response"), ensure_ascii=False)
                checks["fact_explanation"] = not any(
                    term in text for term in case.forbidden_response_terms
                )
            if case.expected_plan_start_date:
                calls = state.get("pending_tool_calls", [])
                checks["plan_date"] = (
                    bool(calls)
                    and all(
                        call.get("arguments", {}).get("start_date") == case.expected_plan_start_date
                        for call in calls
                        if call.get("name") == "create_meal_plan"
                    )
                    and any(call.get("name") == "create_meal_plan" for call in calls)
                )
            if case.expected_error_code:
                checks["error_code"] = (state.get("error") or {}).get(
                    "code"
                ) == case.expected_error_code
            if case.expected_plan_item_count is not None:
                checks["plan_items"] = any(
                    len(call.get("arguments", {}).get("items", [])) == case.expected_plan_item_count
                    for call in state.get("pending_tool_calls", [])
                    if call.get("name") == "create_meal_plan"
                )
            if case.expected_plan_nutrition_basis is not None:
                quality = state.get("plan_quality") or {}
                checks["nutrition_basis"] = (
                    quality.get("nutrition_basis") == case.expected_plan_nutrition_basis
                    and bool(quality.get("daily_totals"))
                    and all("target_met" not in day for day in quality["daily_totals"])
                )
            failures = [name for name, passed in checks.items() if not passed]
            results.append(
                EvalResult(
                    case_id=case.case_id,
                    passed=not failures,
                    predicted_intent=state.get("intent"),
                    actual_tools=tools,
                    actual_slots=state.get("slots", {}),
                    checks=checks,
                    phase=state.get("phase", Phase.FAILED.value),
                    trace_id=state["trace_id"],
                    trace_ids=traces,
                    latency_ms=elapsed_ms,
                    failure_reasons=failures,
                    input_tokens=sum(event.input_tokens for event in events),
                    output_tokens=sum(event.output_tokens for event in events),
                )
            )
            prompt_versions.update(state.get("prompt_versions", {}))

        return EvalReport(
            run_id=run_id,
            dataset_version=dataset_version,
            prompt_versions=prompt_versions,
            runtime={
                "models": ",".join(sorted(models)),
                "mode": (
                    "fake"
                    if models and all(model.startswith("fake-") for model in models)
                    else "real"
                    if models and not any(model.startswith("fake-") for model in models)
                    else "unknown_or_mixed"
                ),
            },
            total_cases=len(results),
            passed_cases=sum(result.passed for result in results),
            metrics={
                "case_pass_rate": rate([item.passed for item in results]),
                "intent_accuracy": rate([item.checks["intent"] for item in results]),
                "slot_case_accuracy": rate([item.checks["slots"] for item in results]),
                "tool_selection_accuracy": rate([item.checks["tools"] for item in results]),
                "safety_accuracy": rate([item.checks["blocked"] for item in results]),
                "average_latency_ms": round(
                    sum(item.latency_ms for item in results) / len(results), 3
                )
                if results
                else 0.0,
                "input_tokens": float(sum(item.input_tokens for item in results)),
                "output_tokens": float(sum(item.output_tokens for item in results)),
                "p95_latency_ms": sorted(item.latency_ms for item in results)[
                    max(0, (len(results) * 95 + 99) // 100 - 1)
                ]
                if results
                else 0.0,
            },
            results=results,
        )
