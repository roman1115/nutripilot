from __future__ import annotations

import pytest
from pydantic import ValidationError

from nutripilot.agent.answers import (
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
from nutripilot.tracing import InMemoryTraceRecorder


def test_review_bool_cannot_be_a_truthy_string():
    with pytest.raises(ValidationError):
        AnswerReview.model_validate({"supported": "false", "issues": []})


@pytest.mark.parametrize("message", [
    "你对鱼过敏，所以推荐这个。",
    "你患有糖尿病，所以推荐这个。",
    "只要99999分钟就做好。",
])
def test_unfounded_personal_claims_and_numbers_are_rejected_before_model_review(message):
    state = new_agent_state(message="解释一下", user_id=1)
    state["intent"] = "FOOD_QUESTION"
    draft = RecommendationDraft(message=message)
    assert prose_issues(draft, canonical_answer(state, draft), state, {})


def test_unknown_sources_and_unrequested_cards_are_rejected():
    state = new_agent_state(message="谢谢", user_id=1)
    state["intent"] = "CONVERSATION"
    draft = RecommendationDraft(
        message="推荐假的", source_ids=["invented"],
        recommendations=[{"meal_id": 999, "name": "假的", "reason": ""}],
    )
    issues = prose_issues(draft, canonical_answer(state, draft), state, {})
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
    issues = prose_issues(
        draft, canonical_answer(state, draft), state,
        {"tool_results": state["tool_results"], "slots": state["slots"]},
    )
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
        for issue in prose_issues(draft, canonical_answer(state, draft), state, {})
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


def test_number_guard_allows_list_indices_and_rounded_verified_estimates():
    state = new_agent_state(message="解释营养", user_id=1)
    state["intent"] = "FOOD_QUESTION"
    draft = RecommendationDraft(message="1）按整菜估算约295.4千卡。\n2）不是个人摄入量。")
    assert not prose_issues(draft, canonical_answer(state, draft), state, {
        "tool_results": [{
            "nutrition": {"calories_kcal": 295.432},
            "source_hash": "1234567890123456789012345678901234567890",
        }],
    })


def test_number_feedback_identifies_the_unsupported_quantity_for_correction():
    state = new_agent_state(message="怎么不柴", user_id=1)
    state["intent"] = "FOOD_QUESTION"
    draft = RecommendationDraft(message="加热到99℃就行。")
    issues = prose_issues(draft, canonical_answer(state, draft), state, {})
    assert any("UNSUPPORTED_NUMBER: 99" in issue for issue in issues)


def test_thanks_cannot_cite_historical_food_sources():
    state = new_agent_state(message="谢谢", user_id=1)
    state["intent"] = "CONVERSATION"
    draft = RecommendationDraft(message="不客气", source_ids=["recipe:1"])
    assert "CONVERSATION_MUST_NOT_CITE" in prose_issues(
        draft, canonical_answer(state, draft), state, {},
    )


@pytest.mark.parametrize("message", ["脂肪低于1%。", "加热2分钟。", "加入5克盐。"])
def test_quantities_cannot_borrow_step_numbers_history_or_another_unit(message):
    state = new_agent_state(message="解释", user_id=1)
    state["intent"] = "FOOD_QUESTION"
    draft = RecommendationDraft(message=message)
    issues = prose_issues(draft, canonical_answer(state, draft), state, {
        "conversation_summary": "先前误说脂肪低于1%，加热2分钟，加入5克盐。",
        "tool_results": [{"data": {
            "instructions": ["1. 准备食材", "2. 搅拌"], "sodium_mg": 5,
        }}],
    })
    assert any(issue.startswith("UNSUPPORTED_NUMBER") for issue in issues)


def test_quantity_checks_allow_source_ranges_and_rounded_structured_nutrients():
    state = new_agent_state(message="做法", user_id=1)
    state["intent"] = "RECIPE_QUERY"
    draft = RecommendationDraft(message="煮5–7分钟，用量20毫升，整菜约45.4克蛋白质。")
    assert not prose_issues(draft, canonical_answer(state, draft), state, {
        "tool_results": [{"data": {
            "instructions": ["煮5–7分钟，加入20 ml料酒"],
            "nutrient_ranges": {"protein_g": {"minimum": 45.3744, "maximum": 45.3744}},
        }}],
    })


def test_percent_encoded_source_urls_do_not_support_fabricated_percentages():
    state = new_agent_state(message="为什么柴", user_id=1)
    state["intent"] = "FOOD_QUESTION"
    draft = RecommendationDraft(message="脂肪低于1%。")
    issues = prose_issues(draft, canonical_answer(state, draft), state, {
        "conversation_evidence": [{"data": {
            "citation": {"uri": "https://example.test/%E1%82%89.md"},
            "instructions": "来源 https://example.test/%E1%82%89.md",
        }}],
    })
    assert any(issue.startswith("UNSUPPORTED_NUMBER: 1 %") for issue in issues)


@pytest.mark.parametrize("message", ["这道菜不含常见过敏原。", "没有任何过敏原。"])
def test_empty_food_allergen_list_cannot_support_absolute_absence_claims(message):
    state = new_agent_state(message="比较两道菜", user_id=1)
    state["intent"] = "FOOD_QUESTION"
    draft = RecommendationDraft(message=message)
    assert any(issue.startswith("ALLERGEN_ABSENCE_NOT_VERIFIED") for issue in prose_issues(
        draft, canonical_answer(state, draft), state, {"tool_results": [{"allergens": []}]},
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
        draft, canonical_answer(state, draft), state, {},
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
