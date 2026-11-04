from __future__ import annotations

import pytest

from nutripilot.agent.planning import candidate_meals_from_results
from nutripilot.application import build_container
from nutripilot.config import Settings
from nutripilot.model_gateway import FakeModelGateway
from nutripilot.model_gateway.budget import ExecutionLimitExceeded


@pytest.fixture
def container():
    return build_container(settings=Settings(_env_file=None))


@pytest.mark.asyncio
async def test_verified_natural_prose_and_reasons_survive_the_output_boundary(container):
    gateway = container.gateway
    assert isinstance(gateway, FakeModelGateway)
    text = "你这次想吃鸡胸肉，我会先考虑凉拌鸡丝。\n\n它的收录配方用到了鸡胸肉。"
    reason = "这份配方包含鸡胸肉，正好回应你这次想吃的食材。"
    gateway.queue_response("recommendation", {"response": {
        "message": text,
        "recommendations": [{"meal_id": 1034, "name": "凉拌鸡丝", "reason": reason}],
    }})
    state = await container.chat_service.chat(user_id=1, message="想吃鸡胸肉")
    assert state["final_response"]["message"] == text
    card = state["final_response"]["recommendations"][0]
    assert card["reason"] == reason
    assert card["calories_kcal"] is not None
    source = next(meal for meal in candidate_meals_from_results(state["tool_results"])
                  if (meal.get("meal_id") or meal.get("id")) == card["meal_id"])
    for field in ("protein_g", "carbohydrates_g", "fat_g"):
        assert card[field] == source["nutrition"][field]
    assert state["final_response"]["citations"]
    assert state["approved_answer"] is not None
    assert any(
        event.event_type == "safety.output_checked"
        and event.payload["approved_prose_applied"]
        for event in container.recorder.get_trace(state["trace_id"])
    )


@pytest.mark.asyncio
async def test_grouped_explanation_can_select_fewer_dishes_without_a_template(container):
    text = "凉拌鸡丝和蛋包饭的收录配方都用了鸡胸肉，可以从这两道里选。"
    container.gateway.queue_response("recommendation", {"response": {
        "message": text,
        "recommendations": [
            {"meal_id": 1034, "name": "凉拌鸡丝", "reason": "配方包含鸡胸肉。"},
            {"meal_id": 1067, "name": "蛋包饭", "reason": "配方包含鸡胸肉。"},
        ],
    }})
    state = await container.chat_service.chat(
        user_id=1, thread_id="grouped-answer", message="想吃鸡胸肉",
    )
    assert state["final_response"]["message"] == text
    assert len(state["final_response"]["recommendations"]) == 2
    assert state["approved_answer"]
    assert not state["fallback_events"]
    payload = next(
        call["payload"] for call in container.gateway.call_history
        if call["worker"] == "recommendation"
    )
    assert "verified_response" not in payload
    assert "verified_facts" not in payload
    assert "tool_results" in payload
    assert not any(
        result["tool_name"] == "get_recipe" for result in state["tool_results"]
    )


