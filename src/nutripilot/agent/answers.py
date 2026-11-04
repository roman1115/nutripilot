"""Evidence-bound prose, kept separate from canonical meal facts."""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal
from typing import Any

from nutripilot.agent.agents.base import compact_tool_results
from nutripilot.agent.contracts import AnswerReview, RecommendationDraft
from nutripilot.agent.grounding import _meal_records, ground_recommendation_facts
from nutripilot.agent.state import AgentState
from nutripilot.domain.enums import Intent
from nutripilot.domain.models import Citation, FinalResponse
from nutripilot.safety import OutputGuard, has_dietary_conflict, merge_dietary_terms
from nutripilot.safety.dietary import match_ingredient, meets_ingredient_requirements

_RECOMMENDATION_INTENTS = {
    Intent.MEAL_RECOMMENDATION.value, Intent.MEAL_ADJUSTMENT.value, Intent.MEAL_PLAN.value,
}
_DOMAIN_INTENTS = {Intent.FOOD_QUESTION.value, Intent.CONVERSATION.value}
_NUMBER_UNITS = {
    "calories_kcal": "kcal", "max_calories_kcal": "kcal", "daily_calorie_target": "kcal",
    "protein_g": "g", "min_protein_g": "g", "daily_protein_target_g": "g",
    "fat_g": "g", "fiber_g": "g", "carbohydrates_g": "g", "serving_size_g": "g",
    "sodium_mg": "mg", "prep_minutes": "分钟", "max_prep_minutes": "分钟",
    "price_cny": "元", "max_price_cny": "元", "temperature_c": "℃",
    "humidity_percent": "%",
}
_UNIT_ALIASES = {
    "千卡": "kcal", "大卡": "kcal", "千克": "kg", "公斤": "kg", "克": "g",
    "毫克": "mg", "毫升": "ml", "mL": "ml", "升": "l", "°C": "℃",
    "厘米": "cm", "毫米": "mm",
}


def answer_evidence(results: list[dict[str, Any]], state: AgentState) -> list[dict[str, Any]]:
    """Project relevant context; numeric and final safety guards use original evidence."""
    message = state.get("user_message", "")
    slots = state.get("slots", {})
    needs_nutrition = (
        state.get("intent") == Intent.NUTRITION_QUERY.value
        or any(slots.get(key) is not None for key in (
            "max_calories_kcal", "min_protein_g", "daily_calorie_target",
            "daily_protein_target_g",
        ))
        or bool(re.search(r"营养|热量|千卡|大卡|蛋白|脂肪|碳水|低脂|高蛋白|钠|盐分", message))
    )
    needs_weather = bool(re.search(r"天气|下雨|炎热|寒冷|降温|天热|天冷", message))
    projected = []
    for result in compact_tool_results(results):
        name = result.get("tool_name")
        if name == "get_weather_context" and not needs_weather:
            continue
        data = result.get("data")
        if isinstance(data, dict):
            if name == "get_user_profile":
                result = {**result, "data": {
                    key: value for key, value in data.items()
                    if key in {
                        "allergens", "dislikes", "excluded_ingredients", "preferences",
                        "tastes", "cuisines", "dietary_tags",
                    }
                }}
            elif not needs_nutrition:
                if name == "search_meals":
                    result = {**result, "data": {**data, "hits": [
                        {key: value for key, value in meal.items() if key != "nutrition"}
                        for meal in data.get("hits", [])
                    ]}}
                elif name in {"get_recipe", "get_nutrition"}:
                    result = {**result, "data": {
                        key: value for key, value in data.items() if key != "nutrition"
                    }}
        projected.append(result)
    return projected


