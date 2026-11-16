from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient

from nutripilot.api.app import create_app
from nutripilot.application import build_container
from nutripilot.config import Settings

NOW = datetime(2026, 11, 12, 4, tzinfo=UTC)
TOKEN_A = "a" * 32
TOKEN_B = "b" * 32


def payload() -> dict:
    return {
        "mode": "structured",
        "channel": "manual",
        "entry": {
            "actuality": "actual",
            "subject": "self",
            "temporal": {"local_date": "2026-11-12", "meal_type": "lunch"},
            "items": [
                {
                    "client_item_key": "fixture",
                    "food": {"kind": "unknown", "name": "fixture"},
                    "quantity": {"kind": "unknown", "description": "unknown"},
                }
            ],
        },
        "evidence": [
            {
                "field": "food",
                "source_kind": "user_form",
                "source_ref": "fixture",
                "original_text": "fixture",
                "operation": "set",
                "status": "explicit",
            }
        ],
    }


def test_diet_api_owner_versions_confirmation_and_receipt_replay() -> None:
    container = build_container(
        settings=Settings(
            app_mode="test",
            api_access_tokens={TOKEN_A: 1, TOKEN_B: 2},
            user_timezone="Asia/Shanghai",
        )
    )
    container.diet.clock = lambda: NOW
    container.diet.intake.clock = lambda: NOW
    container.diet.records.clock = lambda: NOW
    owner = {"Authorization": f"Bearer {TOKEN_A}", "Idempotency-Key": "request"}
    other = {"Authorization": f"Bearer {TOKEN_B}", "Idempotency-Key": "request"}
    with TestClient(create_app(container)) as client:
        unauthorized = client.get("/api/v1/diet/context")
        assert unauthorized.status_code == 401
        assert unauthorized.headers["cache-control"] == "no-store"
        created = client.post(
            "/api/v1/diet/drafts", json=payload(), headers={**owner, "X-User-ID": "2"}
        )
        assert created.status_code == 201, created.text
        draft = created.json()
        assert draft["user_id"] == 1 and created.headers["etag"] == '"1"'
        repeated = client.post("/api/v1/diet/drafts", json=payload(), headers=owner)
        assert repeated.json()["id"] == draft["id"]
        assert client.get(f"/api/v1/diet/drafts/{draft['id']}", headers=other).status_code == 404
        body = {
            "approved": True,
            "preview_version": 1,
            "content_hash": draft["preview_hash"],
        }
        path = f"/api/v1/diet/previews/{draft['id']}/confirm"
        assert client.post(path, json=body, headers=owner).status_code == 428
        saved = client.post(path, json=body, headers={**owner, "If-Match": '"1"'})
        assert saved.status_code == 200, saved.text
        assert (
            client.post(path, json=body, headers={**owner, "If-Match": '"1"'}).json()
            == saved.json()
        )
        record_id = saved.json()["result_refs"][0]["id"]
        assert client.get(f"/api/v1/diet/logs/{record_id}", headers=other).status_code == 404
        records = client.get(
            "/api/v1/diet/logs?start_date=2026-11-12&end_date=2026-11-12", headers=owner
        )
        assert len(records.json()["items"]) == 1
        assert (
            records.json()["items"][0]["items"][0]["nutrition_snapshot"]["calories_kcal"]["value"]
            is None
        )
        unknown = client.get(
            "/api/v1/diet/summary?start_date=2026-11-12&end_date=2026-11-12", headers=owner
        )
        assert unknown.status_code == 200, unknown.text
        assert unknown.json()["items"][0]["actual"]["state"] == "partial"
        invalid = client.post(
            "/api/v1/diet/drafts", json={**payload(), "user_id": 2}, headers=owner
        )
        assert invalid.status_code == 422
        broken_range = client.get(
            "/api/v1/diet/logs?start_date=2026-01-01&end_date=2026-11-12", headers=owner
        )
        assert broken_range.status_code == 422


