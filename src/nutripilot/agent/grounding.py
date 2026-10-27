"""Canonicalise model-authored recommendations against authoritative tool results."""

from __future__ import annotations

from typing import Any

from nutripilot.domain.models import Citation, FinalResponse

_ESTIMATE_WARNING = (
    "营养值为未独立核验的演示估算，份量缺少统一克重；实际值会随食材、份量和烹调方式变化，"
    "不应作为精确营养或健康目标依据。"
)


def ground_recommendation_facts(
    response: FinalResponse,
    tool_results: list[dict[str, Any]],
    slots: dict[str, Any] | None = None,
) -> FinalResponse:
    meals = _meal_records(tool_results)
    grounded = []
    citations: list[Citation] = []
    for recommendation in response.recommendations:
        meal = meals.get(recommendation.meal_id)
        if meal is None:
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
                    "reason": _meal_explanation(meal) + _preference_explanation(
                        meal, _latest_data(tool_results, "get_user_profile"), slots or {}
                    ),
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
                uri=_optional_string(facts.get("source_uri"))
                or _optional_string(citation.get("uri")),
                score=_number(meal.get("score")),
            )
        )
        citations.extend(_ingredient_citations(facts))

    if not any(
        result.get("tool_name") == "search_meals" and result.get("status") == "success"
        for result in tool_results
    ):
        return _ground_nutrition(response, tool_results)

    # Free text must never turn a meal's allergens into a claim about the user.
    warnings = list(
        dict.fromkeys(
            warning
            for item in grounded
            for warning in _nutrition_warnings(meals.get(item.meal_id, {}))
        )
    )
    profile = _latest_data(tool_results, "get_user_profile")
    profile_allergens = _strings(profile.get("allergens"))
    if profile_allergens:
        warnings.append(f"用户画像记录的过敏原：{'、'.join(profile_allergens)}。")
    if citations:
        warnings.append("餐食过敏原标注不涵盖制作过程中的交叉接触，请核对实际食材。")
    canonical_names = [item.name for item in grounded]
    message = (
        f"根据已检索的餐食记录，推荐：{'、'.join(canonical_names)}。"
        if canonical_names
        else "没有找到可验证且满足当前条件的餐食，请调整非过敏、非忌口条件后重试。"
    )
    plan = _latest_data(tool_results, "create_meal_plan")
    if plan and canonical_names:
        message = f"饮食计划已保存。餐食包括：{'、'.join(canonical_names)}。"
    return response.model_copy(
        update={
            "status": response.status if grounded else "degraded",
            "message": message,
            "recommendations": grounded,
            "citations": citations,
            "warnings": warnings,
            "suggested_actions": ["换一批餐食"] if grounded else ["调整条件"],
        }
    )


def _preference_explanation(
    meal: dict[str, Any], profile: dict[str, Any], slots: dict[str, Any]
) -> str:
    matches = []
    for field, label in (
        ("tastes", "口味"),
        ("cuisines", "菜系"),
        ("dietary_tags", "饮食标签"),
    ):
        profile_field = "preferences" if field == "tastes" else field
        requested = set(_strings(slots.get(field))) | set(_strings(profile.get(profile_field)))
        matched = sorted(requested & set(_strings(meal.get(field))))
        if matched:
            matches.append(f"与已记录的{label}偏好匹配：{'、'.join(matched)}")
    facts = meal.get("nutrition") or {}
    maximum = _number(slots.get("max_calories_kcal"))
    calories = _number(facts.get("calories_kcal"))
    if maximum is not None and calories is not None and calories <= maximum:
        matches.append(f"每份热量不超过本轮设置的 {maximum:g} 千卡")
    minimum = _number(slots.get("min_protein_g"))
    protein = _number(facts.get("protein_g"))
    if minimum is not None and protein is not None and protein >= minimum:
        matches.append(f"每份蛋白质达到本轮设置的至少 {minimum:g} 克")
    return "；".join(matches) + "。" if matches else ""


def _latest_data(results: list[dict[str, Any]], name: str) -> dict[str, Any]:
    for result in reversed(results):
        if result.get("tool_name") == name and result.get("status") == "success":
            data = result.get("data")
            return data if isinstance(data, dict) else {}
    return {}


def _meal_explanation(meal: dict[str, Any]) -> str:
    raw_nutrition = meal.get("nutrition")
    facts = raw_nutrition if isinstance(raw_nutrition, dict) else meal
    parts = []
    serving = _optional_string(facts.get("serving_size")) or "标准份量"
    weight = _number(facts.get("serving_weight_g"))
    if weight:
        label = "投料" if facts.get("weight_basis") == "ingredient_input" else ""
        serving = f"{serving}（{label}{weight:g} 克）"
    calories, protein = _number(facts.get("calories_kcal")), _number(facts.get("protein_g"))
    qualifier = "估算" if _nutrition_is_unverified(meal) else ""
    if calories is not None:
        parts.append(f"每{serving}{qualifier}约 {calories:g} 千卡")
    if protein is not None:
        parts.append(f"蛋白质{qualifier}约 {protein:g} 克")
    if meal.get("meal_role", "complete_meal") != "complete_meal":
        parts.append("这是单道菜、主食或加餐，不能单独代表完整一餐")
    allergens = _strings(meal.get("allergens"))
    parts.append(
        f"餐食记录标注的过敏原：{'、'.join(allergens)}"
        if allergens
        else "餐食记录未标注过敏原，不代表实际制作时无过敏风险"
    )
    return "；".join(parts) + "。"


def _ground_nutrition(response: FinalResponse, tool_results: list[dict[str, Any]]) -> FinalResponse:
    data = _latest_data(tool_results, "get_nutrition")
    if not data:
        return response
    citation = data.get("citation") or {}
    nutrition = data.get("nutrition")
    facts = nutrition if isinstance(nutrition, dict) else {}
    warnings = _nutrition_warnings(data)
    return FinalResponse(
        message=f"{data.get('name', '该餐食')}：{_meal_explanation(data)}",
        citations=[
            Citation(
                source_id=str(citation.get("source_id") or f"meal:{data['meal_id']}"),
                title=str(data.get("name") or "餐食营养"),
                uri=_optional_string(facts.get("source_uri"))
                or _optional_string(citation.get("uri")),
            ),
            *_ingredient_citations(facts),
        ],
        warnings=warnings,
    )


def _nutrition_is_unverified(meal: dict[str, Any]) -> bool:
    nutrition = meal.get("nutrition")
    facts = nutrition if isinstance(nutrition, dict) else meal
    return bool(facts.get("source_status", "unverified_estimate") != "verified")


def _nutrition_warnings(meal: dict[str, Any]) -> list[str]:
    nutrition = meal.get("nutrition")
    facts = nutrition if isinstance(nutrition, dict) else meal
    if facts.get("source_status") == "calculated_estimate":
        return [
            str(facts.get("calculation_note") or "营养值为按配方计算的估算，未经成菜检测。")
        ]
    return [_ESTIMATE_WARNING] if _nutrition_is_unverified(meal) else []


def _ingredient_citations(facts: dict[str, Any]) -> list[Citation]:
    return [
        Citation(
            source_id=f"ingredient-nutrition:{uri}",
            title="食材营养数据（每100克）",
            source_type="ingredient",
            uri=uri,
        )
        for uri in _strings(facts.get("ingredient_source_uris"))
    ]


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
