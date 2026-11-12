from __future__ import annotations

import pytest
from pydantic import ValidationError

from nutripilot.agent.answers import (
    answer_needs_chicken_safety,
    answer_nutrition_calculations,
    answer_quantity_hints,
    apply_approved_answer,
    approve_answer,
    canonical_answer,
    prose_issues,
)
from nutripilot.agent.contracts import AnswerReview, RecommendationDraft
from nutripilot.agent.nodes.output import OutputGuardNode
from nutripilot.agent.state import new_agent_state
from nutripilot.domain.models import Citation, FinalResponse, MealRecommendation
from nutripilot.safety import OutputGuard
from nutripilot.safety.guards import CHICKEN_SAFETY_REFERENCE, dietary_prose_conflicts
from nutripilot.tracing import InMemoryTraceRecorder


def test_review_bool_cannot_be_a_truthy_string():
    with pytest.raises(ValidationError):
        AnswerReview.model_validate({"supported": "false", "issues": []})


@pytest.mark.parametrize("message", [
    "你对鱼过敏，所以推荐这个。",
    "你患有糖尿病，所以推荐这个。",
])
def test_unfounded_personal_claims_are_rejected_before_model_review(message):
    state = new_agent_state(message="解释一下", user_id=1)
    state["intent"] = "FOOD_QUESTION"
    draft = RecommendationDraft(message=message)
    assert prose_issues(draft, canonical_answer(state, draft), state)


def test_unknown_sources_and_unrequested_cards_are_rejected():
    state = new_agent_state(message="谢谢", user_id=1)
    state["intent"] = "CONVERSATION"
    draft = RecommendationDraft(
        message="推荐假的", source_ids=["invented"],
        recommendations=[{"meal_id": 999, "name": "假的", "reason": ""}],
    )
    issues = prose_issues(draft, canonical_answer(state, draft), state)
    assert "UNKNOWN_SOURCE" in issues
    assert "UNSUPPORTED_MEAL" in issues


def test_reviewed_prose_is_invalidated_when_canonical_facts_change():
    canonical = FinalResponse(message="原始", recommendations=[
        MealRecommendation(meal_id=1, name="测试菜", reason="原始", calories_kcal=100),
    ])
    approved = approve_answer(RecommendationDraft(message="审核后的自然解释"), canonical)
    assert apply_approved_answer(canonical, approved).message == "审核后的自然解释"
    changed = canonical.model_copy(update={"recommendations": [
        canonical.recommendations[0].model_copy(update={"calories_kcal": 200}),
    ]})
    assert apply_approved_answer(changed, approved).message == "原始"


@pytest.mark.asyncio
async def test_final_safety_removal_invalidates_narrative_and_recipe_sources():
    state = new_agent_state(message="想吃午餐", user_id=1)
    state.update(
        intent="MEAL_RECOMMENDATION", candidate_ids=[10],
        dietary_constraints={"allergens": ["鸡蛋"], "excluded_ingredients": []},
    )
    state["tool_results"] = [{
        "tool_name": "search_meals", "status": "success",
        "data": {"hits": [{
            "meal_id": 10, "name": "炒鸡蛋", "ingredients": ["鸡蛋"], "allergens": ["鸡蛋"],
            "nutrition": {"calories_kcal": 100},
            "citation": {"source_id": "recipe:10"},
        }]},
    }]
    draft = RecommendationDraft(
        message="可以选炒鸡蛋。",
        recommendations=[{"meal_id": 10, "name": "炒鸡蛋", "reason": "适合你"}],
    )
    canonical = canonical_answer(state, draft)
    state["approved_answer"] = approve_answer(draft, canonical)
    state["final_response"] = canonical.model_dump(mode="json")
    result = await OutputGuardNode(OutputGuard(), InMemoryTraceRecorder()).run(state)
    response = result["final_response"]
    assert not response["recommendations"]
    assert "炒鸡蛋" not in response["message"]
    assert "recipe:10" not in {source["source_id"] for source in response["citations"]}


