from __future__ import annotations

import pytest
from pydantic import ValidationError

from nutripilot.agent.answers import (
    answer_review_issues,
    answer_review_records,
    answer_review_units,
)
from nutripilot.agent.contracts import (
    AnswerConditionReview,
    AnswerReview,
    RecommendationDraft,
    contains_evidence_quote,
)
from nutripilot.agent.state import new_agent_state


def review_for(
    units: dict[str, str], *, kind: str = "general_advice", evidence: list | None = None,
) -> AnswerReview:
    return AnswerReview.model_validate({
        "supported": True, "issues": [], "coherent": True,
        "checks": [
            {
                "unit_id": key, "kind": kind, "supported": True,
                "has_object_count": False, "object_counts": [],
                "conditions_preserved": True, "conditions": [], "evidence": evidence or [],
                "explanation": "已核对整段的对象、依据及适用条件。",
            } for key in units
        ],
    })


def test_review_units_cover_prose_and_every_nonempty_card_reason():
    draft = RecommendationDraft(
        message="可以选凉拌鸡丝。\n\n配方包含鸡胸肉。",
        recommendations=[{"meal_id": 1034, "name": "凉拌鸡丝", "reason": "包含想吃的食材。"}],
    )
    units = answer_review_units(draft)
    assert list(units) == ["message:0", "message:1", "reason:0"]
    review = review_for(units)
    review.checks.pop()
    assert any("INCOMPLETE_REVIEW" in issue for issue in answer_review_issues(review, units, {}))
    review = review_for(units)
    review.checks.append(review.checks[0])
    assert any("INCOMPLETE_REVIEW" in issue for issue in answer_review_issues(review, units, {}))


def test_recommendation_review_separates_each_dish_method_from_shared_ingredient_claim():
    draft = RecommendationDraft(
        message=(
            "三道菜都含鸡胸肉：凉拌鸡丝含鸡胸肉；汤面把鸡胸肉切片入汤；"
            "蛋包饭把鸡胸肉裹入蛋皮。"
        ),
        recommendations=[{"meal_id": 1034, "name": "凉拌鸡丝"}],
    )
    units = answer_review_units(draft)
    assert list(units.values()) == [
        "三道菜都含鸡胸肉：凉拌鸡丝含鸡胸肉；", "汤面把鸡胸肉切片入汤；",
        "蛋包饭把鸡胸肉裹入蛋皮。",
    ]
    review = review_for(units)
    review.checks[1].supported = False
    review.checks[1].explanation = "当前只有食材资料，没有切片入汤的配方原文。"
    assert any("切片入汤" in issue for issue in answer_review_issues(review, units, {}))


def test_question_review_keeps_conditions_with_their_conclusion_and_splits_sentences():
    draft = RecommendationDraft(message="如果已经熟透，可以撕细拌酱。避免反复长时间加热。")
    assert list(answer_review_units(draft).values()) == [
        "如果已经熟透，可以撕细拌酱。", "避免反复长时间加热。",
    ]


def test_valid_review_is_not_rejected_only_for_a_long_explanation():
    units = {"message:0": "可以撕细拌酱。"}
    data = review_for(units).model_dump()
    data["checks"][0]["explanation"] = "这是通用烹饪建议，不保证恢复流失的水分。" * 30
    review = AnswerReview.model_validate(
        data, context={"payload": {"review_units": units, "evidence_records": {}}},
    )
    assert not answer_review_issues(review, units, {})


def test_compact_json_excerpt_is_verified_as_fields_without_inventing_values_or_objects():
    record = (
        'data.hits[0].name: 菜1\n'
        'data.hits[0].nutrition: '
        '{"fat_g":25.8,"protein_g":30.1,"serving_size":"整道菜","bands":[1]}'
    )
    assert contains_evidence_quote(
        record, 'data.hits[0].nutrition: {"protein_g":30.1,"fat_g":25.8}',
    )
    for quote in (
        'data.hits[1].nutrition: {"protein_g":30.1}',
        'data.hits[0].nutrition: {"protein_g":30.2}',
        'data.hits[0].nutrition: {"servings":1}',
        'data.hits[0].nutrition: {}',
        'data.hits[0].nutrition: {"protein_g":true}',
        'data.hits[0].nutrition: {"bands":[true]}',
    ):
        assert not contains_evidence_quote(record, quote)


