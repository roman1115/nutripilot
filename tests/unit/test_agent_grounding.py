from nutripilot.agent.grounding import ground_recommendation_facts
from nutripilot.domain.models import FinalResponse, MealRecommendation


def test_grounding_overwrites_model_authored_meal_facts() -> None:
    response = FinalResponse(
        message="模型声称热量是 9999 千卡",
        recommendations=[
            MealRecommendation(
                meal_id=2,
                name="错误菜名",
                reason="模型生成的个性化理由",
                calories_kcal=9999,
                protein_g=0,
            )
        ],
    )
    tool_results = [
        {
            "tool_name": "search_meals",
            "status": "success",
            "data": {
                "hits": [
                    {
                        "meal_id": 2,
                        "name": "鸡胸藜麦能量碗",
                        "nutrition": {"calories_kcal": 516, "protein_g": 43},
                        "health_goals": ["高蛋白"],
                        "score": 0.91,
                        "citation": {
                            "source_id": "meal:2",
                            "uri": "seed://meal/2",
                        },
                    }
                ]
            },
        }
    ]

    grounded = ground_recommendation_facts(response, tool_results)

    item = grounded.recommendations[0]
    assert item.name == "鸡胸藜麦能量碗"
    assert item.calories_kcal == 516
    assert item.protein_g == 43
    assert "516" in item.reason
    assert "43" in item.reason
    assert "模型生成" not in item.reason
    assert grounded.citations[0].source_id == "meal:2"
    assert "9999" not in grounded.message


def test_grounding_explains_verified_preferences_without_inventing_personal_facts() -> None:
    response = FinalResponse(
        message="模型自由解释",
        recommendations=[MealRecommendation(meal_id=2, name="餐食", reason="你患有疾病")],
    )
    grounded = ground_recommendation_facts(
        response,
        [
            {
                "tool_name": "get_user_profile",
                "status": "success",
                "data": {"preferences": ["清淡"], "cuisines": ["中式"], "allergens": ["花生"]},
            },
            {
                "tool_name": "search_meals",
                "status": "success",
                "data": {
                    "hits": [
                        {
                            "meal_id": 2,
                            "name": "餐食",
                            "tastes": ["清淡"],
                            "cuisines": ["中式"],
                            "allergens": ["鱼"],
                            "nutrition": {"calories_kcal": 500, "protein_g": 40},
                        }
                    ]
                },
            },
        ],
        {"max_calories_kcal": 600, "min_protein_g": 30},
    )
    reason = grounded.recommendations[0].reason
    assert "口味偏好匹配：清淡" in reason
    assert "菜系偏好匹配：中式" in reason
    assert "不超过本轮设置的 600 千卡" in reason
    assert "至少 30 克" in reason
    assert "你患有" not in grounded.model_dump_json()
    assert "餐食记录标注的过敏原：鱼" in reason