def answer_sources(state: AgentState) -> dict[str, Citation]:
    sources: dict[str, Citation] = {}
    for result in [*state.get("conversation_evidence", []), *state.get("tool_results", [])]:
        if result.get("status") != "success":
            continue
        data = result.get("data")
        if not isinstance(data, dict):
            continue
        records = [data, *data.get("hits", [])]
        for record in records:
            if not isinstance(record, dict):
                continue
            meal = record.get("meal", record)
            raw = record.get("citation") or meal.get("citation") or {}
            source_id = raw.get("source_id") or record.get("source_id")
            meal_id = meal.get("meal_id") or meal.get("id")
            if not source_id and meal_id:
                source_id = f"meal:{meal_id}"
            if source_id:
                sources[str(source_id)] = Citation(
                    source_id=str(source_id),
                    title=str(
                        raw.get("title") or meal.get("name") or record.get("title", "参考资料")
                    ),
                    uri=raw.get("uri") or record.get("source_uri"),
                    source_type="guideline" if record.get("source_id") else "meal",
                )
    return sources


def canonical_answer(state: AgentState, draft: RecommendationDraft) -> FinalResponse:
    if state.get("intent") == Intent.FOOD_QUESTION.value:
        why = canonical_why_answer(state)
        if why:
            return why
        comparison = canonical_comparison_answer(state)
        if comparison:
            return comparison
        discussed = state.get("discussed_meals", [])
        if discussed and any(term in state.get("user_message", "") for term in (
            "区别", "比较", "对比",
        )):
            names = "和".join(item["name"] for item in discussed[:2])
            return FinalResponse(
                message=(
                    f"你问的是{names}。目前资料不足，无法可靠比较；"
                    "可以稍后重试，或指定已知的比较维度。"
                ),
                citations=list(answer_sources(state).values()),
            )
    if state.get("intent") in _DOMAIN_INTENTS:
        sources = answer_sources(state)
        return FinalResponse(
            message=(
                "不客气，有饮食或做菜的问题可以继续聊。"
                if state.get("intent") == Intent.CONVERSATION.value
                else "目前还不能可靠地回答这个问题。你想了解哪道菜，或哪个具体的烹饪问题？"
            ),
            citations=[
                sources[source_id] for source_id in dict.fromkeys(draft.source_ids)
                if source_id in sources and state.get("intent") != Intent.CONVERSATION.value
            ],
        )
    # Unreviewed model text must never become the fallback for missing tool evidence.
    safe_draft = draft.model_copy(update={
        "message": "暂时没有足够的可靠资料回答这个问题，请补充具体对象或稍后重试。",
    })
    response = ground_recommendation_facts(
        FinalResponse.model_validate(safe_draft.model_dump(exclude={"source_ids"})),
        state.get("tool_results", []), state.get("slots", {}),
        user_message=state.get("user_message", ""),
        require_meal_evidence=state.get("intent") in _RECOMMENDATION_INTENTS,
    )
    response.warnings = list(dict.fromkeys([
        *response.warnings, *(state.get("plan_quality") or {}).get("warnings", []),
    ]))
    return response


