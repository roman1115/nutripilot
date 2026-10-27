from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from nutripilot.agent.contracts import PlanSpec
from nutripilot.agent.planning import PlanBuilder, PlanBuildError
from nutripilot.api.app import create_app
from nutripilot.application import build_container
from nutripilot.config import Settings
from nutripilot.domain.enums import Phase
from nutripilot.retrieval import MealSearchFilters, MealSearchRequest, RetrievalService


@pytest.fixture
def container():
    return build_container(
        settings=Settings(_env_file=None, app_mode="demo", model_provider="fake")
    )


def test_catalog_api_searches_sources_and_exposes_pending_recipes(container) -> None:
    with TestClient(create_app(container)) as client:
        summary = client.get("/api/v1/catalog").json()
        assert summary["ingredients"] == 7793
        assert summary["recipes"] == 50
        assert summary["pending_recipes"] == 47
        assert summary["calculation_ready_recipes"] == 3
        assert {source["license"] for source in summary["sources"]} == {"CC0-1.0", "Unlicense"}

        ingredients = client.get("/api/v1/ingredients", params={"q": "西红柿", "limit": 1}).json()
        assert ingredients["items"][0]["id"] == "fdc:170457"
        assert ingredients["total"] == 1
        ingredient = client.get("/api/v1/ingredients/fdc:170457").json()
        assert ingredient["nutrients_per_100g"]["calories_kcal"] == 18
        assert client.get("/api/v1/ingredients/unknown").status_code == 404
        assert client.get("/api/v1/ingredients?limit=101").status_code == 422
        assert client.get("/api/v1/ingredients?offset=-1").status_code == 422

        ready = client.get("/api/v1/recipes", params={"status": "calculation_ready"}).json()
        assert ready["total"] == 3
        page = client.get("/api/v1/recipes", params={"limit": 1, "offset": 1}).json()
        assert len(page["items"]) == 1 and page["total"] == 50
        assert client.get("/api/v1/recipes?status=verified").status_code == 422
        pending = client.get("/api/v1/recipes/1001").json()
        assert pending["status"] == "pending" and pending["nutrition"] is None
        assert pending["review_issues"] and "鸡蛋" in pending["raw_quantities"]
        assert pending["source"]["version"] == "308cc8b7dab1da84b20e7fafdf836a715437f582"
        recipe = client.get("/api/v1/recipes/1004").json()
        assert recipe["nutrition"]["calories_kcal"] == pytest.approx(56.7)
        assert client.get("/api/v1/recipes/999999").status_code == 404


@pytest.mark.asyncio
async def test_calculated_recipe_reaches_agent_response_with_honest_provenance(container) -> None:
    state = await container.chat_service.chat(user_id=1, message="查询糖拌西红柿的营养")
    assert state["phase"] == Phase.COMPLETED.value
    response = state["final_response"]
    assert "56.7" in response["message"]
    assert "投料110" in response["message"]
    assert any("计算估算" in warning for warning in response["warnings"])
    assert not any("份量缺少统一克重" in warning for warning in response["warnings"])
    assert len(response["citations"]) == 3
    assert sum(item["source_type"] == "ingredient" for item in response["citations"]) == 2


@pytest.mark.asyncio
async def test_calculated_recipes_respect_unknown_price_time_and_avoidance_filters(
    container,
) -> None:
    service = RetrievalService(container.data_store)
    for filters in (
        MealSearchFilters(max_price_cny=100),
        MealSearchFilters(max_prep_minutes=1440),
    ):
        response = await service.search_meals(
            MealSearchRequest(query="糖拌西红柿", filters=filters, limit=20)
        )
        assert all(item.meal.recipe_id is None for item in response.hits)
    response = await service.search_meals(
        MealSearchRequest(
            query="糖拌西红柿",
            filters=MealSearchFilters(excluded_ingredients=["番茄"]),
            limit=20,
        )
    )
    assert all(hit.meal.id != 1004 for hit in response.hits)


@pytest.mark.asyncio
async def test_side_dish_or_whole_pot_cannot_be_saved_as_a_complete_meal(container) -> None:
    meal = await container.data_store.get_meal(1023)
    assert meal is not None and meal.meal_role == "staple"
    candidate = meal.model_dump(mode="json")
    spec = PlanSpec(
        start_date=date(2026, 10, 27), plan_range="one_day", meal_type="breakfast"
    )
    builder = PlanBuilder()
    with pytest.raises(PlanBuildError, match="not enough"):
        builder.build(spec, [candidate])
    plan = builder.parse_request(
        {
            "title": "错误地把整锅粥当早餐",
            "start_date": "2026-10-27",
            "items": [{"meal_id": 1023, "meal_type": "breakfast", "meal_date": "2026-10-27"}],
        }
    )
    with pytest.raises(PlanBuildError) as error:
        builder.validate_prewrite(
            spec=spec,
            request=plan,
            candidates=[candidate],
            allowed_meal_ids={1023},
            declared_allergens=[],
            excluded_ingredients=[],
        )
    assert error.value.code == "PLAN_MEAL_TYPE_MISMATCH"


def test_catalog_routes_require_configured_access_token() -> None:
    container = build_container(
        settings=Settings(
            _env_file=None,
            app_mode="demo",
            model_provider="fake",
            api_access_tokens={"x" * 32: 1},
        )
    )
    with TestClient(create_app(container)) as client:
        assert client.get("/api/v1/catalog").status_code == 401
        assert client.get("/api/v1/recipes").status_code == 401
        assert client.get("/api/v1/ingredients").status_code == 401
        assert (
            client.get(
                "/api/v1/catalog",
                headers={"Authorization": "Bearer " + "x" * 32},
            ).status_code
            == 200
        )