def test_demo_identity_is_fixed_and_preview_discard_invalidates_confirmation() -> None:
    container = build_container(settings=Settings(app_mode="test", user_timezone="Asia/Shanghai"))
    container.diet.clock = lambda: NOW
    container.diet.intake.clock = lambda: NOW
    with TestClient(create_app(container)) as client:
        headers = {"Idempotency-Key": "draft", "X-User-ID": "999"}
        created = client.post("/api/v1/diet/drafts", json=payload(), headers=headers)
        assert created.status_code == 201, created.text
        draft = created.json()
        assert draft["user_id"] == 1
        path = f"/api/v1/diet/previews/{draft['id']}/confirm"
        confirmation = {
            "approved": False,
            "preview_version": 1,
            "content_hash": draft["preview_hash"],
        }
        headers["If-Match"] = '"1"'
        cancelled = client.post(path, json=confirmation, headers=headers)
        assert cancelled.status_code == 200 and cancelled.json()["state"] == "discarded"
        response = client.post(path, json={**confirmation, "approved": True}, headers=headers)
        assert response.status_code == 409
        assert (
            client.get("/api/v1/diet/logs?start_date=2026-11-12&end_date=2026-11-12").json()[
                "items"
            ]
            == []
        )


def confirm(client: TestClient, preview: dict, *, owner: str = TOKEN_A):
    return client.post(
        f"/api/v1/diet/previews/{preview['id']}/confirm",
        json={
            "approved": True,
            "preview_version": preview["version"],
            "content_hash": preview.get("preview_hash", preview.get("content_hash")),
        },
        headers={
            "Authorization": f"Bearer {owner}",
            "Idempotency-Key": uuid4().hex,
            "If-Match": f'"{preview["version"]}"',
        },
    )


def test_personal_food_pages_versions_and_unknown_allergen_facts() -> None:
    active = build_container(
        settings=Settings(app_mode="test", api_access_tokens={TOKEN_A: 1, TOKEN_B: 2})
    )
    owner = {"Authorization": f"Bearer {TOKEN_A}"}
    other = {"Authorization": f"Bearer {TOKEN_B}"}
    data = {
        "name": "fixture-label",
        "food_state": "as_sold",
        "basis": "per_serving",
        "serving_grams": "80",
        "source": "manual_label",
        "source_note": "fixture label only",
        "nutrients": {
            "calories_kcal": {"lower": "100", "upper": "110"},
            **dict.fromkeys(["protein_g", "fat_g", "carbohydrates_g", "fiber_g", "sodium_mg"]),
        },
        "allergens": [
            {"name": "milk", "state": "unknown", "evidence_ref": None, "explanation": "not listed"}
        ],
    }
    with TestClient(create_app(active)) as client:
        ids = []
        for index in range(2):
            response = client.post(
                "/api/v1/diet/previews",
                json={
                    "operation": "personal_food_create",
                    "arguments": {
                        "action": "create",
                        "data": {**data, "name": f"fixture-label{index}"},
                    },
                },
                headers={**owner, "Idempotency-Key": str(index)},
            )
            assert response.status_code == 201, response.text
            saved = confirm(client, response.json())
            assert saved.status_code == 200, saved.text
            ids.append(saved.json()["result_refs"][0]["id"])
        page = client.get("/api/v1/diet/personal-foods?limit=1", headers=owner)
        assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
        assert len(page.json()["items"]) == 1 and page.json()["truncated"]
        cursor = page.json()["next_cursor"]
        second = client.get("/api/v1/diet/personal-foods", params={"cursor": cursor}, headers=owner)
        assert len(second.json()["items"]) == 1
        assert (
            client.get(
                "/api/v1/diet/personal-foods", params={"cursor": cursor}, headers=other
            ).status_code
            == 422
        )
        assert client.get("/api/v1/diet/personal-foods", headers=other).json()["items"] == []
        assert client.get(f"/api/v1/diet/foods/{ids[0]}", headers=other).status_code == 404
        food = client.get(f"/api/v1/diet/foods/{ids[0]}", headers=owner).json()
        assert food["allergens"][0]["state"] == "unknown"
        assert food["evidence"]["nutrients"]["protein_g"]["value"] is None
        change = {
            "operation": "personal_food_delete",
            "arguments": {"action": "delete", "food_id": ids[0], "base_version": 1},
        }
        assert (
            client.post(
                "/api/v1/diet/previews", json=change, headers={**owner, "Idempotency-Key": "delete"}
            ).status_code
            == 428
        )
        deleted = client.post(
            "/api/v1/diet/previews",
            json=change,
            headers={**owner, "Idempotency-Key": "delete", "If-Match": '"1"'},
        )
        assert confirm(client, deleted.json()).status_code == 200
        assert (
            client.get(
                "/api/v1/diet/personal-foods", params={"cursor": cursor}, headers=owner
            ).status_code
            == 409
        )
        assert len(client.get("/api/v1/diet/personal-foods", headers=owner).json()["items"]) == 1
        assert (
            len(
                client.get(
                    "/api/v1/diet/personal-foods?include_deleted=true", headers=owner
                ).json()["items"]
            )
            == 2
        )


