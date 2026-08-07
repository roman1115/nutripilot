from nutripilot.agent.nodes.tool import _extract_grounding


def test_grounding_filters_conflicting_and_incomplete_meal_metadata() -> None:
    results = [
        {
            "tool_name": "search_meals",
            "status": "success",
            "data": {
                "hits": [
                    {
                        "meal_id": 5,
                        "ingredients": ["香菇", "豆腐"],
                        "allergens": ["大豆"],
                        "citation": {"source_id": "meal:5", "title": "香菇豆腐"},
                    },
                    {
                        "meal_id": 2,
                        "ingredients": ["鸡胸肉", "藜麦"],
                        "allergens": [],
                        "citation": {"source_id": "meal:2", "title": "鸡胸藜麦"},
                    },
                    {
                        "meal_id": 99,
                        "citation": {"source_id": "meal:99", "title": "字段不完整"},
                    },
                ]
            },
        }
    ]

    candidate_ids, citations, filtered_ids = _extract_grounding(
        results,
        {"allergens": ["香菇"], "excluded_ingredients": ["香菇"]},
    )

    assert candidate_ids == [2]
    assert [citation["source_id"] for citation in citations] == ["meal:2"]
    assert filtered_ids == {5, 99}
