import pytest

from nutripilot.agent.grounding import ground_recommendation_facts
from nutripilot.domain.models import FinalResponse, MealRecommendation, NumericRange


def test_named_candidate_is_prioritized_without_inventing_an_unretrieved_meal() -> None:
    response = FinalResponse(
        message="推荐相似菜",
        recommendations=[MealRecommendation(meal_id=1002, name="红烧鱼", reason="相似")],
    )
    results = [
        {
            "tool_name": "search_meals",
            "status": "success",
            "data": {
                "hits": [
                    {
                        "meal_id": 1001,
                        "name": "红烧鲤鱼",
                        "nutrition": {"calories_kcal": 1900, "protein_g": 90},
                    },
                    {
                        "meal_id": 1002,
                        "name": "红烧鱼",
                        "nutrition": {"calories_kcal": 800, "protein_g": 50},
                    },
                ]
            },
        }
    ]

    grounded = ground_recommendation_facts(response, results, {"food_name": "红烧鲤鱼"})

    assert [item.meal_id for item in grounded.recommendations] == [1001]
    assert grounded.recommendations[0].calories_kcal == 1900
    absent = ground_recommendation_facts(response, results, {"food_name": "未收录菜名"})
    assert absent.recommendations == []
    assert "未收录菜名" in absent.message
    assert "红烧鱼" not in absent.message


def test_no_current_search_cannot_publish_old_recommendations_or_prose() -> None:
    response = FinalResponse(
        message="推荐虚构三文鱼，热量9999千卡",
        recommendations=[MealRecommendation(meal_id=1034, name="虚构三文鱼", reason="假的")],
    )
    for results in ([], [{"tool_name": "search_meals", "status": "failure"}]):
        grounded = ground_recommendation_facts(response, results)
        assert grounded.recommendations == []
        assert grounded.citations == []
        assert "虚构" not in grounded.message
        assert "9999" not in grounded.message
        assert grounded.status == "degraded"


def test_successful_candidates_are_not_lost_when_model_declines_to_select() -> None:
    result = ground_recommendation_facts(
        FinalResponse(message="没有可推荐的"),
        [{"tool_name": "search_meals", "status": "success", "data": {"hits": [{
            "meal_id": 1034, "name": "凉拌鸡丝",
            "nutrition": {"calories_kcal": 200, "protein_g": 20},
        }]}}],
    )
    assert [card.meal_id for card in result.recommendations] == [1034]
    assert "凉拌鸡丝" in result.message


@pytest.mark.parametrize("count", [0, 1, 2, 3])
@pytest.mark.parametrize("required", [False, True])
def test_fallback_prose_agrees_with_the_final_card_count(count, required) -> None:
    meals = [
        {"meal_id": index + 1, "name": f"测试菜{index + 1}", "ingredients": ["鸡胸肉"]}
        for index in range(count)
    ]
    response = ground_recommendation_facts(
        FinalResponse(message="原稿"),
        [{"tool_name": "search_meals", "status": "success", "data": {"hits": meals}}],
        {"required_ingredients": ["鸡胸肉"]} if required else {},
    )

    assert len(response.recommendations) == count
    if count == 1:
        assert "这几道" not in response.message
        assert "这些菜" not in response.message
        assert "这道菜" in response.message
    elif count > 1:
        assert ("这些菜" if required else "这几道菜") in response.message
    else:
        assert "没有找到" in response.message