def test_partial_unsupported_removal_cannot_leave_old_recommendation_in_body():
    response = FinalResponse(
        message="推荐真实菜和虚构菜",
        recommendations=[
            MealRecommendation(meal_id=1, name="真实菜", reason="可靠"),
            MealRecommendation(meal_id=999, name="虚构菜", reason="编造"),
        ],
        citations=[Citation(source_id="recipe:999", title="虚构")],
    )
    guarded, _ = OutputGuard().validate(response, allowed_meal_ids={1})
    assert "虚构菜" not in guarded.message
    assert not guarded.citations


def test_medical_claim_in_card_reason_is_also_blocked():
    response = FinalResponse(
        message="看看这个",
        recommendations=[MealRecommendation(meal_id=1, name="菜", reason="能治愈疾病")],
    )
    guarded, flags = OutputGuard().validate(response, allowed_meal_ids={1})
    assert guarded.status == "blocked"
    assert "MEDICAL_CLAIM_BLOCKED" in flags


@pytest.mark.parametrize(("text", "conflict"), [
    ("撒花生碎（可选）。", True),
    ("不要加入花生碎。", False),
    ("花生碎应省略。", False),
    ("花生碎因您对花生过敏，全程不可添加。", False),
    ("花生碎因过敏须避免。", False),
    ("如需增香，可撒上葱花、香菜（花生碎因过敏须避免）。", False),
    ("- 花生碎 10g（可选，但您对花生过敏，请勿添加）", False),
    ("- 花生碎 10g（可选）", True),
    ("用户记录花生过敏，这不是菜品过敏原信息。", False),
    ("不要加花生碎，但可以加花生油。", True),
    ("不要加花生碎但可以用花生油。", True),
    ("豆腐切块，与皮蛋拌匀。", False),
])
def test_known_dietary_exclusions_apply_to_optional_ingredients(text, conflict):
    assert bool(dietary_prose_conflicts(text, ["花生"])) is conflict


@pytest.mark.parametrize(("ingredients", "needed"), [
    ("鸡胸肉、姜、料酒", True),
    ("三黄鸡、香菇", True),
    ("鸡蛋、西葫芦、鸡精", False),
    ("豆腐、皮蛋", False),
])
def test_chicken_safety_reference_applies_to_meat_not_eggs_or_seasoning(ingredients, needed):
    state = new_agent_state(user_id=1, message="做法")
    state.update(intent="RECIPE_QUERY", tool_results=[{
        "tool_name": "get_recipe", "status": "success",
        "data": {"raw_ingredients": ingredients},
    }])
    assert answer_needs_chicken_safety(state) is needed


@pytest.mark.asyncio
async def test_sourced_chicken_safety_notice_survives_a_generated_recipe():
    from nutripilot.agent.answers import guard_answer_facts

    state = new_agent_state(user_id=1, message="凉拌鸡丝做法")
    state.update(intent="RECIPE_QUERY", tool_results=[{
        "tool_name": "get_recipe", "status": "success", "data": {
            "meal_id": 1, "name": "凉拌鸡丝", "raw_ingredients": "鸡胸肉、姜",
            "instructions": "煮熟。",
        },
    }])
    draft = RecommendationDraft(message="按原文准备，鸡肉用食品温度计核对熟度。")
    canonical, _, _ = guard_answer_facts(canonical_answer(state, draft), state, OutputGuard())
    state["final_response"] = canonical.model_dump(mode="json")
    state["approved_answer"] = approve_answer(draft, canonical)
    result = await OutputGuardNode(OutputGuard(), InMemoryTraceRecorder()).run(state)
    assert result["final_response"]["message"] == draft.message
    assert any("74°C" in item and "计时" in item
               for item in result["final_response"]["warnings"])
    assert any(item["source_id"] == CHICKEN_SAFETY_REFERENCE["source_id"]
               for item in result["final_response"]["citations"])


