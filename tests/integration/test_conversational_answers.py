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
    assert payload["nutrition_calculations"] == []
    assert not any(
        result["tool_name"] == "get_recipe" for result in state["tool_results"]
    )


@pytest.mark.asyncio
async def test_requested_card_count_is_repaired_without_a_semantic_review(container):
    gateway = container.gateway
    invalid = {
        "message": "可以选凉拌鸡丝和蛋包饭。",
        "recommendations": [
            {"meal_id": 1034, "name": "凉拌鸡丝", "reason": "配方包含鸡胸肉。"},
            {"meal_id": 1067, "name": "蛋包饭", "reason": "配方包含鸡胸肉。"},
        ],
    }
    corrected = {
        **invalid,
        "message": "可以选凉拌鸡丝，这道菜的收录配方包含鸡胸肉。",
        "recommendations": invalid["recommendations"][:1],
    }
    gateway.queue_response("recommendation", {"response": invalid})
    gateway.queue_response("recommendation", {"response": corrected})
    state = await container.chat_service.chat(
        user_id=1, message="只推荐一道含鸡胸肉的菜",
    )

    response = state["final_response"]
    assert len(response["recommendations"]) == 1
    assert response["recommendations"][0]["name"] == "凉拌鸡丝"
    assert "这几道" not in response["message"]
    assert "这些菜" not in response["message"]
    calls = [call for call in gateway.call_history if call["worker"] == "recommendation"]
    assert len(calls) == 2
    assert any(issue.startswith("REQUESTED_COUNT") for issue in calls[1]["payload"][
        "answer_feedback"
    ])
    reviews = [call for call in gateway.call_history if call["worker"] == "answer_review"]
    assert not reviews
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
    assert [result["tool_name"] for result in state["tool_results"]] == ["get_user_profile"]
    assert state["dietary_constraints"]["allergens"] == ["花生"]
    assert state["approved_answer"]