def test_rejected_review_keeps_feedback_without_spending_retries_repairing_its_citations():
    units = {"message:0": "只需5分钟就一定熟了。"}
    data = review_for(units, kind="source_fact", evidence=[{
        "record_id": "invented", "quote": "不存在",
    }]).model_dump()
    data["checks"][0]["supported"] = False
    data["checks"][0]["explanation"] = "来源要求未熟延长，不能保证到时即熟。"
    data["supported"] = False
    review = AnswerReview.model_validate(
        data, context={"payload": {"review_units": units, "evidence_records": {}}},
    )
    issues = answer_review_issues(review, units, {})
    assert any("不能保证" in issue for issue in issues)
    assert any("INVALID_EVIDENCE_REFERENCE" in issue for issue in issues)


def test_source_condition_in_following_clause_is_preserved_for_the_whole_answer():
    units = {"message:0": "时间视大小调整，", "message:1": "未熟应继续煮。"}
    review = review_for(units)
    review.checks[0].conditions = [AnswerConditionReview.model_validate({
        "source": {"record_id": "current:0", "quote": "未熟延长"},
        "answer_quote": "未熟应继续煮。",
    })]
    assert not answer_review_issues(review, units, {"current:0": "instructions: 未熟延长"})


@pytest.mark.parametrize(("kind", "text"), [
    ("ranking", "第一道被优先推荐，最大程度保留嫩滑口感。"),
    ("health_effect", "两者搭配能延缓餐后血糖波动。"),
    ("source_fact", "这道菜只需要简单调味，制作快捷。"),
])
def test_positive_reviewer_vote_cannot_publish_a_factual_claim_without_evidence(kind, text):
    units = answer_review_units(RecommendationDraft(message=text))
    issues = answer_review_issues(review_for(units, kind=kind), units, {})
    assert all(any(
        "MISSING_CLAIM_EVIDENCE" in issue and unit in issue for issue in issues
    ) for unit in units.values())


@pytest.mark.parametrize("evidence", [
    [{"record_id": "invented", "quote": "鸡胸肉"}],
    [{"record_id": "current:0", "quote": "并不存在的步骤"}],
    [{"record_id": "current:0", "quote": " "}],
])
def test_fabricated_or_blank_evidence_cannot_pass(evidence):
    units = {"message:0": "配方包含鸡胸肉。"}
    records = {"current:0": "data.raw_ingredients: 鸡胸肉、姜、料酒"}
    assert any("INVALID_EVIDENCE_REFERENCE" in issue for issue in answer_review_issues(
        review_for(units, kind="source_fact", evidence=evidence), units, records,
    ))


def test_true_quote_does_not_override_reviewer_rejection_of_a_missing_condition():
    units = {"message:0": "非冷冻鸡胸肉煮5分钟即关火。"}
    records = {
        "history:0": "data.instructions: 非冷冻肉煮5分钟；大小影响成熟时间，如果不熟需延长。",
    }
    review = review_for(units, kind="source_fact", evidence=[
        {"record_id": "history:0", "quote": "非冷冻肉煮5分钟"},
    ])
    review.checks[0].conditions_preserved = False
    review.checks[0].explanation = "遗漏大小和成熟情况的条件，不能保证到时即熟。"
    issues = answer_review_issues(review, units, records)
    assert any("遗漏大小和成熟情况" in issue for issue in issues)


def test_supported_paraphrase_keeps_qualifiers_and_real_evidence():
    units = {"message:0": "配方的煮制时间供参考，要视鸡肉大小和成熟情况调整，未熟需继续煮。"}
    records = {"current:0": "data.instructions: 鸡胸肉大小会影响成熟时间。如果不熟需延长煮制时间。"}
    review = review_for(units, kind="source_fact", evidence=[{
        "record_id": "current:0", "quote": "鸡胸肉大小会影响成熟时间。如果不熟需延长煮制时间。",
    }])
    assert not answer_review_issues(review, units, records)