def canonical_why_answer(state: AgentState) -> FinalResponse | None:
    message = state.get("user_message", "")
    discussed = state.get("discussed_meals", [])
    if (
        state.get("intent") != Intent.FOOD_QUESTION.value
        or not discussed
        or not any(term in message for term in ("为什么推荐", "为什么选"))
    ):
        return None
    index = 1 if any(term in message for term in ("第二", "2")) else 0
    if index >= len(discussed):
        return FinalResponse(message="当前展示顺序里没有这道菜，请告诉我具体菜名。")
    selected = discussed[index]
    meal = None
    for result in [*state.get("conversation_evidence", []), *state.get("tool_results", [])]:
        data = result.get("data")
        if (
            result.get("tool_name") != "search_meals"
            or result.get("status") != "success"
            or not isinstance(data, dict)
        ):
            continue
        for hit in data.get("hits", []):
            record = hit.get("meal", hit) if isinstance(hit, dict) else None
            if (
                isinstance(record, dict)
                and (record.get("meal_id") or record.get("id")) == selected["meal_id"]
            ):
                meal = record
                break
        if meal:
            break
    raw_required = state.get("slots", {}).get("required_ingredients", [])
    required = [str(item) for item in raw_required] if isinstance(raw_required, list) else []
    matched_ingredients = [
        term for term in required
        if meal and match_ingredient(
            term, meal.get("ingredients", []), meal.get("ingredient_records", [])
        )
    ]
    reasons = (
        [f"收录配方包含{'、'.join(matched_ingredients)}"]
        if matched_ingredients else []
    )
    meal_type = state.get("slots", {}).get("meal_type")
    meal_type_labels = {
        "breakfast": "早餐", "lunch": "午餐", "dinner": "晚餐",
        "snack": "加餐", "late_snack": "夜宵",
    }
    if (
        meal and meal_type in meal.get("meal_types", [])
        and meal_type in meal_type_labels
    ):
        reasons.append(f"收录餐次类型包含{meal_type_labels[meal_type]}")
    for field, label in (
        ("tastes", "口味"), ("cuisines", "菜系"),
        ("dietary_tags", "饮食标签"), ("health_goals", "筛选标签"),
    ):
        requested = state.get("slots", {}).get(field, [])
        available = meal.get(field, []) if meal else []
        if isinstance(requested, list) and isinstance(available, list):
            matched = [term for term in requested if term in available]
            if matched:
                reasons.append(f"记录的{label}包含{'、'.join(matched)}")
    answer = (
        f"列出{selected['name']}，是因为{'；'.join(reasons)}，符合你提出的要求。"
        if reasons else
        f"你问的是{selected['name']}，但目前保留的资料不足以确认具体匹配理由。"
    )
    return FinalResponse(
        message=(
            answer + "展示顺序不代表排名，也不能据此说它比其他候选更好。"
        ),
        citations=[
            citation for citation in answer_sources(state).values()
            if citation.source_id in {
                f"meal:{selected['meal_id']}", f"recipe:{selected['meal_id']}",
            }
        ],
    )


def canonical_comparison_answer(state: AgentState) -> FinalResponse | None:
    message = state.get("user_message", "")
    discussed = state.get("discussed_meals", [])
    if (
        state.get("intent") != Intent.FOOD_QUESTION.value
        or len(discussed) < 2
        or not any(term in message for term in ("区别", "比较", "对比"))
        or any(term in message for term in (
            "营养", "热量", "蛋白", "脂肪", "碳水", "钠", "做法", "步骤",
            "口味", "味道", "烹饪",
        ))
    ):
        return None
    recipes = {}
    for result in [*state.get("conversation_evidence", []), *state.get("tool_results", [])]:
        if result.get("tool_name") != "get_recipe" or result.get("status") != "success":
            continue
        data = result.get("data")
        if isinstance(data, dict) and isinstance(data.get("meal_id"), int):
            recipes[data["meal_id"]] = data
    compared = []
    sources = answer_sources(state)
    for item in discussed[:2]:
        recipe = recipes.get(item["meal_id"])
        ingredients = recipe.get("raw_ingredients") if recipe else None
        if not isinstance(ingredients, str) or not ingredients.strip():
            return None
        compared.append(f"{item['name']}的收录配方食材：{ingredients.strip()}")
    citations = [
        citation for citation in sources.values()
        if citation.source_id in {
            f"recipe:{item['meal_id']}" for item in discussed[:2]
        }
    ]
    return FinalResponse(
        message=(
            "按收录配方原文，" + "；".join(compared)
            + "。这只能比较配方列出的食材，不能据此判断口味或营养高低。"
        ),
        citations=citations,
    )


def response_digest(response: FinalResponse) -> str:
    return hashlib.sha256(json.dumps(
        response.model_dump(mode="json"), ensure_ascii=False, sort_keys=True,
    ).encode()).hexdigest()