@pytest.mark.asyncio
async def test_semantic_review_repairs_count_mismatch_without_a_count_regex(container):
    gateway = container.gateway
    invalid = {
        "message": "可以试试这几道菜，这些菜的收录配方都包含鸡胸肉。",
        "recommendations": [
            {"meal_id": 1034, "name": "凉拌鸡丝", "reason": "配方包含鸡胸肉。"},
        ],
    }
    corrected = {
        **invalid,
        "message": "可以选凉拌鸡丝，这道菜的收录配方包含鸡胸肉。",
    }
    gateway.queue_response("recommendation", {"response": invalid})
    gateway.queue_response("answer_review", {
        "supported": False, "issues": ["正文说有几道菜，但实际只选了一道。"],
        "checks": [],
    })
    gateway.queue_response("recommendation", {"response": corrected})
    state = await container.chat_service.chat(
        user_id=1, message="想吃鸡胸肉，整道菜不超过600千卡",
    )

    response = state["final_response"]
    assert len(response["recommendations"]) == 1
    assert response["recommendations"][0]["name"] == "凉拌鸡丝"
    assert "这几道" not in response["message"]
    assert "这些菜" not in response["message"]
    calls = [call for call in gateway.call_history if call["worker"] == "recommendation"]
    assert len(calls) == 2
    assert "正文说有几道菜，但实际只选了一道。" in calls[1]["payload"]["answer_feedback"]
    reviews = [call for call in gateway.call_history if call["worker"] == "answer_review"]
    assert len(reviews) == 2
    assert reviews[0]["payload"]["verified_facts"]["recommendations"] == [
        {"meal_id": 1034, "name": "凉拌鸡丝"}
    ]
    assert response["message"] == corrected["message"]
    assert state["approved_answer"]
    assert not state["fallback_events"]


@pytest.mark.asyncio
async def test_general_rescue_answer_does_not_need_a_recipe_or_create_recommendations(container):
    text = (
        "已经熟透但发柴的话，可以撕成细丝拌入酱汁，或加入汤里再吃，"
        "改善入口的干涩感，但不能恢复已经流失的水分。\n\n"
        "不要为了变嫩继续长时间煮；如果还没熟透，应先确保熟透。"
    )
    container.gateway.queue_response("intent", {
        "intent": "FOOD_QUESTION", "confidence": 1, "extracted_slots": {},
    })
    container.gateway.queue_response("recommendation", {"response": {"message": text}})
    state = await container.chat_service.chat(user_id=1, message="鸡胸肉已经做柴了，怎么补救？")
    assert state["final_response"]["message"] == text
    assert not state["final_response"]["recommendations"]
    assert not state["tool_results"]
    assert state["approved_answer"]


@pytest.mark.asyncio
async def test_verified_empty_search_can_explain_the_gap_without_unreviewed_fallback(container):
    text = "没找到同时包含鸡胸肉、又符合这个整菜热量上限的菜品。可以调整热量上限后再找。"
    container.gateway.queue_response("recommendation", {"response": {
        "status": "degraded", "message": text,
    }})
    state = await container.chat_service.chat(
        user_id=1, message="想吃鸡胸肉，整道菜不超过1千卡",
    )
    assert state["final_response"]["message"] == text
    assert not state["final_response"]["recommendations"]
    assert state["approved_answer"]
    assert not state["fallback_events"]
    assert state["slots"]["max_calories_kcal"] == 1
    assert any(
        result["tool_name"] == "search_meals" and result["status"] == "success"
        for result in state["tool_results"]
    )