@pytest.mark.parametrize("answer_quote", ["", "根据大小调整，未熟需延长。"])
def test_source_condition_must_actually_appear_in_the_published_unit(answer_quote):
    units = {"message:0": "煮5分钟就熟了。"}
    records = {"current:0": "instructions: 大小影响时间，未熟需延长。"}
    review = review_for(units, kind="source_fact", evidence=[
        {"record_id": "current:0", "quote": "大小影响时间"},
    ])
    review.checks[0].conditions = [AnswerConditionReview.model_validate({
        "source": {"record_id": "current:0", "quote": "未熟需延长"},
        "answer_quote": answer_quote,
    })]
    assert any("MISSING_ANSWER_CONDITION" in issue for issue in answer_review_issues(
        review, units, records,
    ))


def test_condition_paraphrase_is_allowed_but_source_quote_cannot_be_fabricated():
    units = {"message:0": "时间要视大小调整，没有熟透就继续煮。"}
    records = {"current:0": "instructions: 大小影响时间，未熟需延长。"}
    review = review_for(units)
    data = review.model_dump()
    data["checks"][0]["conditions"] = [{
        "source": {"record_id": "current:0", "quote": "大小影响时间，未熟需延长"},
        "answer_quote": units["message:0"],
    }]
    assert not answer_review_issues(AnswerReview.model_validate(data), units, records)
    data["checks"][0]["conditions"][0]["source"]["quote"] = "未有的来源条件"
    assert any("INVALID_EVIDENCE_REFERENCE" in issue for issue in answer_review_issues(
        AnswerReview.model_validate(data), units, records,
    ))


def test_object_mismatch_is_rejected_even_with_valid_ingredient_evidence():
    units = {"message:0": "这两道菜含鸡胸肉：凉拌鸡丝。"}
    review = review_for(units)
    data = review.model_dump()
    data["checks"][0]["has_object_count"] = True
    data["checks"][0]["object_counts"] = [{
        "quote": "这两道菜", "scope": "recommendations", "minimum": 2, "maximum": 2,
    }]
    assert any("OBJECT_COUNT_MISMATCH" in issue for issue in answer_review_issues(
        AnswerReview.model_validate(data), units, {}, {"recommendations": [1034]},
    ))


@pytest.mark.parametrize(("quote", "minimum", "maximum", "actual", "expected"), [
    ("这三道菜", 3, 3, 3, True),
    ("这道菜", 1, 1, 1, True),
    ("这几道菜", 2, None, 1, False),
    ("这几道菜", 2, None, 3, True),
    ("这些菜都", 2, None, 0, False),
    ("没有菜", 0, 0, 0, True),
    ("这两道菜", 3, 2, 2, False),
])
def test_object_count_is_compared_to_final_facts_without_a_text_rule(
    quote, minimum, maximum, actual, expected,
):
    units = {"message:0": quote}
    data = review_for(units).model_dump()
    data["checks"][0]["has_object_count"] = True
    data["checks"][0]["object_counts"] = [{
        "quote": quote, "scope": "recommendations", "minimum": minimum, "maximum": maximum,
    }]
    assert (not answer_review_issues(
        AnswerReview.model_validate(data), units, {}, {"recommendations": list(range(actual))},
    )) is expected


def test_object_count_quote_and_scope_must_be_verifiable():
    units = {"message:0": "这三道菜"}
    data = review_for(units).model_dump()
    data["checks"][0]["has_object_count"] = True
    data["checks"][0]["object_counts"] = [{
        "quote": "这两道菜", "scope": "display", "minimum": 2, "maximum": 2,
    }]
    review = AnswerReview.model_validate(data)
    assert any("INVALID_OBJECT_COUNT" in issue for issue in answer_review_issues(
        review, units, {}, {"display": [1034, 1067, 1061]},
    ))
    review.checks[0].object_counts[0].quote = "这三道菜"
    assert any("INVALID_OBJECT_COUNT" in issue for issue in answer_review_issues(
        review, units, {},
    ))


def test_singular_reference_to_one_displayed_meal_is_not_a_claim_about_total_cards():
    units = {"message:0": "这道菜的配方包含鸡胸肉。"}
    data = review_for(units).model_dump()
    data["checks"][0]["has_object_count"] = True
    data["checks"][0]["object_counts"] = [{
        "quote": "这道菜", "scope": "display", "meal_ids": [1034],
        "minimum": 1, "maximum": 1,
    }]
    review = AnswerReview.model_validate(data)
    objects = {"display": [1034, 1067, 1061]}
    assert not answer_review_issues(review, units, {}, objects)
    review.checks[0].object_counts[0].meal_ids = [9999]
    assert any("INVALID_OBJECT_COUNT" in issue for issue in answer_review_issues(
        review, units, {}, objects,
    ))


