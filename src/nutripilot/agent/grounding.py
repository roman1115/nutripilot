"""Canonicalise model-authored recommendations against authoritative tool results."""

from __future__ import annotations

from typing import Any

from nutripilot.domain.models import Citation, FinalResponse


def ground_recommendation_facts(
    response: FinalResponse,
    tool_results: list[dict[str, Any]],
) -> FinalResponse:
    meals = _meal_records(tool_results)
    grounded = []
    citations: list[Citation] = []
    for recommendation in response.recommendations:
        meal = meals.get(recommendation.meal_id)
        if meal is None:
            grounded.append(recommendation)
            continue
        nutrition = meal.get("nutrition")
        facts = nutrition if isinstance(nutrition, dict) else meal
        grounded.append(
            recommendation.model_copy(
                update={
                    "name": str(meal.get("name") or recommendation.name),
                    "calories_kcal": _number(facts.get("calories_kcal")),
                    "protein_g": _number(facts.get("protein_g")),
                    "tags": _strings(meal.get("health_goals")),
                }
            )
        )
        raw_citation = meal.get("citation")
        citation = raw_citation if isinstance(raw_citation, dict) else {}
        citations.append(
            Citation(
                source_id=str(citation.get("source_id") or f"meal:{recommendation.meal_id}"),
                title=str(meal.get("name") or recommendation.name),
                source_type="meal",
                uri=_optional_string(citation.get("uri")),
                score=_number(meal.get("score")),
            )
        )

    canonical_names = [
        item.name for item in grounded if item.meal_id in meals
    ]
    message = response.message
    if canonical_names:
        message = f"根据当前画像、餐次与安全约束，优先推荐：{'、'.join(canonical_names)}。"
    return response.model_copy(
        update={
            "message": message,
            "recommendations": grounded,
            "citations": citations or response.citations,
        }
    )


def _meal_records(tool_results: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    records: dict[int, dict[str, Any]] = {}
    for result in tool_results:
        if result.get("tool_name") != "search_meals" or result.get("status") != "success":
            continue
        data = result.get("data")
        hits = data.get("hits", []) if isinstance(data, dict) else []
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            raw_meal = hit.get("meal", hit)
            if not isinstance(raw_meal, dict):
                continue
            meal = {**raw_meal}
            if isinstance(hit.get("citation"), dict):
                meal["citation"] = hit["citation"]
            if "score" in hit:
                meal["score"] = hit["score"]
            meal_id = meal.get("id") or meal.get("meal_id")
            if isinstance(meal_id, int):
                records[meal_id] = meal
    return records


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _strings(value: Any) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _optional_string(value: Any) -> str | None:
    return str(value) if isinstance(value, str) and value else None
