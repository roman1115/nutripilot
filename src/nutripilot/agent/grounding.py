"""Canonicalise model-authored recommendations against authoritative tool results."""

from __future__ import annotations

import re
from typing import Any

from nutripilot.domain.models import Citation, FinalResponse, MealRecommendation
from nutripilot.safety.dietary import match_ingredient, meets_ingredient_requirements

_ESTIMATE_WARNING = (
    "营养值为未独立核验的演示估算，份量缺少统一克重；实际值会随食材、份量和烹调方式变化，"
    "不应作为精确营养或健康目标依据。"
)


def ground_recommendation_facts(
    response: FinalResponse,
    tool_results: list[dict[str, Any]],
    slots: dict[str, Any] | None = None,
    *,
    user_message: str = "",
    require_meal_evidence: bool = False,
    auto_select: bool = True,
) -> FinalResponse:
    recipe = _latest_data(tool_results, "get_recipe")
    if recipe:
        return _ground_recipe(recipe)
    if not any(
        result.get("tool_name") == "search_meals" and result.get("status") == "success"
        for result in tool_results
    ):
        if require_meal_evidence or response.recommendations or any(
            result.get("tool_name") == "search_meals" for result in tool_results
        ):
            failed = any(result.get("tool_name") == "search_meals" for result in tool_results)
            return FinalResponse(
                status="degraded",
                message=(
                    "本轮餐食检索未成功，暂时无法提供可靠推荐，请稍后重试。"
                    if failed else "本轮缺少可验证的餐食依据，请重新检索后再试。"
                ),
            )
        return _ground_nutrition(response, tool_results)
    meals = _meal_records(tool_results)
    required = _strings((slots or {}).get("required_ingredients"))
    groups = (slots or {}).get("ingredient_any_groups", [])
    if required or groups:
        meals = {
            meal_id: meal for meal_id, meal in meals.items()
            if meets_ingredient_requirements(meal, required, groups)
        }
    recommendations = [item for item in response.recommendations if item.meal_id in meals][:3]
    if not recommendations and auto_select:
        recommendations = [
            MealRecommendation(meal_id=meal_id, name=meal["name"], reason="")
            for meal_id, meal in list(meals.items())[:3]
        ]
    requested_name = str((slots or {}).get("food_name") or "").strip().casefold()
    if requested_name:
        meals = {
            meal_id: meal for meal_id, meal in meals.items()
            if requested_name in {
                str(name).strip().casefold()
                for name in (meal.get("name"), *meal.get("aliases", []))
            }
        }
        recommendations = (
            [MealRecommendation(meal_id=meal_id, name=meal["name"], reason="")
             for meal_id, meal in meals.items()][:3]
            if auto_select else [item for item in recommendations if item.meal_id in meals]
        )
    grounded = []
    citations: list[Citation] = []
    for recommendation in recommendations:
        meal = meals.get(recommendation.meal_id)
        if meal is None:
            continue
        nutrition = meal.get("nutrition")
        facts = nutrition if isinstance(nutrition, dict) else meal
        grounded.append(
            MealRecommendation.model_validate(
                {
                    **recommendation.model_dump(),
                    "name": str(meal.get("name") or recommendation.name),
                    "calories_kcal": _number(facts.get("calories_kcal")),
                    "protein_g": _number(facts.get("protein_g")),
                    "carbohydrates_g": _number(facts.get("carbohydrates_g")),
                    "fat_g": _number(facts.get("fat_g")),
                    "nutrient_ranges": facts.get("nutrient_ranges") or {},
                    "tags": _strings(meal.get("health_goals")),
                    "reason": _recommendation_explanation(
                        meal, _latest_data(tool_results, "get_user_profile"),
                        slots or {}, user_message,
                    ) if auto_select else "",
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
                excerpt=_optional_string(facts.get("calculation_note")),
            )
        )
        citations.extend(_ingredient_citations(facts))

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
    for field, label in (("max_price_cny", "价格"), ("max_prep_minutes", "准备时间")):
        if (slots or {}).get(field) is not None:
            warnings.append(f"已按{label}上限筛选，{label}未知的菜谱未纳入候选。")
    if citations:
        warnings.append("餐食过敏原标注不涵盖制作过程中的交叉接触，请核对实际食材。")
    warnings.extend(_allergen_warning(meals[item.meal_id]) for item in grounded)
    canonical_names = [item.name for item in grounded]
    dish_reference = "这道菜" if len(grounded) == 1 else "这几道菜"
    message = (
        f"可以试试{dish_reference}：{'、'.join(canonical_names)}。"
        if canonical_names
        else "没有找到可验证且满足当前条件的餐食，请调整非过敏、非忌口条件后重试。"
    )
    plan = _latest_data(tool_results, "create_meal_plan")
    if required or groups:
        ingredient_text = "、".join([*required, *("或".join(group) for group in groups)])
        ingredient_match = (
            "这道菜的收录配方包含" if len(grounded) == 1 else "这些菜的收录配方都包含"
        )
        message = (
            f"想吃{ingredient_text}的话，可以选{'、'.join(canonical_names)}；"
            f"{ingredient_match}你要的食材。"
            if canonical_names
            else f"目前没有找到满足食材条件（{ingredient_text}）且符合其他条件的菜，"
            "暂时无法按这个要求推荐。"
        )
    if not grounded:
        if requested_name:
            message = (
                f"没有找到“{(slots or {})['food_name']}”的匹配记录且满足当前条件的菜品，"
                "暂时无法按这个对象推荐；可以核对名称，或明确选择其他食材或菜品。"
            )
        diagnostics = _latest_data(tool_results, "search_meals").get("filter_diagnostics", {})
        details = []
        active = diagnostics.get("active_meals", 0)
        for code, label, constraint in (
            ("unknown_price", "价格", "预算"),
            ("unknown_prep", "准备时间", "时间要求"),
        ):
            count = diagnostics.get(code, 0)
            if isinstance(count, int) and count > 0:
                scope = "目前收录菜谱的" if count == active else "部分菜谱的"
                details.append(f"{scope}{label}未知，无法核实是否符合你的{constraint}。")
        if not details:
            for code, label in (
                ("ingredient_requirements", "收录配方中没有匹配所需食材的菜品。"),
                ("dietary_conflict", "收录菜品均与过敏或忌口条件冲突。"),
                ("calories", "收录菜品均超出热量上限，热量按整道菜计算。"),
                ("protein", "收录菜品均未达到蛋白质下限，蛋白质按整道菜计算。"),
            ):
                if active > 0 and diagnostics.get(code) == active:
                    details.append(label)
                    break
        message += "".join(details)
    if plan:
        selected_ids = {item.get("meal_id") for item in plan.get("items", [])}
        grounded = [item for item in grounded if item.meal_id in selected_ids]
        message = (
            f"菜品餐单已保存，共安排 {len(plan.get('items', []))} 个餐次。"
            "营养按所选整道菜估算，不代表个人摄入量。"
        )
    else:
        explanations = [f"{item.name}：{item.reason}" for item in grounded if item.reason]
        if explanations:
            message += "\n\n" + "\n".join(explanations)
    if not auto_select and grounded and not plan:
        message = "本次未能完成可靠的推荐说明，以下仅保留已核对的菜品与整菜营养估算。"
    elif not auto_select and meals and not grounded and not plan:
        message = "检索到了候选，但这次未能完成可靠选择，请稍后重试。"
    return response.model_copy(
        update={
            "status": (
                "completed" if plan
                else response.status if grounded or not auto_select else "degraded"
            ),
            "message": message,
            "recommendations": grounded,
            "citations": citations,
            "warnings": warnings,
            "suggested_actions": ["换一批菜品"] if plan or grounded else ["调整条件"],
        }
    )


def _recommendation_explanation(
    meal: dict[str, Any], profile: dict[str, Any], slots: dict[str, Any],
    user_message: str,
) -> str:
    matches: list[str] = []
    required = _strings(slots.get("required_ingredients"))
    terms = list(dict.fromkeys([
        *required, *(term for group in slots.get("ingredient_any_groups", []) for term in group)
    ]))
    matched_terms = [
        term for term in terms
        if match_ingredient(term, meal.get("ingredients", []), meal.get("ingredient_records", []))
    ]
    if matched_terms:
        matches.append(
            f"{meal['name']}的这份配方用到了{'、'.join(matched_terms)}，符合你这次想吃的食材"
        )
    elif slots.get("food_name") == meal.get("name"):
        matches.append("这是你点名想吃的菜")
    facts = meal.get("nutrition") or {}
    maximum = _number(slots.get("max_calories_kcal"))
    calories = _nutrient_bound(facts, "calories_kcal", "maximum")
    if maximum is not None and calories is not None and calories <= maximum:
        matches.append(f"整道菜热量不超过你设定的 {_display_number(maximum)} 千卡")
    minimum = _number(slots.get("min_protein_g"))
    protein = _nutrient_bound(facts, "protein_g", "minimum")
    if minimum is not None and protein is not None and protein >= minimum:
        matches.append(f"整道菜蛋白质至少 {_display_number(minimum)} 克，达到你的下限要求")
    for field, label, unit, bound in (
        ("prep_minutes", "准备时间", "分钟", "max_prep_minutes"),
        ("price_cny", "记录价格", "元", "max_price_cny"),
    ):
        value, limit = _number(meal.get(field)), _number(slots.get(bound))
        if value is not None and limit is not None and value <= limit:
            matches.append(f"{label} {_display_number(value)} {unit}，在你的要求内")
    supplemental: list[str] = []
    for field, label in (
        ("tastes", "口味"),
        ("cuisines", "菜系"),
        ("dietary_tags", "饮食标签"),
    ):
        profile_field = "preferences" if field == "tastes" else field
        available = set(_strings(meal.get(field)))
        explicit = sorted(
            term for term in _strings(slots.get(field))
            if term in user_message and term in available
        )
        if explicit:
            matches.insert(
                1 if matched_terms else 0,
                f"{label}是{'、'.join(explicit)}，符合你这次的要求",
            )
        else:
            matched = sorted(set(_strings(profile.get(profile_field))) & available)
            if matched:
                supplemental.append(f"{'、'.join(matched[:2])}{label}")
    if not matches:
        ingredients = _strings(meal.get("ingredients"))
        if ingredients:
            matches.append(f"这道菜的配方包含{'、'.join(ingredients[:2])}")
    if supplemental and not matched_terms and len("；".join(matches)) < 60:
        matches.append(f"也照顾到你平时偏好的{'、'.join(supplemental)}")
    return "；".join(matches) + "。" if matches else ""


def _allergen_warning(meal: dict[str, Any]) -> str:
    allergens = _strings(meal.get("allergens"))
    detail = (
        f"餐食记录标注的过敏原：{'、'.join(allergens)}"
        if allergens else "未标注过敏原，不代表实际制作时无过敏风险"
    )
    return f"{meal.get('name', '该菜品')}：{detail}。"


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
        weight_range = facts.get("input_weight_range_g")
        display_weight = _format_range(weight_range) or _display_number(weight)
        serving = f"{serving}（{label}{display_weight} 克）"
    calories, protein = _number(facts.get("calories_kcal")), _number(facts.get("protein_g"))
    qualifier = "估算" if _nutrition_is_unverified(meal) else ""
    if calories is not None:
        parts.append(f"按{serving}{qualifier}约 {_nutrient_text(facts, 'calories_kcal')} 千卡")
    if protein is not None:
        parts.append(f"蛋白质{qualifier}约 {_nutrient_text(facts, 'protein_g')} 克")
    for field, name, unit in (
        ("carbohydrates_g", "碳水", "克"),
        ("fat_g", "脂肪", "克"),
        ("fiber_g", "纤维", "克"),
        ("sodium_mg", "钠", "毫克"),
    ):
        if _number(facts.get(field)) is not None:
            parts.append(f"{name}{qualifier}约 {_nutrient_text(facts, field)} {unit}")
    if meal.get("meal_role", "complete_meal") != "complete_meal":
        parts.append("这是单道菜、主食或加餐，不能单独代表完整一餐")
    allergens = _strings(meal.get("allergens"))
    parts.append(
        f"餐食记录标注的过敏原：{'、'.join(allergens)}"
        if allergens
        else "餐食记录未标注过敏原，不代表实际制作时无过敏风险"
    )
    return "；".join(parts) + "。"


def _nutrient_bound(facts: dict[str, Any], field: str, side: str) -> float | None:
    ranges = facts.get("nutrient_ranges")
    bounds = ranges.get(field) if isinstance(ranges, dict) else None
    return _number(bounds.get(side)) if isinstance(bounds, dict) else _number(facts.get(field))


def _format_range(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    low, high = _number(value.get("minimum")), _number(value.get("maximum"))
    if low is None or high is None:
        return None
    return _display_number(low) if low == high else (
        f"{_display_number(low)}～{_display_number(high)}"
    )


def _display_number(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _nutrient_text(facts: dict[str, Any], field: str) -> str:
    ranges = facts.get("nutrient_ranges")
    bounds = ranges.get(field) if isinstance(ranges, dict) else None
    return _format_range(bounds) or _display_number(facts[field])


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
                excerpt=_optional_string(facts.get("calculation_note")),
            ),
            *_ingredient_citations(facts),
        ],
        warnings=warnings,
    )


