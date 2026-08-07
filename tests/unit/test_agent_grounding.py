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
    assert item.reason == "模型生成的个性化理由"
    assert grounded.citations[0].source_id == "meal:2"
    assert "9999" not in grounded.message