@pytest.mark.asyncio
async def test_final_guard_rechecks_prose_against_current_user_exclusions():
    state = new_agent_state(user_id=1, message="怎么做")
    state.update(intent="FOOD_QUESTION", dietary_constraints={"allergens": ["花生"]})
    canonical = FinalResponse(message="暂时无法回答。")
    state["final_response"] = canonical.model_dump(mode="json")
    state["approved_answer"] = approve_answer(
        RecommendationDraft(message="加花生碎拌匀。"), canonical,
    )
    result = await OutputGuardNode(OutputGuard(), InMemoryTraceRecorder()).run(state)
    assert result["final_response"]["status"] == "blocked"
    assert "加花生碎" not in result["final_response"]["message"]


def test_missing_nutrition_evidence_never_uses_the_unreviewed_draft_as_fallback():
    state = new_agent_state(message="查询营养", user_id=1)
    state["intent"] = "NUTRITION_QUERY"
    draft = RecommendationDraft(message="未经核对的营养说明99999千卡")
    canonical = canonical_answer(state, draft)
    assert "99999" not in canonical.message
    assert "未经核对" not in canonical.message


@pytest.mark.parametrize("status", [None, "error", "success"])
def test_only_a_successful_empty_search_supports_an_explanation_without_cards(status):
    state = new_agent_state(message="想吃鸡胸肉，整道菜不超过1千卡", user_id=1)
    state["intent"] = "MEAL_RECOMMENDATION"
    state["slots"] = {"required_ingredients": ["鸡胸肉"], "max_calories_kcal": 1}
    state["tool_results"] = [{
        "tool_name": "search_meals", "status": status, "data": {"hits": []},
    }] if status else []
    draft = RecommendationDraft(message="没有找到同时符合这些条件的菜品。")
    issues = prose_issues(draft, canonical_answer(state, draft), state)
    assert ("NO_CURRENT_MEAL_EVIDENCE" not in issues) is (status == "success")


@pytest.mark.parametrize("message", [
    "可以试试这几道菜。",
    "这些菜的配方都包含鸡胸肉。",
    "如果需要，可以再推荐两道菜。",
    "一道菜不能代表完整一餐。",
])
def test_prose_safety_does_not_parse_natural_count_phrasing(message):
    state = new_agent_state(message="推荐菜品", user_id=1)
    state["intent"] = "MEAL_RECOMMENDATION"
    draft = RecommendationDraft(message=message)
    assert not any(
        issue.startswith(("MEAL_COUNT_MISMATCH", "UNSUPPORTED_QUALITATIVE_CLAIM"))
        for issue in prose_issues(draft, canonical_answer(state, draft), state)
    )


@pytest.mark.asyncio
async def test_final_safety_filter_cannot_keep_plural_approved_prose_for_one_surviving_card():
    state = new_agent_state(message="推荐午餐", user_id=1)
    state.update(
        intent="MEAL_RECOMMENDATION", candidate_ids=[1, 2],
        dietary_constraints={"allergens": ["鸡蛋"]},
        tool_results=[{
            "tool_name": "search_meals", "status": "success", "data": {"hits": [
                {"meal_id": 1, "name": "凉拌鸡丝", "ingredients": ["鸡胸肉"], "allergens": []},
                {"meal_id": 2, "name": "炒鸡蛋", "ingredients": ["鸡蛋"], "allergens": ["鸡蛋"]},
            ]},
        }],
    )
    draft = RecommendationDraft(
        message="这两道菜都可以选。",
        recommendations=[
            {"meal_id": 1, "name": "凉拌鸡丝", "reason": ""},
            {"meal_id": 2, "name": "炒鸡蛋", "reason": ""},
        ],
    )
    canonical = canonical_answer(state, draft)
    state["final_response"] = canonical.model_dump(mode="json")
    state["approved_answer"] = approve_answer(draft, canonical)
    recorder = InMemoryTraceRecorder()

    result = await OutputGuardNode(OutputGuard(), recorder).run(state)

    assert len(result["final_response"]["recommendations"]) == 1
    assert "这两道" not in result["final_response"]["message"]
    assert "这几道" not in result["final_response"]["message"]
    assert "这些菜" not in result["final_response"]["message"]
    assert "炒鸡蛋" not in result["final_response"]["message"]
    checked = next(event for event in recorder.get_trace(state["trace_id"])
                   if event.event_type == "safety.output_checked")
    assert checked.payload["approved_prose_applied"] is False