@pytest.mark.asyncio
async def test_explanation_and_comparison_keep_display_order_without_recommending_again(container):
    first = await container.chat_service.chat(
        user_id=1, thread_id="explain", message="推荐午餐"
    )
    cards = first["final_response"]["recommendations"]
    before_why = len(container.gateway.call_history)
    second = await container.chat_service.chat(
        user_id=1, thread_id="explain", message="为什么推荐第二个？"
    )
    assert second["intent"] == "FOOD_QUESTION"
    assert not second["final_response"]["recommendations"]
    assert cards[1]["name"] in second["final_response"]["message"]
    assert not second["tool_results"]
    assert second["discussed_meals"] == [
        {"meal_id": card["meal_id"], "name": card["name"], "reason": card["reason"]}
        for card in cards
    ]
    why_calls = container.gateway.call_history[before_why:]
    assert any(call["worker"] == "recommendation" for call in why_calls)
    assert any(call["worker"] == "answer_review" for call in why_calls)
    why_payload = next(
        call["payload"] for call in why_calls if call["worker"] == "recommendation"
    )
    assert why_payload["discussed_meals"] == second["discussed_meals"]
    call_count = len(container.gateway.call_history)
    third = await container.chat_service.chat(
        user_id=1, thread_id="explain", message="前两个有什么区别？"
    )
    assert third["intent"] == "FOOD_QUESTION"
    assert not third["final_response"]["recommendations"]
    recipes = [
        result["data"]["meal_id"] for result in third["tool_results"]
        if result["tool_name"] == "get_recipe" and result["status"] == "success"
    ]
    assert recipes == [card["meal_id"] for card in cards[:2]]
    assert all(card["name"] in third["final_response"]["message"] for card in cards[:2])
    assert third["final_response"]["citations"]
    followup_calls = container.gateway.call_history[call_count:]
    assert any(call["worker"] == "recommendation" for call in followup_calls)
    assert any(call["worker"] == "answer_review" for call in followup_calls)
    generated_payload = next(
        call["payload"] for call in followup_calls if call["worker"] == "recommendation"
    )
    assert generated_payload["discussed_meals"][:2] == [
        {"meal_id": card["meal_id"], "name": card["name"], "reason": card["reason"]}
        for card in cards[:2]
    ]
    recipe = await container.chat_service.chat(
        user_id=1, thread_id="explain", message="第二道怎么做？"
    )
    assert recipe["slots"]["food_name"] == cards[1]["name"]


@pytest.mark.asyncio
async def test_comparison_reuses_available_recipes_without_duplicate_reads(container):
    first = await container.chat_service.chat(
        user_id=1, thread_id="comparison-reuse", message="推荐午餐",
    )
    cards = first["final_response"]["recommendations"]
    compared = await container.chat_service.chat(
        user_id=1, thread_id="comparison-reuse", message="前两个有什么区别？",
    )
    assert len(compared["tool_results"]) == 2
    again = await container.chat_service.chat(
        user_id=1, thread_id="comparison-reuse", message="前两个有什么区别？",
    )
    assert not again["tool_results"]
    assert again["discussed_meals"] == compared["discussed_meals"]
    assert all(card["name"] in again["final_response"]["message"] for card in cards[:2])


@pytest.mark.asyncio
async def test_nutrition_only_comparison_does_not_load_unneeded_recipes(container):
    await container.chat_service.chat(
        user_id=1, thread_id="nutrition-only-comparison", message="推荐午餐",
    )
    result = await container.chat_service.chat(
        user_id=1, thread_id="nutrition-only-comparison", message="前两个的热量有什么区别？",
    )
    assert result["intent"] == "FOOD_QUESTION"
    assert not result["tool_results"]
    assert not result["final_response"]["recommendations"]


@pytest.mark.asyncio
async def test_unresolved_reference_cannot_be_sent_as_a_literal_recipe_lookup(container):
    container.gateway.queue_response("intent", {
        "intent": "FOOD_QUESTION", "confidence": 1, "extracted_slots": {},
    })
    container.gateway.queue_response("planner", {
        "complete": False, "plan_summary": "读取所指菜品",
        "tool_calls": [{
            "call_id": "unresolved", "name": "get_nutrition",
            "arguments": {"name": "那道菜"},
        }],
    })
    state = await container.chat_service.chat(
        user_id=1, message="你说的那道菜为什么适合我？",
    )
    assert state["intent"] == "FOOD_QUESTION"
    assert state["phase"] == "COMPLETED"
    assert not state["tool_results"]
    assert state["approved_answer"]
    assert "哪" in state["final_response"]["message"]


@pytest.mark.asyncio
async def test_domain_question_and_thanks_do_not_trigger_meal_search_or_write(container):
    for message, intent in [
        ("不要推荐菜，我只想知道鸡胸肉为什么容易做柴", "FOOD_QUESTION"),
        ("好的，谢谢", "CONVERSATION"),
    ]:
        state = await container.chat_service.chat(user_id=1, message=message)
        assert state["intent"] == intent
        assert state["phase"] == "COMPLETED"
        assert not state["tool_results"]
        assert not state["final_response"]["recommendations"]
        assert state["final_response"]["message"]
        assert state["approved_answer"]
        review = next(
            call["payload"] for call in reversed(container.gateway.call_history)
            if call["worker"] == "answer_review"
        )
        assert "verified_response" not in review
        assert "message" not in review["verified_facts"]
    assert not await container.data_store.list_plans(1)