@pytest.mark.asyncio
async def test_general_advice_gets_safety_reference_without_a_second_model(container):
    gateway = container.gateway
    gateway.queue_response("intent", {
        "intent": "FOOD_QUESTION", "confidence": 1, "extracted_slots": {},
        "answer_task": {"context": "general"},
    })
    gateway.queue_response("recommendation", {"response": {
        "message": "用食品温度计核对鸡肉中心，不要单靠计时判断成熟。",
    }})
    gateway.queue_response("answer_boundary", RuntimeError("offline only"))
    state = await container.chat_service.chat(user_id=1, message="鸡胸肉一般怎么煮？")
    assert state["approved_answer"]
    assert "肯定熟透" not in state["final_response"]["message"]
    calls = [call for call in gateway.call_history if call["worker"] == "recommendation"]
    assert len(calls) == 1
    assert not any(call["worker"] == "answer_boundary" for call in gateway.call_history)
    assert calls[0]["payload"]["food_safety_reference"]["uri"] == (
        "https://www.cdc.gov/food-safety/foods/chicken.html"
    )


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
    assert [result["tool_name"] for result in second["tool_results"]] == ["get_user_profile"]
    assert second["discussed_meals"] == [
        {"meal_id": card["meal_id"], "name": card["name"], "reason": card["reason"]}
        for card in cards
    ]
    why_calls = container.gateway.call_history[before_why:]
    assert any(call["worker"] == "recommendation" for call in why_calls)
    assert not any(call["worker"] == "answer_review" for call in why_calls)
    why_payload = next(
        call["payload"] for call in why_calls if call["worker"] == "recommendation"
    )
    assert why_payload["discussed_meals"] == [
        {key: value for key, value in meal.items() if key != "reason"}
        for meal in second["discussed_meals"]
    ]
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
    assert not any(call["worker"] == "answer_review" for call in followup_calls)
    generated_payload = next(
        call["payload"] for call in followup_calls if call["worker"] == "recommendation"
    )
    assert generated_payload["discussed_meals"][:2] == [
        {"meal_id": card["meal_id"], "name": card["name"]}
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
    assert [result["tool_name"] for result in compared["tool_results"]] == [
        "get_user_profile", "get_recipe", "get_recipe",
    ]
    again = await container.chat_service.chat(
        user_id=1, thread_id="comparison-reuse", message="前两个有什么区别？",
    )
    assert [result["tool_name"] for result in again["tool_results"]] == ["get_user_profile"]
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
    assert [item["tool_name"] for item in result["tool_results"]] == ["get_user_profile"]
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
    assert [result["tool_name"] for result in state["tool_results"]] == ["get_user_profile"]
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
        assert [result["tool_name"] for result in state["tool_results"]] == (
            ["get_user_profile"] if intent == "FOOD_QUESTION" else []
        )
        assert not state["final_response"]["recommendations"]
        assert state["final_response"]["message"]
        assert state["approved_answer"]
        answer = next(
            call["payload"] for call in reversed(container.gateway.call_history)
            if call["worker"] == "recommendation"
        )
        assert "verified_response" not in answer
        assert "evidence_records" not in answer
    assert not await container.data_store.list_plans(1)


@pytest.mark.asyncio
async def test_answer_context_is_bounded_and_thread_and_user_isolated(container):
    await container.chat_service.chat(
        user_id=1, thread_id="private", message="推荐午餐"
    )
    await container.chat_service.chat(
        user_id=1, thread_id="private", message="鸡胸肉为什么容易做柴？"
    )
    payload = next(
        item["payload"] for item in reversed(container.gateway.call_history)
        if item["worker"] == "recommendation"
    )
    assert payload["conversation_summary"] is None
    assert payload["recent_messages"] == [{"role": "user", "content": "推荐午餐"}]
    assert not payload["discussed_meals"]
    assert all(
        item["tool_name"] == "get_user_profile" for item in payload["conversation_evidence"]
    )
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
    assert not state["final_response"]["recommendations"]
    assert state["final_response"]["status"] == "degraded"
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
@pytest.mark.parametrize("failure", [
    RuntimeError("review unavailable"), ExecutionLimitExceeded("TOKEN_BUDGET_EXCEEDED"),
])
async def test_offline_reviewer_outage_cannot_degrade_a_normal_answer(container, failure):
    gateway = container.gateway
    text = "可以选凉拌鸡丝，配方里有你想吃的鸡胸肉。"
    gateway.queue_response("recommendation", {"response": {
        "message": text, "recommendations": [{"meal_id": 1034, "name": "凉拌鸡丝"}],
    }})
    gateway.queue_response("answer_review", failure)
    state = await container.chat_service.chat(user_id=1, message="想吃鸡胸肉")
    assert state["final_response"]["message"] == text
    assert state["approved_answer"]
    assert not state["fallback_events"]
    assert not any(call["worker"] == "answer_review" for call in gateway.call_history)
    events = container.recorder.get_trace(state["trace_id"])
    assert any(
        event.event_type == "response.facts_checked" and event.payload["passed"]
        and event.payload["mode"] == "deterministic"
        for event in events
    )
    assert not any(event.event_type == "response.reviewed" for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("issue", [
    "模型评审不喜欢该表达。", "要求完整复述全部菜谱。", "数字提示未匹配。",
])
async def test_offline_semantic_opinions_cannot_rewrite_normal_answers(container, issue):
    gateway = container.gateway
    cards = [{"meal_id": 1034, "name": "凉拌鸡丝"}]
    text = "可以选凉拌鸡丝，配方里有鸡胸肉。营养按来源整道菜估算。"
    gateway.queue_response("recommendation", {"response": {
        "message": text, "recommendations": cards,
    }})
    gateway.queue_response("answer_boundary", {
        "facts_match": True, "objects_match": True, "portion_basis_preserved": True,
        "cooking_conditions_preserved": True, "issues": [issue],
    })
    state = await container.chat_service.chat(user_id=1, message="只推荐一道含鸡胸肉的菜")
    assert state["approved_answer"]
    assert state["final_response"]["message"] == text
    calls = [call for call in gateway.call_history if call["worker"] == "recommendation"]
    assert len(calls) == 1
    boundary_calls = [
        call for call in gateway.call_history if call["worker"] == "answer_boundary"
    ]
    assert not boundary_calls


@pytest.mark.asyncio
async def test_offline_boundary_outage_cannot_degrade_the_normal_answer(container):
    gateway = container.gateway
    gateway.queue_response("recommendation", {"response": {
        "message": "来源营养按整道菜估算，不代表一人份。",
        "recommendations": [{"meal_id": 1034, "name": "凉拌鸡丝"}],
    }})
    gateway.queue_response("answer_boundary", RuntimeError("boundary unavailable"))
    state = await container.chat_service.chat(user_id=1, message="想吃鸡胸肉")
    assert state["final_response"]["status"] == "completed"
    assert state["final_response"]["recommendations"]
    assert state["approved_answer"]
    assert not state["fallback_events"]
    assert not any(call["worker"] == "answer_boundary" for call in gateway.call_history)


@pytest.mark.asyncio
async def test_unknown_source_is_repaired_without_losing_the_original_task(container):
    gateway = container.gateway
    invalid = {
        "message": "可以选凉拌鸡丝，配方含鸡胸肉。",
        "recommendations": [{"meal_id": 1034, "name": "凉拌鸡丝"}],
        "source_ids": ["invented:source"],
    }
    corrected = {**invalid, "source_ids": []}
    gateway.queue_response("recommendation", {"response": invalid})
    gateway.queue_response("recommendation", {"response": corrected})

    state = await container.chat_service.chat(user_id=1, message="只推荐一道含鸡胸肉的菜")
    assert state["final_response"]["message"] == corrected["message"]
    assert state["approved_answer"]
    calls = [call for call in gateway.call_history if call["worker"] == "recommendation"]
    assert len(calls) == 2
    assert "UNKNOWN_SOURCE" in calls[1]["payload"]["answer_feedback"]
    assert calls[1]["payload"]["answer_task"] == calls[0]["payload"]["answer_task"]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "这道菜能治愈疾病。",
    "这道菜不含任何过敏原。",
])
async def test_hard_safety_failures_still_cannot_publish_prose(container, text):
    gateway = container.gateway
    for _ in range(2):
        gateway.queue_response("recommendation", {"response": {"message": text}})
    state = await container.chat_service.chat(user_id=1, message="想吃鸡胸肉")
    assert not state["approved_answer"]
    assert text not in state["final_response"]["message"]
    calls = [call for call in gateway.call_history if call["worker"] == "recommendation"]
    assert len(calls) == 2
    assert calls[1]["payload"]["answer_to_revise"]["message"] == text
    assert calls[1]["payload"]["answer_feedback"]
    assert calls[1]["payload"]["tool_results"] == calls[0]["payload"]["tool_results"]
    events = container.recorder.get_trace(state["trace_id"])
    checks = [event for event in events if event.event_type == "response.facts_checked"]
    assert len(checks) == 2
    assert all(not event.payload["passed"] for event in checks)
    boundary_checks = [
        event for event in events if event.event_type == "response.boundary_checked"
    ]
    assert not boundary_checks


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
async def test_generation_budget_exhaustion_keeps_only_the_checked_cards(container):
    container.gateway.queue_response("recommendation", {"response": {
        "message": "可以选凉拌鸡丝。",
        "recommendations": [{"meal_id": 1034, "name": "凉拌鸡丝", "reason": "待核对"}],
        "source_ids": ["invented:source"],
    }})
    container.gateway.queue_response(
        "recommendation", ExecutionLimitExceeded("TOKEN_BUDGET_EXCEEDED"),
    )
    state = await container.chat_service.chat(user_id=1, message="想吃鸡胸肉")
    assert state["phase"] == "COMPLETED"
    assert state["final_response"]["recommendations"]
    for card in state["final_response"]["recommendations"]:
        assert card["reason"] == ""
    assert state["final_response"]["status"] == "degraded"
    assert state["approved_answer"] is None
    assert state["fallback_events"][-1]["code"] == "TOKEN_BUDGET_EXCEEDED"


