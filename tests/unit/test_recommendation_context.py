import pytest

from nutripilot.agent.agents.intent import _resolved_slots
from nutripilot.agent.contracts import IntentOutput
from nutripilot.agent.grounding import ground_recommendation_facts
from nutripilot.agent.state import new_agent_state
from nutripilot.domain.models import FinalResponse, MealRecommendation


@pytest.mark.parametrize(
    ("message", "extracted", "expected"),
    [
        ("午餐想吃鸡胸肉", ["鸡胸肉"], ["鸡胸肉"]),
        ("用鸡胸肉和番茄做点什么", ["鸡胸肉", "番茄"], ["鸡胸肉", "番茄"]),
        ("鸡胸肉有什么推荐？", ["鸡胸肉"], ["鸡胸肉"]),
        ("不想吃鸡胸肉，推荐午餐", ["鸡胸肉"], []),
        ("我对鸡胸肉过敏，推荐午餐", ["鸡胸肉"], []),
        ("不吃鸡翅，想吃鸡胸肉", ["鸡翅", "鸡胸肉"], ["鸡胸肉"]),
        ("推荐午餐", ["鸡胸肉"], []),
    ],
)
def test_required_food_is_current_and_positive(message, extracted, expected):
    state = new_agent_state(message=message, user_id=1)
    state["slots"] = {"required_ingredients": ["鸡翅"]}
    slots, missing = _resolved_slots(
        state,
        IntentOutput(
            intent="MEAL_RECOMMENDATION", confidence=1,
            extracted_slots={"required_ingredients": extracted},
        ),
    )
    assert slots["required_ingredients"] == expected
    if expected:
        assert not missing


@pytest.mark.parametrize(
    ("intent", "message", "extracted", "expected"),
    [
        ("MEAL_ADJUSTMENT", "换一批", [], ["鸡胸肉"]),
        ("MEAL_ADJUSTMENT", "换一批，鸡翅可以吗", ["鸡翅"], ["鸡翅"]),
        ("MEAL_ADJUSTMENT", "换一批，不要鸡胸肉", [], []),
        ("MEAL_PLAN", "安排一周餐单", [], []),
    ],
)
def test_required_food_only_carries_into_an_unchanged_adjustment(
    intent, message, extracted, expected
):
    state = new_agent_state(message=message, user_id=1)
    state["slots"] = {"required_ingredients": ["鸡胸肉"]}
    slots, _ = _resolved_slots(
        state,
        IntentOutput(
            intent=intent, confidence=1,
            extracted_slots={"required_ingredients": extracted},
        ),
    )
    assert slots["required_ingredients"] == expected


def test_reason_responds_to_ingredients_and_current_limits_before_profile():
    hits = [
        {
            "meal_id": 1034, "name": "凉拌鸡丝",
            "ingredients": ["去皮去骨生鸡胸肉", "食盐"],
            "cuisines": ["中式"], "tastes": ["清淡"],
            "nutrition": {"calories_kcal": 400, "protein_g": 35},
        },
        {
            "meal_id": 1080, "name": "烤鸡翅", "ingredients": ["生鸡翅肉皮"],
            "nutrition": {"calories_kcal": 300, "protein_g": 30},
        },
    ]
    results = [
        {"tool_name": "get_user_profile", "status": "success",
         "data": {"cuisines": ["中式"], "preferences": ["清淡"]}},
        {"tool_name": "search_meals", "status": "success", "data": {"hits": hits}},
    ]
    response = ground_recommendation_facts(
        FinalResponse(
            message="推荐鸡翅",
            recommendations=[
                MealRecommendation(meal_id=1080, name="烤鸡翅", reason="你患有疾病")
            ],
        ),
        results,
        {"required_ingredients": ["鸡胸肉"], "max_calories_kcal": 500},
        user_message="想吃鸡胸肉，不超过500千卡",
    )
    assert [item.meal_id for item in response.recommendations] == [1034]
    reason = response.recommendations[0].reason
    assert reason.index("鸡胸肉") < reason.index("500 千卡")
    assert "中式" not in reason
    assert "你患有" not in response.model_dump_json()
    assert "烤鸡翅" not in response.model_dump_json()
    empty = ground_recommendation_facts(
        FinalResponse(message="推荐鸡翅"), results,
        {"required_ingredients": ["不存在的食材"]},
    )
    assert not empty.recommendations
    assert "没有找到" in empty.message


def test_current_taste_precedes_profile_and_unknown_time_is_not_a_reason():
    response = ground_recommendation_facts(
        FinalResponse(
            message="推荐", recommendations=[
                MealRecommendation(meal_id=1, name="餐食", reason="只需5分钟，减脂必备")
            ],
        ),
        [
            {"tool_name": "get_user_profile", "status": "success",
             "data": {"cuisines": ["中式"]}},
            {"tool_name": "search_meals", "status": "success", "data": {"hits": [
                {"meal_id": 1, "name": "餐食", "tastes": ["清淡"],
                 "cuisines": ["中式"], "prep_minutes": None}
            ]}},
        ],
        {"tastes": ["清淡"], "max_prep_minutes": 10},
        user_message="推荐清淡的菜，10分钟内做好",
    )
    reason = response.recommendations[0].reason
    assert reason.startswith("口味是清淡，符合你这次的要求")
    assert reason.index("清淡") < reason.index("中式")
    assert "分钟" not in reason
    assert "减脂" not in reason


def test_empty_result_explains_unknown_price_without_internal_filter_counts():
    response = ground_recommendation_facts(
        FinalResponse(message="错误"),
        [{"tool_name": "search_meals", "status": "success", "data": {
            "hits": [],
            "filter_diagnostics": {
                "active_meals": 100, "unknown_price": 100,
                "ingredient_requirements": 97, "dietary_conflict": 34, "eligible_meals": 0,
            },
        }}],
        {"required_ingredients": ["鸡胸肉"], "max_price_cny": 20},
    )
    assert "价格未知" in response.message
    assert "无法核实是否符合你的预算" in response.message
    assert "筛选记录" not in response.message
    assert "100" not in response.message
    assert not response.recommendations


@pytest.mark.parametrize("message", ["取消食材限制", "不限食材", "改成豆腐"])
def test_replacing_or_clearing_a_named_dish_removes_its_old_name(message):
    state = new_agent_state(user_id=1, message=message)
    state["slots"] = {"food_name": "红烧鲤鱼", "meal_type": "lunch"}
    slots, _ = _resolved_slots(state, IntentOutput(
        intent="MEAL_ADJUSTMENT", confidence=1,
        extracted_slots={"required_ingredients": ["豆腐"]} if message == "改成豆腐" else {},
    ))
    assert "food_name" not in slots

