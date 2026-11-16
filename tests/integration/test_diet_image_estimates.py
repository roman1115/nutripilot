"""Photos can produce useful estimates without catalog matches or measured portions."""

import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.integration.test_diet_api import TOKEN_A, TOKEN_B
from tests.integration.test_diet_purge_export import NOW, OTHER, OWNER, approve, finished, headers
from tests.unit.test_diet_media import image_bytes

from nutripilot.api.app import create_app
from nutripilot.application import build_container
from nutripilot.application.diet_parsing import IMAGE_PROMPT_VERSION
from nutripilot.config import Settings


def photo_output() -> dict:
    return json.loads(Path("docs/specs/diet-image-estimate.example.json").read_text())


@pytest.fixture
def active(tmp_path):
    container = build_container(
        settings=Settings(
            _env_file=None,
            app_mode="test",
            user_timezone="UTC",
            api_access_tokens={TOKEN_A: 1, TOKEN_B: 2},
            diet_image_enabled=True,
            vision_model_name="fake-image",
            diet_media_root=str(tmp_path / "media"),
        )
    )
    container.diet.clock = lambda: NOW
    container.diet.intake.clock = lambda: NOW
    return container


def prepare_photo(client, active, output):
    active.diet_parsing.vision.delegate.queue_response("diet_parser", output)
    upload = client.post(
        "/api/v1/diet/media",
        headers=headers(),
        data={"kind": "intake_image"},
        files=[("files", ("meal.png", image_bytes(), "image/png"))],
    )
    assert upload.status_code == 201, upload.text
    response = client.post(
        "/api/v1/diet/drafts",
        headers=headers(),
        json={
            "mode": "image",
            "media_ids": [upload.json()["items"][0]["id"]],
            "send_images_authorized": True,
        },
    )
    assert response.status_code == 201, response.text
    draft = response.json()
    finished(client, {"id": draft["job_id"]})
    return client.get(f"/api/v1/diet/drafts/{draft['id']}", headers=OWNER).json()


def test_photo_estimates_survive_edit_confirm_replay_without_catalog_or_measured_weight(active):
    with TestClient(create_app(active)) as client:
        draft = prepare_photo(client, active, photo_output())
        assert draft["state"] == "ready"
        item = draft["content"]["items"][0]
        assert item["food_ref"]["kind"] == "unknown"
        evidence = item["evidence_snapshot"]
        assert evidence["source_kind"] == "model_estimate"
        assert evidence["model_name"] == "fake-nutripilot-v1"
        assert evidence["prompt_version"] == IMAGE_PROMPT_VERSION
        assert evidence["media_ids"] == draft["media_ids"]
        assert item["nutrition_snapshot"]["calories_kcal"]["value"] == {
            "lower": "150",
            "upper": "400",
        }
        assert item["nutrition_snapshot"]["sodium_mg"]["value"] is None
        assert client.get(f"/api/v1/diet/drafts/{draft['id']}", headers=OTHER).status_code == 404
        query = "/api/v1/diet/logs?start_date=2026-11-12&end_date=2026-11-12"
        assert client.get(query, headers=OWNER).json()["items"] == []
        revised = client.patch(
            f"/api/v1/diet/drafts/{draft['id']}",
            headers=headers(draft["version"]),
            json={
                "patch": {
                    "item_edits": [
                        {
                            "action": "update",
                            "item_id": item["id"],
                            "changes": {
                                "quantity": {
                                    "kind": "mass",
                                    "grams": "200",
                                    "food_state": "cooked",
                                },
                            },
                        }
                    ]
                },
                "evidence": [
                    {
                        "field": "quantity",
                        "source_kind": "user_form",
                        "source_ref": "form",
                        "original_text": "大约200克",
                        "operation": "set",
                        "status": "explicit",
                    }
                ],
            },
        )
        assert revised.status_code == 200, revised.text
        preview = revised.json()
        calories = preview["content"]["items"][0]["nutrition_snapshot"]["calories_kcal"]
        assert calories["value"] == {"lower": "200", "upper": "320"}
        assert calories["basis"] == "model_estimate"
        assert approve(client, preview).status_code == 200
        assert approve(client, preview).status_code == 200
        logs = client.get(query, headers=OWNER).json()["items"]
        assert len(logs) == 1
        assert logs[0]["items"][0]["evidence_snapshot"] == evidence
        assert Decimal(logs[0]["items"][0]["quantity"]["grams"]) == 200