@pytest.mark.asyncio
async def test_answer_context_is_bounded_and_thread_and_user_isolated(container):
    first = await container.chat_service.chat(
        user_id=1, thread_id="private", message="推荐午餐"
    )
    await container.chat_service.chat(
        user_id=1, thread_id="private", message="鸡胸肉为什么容易做柴？"
    )
    payload = next(
        item["payload"] for item in reversed(container.gateway.call_history)
        if item["worker"] == "recommendation"
    )
    assert "推荐午餐" in payload["conversation_summary"]
    assert len(payload["conversation_summary"]) <= 2100
    assert payload["discussed_meals"][1]["meal_id"] == (
        first["final_response"]["recommendations"][1]["meal_id"]
    )
    assert payload["conversation_evidence"]
    assert "calculation_note" not in str(payload["conversation_evidence"])
    new = await container.chat_service.chat(
        user_id=2, thread_id="separate", message="为什么推荐第二个？"
    )
    assert not new["discussed_meals"]
    assert not new["conversation_evidence"]
    assert not new["final_response"]["recommendations"]
    assert not new["final_response"]["citations"]
    assert "哪" in new["final_response"]["message"]


@pytest.mark.asyncio
async def test_rejected_narrative_is_repaired_once_then_uses_canonical_facts(container):
    gateway = container.gateway
    for _ in range(2):
        gateway.queue_response("recommendation", {"response": {
            "message": "这个菜可以治愈疾病，只需99999分钟。",
            "recommendations": [],
        }})
    state = await container.chat_service.chat(user_id=1, message="想吃鸡胸肉")
    text = str(state["final_response"])
    assert "治愈" not in text
    assert "99999" not in text
    assert state["final_response"]["recommendations"]
    assert state["approved_answer"] is None
    assert any(event["code"] == "ANSWER_NOT_GROUNDED" for event in state["fallback_events"])
    calls = [call for call in gateway.call_history if call["worker"] == "recommendation"]
    assert len(calls) == 2
    assert calls[1]["payload"]["answer_feedback"]
    assert calls[1]["payload"]["answer_to_revise"]["message"] == (
        "这个菜可以治愈疾病，只需99999分钟。"
    )
    assert calls[0]["system_prompt"] != calls[1]["system_prompt"]
    assert "回答修订器" in calls[1]["system_prompt"]


@pytest.mark.asyncio
async def test_semantic_rejection_and_reviewer_outage_cannot_publish_unchecked_text(container):
    gateway = container.gateway
    for _ in range(2):
        gateway.queue_response("recommendation", {"response": {
            "message": "这是未经审核的个性化解释。",
            "recommendations": [],
        }})
    gateway.queue_response("answer_review", {
        "supported": False, "issues": ["没有根据当前问题解释"],
        "checks": [],
    })
    gateway.queue_response("answer_review", RuntimeError("review unavailable"))
    state = await container.chat_service.chat(user_id=1, message="想吃鸡胸肉")
    assert "未经审核" not in state["final_response"]["message"]
    assert state["approved_answer"] is None
    assert state["final_response"]["recommendations"]


