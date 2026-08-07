from fastapi.testclient import TestClient

from nutripilot.api.app import create_app
from nutripilot.application import build_container


def test_profile_feedback_and_plan_endpoints_share_the_application_store() -> None:
    container = build_container()
    with TestClient(create_app(container)) as client:
        profile = client.get("/api/v1/profile", headers={"X-User-ID": "2"})
        assert profile.status_code == 200
        assert profile.json()["user_id"] == 2

        updated = client.put(
            "/api/v1/profile",
            headers={"X-User-ID": "2"},
            json={
                "display_name": "小史",
                "goals": ["减脂"],
                "tastes": ["清淡"],
                "cuisines": ["粤菜"],
                "avoidances": ["香菜"],
                "allergens": ["花生"],
                "daily_calorie_target": 1800,
                "location": "广州",
            },
        )
        assert updated.status_code == 200
        assert updated.json()["display_name"] == "小史"
        assert updated.json()["health_goals"] == ["减脂"]
        assert updated.json()["cuisines"] == ["粤菜"]
        assert updated.json()["allergens"] == ["花生"]
        assert updated.json()["daily_calorie_target"] == 1800
        assert updated.json()["location"] == "广州"

        reloaded = client.get("/api/v1/profile", headers={"X-User-ID": "2"})
        assert reloaded.json() == updated.json()

        feedback_payload = {
            "meal_id": 1,
            "feedback_type": "like",
            "rating": 5,
            "reason": "适合工作日",
        }
        first = client.post(
            "/api/v1/feedback",
            headers={"X-User-ID": "2", "Idempotency-Key": "feedback-1"},
            json=feedback_payload,
        )
        repeated = client.post(
            "/api/v1/feedback",
            headers={"X-User-ID": "2", "Idempotency-Key": "feedback-1"},
            json=feedback_payload,
        )
        assert first.status_code == 200
        assert repeated.json()["id"] == first.json()["id"]

        waiting = client.post(
            "/api/v1/chat",
            json={
                "user_id": 2,
                "thread_id": "resource-plan",
                "message": "帮我制定一周晚餐饮食计划",
            },
        )
        assert waiting.json()["requires_confirmation"] is True
        pending_action = waiting.json()["pending_action"]
        assert pending_action["operation"] == "create_meal_plan"
        assert pending_action["payload"]["items"]
        confirmed = client.post(
            "/api/v1/chat/resource-plan/confirm",
            json={
                "user_id": 2,
                "approved": True,
                "confirmation_id": pending_action["id"],
                "idempotency_key": pending_action["idempotency_key"],
            },
        )
        assert confirmed.status_code == 200

        plans = client.get("/api/v1/meal-plans", headers={"X-User-ID": "2"})
        assert plans.status_code == 200
        assert len(plans.json()) == 1
        assert plans.json()[0]["items"][0]["meal_name"]


def test_evaluation_run_uses_default_dataset_and_is_retrievable() -> None:
    with TestClient(create_app()) as client:
        started = client.post("/api/v1/evaluations/run", json={})
        assert started.status_code == 200
        report = started.json()
        assert report["total_cases"] == 60

        fetched = client.get(f"/api/v1/evaluations/{report['run_id']}")
        assert fetched.status_code == 200
        assert fetched.json()["run_id"] == report["run_id"]

        missing = client.get("/api/v1/evaluations/not-found")
        assert missing.status_code == 404