def compact_answer(response: FinalResponse) -> dict[str, Any]:
    """Project verified identities, never fallback prose or generated explanations."""
    return {
        "status": response.status,
        "recommendations": [
            {"meal_id": card.meal_id, "name": card.name}
            for card in response.recommendations
        ],
    }


def answer_review_units(draft: RecommendationDraft) -> dict[str, str]:
    paragraphs = draft.message.split("\n\n")
    if draft.recommendations:
        # Split dish descriptions so a true ingredient match cannot hide a made-up method.
        paragraphs = [
            clause for paragraph in paragraphs
            for clause in re.split(r"(?<=[。；：])", paragraph)
        ]
    units = {
        f"message:{index}": paragraph.strip()
        for index, paragraph in enumerate(item for item in paragraphs if item.strip())
    }
    units.update({
        f"reason:{index}": item.reason
        for index, item in enumerate(draft.recommendations) if item.reason.strip()
    })
    return units


def guard_answer_facts(
    response: FinalResponse, state: AgentState, guard: OutputGuard,
) -> tuple[FinalResponse, list[str], set[int]]:
    forbidden = _find_constraint_violations(state)
    guarded, flags = guard.validate(
        response,
        allowed_meal_ids=(
            set(state.get("candidate_ids", []))
            & set(_meal_records(state.get("tool_results", [])))
        ),
        forbidden_meal_ids=forbidden,
    )
    return guarded, flags, forbidden


def _find_constraint_violations(state: AgentState) -> set[int]:
    constraints = state.get("dietary_constraints", {})
    declared_allergens = merge_dietary_terms(constraints.get("allergens"))
    excluded_ingredients = merge_dietary_terms(constraints.get("excluded_ingredients"))
    required = state.get("slots", {}).get("required_ingredients", [])
    groups = state.get("slots", {}).get("ingredient_any_groups", [])
    if not declared_allergens and not excluded_ingredients and not required and not groups:
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
                    ingredient_records=meal.get("ingredient_records", []),
                )
                or not meets_ingredient_requirements(meal, required, groups)
            ):
                forbidden.add(meal_id)
    return forbidden


def answer_review_records(
    state: AgentState, draft: RecommendationDraft | None = None,
) -> dict[str, str]:
    records: dict[str, Any] = {
        "request": {"user_message": state.get("user_message"), "slots": state.get("slots", {})},
        "display": {
            "meals": [
                {"meal_id": item["meal_id"], "name": item["name"]}
                for item in state.get("discussed_meals", [])
            ],
            "order_is_ranking": False,
        },
    }
    review_state: AgentState = {
        **state,
        "user_message": state.get("user_message", "") + (
            "\n" + "\n".join(answer_review_units(draft).values()) if draft else ""
        ),
    }
    for scope, results in (
        ("current", state.get("tool_results", [])),
        ("history", state.get("conversation_evidence", [])),
    ):
        for index, result in enumerate(answer_evidence(results, review_state)):
            if result.get("status") == "success":
                if result.get("tool_name") == "search_meals":
                    data = result.get("data")
                    if isinstance(data, dict):
                        result = {**result, "data": {**data, "hits": [
                            {key: value for key, value in meal.items() if key not in {
                                "description", "is_active", "recipe_id",
                            }}
                            for meal in data.get("hits", [])
                        ]}}
                records[f"{scope}:{index}"] = result
    return {key: _review_record_text(value) for key, value in records.items()}


def _review_record_text(value: Any, path: str = "") -> str:
    if isinstance(value, dict) and value:
        return "\n".join(
            _review_record_text(item, f"{path}.{key}" if path else key)
            for key, item in value.items()
        )
    if isinstance(value, list) and value:
        return "\n".join(
            _review_record_text(item, f"{path}[{index}]")
            for index, item in enumerate(value)
        )
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return f"{path}: {text}"