@pytest.mark.asyncio
async def test_output_boundary_rejects_old_ids_even_if_candidate_state_is_stale() -> None:
    from nutripilot.agent.nodes.output import OutputGuardNode
    from nutripilot.agent.state import new_agent_state
    from nutripilot.safety import OutputGuard
    from nutripilot.tracing import InMemoryTraceRecorder

    state = new_agent_state(message="取消食材限制", user_id=1)
    state.update(intent="MEAL_ADJUSTMENT", candidate_ids=[1034])
    state["final_response"] = {
        "message": "虚构三文鱼9999千卡",
        "recommendations": [{"meal_id": 1034, "name": "虚构三文鱼", "reason": ""}],
    }
    update = await OutputGuardNode(OutputGuard(), InMemoryTraceRecorder()).run(state)
    assert update["final_response"]["recommendations"] == []
    assert "虚构" not in update["final_response"]["message"]
    assert "9999" not in update["final_response"]["message"]


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
                carbohydrates_g=9999,
                fat_g=9999,
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
                        "ingredients": ["鸡胸肉", "藜麦"],
                        "nutrition": {
                            "calories_kcal": 516,
                            "protein_g": 43,
                            "carbohydrates_g": 38,
                            "fat_g": 17,
                            "nutrient_ranges": {
                                "calories_kcal": {"minimum": 500, "maximum": 516},
                                "fat_g": {"minimum": 15, "maximum": 17},
                            },
                        },
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
    assert item.carbohydrates_g == 38
    assert item.fat_g == 17
    assert item.nutrient_ranges["calories_kcal"] == NumericRange(minimum=500, maximum=516)
    assert item.nutrient_ranges["fat_g"] == NumericRange(minimum=15, maximum=17)
    assert f"{item.name}：{item.reason}" in grounded.message
    assert "千卡" not in item.reason
    assert "蛋白质" not in item.reason
    assert "模型生成" not in item.reason
    assert grounded.citations[0].source_id == "meal:2"
    assert "9999" not in grounded.message
    assert any("未独立核验" in warning for warning in grounded.warnings)
    assert '"maximum":516.0' in grounded.model_dump_json(warnings="error")


@pytest.mark.parametrize("facts", [{}, {"carbohydrates_g": None, "fat_g": None},
                                   {"carbohydrates_g": 0, "fat_g": 0}])
def test_card_macros_preserve_source_unknowns_and_zero(facts) -> None:
    response = FinalResponse(message="推荐", recommendations=[
        MealRecommendation(meal_id=1001, name="测试菜", reason="模型理由",
                           carbohydrates_g=9999, fat_g=9999),
    ])
    grounded = ground_recommendation_facts(response, [{
        "tool_name": "search_meals", "status": "success", "data": {"hits": [{
            "meal_id": 1001, "name": "测试菜", "nutrition": facts,
        }]},
    }])
    card = grounded.recommendations[0].model_dump()
    assert card["carbohydrates_g"] == facts.get("carbohydrates_g")
    assert card["fat_g"] == facts.get("fat_g")


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
    assert "平时偏好的清淡口味、中式菜系" in reason
    assert reason.index("600 千卡") < reason.index("平时偏好")
    assert "不超过你设定的 600 千卡" in reason
    assert "至少 30 克" in reason
    assert "你患有" not in grounded.model_dump_json()
    assert any("餐食记录标注的过敏原：鱼" in w for w in grounded.warnings)
    assert any("用户画像记录的过敏原：花生" in w for w in grounded.warnings)
    assert len(reason) < 100


def test_verified_nutrition_links_its_source_and_omits_estimate_warning() -> None:
    grounded = ground_recommendation_facts(
        FinalResponse(
            message="原始模型内容",
            recommendations=[MealRecommendation(meal_id=2, name="餐食", reason="原始理由")],
        ),
        [
            {
                "tool_name": "search_meals",
                "status": "success",
                "data": {
                    "hits": [
                        {
                            "meal_id": 2,
                            "name": "餐食",
                            "nutrition": {
                                "calories_kcal": 400,
                                "protein_g": 30,
                                "serving_size": "1碗",
                                "serving_weight_g": 350,
                                "source_status": "verified",
                                "source_uri": "https://example.test/nutrition/2",
                            },
                            "allergens": [],
                        }
                    ]
                },
            }
        ],
    )

    assert grounded.citations[0].uri == "https://example.test/nutrition/2"
    assert "估算" not in grounded.recommendations[0].reason
    assert not any("未独立核验" in warning for warning in grounded.warnings)