def test_day_declaration_requires_confirmation_and_races_with_new_records() -> None:
    active = build_container(
        settings=Settings(
            app_mode="test",
            api_access_tokens={TOKEN_A: 1, TOKEN_B: 2},
            user_timezone="Asia/Shanghai",
        )
    )
    for component in (active.diet, active.diet.intake, active.diet.records, active.diet.queries):
        component.clock = lambda: NOW
    owner = {"Authorization": f"Bearer {TOKEN_A}", "Idempotency-Key": "declare", "If-Match": '"1"'}
    path = "/api/v1/diet/days/2026-11-12"
    scope = "start_date=2026-11-12&end_date=2026-11-12"
    mutation = {
        "operation": "day_status_set",
        "arguments": {"local_date": "2026-11-12", "status": "no_intake", "base_version": 1},
    }
    with TestClient(create_app(active)) as client:
        assert client.get(path, headers=owner).json()["version"] == 1
        prepared = client.post("/api/v1/diet/previews", json=mutation, headers=owner)
        assert prepared.status_code == 201, prepared.text
        assert client.get(path, headers=owner).json()["status"] == "open"
        assert confirm(client, prepared.json(), owner=TOKEN_B).status_code == 404
        saved = confirm(client, prepared.json())
        assert saved.status_code == 200, saved.text
        assert saved.json()["result_refs"][0]["id"] == "2026-11-12"
        assert confirm(client, prepared.json()).json() == saved.json()
        no_intake = client.get(f"/api/v1/diet/summary?{scope}", headers=owner).json()["items"][0]
        assert no_intake["actual"]["nutrients"]["calories_kcal"]["total"] == {
            "lower": "0",
            "upper": "0",
        }
        draft = client.post(
            "/api/v1/diet/drafts", json=payload(), headers={**owner, "Idempotency-Key": "intake"}
        ).json()
        assert confirm(client, draft).status_code == 200
        assert client.get(path, headers=owner).json()["status"] == "open"
        assert confirm(client, prepared.json()).json() == saved.json()
        assert client.get(path, headers=owner).json()["status"] == "open"
        version = client.get(path, headers=owner).json()["version"]
        no_intake_again = client.post(
            "/api/v1/diet/previews",
            json={**mutation, "arguments": {**mutation["arguments"], "base_version": version}},
            headers={**owner, "Idempotency-Key": "again", "If-Match": f'"{version}"'},
        )
        assert no_intake_again.status_code == 422
        assert no_intake_again.json()["detail"]["code"] == "DAY_HAS_RECORDS"


def test_diet_request_size_no_store_and_safe_database_error(monkeypatch) -> None:
    from sqlalchemy.exc import SQLAlchemyError

    active = build_container(settings=Settings(app_mode="test"))

    async def failed(*args, **kwargs):
        raise SQLAlchemyError("private-db-connection-string-and-food")

    with TestClient(create_app(active)) as client:
        too_large = client.post(
            "/api/v1/diet/drafts",
            content=b"x" * (256 * 1024 + 1),
            headers={"Content-Type": "application/json", "Idempotency-Key": "oversized"},
        )
        assert too_large.status_code == 413
        assert too_large.headers["cache-control"] == "no-store"
        invalid = client.post(
            "/api/v1/diet/drafts", json={}, headers={"Idempotency-Key": "invalid"}
        )
        assert invalid.status_code == 422 and invalid.headers["cache-control"] == "no-store"
        monkeypatch.setattr(active.diet.queries, "logs", failed)
        response = client.get("/api/v1/diet/logs?start_date=2026-11-12&end_date=2026-11-12")
        assert response.status_code == 503 and response.headers["cache-control"] == "no-store"
        assert "private-db" not in response.text