def test_count_claim_cannot_repeat_an_id_to_inflate_the_number():
    units = {"message:0": "这两道菜都含鸡胸肉。"}
    data = review_for(units).model_dump()
    data["checks"][0]["has_object_count"] = True
    data["checks"][0]["object_counts"] = [{
        "quote": "这两道菜都", "scope": "display", "meal_ids": [1034, 1034],
        "minimum": 2, "maximum": 2,
    }]
    assert any("INVALID_OBJECT_COUNT" in issue for issue in answer_review_issues(
        AnswerReview.model_validate(data), units, {}, {"display": [1034, 1067, 1061]},
    ))


def test_exact_field_text_is_valid_but_reconstructed_json_is_not():
    units = {"message:0": "配方含鸡胸肉。"}
    records = {"request": "slots.required_ingredients[0]: 鸡胸肉\nslots.meal_type: lunch"}
    review = review_for(units, kind="source_fact", evidence=[{
        "record_id": "request", "quote": "slots.required_ingredients[0]: 鸡胸肉",
    }])
    assert not answer_review_issues(review, units, records)
    review.checks[0].evidence[0].quote = '"required_ingredients": ["鸡胸肉"], ...'
    assert any("INVALID_EVIDENCE_REFERENCE" in issue for issue in answer_review_issues(
        review, units, records,
    ))


def test_multiline_excerpts_must_all_exist_in_source_order():
    units = {"message:0": "配方含鸡胸肉。"}
    records = {"current:0": "id: 1034\nname: 凉拌鸡丝\nactive: true\ningredients: 鸡胸肉"}
    review = review_for(units, kind="source_fact", evidence=[{
        "record_id": "current:0", "quote": "name: 凉拌鸡丝\ningredients: 鸡胸肉",
    }])
    assert not answer_review_issues(review, units, records)
    for quote in (
        "name: 凉拌鸡丝\ningredients: 牛肉",
        "ingredients: 鸡胸肉\nname: 凉拌鸡丝",
        "name: 凉拌鸡丝 ingredients: 鸡胸肉",
    ):
        review.checks[0].evidence[0].quote = quote
        assert any("INVALID_EVIDENCE_REFERENCE" in issue for issue in answer_review_issues(
            review, units, records,
        ))


def test_compact_json_supports_exact_source_paths_but_not_invented_fields_or_values():
    record = (
        'data.hits[0]: {"name":"凉拌鸡丝","ingredients":["鸡胸肉"],'
        '"nutrition":{"protein_g":45.3}}'
    )
    assert contains_evidence_quote(record, "凉拌鸡丝")
    assert contains_evidence_quote(record, "data.hits[0].ingredients[0]: 鸡胸肉")
    assert contains_evidence_quote(record, "data.hits[0].nutrition.protein_g: 45.3")
    assert not contains_evidence_quote(record, "data.hits[0].ingredients[0]: 牛肉")
    assert not contains_evidence_quote(record, "data.hits[1].name: 凉拌鸡丝")
    assert not contains_evidence_quote(record, "data.hits[0].nutrition.fat_g: 45.3")


def test_empty_answer_cannot_pass_with_an_empty_review():
    assert any("INCOMPLETE_REVIEW" in issue for issue in answer_review_issues(
        review_for({}), {}, {},
    ))


def test_general_advice_and_conversation_do_not_require_recipe_evidence():
    for kind, text in [
        ("general_advice", "已经熟透但发柴，可以撕细拌酱改善入口感受，不能恢复流失的水分。"),
        ("conversation", "不客气。"),
    ]:
        units = {"message:0": text}
        assert not answer_review_issues(review_for(units, kind=kind), units, {})


def test_quantity_assessment_cannot_omit_extracted_counts():
    data = review_for({"message:0": "这几道菜"}).model_dump()
    data["checks"][0]["has_object_count"] = True
    with pytest.raises(ValidationError, match="数量判断"):
        AnswerReview.model_validate(data)