def answer_review_issues(
    review: AnswerReview, units: dict[str, str], records: dict[str, str],
    objects: dict[str, list[int]] | None = None,
) -> list[str]:
    issues = list(review.issues)
    if not review.supported and not issues:
        issues.append("ANSWER_NOT_SUPPORTED")
    reviewed_ids = [check.unit_id for check in review.checks]
    if (
        not units or len(set(reviewed_ids)) != len(reviewed_ids)
        or set(reviewed_ids) != set(units)
    ):
        issues.append("INCOMPLETE_REVIEW: 必须逐段核对正文和推荐理由，不得漏审或重复。")
    for check in review.checks:
        text = units.get(check.unit_id, "")
        prefix = f"{check.unit_id}「{text}」"
        if not check.supported or not check.conditions_preserved:
            issues.append(f"{prefix}: {check.explanation}")
        for claim in check.object_counts:
            known_ids = (objects or {}).get(claim.scope)
            claimed_ids = claim.meal_ids or known_ids or []
            actual = len(claimed_ids)
            if (
                claim.quote not in text or known_ids is None
                or len(set(claimed_ids)) != actual or set(claimed_ids) - set(known_ids)
            ) or (
                claim.maximum is not None and claim.maximum < claim.minimum
            ):
                issues.append(f"{prefix}: INVALID_OBJECT_COUNT: 数量摘录或核对对象无效。")
            elif actual < claim.minimum or (
                claim.maximum is not None and actual > claim.maximum
            ):
                issues.append(
                    f"{prefix}: OBJECT_COUNT_MISMATCH: 「{claim.quote}」与实际{actual}道不符。"
                )
        if check.kind in {"source_fact", "ranking", "health_effect"} and not check.evidence:
            issues.append(f"{prefix}: MISSING_CLAIM_EVIDENCE: 缺少对应原始证据，删除或收窄断言。")
        for condition in check.conditions:
            if not condition.answer_quote.strip() or condition.answer_quote not in text:
                issues.append(
                    f"{prefix}: MISSING_ANSWER_CONDITION: "
                    f"正文未表达来源条件「{condition.source.quote}」，须补齐条件或删除相关结论。"
                )
        for reference in [
            *check.evidence, *(condition.source for condition in check.conditions),
        ]:
            if (
                reference.record_id not in records
                or not _contains_evidence_quote(records[reference.record_id], reference.quote)
            ):
                issues.append(
                    f"{prefix}: INVALID_EVIDENCE_REFERENCE: "
                    f"{reference.record_id} 未包含引文「{reference.quote}」。"
                )
    return list(dict.fromkeys(issues))


def _contains_evidence_quote(record: str, quote: str) -> bool:
    # A reviewer may excerpt several source lines; every fragment must exist in source order.
    if not quote.strip():
        return False
    cursor = 0
    for fragment in quote.splitlines():
        if not fragment.strip():
            continue
        position = record.find(fragment, cursor)
        if position < 0:
            return False
        cursor = position + len(fragment)
    return True