def test_quantity_hints_allow_list_indices_and_rounded_verified_estimates():
    draft = RecommendationDraft(message="1）按整菜估算约295.4千卡。\n2）不是个人摄入量。")
    assert answer_quantity_hints(draft, {
        "source_facts": [{
            "status": "success",
            "nutrition": {"calories_kcal": 295.432},
            "source_hash": "1234567890123456789012345678901234567890",
        }],
    }) == {"unmatched_quantities": []}


def test_unmatched_quantity_is_a_hint_and_does_not_preempt_semantic_review():
    state = new_agent_state(message="怎么不柴", user_id=1)
    state["intent"] = "FOOD_QUESTION"
    draft = RecommendationDraft(message="加热到99℃就行。")
    assert answer_quantity_hints(draft, {}) == {
        "unmatched_quantities": [{"value": "99", "unit": "℃"}],
    }
    assert not prose_issues(draft, canonical_answer(state, draft), state)


def test_fractional_recipe_estimate_and_close_coarse_nutrition_rounding_are_supported():
    state = new_agent_state(message="高蛋白午餐", user_id=1)
    state["intent"] = "FOOD_QUESTION"
    state["tool_results"] = [{
        "tool_name": "search_meals", "status": "success", "data": {"hits": [{
            "meal_id": 1, "name": "肉蛋饭", "nutrition": {
                "calories_kcal": 1717.1665, "protein_g": 85.6754,
            },
        }]},
    }]
    draft = RecommendationDraft(message="假设均分后取一半配方，约860千卡、43克蛋白质。")
    evidence = {"nutrition_calculations": answer_nutrition_calculations(state)}
    assert answer_quantity_hints(draft, evidence) == {"unmatched_quantities": []}
    draft.message = "半份500千卡，煮99分钟就行。"
    assert answer_quantity_hints(draft, evidence) == {"unmatched_quantities": [
        {"value": "99", "unit": "分钟"}, {"value": "500", "unit": "kcal"},
    ]}


@pytest.mark.parametrize("context", ["general", "meals"])
def test_question_context_does_not_turn_a_numeric_hint_into_an_error(context):
    state = new_agent_state(message="鸡胸肉为什么发柴", user_id=1)
    state.update(intent="FOOD_QUESTION", answer_task={"context": context})
    draft = RecommendationDraft(message="鸡胸肉脂肪比例较低，常见约2%，加热会失水。")
    assert answer_quantity_hints(draft, {}) == {
        "unmatched_quantities": [{"value": "2", "unit": "%"}],
    }
    assert not prose_issues(draft, canonical_answer(state, draft), state)


def test_general_chicken_safety_citation_is_registered_and_traceable():
    state = new_agent_state(message="鸡肉怎么确认熟透", user_id=1)
    state.update(intent="FOOD_QUESTION", answer_task={"context": "general"})
    draft = RecommendationDraft(
        message="用食品温度计确认鸡肉中心达到约74℃。",
        source_ids=[CHICKEN_SAFETY_REFERENCE["source_id"]],
    )
    response = canonical_answer(state, draft)
    assert not prose_issues(draft, response, state)
    assert answer_quantity_hints(draft, {
        "food_safety_reference": CHICKEN_SAFETY_REFERENCE,
    }) == {"unmatched_quantities": []}
    assert response.citations[0].uri == CHICKEN_SAFETY_REFERENCE["uri"]
    assert response.citations[0].source_type == "guideline"
    assert "165°F" in response.citations[0].excerpt


