import pytest

from nutripilot.application import build_container
from nutripilot.config import Settings
from nutripilot.model_gateway import FakeModelGateway
from nutripilot.safety.dietary import has_dietary_conflict


@pytest.fixture
def container():
    return build_container(settings=Settings(_env_file=None))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial", "cancel", "field"),
    [
        ("午餐推荐一道菜，不超过1千卡", "不限热量了", "max_calories_kcal"),
        ("午餐推荐一道菜，预算不超过20元", "取消预算限制", "max_price_cny"),
        ("午餐推荐一道菜，15分钟内做好", "时间不限", "max_prep_minutes"),
    ],
)
async def test_cancellation_restores_candidates_and_persists_through_next_turn(
    container, initial, cancel, field
):
    first = await container.chat_service.chat(user_id=1, thread_id="cancel", message=initial)
    assert first["slots"][field] is not None
    assert not first["final_response"]["recommendations"]

    second = await container.chat_service.chat(user_id=1, thread_id="cancel", message=cancel)
    assert second["intent"] == "MEAL_ADJUSTMENT"
    assert second["slots"][field] is None
    assert second["final_response"]["recommendations"]
    assert not second["fallback_events"]

    third = await container.chat_service.chat(user_id=1, thread_id="cancel", message="换一批")
    assert field in third["slots"] and third["slots"][field] is None
    assert third["final_response"]["recommendations"]
    assert set(third["candidate_ids"]).isdisjoint(second["candidate_ids"])


@pytest.mark.asyncio
async def test_clear_drops_stale_planner_limits_without_losing_other_constraints(container):
    container.gateway.queue_response("intent", {
        "intent": "MEAL_RECOMMENDATION", "confidence": 1,
        "extracted_slots": {
            "meal_type": "lunch", "max_price_cny": 20, "max_calories_kcal": 600,
            "allergens": ["鸡蛋"], "dislikes": ["香菜"],
            "required_ingredients": ["鸡胸肉"],
        },
    })
    await container.chat_service.chat(
        user_id=1, thread_id="stale-planner",
        message="午餐想吃鸡胸肉，不超过600千卡，预算20元，鸡蛋过敏，不喜欢香菜",
    )

    def stale_planner(payload):
        result = FakeModelGateway._planner(payload)
        for call in result["tool_calls"]:
            if call["name"] == "search_meals":
                call["arguments"]["max_price_cny"] = 20
        return result

    container.gateway.register_handler("planner", stale_planner)
    second = await container.chat_service.chat(
        user_id=1, thread_id="stale-planner", message="取消预算限制"
    )
    assert second["slots"]["max_price_cny"] is None
    assert second["slots"]["max_calories_kcal"] == 600
    assert second["slots"]["allergens"] == ["鸡蛋"]
    assert second["slots"]["dislikes"] == ["香菜"]
    assert second["slots"]["required_ingredients"] == ["鸡胸肉"]
    assert second["final_response"]["recommendations"]
    for recommendation in second["final_response"]["recommendations"]:
        meal = await container.data_store.get_meal(recommendation["meal_id"])
        assert meal.nutrition.bounds("calories_kcal").maximum <= 600
        assert not has_dietary_conflict(
            meal_allergens=meal.allergens, meal_ingredients=meal.ingredients,
            declared_allergens=["鸡蛋"], excluded_ingredients=["香菜"],
            ingredient_records=[item.model_dump() for item in meal.ingredient_records],
        )

    third = await container.chat_service.chat(
        user_id=1, thread_id="stale-planner", message="重新推荐，预算不超过20元"
    )
    assert third["slots"]["max_price_cny"] == 20
    assert not third["final_response"]["recommendations"]