def prose_issues(
    draft: RecommendationDraft, canonical: FinalResponse, state: AgentState,
    evidence: dict[str, Any],
) -> list[str]:
    issues = []
    if set(draft.source_ids) - set(answer_sources(state)):
        issues.append("UNKNOWN_SOURCE")
    if state.get("intent") == Intent.CONVERSATION.value and draft.source_ids:
        issues.append("CONVERSATION_MUST_NOT_CITE")
    cards = {item.meal_id: item for item in canonical.recommendations}
    if len({item.meal_id for item in draft.recommendations}) != len(draft.recommendations):
        issues.append("DUPLICATE_MEAL")
    if any(item.meal_id not in cards or item.name != cards[item.meal_id].name
           for item in draft.recommendations):
        issues.append("UNSUPPORTED_MEAL")
    text = "\n".join([draft.message, *(item.reason for item in draft.recommendations)])
    if re.search(r"你.{0,16}(?:过敏|患有|确诊)|治愈|替代药物|停药|处方", text):
        issues.append("PERSONAL_OR_MEDICAL_CLAIM")
    if re.search(r"(?:不含|没有|无)(?:任何|常见|已知)?过敏原", text):
        issues.append("ALLERGEN_ABSENCE_NOT_VERIFIED: 未标注不等于不含过敏原，不能保证无风险。")
    # Exact numbers need evidence too; semantic attribution is checked by the reviewer.
    numbers = _quantity_numbers(text)
    known = _evidence_quantities({
        key: evidence[key] for key in ("tool_results", "conversation_evidence", "slots")
        if key in evidence
    })
    displayed_numbers = {
        (number.quantize(Decimal(precision)), unit)
        for number, unit in known for precision in ("1", "0.1", "0.01")
        if number.adjusted() < 12
    }
    unsupported = numbers - known - displayed_numbers
    if unsupported:
        issues.append(
            "UNSUPPORTED_NUMBER: " + ", ".join(
                f"{number} {unit}" for number, unit in sorted(unsupported)
            )
            + "；删除这些无依据的数值及相关断言，不换算、不新增或借用其他对象的数值。"
        )
    if (
        state.get("intent") in _RECOMMENDATION_INTENTS
        and not canonical.recommendations
        and not any(
            result.get("tool_name") == "search_meals" and result.get("status") == "success"
            for result in state.get("tool_results", [])
        )
    ):
        issues.append("NO_CURRENT_MEAL_EVIDENCE")
    if canonical.status not in {"completed", "degraded"}:
        issues.append("INVALID_ANSWER_STATE")
    return issues


def approve_answer(draft: RecommendationDraft, canonical: FinalResponse) -> dict[str, Any]:
    return {
        "base_digest": response_digest(canonical),
        "message": draft.message,
        "reasons": {str(item.meal_id): item.reason for item in draft.recommendations},
    }


def apply_approved_answer(
    canonical: FinalResponse, approved: dict[str, Any] | None,
) -> FinalResponse:
    if not approved or approved.get("base_digest") != response_digest(canonical):
        return canonical
    return canonical.model_copy(update={
        "message": approved["message"],
        "recommendations": [
            item.model_copy(update={"reason": approved.get("reasons", {}).get(
                str(item.meal_id), item.reason,
            )}) for item in canonical.recommendations
        ],
    })


def _numbers(text: str) -> set[Decimal]:
    return {Decimal(match) for match in re.findall(r"\d+(?:\.\d+)?", text)}


def _quantity_numbers(text: str) -> set[tuple[Decimal, str]]:
    text = re.sub(r"https?://[^\s<>]+", "", text).replace("**", "")
    quantities = re.findall(
        r"(\d+(?:\.\d+)?(?:\s*[-~～至–]\s*\d+(?:\.\d+)?)?)\s*"
        r"(千卡|大卡|kcal|千克|公斤|毫克|克|kg|mg|g|毫升|升|ml|mL|分钟|小时|秒|元|%|℃|°C|厘米|毫米|cm|mm)",
        text,
    )
    return {
        (number, _UNIT_ALIASES.get(unit, unit))
        for quantity, unit in quantities for number in _numbers(quantity)
    }


def _evidence_quantities(value: Any, unit: str | None = None) -> set[tuple[Decimal, str]]:
    if isinstance(value, dict):
        return set().union(*(
            _evidence_quantities(
                item, _NUMBER_UNITS.get(key, unit if key in {"minimum", "maximum"} else None),
            ) for key, item in value.items() if key not in {
                "citation", "uri", "source_uri", "ingredient_source_uris",
                "source_id", "source_sha256", "version",
            }
        ))
    if isinstance(value, list):
        return set().union(*(_evidence_quantities(item, unit) for item in value))
    if isinstance(value, str):
        return _quantity_numbers(value)
    if unit and isinstance(value, (int, float)) and not isinstance(value, bool):
        return {(Decimal(str(value)), unit)}
    return set()