def test_plan_api_batch_copy_intake_archive_and_owner_isolation() -> None:
    active = build_container(
        settings=Settings(
            app_mode="test",
            api_access_tokens={TOKEN_A: 1, TOKEN_B: 2},
            user_timezone="Asia/Shanghai",
        )
    )
    for component in (active.diet, active.diet.intake, active.diet.plans, active.diet.queries):
        component.clock = lambda: NOW
    owner = {"Authorization": f"Bearer {TOKEN_A}"}
    other = {"Authorization": f"Bearer {TOKEN_B}"}
    entry = payload()["entry"]
    entry["actuality"] = "planned"
    entry["temporal"]["local_date"] = "2026-11-09"
    with TestClient(create_app(active)) as client:
        create = client.post(
            "/api/v1/diet/previews",
            json={
                "operation": "plan_create",
                "arguments": {
                    "action": "create",
                    "parent": {"kind": "new", "title": "fixture-plan", "start_date": "2026-11-09"},
                    "entries": [{"client_entry_key": "lunch", "entry": entry}],
                    "evidence": payload()["evidence"],
                },
            },
            headers={**owner, "Idempotency-Key": "create-plan"},
        )
        assert create.status_code == 201, create.text
        assert confirm(client, create.json()).status_code == 200
        library = client.get(
            "/api/v1/diet/plan-library?start_date=2026-11-09&end_date=2026-11-15", headers=owner
        ).json()
        assert len(library["items"]) == 1
        plan = library["items"][0]
        meals_path = f"/api/v1/diet/plans/{plan['id']}/meals"
        meal = client.get(meals_path, headers=owner).json()["items"][0]["meal"]
        assert client.get(meals_path, headers=other).status_code == 404
        assert (
            client.get(f"/api/v1/diet/planned-meals/{meal['id']}", headers=other).status_code == 404
        )
        intake = {
            **payload(),
            "channel": "plan",
            "source": {
                "kind": "plan",
                "planned_meal_id": meal["id"],
                "base_version": 1,
                "record_again": False,
            },
        }
        intake["entry"]["temporal"]["local_date"] = "2026-11-09"
        created = client.post(
            "/api/v1/diet/drafts", json=intake, headers={**owner, "Idempotency-Key": "ate"}
        )
        assert created.status_code == 201, created.text
        assert confirm(client, created.json()).status_code == 200
        deviation = client.get(
            "/api/v1/diet/deviation?start_date=2026-11-09&end_date=2026-11-15", headers=owner
        )
        assert deviation.status_code == 200, deviation.text
        assert deviation.json()["items"][0]["execution"][0]["recorded_count"] == 1
        archived = client.post(
            "/api/v1/diet/previews",
            json={
                "operation": "plan_archive",
                "arguments": {"action": "archive", "plan_id": plan["id"], "base_version": 1},
            },
            headers={**owner, "Idempotency-Key": "x" * 100, "If-Match": '"1"'},
        )
        assert archived.status_code == 201, archived.text
        assert confirm(client, archived.json()).status_code == 200
        assert (
            client.get(
                "/api/v1/diet/plan-library?start_date=2026-11-09&end_date=2026-11-15", headers=owner
            ).json()["items"]
            == []
        )
        assert (
            len(
                client.get(
                    "/api/v1/diet/logs?start_date=2026-11-09&end_date=2026-11-15", headers=owner
                ).json()["items"]
            )
            == 1
        )
        restore = client.post(
            "/api/v1/diet/previews",
            json={
                "operation": "plan_restore",
                "arguments": {"action": "restore", "plan_id": plan["id"], "base_version": 2},
            },
            headers={**owner, "Idempotency-Key": "restore-plan", "If-Match": '"2"'},
        )
        assert confirm(client, restore.json()).status_code == 200
        copied = client.post(
            "/api/v1/diet/previews",
            json={
                "operation": "plan_copy_week",
                "arguments": {
                    "action": "copy_week",
                    "source_plan_id": plan["id"],
                    "base_version": 3,
                    "parent": {"kind": "new", "title": "next-week", "start_date": "2026-11-16"},
                    "target_start_date": "2026-11-16",
                    "conflict_policy": "reject",
                    "evidence": payload()["evidence"],
                },
            },
            headers={**owner, "Idempotency-Key": "copy-week", "If-Match": '"3"'},
        )
        assert copied.status_code == 201, copied.text
        assert confirm(client, copied.json()).status_code == 200