@pytest.mark.asyncio
async def test_numeric_hint_for_a_negated_claim_does_not_force_revision(container):
    text = "原文没有说凉拌鸡丝使用999克鸡胸肉，请按收录用量准备。"
    container.gateway.queue_response("recommendation", {"response": {"message": text}})
    container.gateway.queue_response("answer_boundary", {
        "facts_match": True, "objects_match": True, "portion_basis_preserved": True,
        "cooking_conditions_preserved": True, "issues": [],
    })
    state = await container.chat_service.chat(user_id=1, message="凉拌鸡丝怎么做？")
    assert state["approved_answer"]
    assert state["final_response"]["message"] == text
    calls = [call for call in container.gateway.call_history if call["worker"] in {
        "recommendation", "answer_boundary",
    }]
    assert [call["worker"] for call in calls] == ["recommendation"]
    assert "quantity_hints" not in calls[0]["payload"]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "原文只写豆腐一盒，未给克重。",
    "鸡肉已经熟透但柴了，可以撕细拌酱。",
    "营养按来源整道菜估算，不知道份数。",
])
async def test_normal_prose_has_one_generation_and_no_online_numeric_gate(container, text):
    gateway = container.gateway
    gateway.queue_response("recommendation", {"response": {"message": text}})
    gateway.queue_response("answer_boundary", {
        "facts_match": False, "objects_match": True, "portion_basis_preserved": True,
        "cooking_conditions_preserved": True, "issues": [],
    })
    state = await container.chat_service.chat(user_id=1, message="凉拌鸡丝怎么做？")
    assert state["approved_answer"]
    assert state["final_response"]["message"] == text
    calls = [call for call in gateway.call_history if call["worker"] == "recommendation"]
    assert len(calls) == 1
    assert "quantity_hints" not in calls[0]["payload"]
    boundary_calls = [call for call in gateway.call_history if call["worker"] == "answer_boundary"]
    assert not boundary_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("attempt", [0, 1])