def test_thanks_cannot_cite_historical_food_sources():
    state = new_agent_state(message="谢谢", user_id=1)
    state["intent"] = "CONVERSATION"
    draft = RecommendationDraft(message="不客气", source_ids=["recipe:1"])
    assert "CONVERSATION_MUST_NOT_CITE" in prose_issues(
        draft, canonical_answer(state, draft), state,
    )


@pytest.mark.parametrize("message", ["脂肪低于1%。", "加热2分钟。", "加入5克盐。"])
def test_quantities_cannot_borrow_step_numbers_history_or_another_unit(message):
    draft = RecommendationDraft(message=message)
    hints = answer_quantity_hints(draft, {
        "conversation_summary": "先前误说脂肪低于1%，加热2分钟，加入5克盐。",
        "source_facts": [{"status": "success", "data": {
            "instructions": ["1. 准备食材", "2. 搅拌"], "sodium_mg": 5,
        }}],
    })
    assert hints["unmatched_quantities"]


def test_quantity_checks_allow_source_ranges_and_rounded_structured_nutrients():
    draft = RecommendationDraft(message="煮5–7分钟，用量20毫升，整菜约45.4克蛋白质。")
    assert answer_quantity_hints(draft, {
        "source_facts": [{"status": "success", "data": {
            "instructions": ["煮5–7分钟，加入20 ml料酒"],
            "nutrient_ranges": {"protein_g": {"minimum": 45.3744, "maximum": 45.3744}},
        }}],
    }) == {"unmatched_quantities": []}


def test_percent_encoded_source_urls_do_not_support_fabricated_percentages():
    draft = RecommendationDraft(message="脂肪低于1%。")
    hints = answer_quantity_hints(draft, {
        "source_facts": [{"status": "success", "data": {
            "citation": {"uri": "https://example.test/%E1%82%89.md"},
            "instructions": "来源 https://example.test/%E1%82%89.md",
        }}],
    })
    assert hints == {"unmatched_quantities": [{"value": "1", "unit": "%"}]}


@pytest.mark.parametrize(("source", "message", "unmatched"), [
    ("蒸60分钟", "蒸1小时。", []),
    ("豆腐300克", "豆腐0.3千克。", []),
    ("盐500毫克", "盐0.5克。", []),
    ("加水1升", "加水1000毫升。", []),
    ("厚1厘米", "厚10毫米。", []),
    ("豆腐300克", "豆腐1千克。", [{"value": "1", "unit": "kg"}]),
    ("豆腐1盒", "豆腐约300克。", [{"value": "300", "unit": "g"}]),
    ("豆腐1盒", "原文没说豆腐是300克。", [{"value": "300", "unit": "g"}]),
    ("煮5–7分钟", "煮5–7分钟。", []),
    ("煮5–7分钟", "煮6分钟。", [{"value": "6", "unit": "分钟"}]),
])
def test_quantity_hints_normalize_exact_units_without_inferring_weights_or_range_midpoints(
    source, message, unmatched,
):
    assert answer_quantity_hints(RecommendationDraft(message=message), {
        "source_facts": [{"status": "success", "data": {"instructions": source}}],
    }) == {"unmatched_quantities": unmatched}


@pytest.mark.parametrize(("source", "message"), [
    ("豆腐300克，葱10克", "使用葱300克。"),
    ({"fat_g": 10, "protein_g": 30}, "脂肪30克。"),
    ("豆腐1盒", "豆腐一盒，约三百克。"),
])
def test_empty_quantity_hints_do_not_prove_attribution_or_cover_chinese_numerals(source, message):
    assert answer_quantity_hints(RecommendationDraft(message=message), {
        "source_facts": [{"status": "success", "data": source}],
    }) == {"unmatched_quantities": []}