@pytest.mark.asyncio
async def test_extracted_count_mismatch_repairs_a_positive_reviewer_vote(container):
    gateway = container.gateway
    invalid = {
        "message": "这几道菜的配方含鸡胸肉，可以选凉拌鸡丝。",
        "recommendations": [{"meal_id": 1034, "name": "凉拌鸡丝"}],
    }
    corrected = {**invalid, "message": "可以选凉拌鸡丝，配方含鸡胸肉。"}
    gateway.queue_response("recommendation", {"response": invalid})
    gateway.queue_response("recommendation", {"response": corrected})

    def review(payload):
        raw = FakeModelGateway._answer_review(payload)
        for check in raw["checks"]:
            text = payload["review_units"][check["unit_id"]]
            if "这几道菜" in text:
                check["object_counts"] = [{
                    "quote": "这几道菜", "scope": "recommendations",
                    "meal_ids": [], "minimum": 2, "maximum": None,
                }]
        return raw

    gateway.register_handler("answer_review", review)
    state = await container.chat_service.chat(user_id=1, message="只推荐一道含鸡胸肉的菜")
    assert state["final_response"]["message"] == corrected["message"]
    assert state["approved_answer"]
    calls = [call for call in gateway.call_history if call["worker"] == "recommendation"]
    assert len(calls) == 2
    assert any("OBJECT_COUNT_MISMATCH" in issue
               for issue in calls[1]["payload"]["answer_feedback"])
    reviews = [call for call in gateway.call_history if call["worker"] == "answer_review"]
    assert reviews[0]["payload"]["objects"]["recommendations"] == [1034]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing_checks", "invented_evidence", "lost_condition"])
async def test_unverifiable_review_cannot_publish_prose_and_passes_feedback_to_repair(
    container, failure,
):
    gateway = container.gateway
    text = "这道菜有独特优势。"
    gateway.queue_response("recommendation", {"response": {"message": text}})

    def review(payload):
        checks = [{
            "unit_id": unit_id, "kind": "source_fact", "supported": True,
            "object_counts": [], "conditions": [],
            "conditions_preserved": failure != "lost_condition",
            "evidence": [{"record_id": "missing", "quote": "没有这条证据"}],
            "explanation": "遗漏了原文完成条件。",
        } for unit_id in payload["review_units"]]
        return {"supported": True, "issues": [], "checks": (
            [] if failure == "missing_checks" else checks
        )}

    gateway.register_handler("answer_review", review)
    state = await container.chat_service.chat(user_id=1, message="想吃鸡胸肉")
    assert not state["approved_answer"]
    assert text not in state["final_response"]["message"]
    calls = [call for call in gateway.call_history if call["worker"] == "recommendation"]
    assert len(calls) == 2
    assert calls[1]["payload"]["answer_to_revise"]["message"] == text
    assert calls[1]["payload"]["answer_feedback"]
    assert "evidence_records" in calls[1]["payload"]
    events = container.recorder.get_trace(state["trace_id"])
    reviews = [event for event in events if event.event_type == "response.reviewed"]
    assert all(not event.payload["supported"] for event in reviews)
    assert all("checks" in event.payload for event in reviews)


@pytest.mark.asyncio
async def test_thanks_does_not_confirm_pending_plan_and_unrelated_topics_stay_scoped(container):
    pending = await container.chat_service.chat(
        user_id=1, thread_id="pending", message="制定一天晚餐餐单"
    )
    assert pending["phase"] == "AWAITING_CONFIRMATION"
    state = await container.chat_service.chat(
        user_id=1, thread_id="pending", message="好的，谢谢"
    )
    assert state["intent"] == "CONVERSATION"
    assert not await container.data_store.list_plans(1)
    unrelated = await container.chat_service.chat(user_id=1, message="帮我写排序算法")
    assert unrelated["intent"] == "OTHER"
    assert not unrelated["tool_results"]


@pytest.mark.asyncio
async def test_review_budget_exhaustion_keeps_verified_results_but_not_unreviewed_prose(container):
    container.gateway.queue_response(
        "answer_review", ExecutionLimitExceeded("TOKEN_BUDGET_EXCEEDED"),
    )
    state = await container.chat_service.chat(user_id=1, message="想吃鸡胸肉")
    assert state["phase"] == "COMPLETED"
    assert state["final_response"]["recommendations"]
    for card in state["final_response"]["recommendations"]:
        assert card["reason"]
        assert card["reason"] in state["final_response"]["message"]
    assert state["approved_answer"] is None
    assert state["fallback_events"][-1]["code"] == "TOKEN_BUDGET_EXCEEDED"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["get_nutrition", "get_recipe"])