def _ground_recipe(data: dict[str, Any]) -> FinalResponse:
    citation = data.get("citation") or {}
    sections = [str(data["name"]), f"食材（原文）\n{data['raw_ingredients']}"]
    if data.get("raw_quantities"):
        sections.append(f"用量（原文）\n{data['raw_quantities']}")
    else:
        sections.append("来源未提供明确用量。")
    sections.append(f"做法\n{data['instructions']}")
    message = re.sub(r"\*\*(.+?)\*\*", r"\1", "\n\n".join(sections))
    return FinalResponse(
        message=message,
        citations=[
            Citation(
                source_id=str(citation.get("source_id") or f"recipe:{data['meal_id']}"),
                title=str(citation.get("title") or data["name"]),
                source_type="recipe",
                uri=_optional_string(citation.get("uri")),
            )
        ],
        warnings=[_allergen_warning(data)],
        suggested_actions=[f"查询{data['name']}的营养"],
    )


def _nutrition_is_unverified(meal: dict[str, Any]) -> bool:
    nutrition = meal.get("nutrition")
    facts = nutrition if isinstance(nutrition, dict) else meal
    return bool(facts.get("source_status", "unverified_estimate") != "verified")


def _nutrition_warnings(meal: dict[str, Any]) -> list[str]:
    nutrition = meal.get("nutrition")
    facts = nutrition if isinstance(nutrition, dict) else meal
    if facts.get("source_status") == "calculated_estimate":
        return ["营养按整道菜估算，不代表个人摄入量或完整一餐，未经成菜检测。"]
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