def test_numeric_constraints_failed_results_and_unrelated_state_are_not_recipe_facts():
    draft = RecommendationDraft(message="这道菜600千卡，用300克豆腐，冷藏10分钟。")
    assert answer_quantity_hints(draft, {
        "slots": {"max_calories_kcal": 600},
        "tool_results": [{"status": "success", "data": {"instructions": "豆腐300克"}}],
        "answer_to_revise": {"message": "冷藏10分钟"},
        "source_facts": [
            {"status": "success", "data": {"calories_kcal": 500}},
            {"status": "error", "data": {"instructions": "豆腐300克，冷藏10分钟"}},
            {"status": "success", "tool_name": "get_user_profile", "data": {
                "preferences": "冷藏10分钟", "daily_calorie_target": 600,
            }},
        ],
    }) == {"unmatched_quantities": [
        {"value": "10", "unit": "分钟"}, {"value": "300", "unit": "g"},
        {"value": "600", "unit": "kcal"},
    ]}


@pytest.mark.parametrize(("data", "message"), [
    ({"raw_quantities": "豆腐0.3千克"}, "豆腐1千克。"),
    ({"raw_quantities": "盐2.4克"}, "盐2克。"),
    ({"prep_minutes": 5.5}, "加热6分钟。"),
    ({"serving_weight_g": 295.4}, "成品295克。"),
])
def test_nutrient_rounding_does_not_extend_to_weights_or_cooking_times(data, message):
    assert answer_quantity_hints(RecommendationDraft(message=message), {
        "source_facts": [{"status": "success", "data": data}],
    })["unmatched_quantities"]


def test_quantity_hints_include_card_reasons_and_ignore_nonfinite_source_values():
    draft = RecommendationDraft(
        message="可以选测试菜。",
        recommendations=[{"meal_id": 1, "name": "测试菜", "reason": "含99克蛋白质。"}],
    )
    assert answer_quantity_hints(draft, {"candidate_nutrition": [{
        "nutrition": {"protein_g": float("nan"), "fat_g": float("inf")},
    }]}) == {"unmatched_quantities": [{"value": "99", "unit": "g"}]}


@pytest.mark.parametrize("message", ["这道菜不含常见过敏原。", "没有任何过敏原。"])
def test_empty_food_allergen_list_cannot_support_absolute_absence_claims(message):
    state = new_agent_state(message="比较两道菜", user_id=1)
    state["intent"] = "FOOD_QUESTION"
    draft = RecommendationDraft(message=message)
    assert any(issue.startswith("ALLERGEN_ABSENCE_NOT_VERIFIED") for issue in prose_issues(
        draft, canonical_answer(state, draft), state,
    ))


@pytest.mark.parametrize("message", [
    "口感清爽，暖胃易消化。",
    "营养搭配均衡，是最适合你的选择，也是绝对主角。",
    "制作简便，优先推荐这道。",
])
def test_subjective_meal_phrasing_is_left_to_semantic_review(message):
    state = new_agent_state(message="为什么推荐第一道？", user_id=1)
    state["intent"] = "FOOD_QUESTION"
    draft = RecommendationDraft(message=message)
    assert "UNSUPPORTED_QUALITATIVE_CLAIM" not in prose_issues(
        draft, canonical_answer(state, draft), state,
    )