def test_photo_multievent_split_scopes_estimates_by_event(active):
    output = photo_output()
    second = deepcopy(output["entries"][0])
    second["temporal"]["meal_type"] = "dinner"
    output["entries"].append(second)
    output["estimates"].append(
        {
            **deepcopy(output["estimates"][0]),
            "entry_index": 1,
            "nutrients_per_100g": {"calories_kcal": {"lower": "200", "upper": "240"}},
        }
    )
    with TestClient(create_app(active)) as client:
        draft = prepare_photo(client, active, output)
        response = client.post(
            f"/api/v1/diet/drafts/{draft['id']}/split", headers=headers(draft["version"]), json={}
        )
        assert response.status_code == 201, response.text
        children = response.json()["items"]
        assert [
            child["content"]["items"][0]["nutrition_snapshot"]["calories_kcal"]["value"]["lower"]
            for child in children
        ] == ["150", "300"]


def test_adopting_photo_suggestion_keeps_estimate_but_renaming_food_drops_it(active):
    with TestClient(create_app(active)) as client:
        draft = prepare_photo(client, active, photo_output())
        original = draft["content"]["items"][0]
        evidence = [
            {
                "field": "food",
                "source_kind": "user_form",
                "source_ref": "form",
                "original_text": "采用提议",
                "operation": "set",
                "status": "explicit",
            }
        ]
        response = client.patch(
            f"/api/v1/diet/drafts/{draft['id']}",
            headers=headers(draft["version"]),
            json={
                "patch": {
                    "item_edits": [
                        {"action": "remove", "item_id": original["id"]},
                        {"action": "add", "item": draft["content"]["entry"]["items"][0]},
                    ]
                },
                "evidence": evidence,
            },
        )
        assert response.status_code == 200, response.text
        adopted = response.json()
        item = adopted["content"]["items"][0]
        assert item["nutrition_snapshot"]["calories_kcal"]["basis"] == "model_estimate"
        response = client.patch(
            f"/api/v1/diet/drafts/{draft['id']}",
            headers=headers(adopted["version"]),
            json={
                "patch": {
                    "item_edits": [
                        {
                            "action": "update",
                            "item_id": item["id"],
                            "changes": {"food": {"kind": "unknown", "name": "改成另一道菜"}},
                        }
                    ]
                },
                "evidence": evidence,
            },
        )
        assert response.status_code == 200, response.text
        renamed = response.json()
        assert renamed["content"]["items"][0]["evidence_snapshot"] is None
        assert approve(client, renamed).status_code == 200


@pytest.mark.parametrize("mode", ["unknown_quantity", "missing_estimate", "renamed"])
def test_uncertain_photo_remains_savable_and_does_not_borrow_wrong_estimate(active, mode):
    output = photo_output()
    if mode == "unknown_quantity":
        output["entries"][0]["items"][0]["quantity"] = {
            "kind": "unknown",
            "description": "份量看不清",
        }
    elif mode == "missing_estimate":
        output.pop("estimates")
    else:
        output["estimates"][0]["name"] = "另一个食物"
    with TestClient(create_app(active)) as client:
        draft = prepare_photo(client, active, output)
        assert draft["state"] == "ready"
        assert draft["content"]["items"][0]["nutrition_snapshot"]["calories_kcal"]["value"] is None
        assert approve(client, draft).status_code == 200
