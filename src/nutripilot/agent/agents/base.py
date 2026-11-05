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


def compact_tool_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project model context; grounding and safety still use the original results."""

    compact = []
    for result in results:
        item = {key: result[key] for key in ("tool_name", "status", "error") if key in result}
        data = result.get("data")
        if isinstance(data, dict) and result.get("tool_name") == "search_meals":
            item["data"] = {
                "filter_diagnostics": data.get("filter_diagnostics", {}),
                "match_evidence": data.get("match_evidence", {}),
                "hits": [
                    _compact_meal({
                        **meal,
                        **({"citation": hit["citation"]} if "citation" in hit else {}),
                    })
                    for hit in data.get("hits", [])
                    if isinstance(hit, dict) and isinstance(meal := hit.get("meal", hit), dict)
                ]
            }
        elif isinstance(data, dict) and result.get("tool_name") == "get_nutrition":
            item["data"] = _compact_meal(data)
        elif isinstance(data, dict) and result.get("tool_name") == "get_user_feedback":
            item["data"] = {"excluded_meal_ids": data.get("excluded_meal_ids", [])}
        else:
            item["data"] = data
        compact.append(item)
    return compact


def _compact_meal(meal: dict[str, Any]) -> dict[str, Any]:
    item = {
        key: meal[key]
        for key in (
            "id",
            "meal_id",
            "is_active",
            "name",
            "description",
            "meal_types",
            "meal_role",
            "recipe_id",
            "ingredients",
            "ingredient_records",
            "allergens",
            "tastes",
            "cuisines",
            "health_goals",
            "dietary_tags",
            "nutrition",
            "prep_minutes",
            "price_cny",
        )
        if key in meal
    }
    if isinstance(citation := meal.get("citation"), dict):
        item["citation"] = {
            key: citation[key] for key in ("source_id", "title", "uri") if key in citation
        }
    if isinstance(nutrition := item.get("nutrition"), dict):
        item["nutrition"] = {
            key: value
            for key, value in nutrition.items()
            if key not in {"calculation_note", "ingredient_source_uris", "source_uri"}
        }
    return item