def test_comparison_fallback_uses_ordered_recipe_source_facts():
    state = new_agent_state(message="前两个有什么区别？", user_id=1)
    state.update(
        intent="FOOD_QUESTION",
        discussed_meals=[
            {"meal_id": 1001, "name": "西红柿炒鸡蛋"},
            {"meal_id": 1014, "name": "炒青菜"},
        ],
        tool_results=[
            {"tool_name": "get_recipe", "status": "success", "data": {
                "meal_id": 1001, "name": "西红柿炒鸡蛋",
                "raw_ingredients": "* 西红柿\n* 鸡蛋",
                "citation": {"source_id": "recipe:1001", "title": "西红柿炒鸡蛋"},
            }},
            {"tool_name": "get_recipe", "status": "success", "data": {
                "meal_id": 1014, "name": "炒青菜",
                "raw_ingredients": "- 青菜",
                "citation": {"source_id": "recipe:1014", "title": "炒青菜"},
            }},
        ],
    )

    response = canonical_answer(state, RecommendationDraft(message="不可靠的原稿"))

    assert "西红柿炒鸡蛋的收录配方食材：* 西红柿\n* 鸡蛋" in response.message
    assert "炒青菜的收录配方食材：- 青菜" in response.message
    assert "不能据此判断口味或营养高低" in response.message
    assert {citation.source_id for citation in response.citations} == {
        "recipe:1001", "recipe:1014",
    }
    assert "不可靠的原稿" not in response.message


@pytest.mark.parametrize("message", [
    "前两个的热量有什么区别？",
    "前两个做法有什么区别？",
    "前两个口味有什么不同？",
])
def test_comparison_shortcut_does_not_override_an_explicit_dimension(message):
    state = new_agent_state(message=message, user_id=1)
    state.update(
        intent="FOOD_QUESTION",
        discussed_meals=[
            {"meal_id": 1001, "name": "西红柿炒鸡蛋"},
            {"meal_id": 1014, "name": "炒青菜"},
        ],
        tool_results=[{
            "tool_name": "get_recipe", "status": "success", "data": {
                "meal_id": 1001, "name": "西红柿炒鸡蛋",
                "raw_ingredients": "* 西红柿\n* 鸡蛋",
            },
        }, {
            "tool_name": "get_recipe", "status": "success", "data": {
                "meal_id": 1014, "name": "炒青菜", "raw_ingredients": "- 青菜",
            },
        }],
    )

    assert canonical_answer(state, RecommendationDraft(message="待生成")) is not None


def test_comparison_fallback_keeps_the_question_subject_when_recipe_data_is_missing():
    state = new_agent_state(message="前两个有什么区别？", user_id=1)
    state.update(
        intent="FOOD_QUESTION",
        discussed_meals=[
            {"meal_id": 1, "name": "甲菜"},
            {"meal_id": 2, "name": "乙菜"},
        ],
    )

    response = canonical_answer(state, RecommendationDraft(message="不可靠的原稿"))

    assert "甲菜和乙菜" in response.message
    assert "资料不足" in response.message
    assert "不可靠的原稿" not in response.message


def test_why_recommendation_fallback_uses_the_verified_ingredient_match_not_rank():
    state = new_agent_state(message="为什么推荐第一道？", user_id=1)
    state.update(
        intent="FOOD_QUESTION",
        slots={"required_ingredients": ["鸡胸肉"]},
        discussed_meals=[
            {"meal_id": 1034, "name": "凉拌鸡丝"},
            {"meal_id": 1067, "name": "蛋包饭"},
        ],
        conversation_evidence=[{
            "tool_name": "search_meals", "status": "success", "data": {"hits": [{
                "meal_id": 1034, "name": "凉拌鸡丝",
                "ingredients": ["鸡胸肉", "食盐"],
                "citation": {"source_id": "meal:1034", "title": "凉拌鸡丝"},
            }]},
        }],
    )

    response = canonical_answer(state, RecommendationDraft(message="最适合你的选择"))

    assert "收录配方包含鸡胸肉" in response.message
    assert "符合你提出的要求" in response.message
    assert "展示顺序不代表排名" in response.message
    assert "最适合你的选择" not in response.message
    assert [citation.source_id for citation in response.citations] == ["meal:1034"]