@pytest.mark.parametrize("failure", [
    RuntimeError("boundary unavailable"), ExecutionLimitExceeded("TOKEN_BUDGET_EXCEEDED"),
])
async def test_generation_or_hard_error_revision_failure_never_publishes_the_draft(
    container, attempt, failure,
):
    gateway = container.gateway
    invalid = "这道菜能治愈疾病。"
    if attempt:
        gateway.queue_response("recommendation", {"response": {"message": invalid}})
    gateway.queue_response("recommendation", failure)
    state = await container.chat_service.chat(user_id=1, message="凉拌鸡丝怎么做？")
    assert not state["approved_answer"]
    assert state["final_response"]["status"] == "degraded"
    assert invalid not in state["final_response"]["message"]


@pytest.mark.asyncio
async def test_recipe_profile_constraints_are_fresh_for_direct_and_followup_answers(container):
    for index, message in enumerate(("皮蛋豆腐怎么做？", "皮蛋豆腐怎么做？")):
        container.gateway.queue_response("recommendation", {"response": {
            "message": "豆腐和皮蛋按收录用量准备，拌入调料；不要使用花生碎，注意交叉接触。",
        }})
        state = await container.chat_service.chat(
            user_id=1, thread_id="allergic-recipe", message=message,
        )
        assert state["approved_answer"], index
        assert state["dietary_constraints"]["allergens"] == ["花生"]
        assert sum(
            result["tool_name"] == "get_user_profile" for result in state["tool_results"]
        ) == 1
        payload = next(
            call["payload"] for call in reversed(container.gateway.call_history)
            if call["worker"] == "recommendation"
        )
        assert payload["dietary_constraints"]["allergens"] == ["花生"]
        assert all(item["role"] == "user" for item in payload["recent_messages"])
        assert all(item["tool_name"] in {"get_recipe", "get_user_profile"}
                   for item in payload["tool_results"] + payload["conversation_evidence"])


@pytest.mark.asyncio
async def test_optional_allergen_is_a_hard_error_and_gets_only_one_revision(container):
    container.gateway.queue_response("recommendation", {"response": {
        "message": "最后撒上花生碎（可选）。",
    }})
    corrected = "按来源准备皮蛋和豆腐；不要添加花生碎，其他配料也要核对。"
    container.gateway.queue_response("recommendation", {"response": {"message": corrected}})
    state = await container.chat_service.chat(user_id=1, message="皮蛋豆腐怎么做？")
    assert state["approved_answer"]
    assert state["final_response"]["message"] == corrected
    calls = [call for call in container.gateway.call_history if call["worker"] == "recommendation"]
    assert len(calls) == 2
    assert any(item.startswith("DIETARY_PROSE_CONFLICT")
               for item in calls[1]["payload"]["answer_feedback"])


@pytest.mark.asyncio
async def test_allergy_avoidance_explanation_does_not_trigger_a_second_generation(container):
    text = (
        "皮蛋和豆腐按原文用量准备，拌入酱汁；"
        "花生碎因您对花生过敏，全程不可添加。"
    )
    container.gateway.queue_response("recommendation", {"response": {"message": text}})
    state = await container.chat_service.chat(user_id=1, message="皮蛋豆腐怎么做？")
    assert state["final_response"]["message"] == text
    assert state["approved_answer"]
    assert not state["fallback_events"]
    assert sum(call["worker"] == "recommendation" for call in container.gateway.call_history) == 1