async def test_comparison_tool_reads_do_not_replace_the_displayed_dish_order(container, tool_name):
    first = await container.chat_service.chat(
        user_id=1, thread_id="comparison-reads", message="推荐午餐"
    )
    cards = first["final_response"]["recommendations"]
    container.gateway.queue_response("planner", {
        "complete": False, "plan_summary": "核对前两道菜的营养",
        "tool_calls": [
            {"call_id": f"compare-{index}", "name": tool_name,
             "arguments": {"meal_id": card["meal_id"]}}
            for index, card in enumerate(cards[:2])
        ],
    })
    await container.chat_service.chat(
        user_id=1, thread_id="comparison-reads", message="前两个有什么区别？"
    )
    recipe = await container.chat_service.chat(
        user_id=1, thread_id="comparison-reads", message="第二道怎么做？"
    )
    assert recipe["phase"] == "COMPLETED"
    assert recipe["slots"]["food_name"] == cards[1]["name"]


@pytest.mark.asyncio
async def test_explicit_thanks_cannot_be_reclassified_as_the_previous_question(container):
    await container.chat_service.chat(
        user_id=1, thread_id="thanks-history", message="鸡胸肉为什么容易做柴"
    )
    container.gateway.queue_response("intent", {
        "intent": "FOOD_QUESTION", "confidence": 0.9, "extracted_slots": {},
    })
    result = await container.chat_service.chat(
        user_id=1, thread_id="thanks-history", message="好的，谢谢"
    )
    assert result["intent"] == "CONVERSATION"
    assert not result["final_response"]["citations"]
    answer = next(
        call["payload"] for call in reversed(container.gateway.call_history)
        if call["worker"] == "recommendation"
    )
    assert "conversation_evidence" not in answer
    assert "conversation_summary" not in answer


@pytest.mark.asyncio
async def test_verified_weather_is_retained_for_follow_up_explanations(container):
    first = await container.chat_service.chat(
        user_id=1, thread_id="weather-evidence", message="根据天气推荐午餐"
    )
    weather = next(
        result for result in first["tool_results"]
        if result["tool_name"] == "get_weather_context" and result["status"] == "success"
    )
    follow_up = await container.chat_service.chat(
        user_id=1, thread_id="weather-evidence", message="为什么推荐第二个？"
    )
    retained = next(
        result for result in follow_up["conversation_evidence"]
        if result["tool_name"] == "get_weather_context"
    )
    assert retained["data"] == weather["data"]


@pytest.mark.asyncio
async def test_extraneous_cards_are_not_published_or_used_to_reject_a_valid_explanation(container):
    first = await container.chat_service.chat(
        user_id=1, thread_id="extra-cards", message="想吃鸡胸肉"
    )
    card = first["final_response"]["recommendations"][0]
    message = f"推荐{card['name']}，是因为它的配方包含你这次想吃的鸡胸肉。"
    container.gateway.queue_response("recommendation", {"response": {
        "message": message,
        "recommendations": [{"meal_id": card["meal_id"], "name": card["name"], "reason": ""}],
    }})
    call_count = len(container.gateway.call_history)
    result = await container.chat_service.chat(
        user_id=1, thread_id="extra-cards", message="为什么推荐第一道？"
    )
    assert card["name"] in result["final_response"]["message"]
    assert not result["final_response"]["recommendations"]
    assert result["approved_answer"]
    assert any(
        call["worker"] in {"recommendation", "answer_review"}
        for call in container.gateway.call_history[call_count:]
    )