def test_cross_paragraph_contradiction_blocks_positive_unit_votes():
    units = {"message:0": "可以蒸一下。", "message:1": "别再加热。"}
    review = review_for(units)
    review.coherent = False
    assert any("INCONSISTENT_ANSWER" in issue for issue in answer_review_issues(review, units, {}))


def test_review_contract_checks_coverage_and_quotes_with_gateway_payload():
    units = {"message:0": "需要确认你说的是哪道菜。"}
    data = review_for(units, kind="conversation").model_dump()
    payload = {"review_units": units, "evidence_records": {"display": "meals: []"}}
    assert AnswerReview.model_validate(data, context={"payload": payload}).supported
    data["checks"][0]["evidence"] = [{"record_id": "invented", "quote": "不存在"}]
    with pytest.raises(ValidationError, match="引用不存在"):
        AnswerReview.model_validate(data, context={"payload": payload})
    data["checks"] = []
    with pytest.raises(ValidationError, match="恰好覆盖"):
        AnswerReview.model_validate(data, context={"payload": payload})


def test_verified_facts_are_registered_but_never_include_unreviewed_prose():
    from nutripilot.domain.models import FinalResponse

    state = new_agent_state(user_id=1, message="那道菜是什么")
    records = answer_review_records(
        state, RecommendationDraft(message="未审核原稿"),
        FinalResponse(message="未审核兜底"),
    )
    assert "recommendations: []" in records["verified_facts"]
    assert "未审核" not in records["verified_facts"]


def test_review_evidence_keeps_source_conditions_but_not_historical_model_claims():
    state = new_agent_state(message="为什么推荐第一道", user_id=1)
    state["conversation_summary"] = "旧模型声称搭配能延缓血糖波动。"
    state["discussed_meals"] = [
        {"meal_id": 1034, "name": "凉拌鸡丝", "reason": "曾声称优先推荐"},
    ]
    state["conversation_evidence"] = [
        {"tool_name": "get_recipe", "status": "success", "data": {
            "instructions": "时间仅供参考；鸡胸肉大小影响成熟时间，不熟需延长。",
        }},
        {"tool_name": "get_recipe", "status": "error", "data": {"instructions": "失败数据"}},
    ]
    records = answer_review_records(state)
    assert "data.instructions: 时间仅供参考；鸡胸肉大小影响成熟时间，不熟需延长。" in (
        records["history:0"]
    )
    assert "history:1" not in records
    assert "meals[0].name: 凉拌鸡丝" in records["display"]
    assert "reason" not in records["display"]
    assert "conversation_summary" not in records


def test_review_records_preserve_nested_identity_numbers_and_multiline_original_text():
    state = new_agent_state(message="煮多久", user_id=1)
    state["tool_results"] = [{
        "tool_name": "get_recipe", "status": "success", "data": {
            "meal_id": 1034, "instructions": "第一行\n大小影响时间，未熟需延长。",
            "optional": None, "tags": [], "confirmed": False,
        },
    }]
    text = answer_review_records(state)["current:0"]
    assert "data.meal_id: 1034" in text
    assert "data.instructions: 第一行\n大小影响时间，未熟需延长。" in text
    assert "data.optional: null" in text
    assert "data.tags: []" in text
    assert "data.confirmed: false" in text


def test_review_keeps_nutrition_when_the_answer_introduces_a_nutrition_claim():
    state = new_agent_state(message="想吃鸡胸肉", user_id=1)
    state["tool_results"] = [{
        "tool_name": "search_meals", "status": "success", "data": {"hits": [{
            "meal_id": 1034, "name": "凉拌鸡丝", "ingredients": ["鸡胸肉"],
            "description": "来源通用说明。", "is_active": True, "recipe_id": 1034,
            "allergens": ["大豆"], "nutrition": {"protein_g": 45.3},
        }]},
    }]
    assert "nutrition" not in answer_review_records(state)["current:0"]
    records = answer_review_records(state, RecommendationDraft(message="凉拌鸡丝蛋白质45.3克。"))
    assert '"protein_g":45.3' in records["current:0"]
    assert 'allergens: ["大豆"]' in records["current:0"]
    assert "description" not in records["current:0"]