@pytest.mark.asyncio
async def test_planner_cannot_invent_user_exclusions_from_rejected_dish_ingredients(container):
    container.gateway.queue_response("planner", {
        "complete": False, "plan_summary": "查找候选",
        "tool_calls": [{
            "call_id": "invented-exclusions", "name": "search_meals",
            "arguments": {
                "query": "鸡胸肉", "excluded_allergens": ["鸡胸肉"],
                "excluded_ingredients": ["鸡胸肉", "菜籽油"],
            },
        }],
    })
    state = await container.chat_service.chat(user_id=1, message="想吃鸡胸肉")
    assert state["candidate_ids"]
    assert state["dietary_constraints"]["allergens"] == ["花生"]
    assert state["dietary_constraints"]["excluded_ingredients"] == ["香菜", "花生"]
    assert "菜籽油" not in state["dietary_constraints"]["excluded_ingredients"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_allergic_recipe_fallback_never_replays_unsafe_raw_recipe(container, failure):
    for _ in range(2):
        container.gateway.queue_response(
            "recommendation", RuntimeError("generation unavailable") if failure else {
                "response": {"message": "加入花生碎（可选），直接食用。"},
            },
        )
    state = await container.chat_service.chat(user_id=1, message="皮蛋豆腐怎么做？")
    assert state["approved_answer"] is None
    assert state["final_response"]["status"] == "degraded"
    assert "加入花生" not in state["final_response"]["message"]
    assert "不能直接照做" in state["final_response"]["message"]
    assert not state["final_response"]["recommendations"]


@pytest.mark.asyncio
async def test_profile_failure_stops_recipe_before_generation(container, monkeypatch):
    async def unavailable(user_id):
        raise RuntimeError("profile storage unavailable")

    monkeypatch.setattr(container.data_store, "get_user_profile", unavailable)
    state = await container.chat_service.chat(user_id=1, message="皮蛋豆腐怎么做？")
    assert state["final_response"]["status"] == "degraded"
    assert [item["tool_name"] for item in state["tool_results"]] == ["get_user_profile"]
    assert not any(call["worker"] == "recommendation" for call in container.gateway.call_history)


@pytest.mark.asyncio
async def test_whitespace_prose_is_revised_once_and_never_published_as_an_empty_success(container):
    for _ in range(2):
        container.gateway.queue_response("recommendation", {"response": {"message": " \n "}})
    state = await container.chat_service.chat(user_id=1, message="皮蛋豆腐怎么做？")
    assert state["approved_answer"] is None
    assert state["final_response"]["status"] == "degraded"
    assert state["final_response"]["message"].strip()
    calls = [call for call in container.gateway.call_history if call["worker"] == "recommendation"]
    assert len(calls) == 2
    assert "EMPTY_ANSWER" in calls[1]["payload"]["answer_feedback"]


@pytest.mark.asyncio
async def test_answer_changes_do_not_inject_a_default_calorie_limit(container):
    container.tool_executor.trace_sink = container.recorder
    state = await container.chat_service.chat(user_id=1, message="想吃鸡胸肉")
    assert state["slots"].get("max_calories_kcal") is None
    assert state["dietary_constraints"].get("max_calories_kcal") is None
    calls = [call for call in container.gateway.call_history if call["worker"] in {
        "recommendation", "answer_boundary",
    }]
    assert calls
    assert all(call["payload"]["slots"].get("max_calories_kcal") is None for call in calls)
    searches = [event for event in container.recorder.get_trace(state["trace_id"])
                if event.event_type == "tool.completed"
                and event.payload["tool_name"] == "search_meals"]
    assert searches
    assert all(event.payload["arguments"].get("max_calories_kcal") is None for event in searches)


@pytest.mark.asyncio
async def test_targeted_replacement_keeps_other_candidates_and_carries_relative_goal(container):
    first = await container.chat_service.chat(
        user_id=1, thread_id="target-replacement", message="推荐午餐",
    )
    cards = first["final_response"]["recommendations"]
    selected, other = cards[:2]
    container.tool_executor.trace_sink = container.recorder
    container.gateway.queue_response("intent", {
        "intent": "MEAL_ADJUSTMENT", "confidence": 1,
        "extracted_slots": {"excluded_ingredients": [selected["name"]]},
        "answer_task": {
            "objective": "只替换指定菜，比较整菜脂肪",
            "context": "meals", "meal_ids": [selected["meal_id"]],
            "recommendation_count": 1, "nutrient_preference": "lower_fat",
            "needs_nutrition": True,
        },
    })
    state = await container.chat_service.chat(
        user_id=1, thread_id="target-replacement",
        message=f"{selected['name']}脂肪太高了，换一个",
    )
    assert selected["name"] not in state["slots"]["excluded_ingredients"]
    search_call = next(
        event.payload for event in container.recorder.get_trace(state["trace_id"])
        if event.event_type == "tool.completed" and event.payload["tool_name"] == "search_meals"
    )
    excluded = search_call["arguments"]["excluded_meal_ids"]
    assert selected["meal_id"] in excluded
    assert other["meal_id"] not in excluded
    assert len(state["final_response"]["recommendations"]) <= 1
    assert all(
        card["fat_g"] < selected["fat_g"]
        for card in state["final_response"]["recommendations"]
    )


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
