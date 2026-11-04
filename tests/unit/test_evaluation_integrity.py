from nutripilot.evaluation.metrics import expected_slots_match
from nutripilot.evaluation.runner import recommendation_integrity


def test_empty_constraint_and_explicit_null_are_not_wildcards() -> None:
    assert not expected_slots_match({"required": []}, {"required": ["鸡胸肉"]})
    assert not expected_slots_match({"limit": None}, {})
    assert expected_slots_match({"required": [], "limit": None}, {"required": [], "limit": None})
    assert not expected_slots_match({"required": []}, {})
    assert expected_slots_match({"groups": [["鸡肉", "豆腐"]]}, {"groups": [["鸡肉", "豆腐"]]})
    assert not expected_slots_match({"groups": [["鸡肉"]]}, {"groups": [["豆腐"]]})


def test_evaluation_rejects_stale_ids_wrong_facts_and_missing_dependencies() -> None:
    from nutripilot.agent.state import new_agent_state

    state = new_agent_state(message="推荐菜", user_id=1)
    state.update(intent="MEAL_RECOMMENDATION", phase="COMPLETED", candidate_ids=[1034])
    state["final_response"] = {
        "message": "虚构菜", "recommendations": [{"meal_id": 1034, "name": "虚构菜", "reason": ""}]
    }
    assert not recommendation_integrity(state)["current_search_evidence"]
    state["tool_results"] = [{
        "tool_name": "search_meals", "status": "success",
        "data": {"hits": [{
            "meal_id": 1034, "name": "凉拌鸡丝",
            "nutrition": {"calories_kcal": 123, "protein_g": 20},
        }]},
    }]
    checks = recommendation_integrity(state)
    assert checks["current_search_evidence"]
    assert not checks["recommendation_facts"]
    assert not checks["recommendation_sources"]
    assert not checks["tool_preconditions"]
