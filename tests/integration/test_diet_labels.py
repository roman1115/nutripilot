"""Label proposals are owned, reviewable and never imply an intake event."""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from tests.integration.test_diet_api import TOKEN_A, TOKEN_B
from tests.integration.test_diet_purge_export import OTHER, OWNER, approve, finished, headers
from tests.unit.test_diet_media import image_bytes

from nutripilot.api.app import create_app
from nutripilot.application import build_container
from nutripilot.config import Settings
from nutripilot.domain.diet_labels import ParsedLabel


def label_output() -> dict:
    return {
        "name": "fixture-label",
        "brand": None,
        "basis": "per_100g",
        "serving_grams": None,
        "original_excerpt": "Synthetic label: 418.4kJ energy, 0.2g sodium per 100g.",
        "nutrients": {
            "calories_kcal": {
                "value": {"lower": "418.4", "upper": "418.4"},
                "unit": "kJ",
                "original_excerpt": "418.4kJ",
            },
            "protein_g": None,
            "fat_g": None,
            "carbohydrates_g": None,
            "fiber_g": None,
            "sodium_mg": {
                "value": {"lower": "0.2", "upper": "0.2"},
                "unit": "g",
                "original_excerpt": "0.2g sodium",
            },
        },
    }


def test_label_units_unknowns_and_invalid_energy_unit() -> None:
    parsed = ParsedLabel.model_validate(label_output())
    assert parsed.normalized()["calories_kcal"].lower == Decimal("100")
    assert parsed.normalized()["sodium_mg"].upper == Decimal("200")
    assert parsed.normalized()["protein_g"] is None
    invalid = label_output()
    invalid["nutrients"]["calories_kcal"]["unit"] = "g"
    with pytest.raises(ValidationError):
        ParsedLabel.model_validate(invalid)


def test_authorized_label_task_review_food_and_shared_media_lifecycle(tmp_path) -> None:
    active = build_container(
        settings=Settings(
            _env_file=None,
            app_mode="test",
            user_timezone="UTC",
            api_access_tokens={TOKEN_A: 1, TOKEN_B: 2},
            diet_image_enabled=True,
            vision_model_name="fake-label-only",
            diet_media_root=str(tmp_path / "media"),
            diet_export_root=str(tmp_path / "exports"),
        )
    )
    with TestClient(create_app(active)) as client:
        active.diet_parsing.vision.delegate.queue_response("diet_label_parser", label_output())
        images = client.post(
            "/api/v1/diet/media",
            headers=headers(),
            data={"kind": "label_image"},
            files=[
                ("files", ("front.png", image_bytes(), "image/png")),
                ("files", ("back.jpg", image_bytes("JPEG"), "image/jpeg")),
            ],
        )
        assert images.status_code == 201, images.text
        media_ids = [asset["id"] for asset in images.json()["items"]]
        response = client.post(
            "/api/v1/diet/drafts",
            headers=headers(),
            json={
                "mode": "image",
                "media_ids": media_ids,
                "send_images_authorized": True,
            },
        )
        assert response.status_code == 201, response.text
        draft = response.json()
        finished(client, {"id": draft["job_id"]})
        proposal_url = f"/api/v1/diet/drafts/{draft['id']}/label"
        assert client.get(proposal_url, headers=OTHER).status_code == 404
        proposal = client.get(proposal_url, headers=OWNER)
        assert proposal.status_code == 200, proposal.text
        data = proposal.json()
        conversion = {key: data.pop(key) for key in ("label_draft_id", "label_draft_version")}
        assert Decimal(data["nutrients"]["calories_kcal"]["lower"]) == Decimal("100")
        assert data["nutrients"]["protein_g"] is None
        assert client.get("/api/v1/diet/personal-foods", headers=OWNER).json()["items"] == []
        preview = client.post(
            "/api/v1/diet/previews",
            headers=headers(),
            json={
                "operation": "personal_food_create",
                "arguments": {
                    "action": "create",
                    **conversion,
                    "label_reviewed": True,
                    "data": {
                        **data,
                        "source": "manual_label",
                        "food_state": "as_sold",
                    },
                },
            },
        )
        assert preview.status_code == 201, preview.text
        assert preview.json()["content"]["after"]["evidence"]["media_ids"] == media_ids
        evidence = preview.json()["content"]["after"]["evidence"]
        assert evidence["model_name"] == "fake-nutripilot-v1"
        assert evidence["prompt_version"] == "diet-label-v1"
        duplicate = client.post(
            "/api/v1/diet/previews",
            headers=headers(),
            json={
                "operation": "personal_food_create",
                "arguments": {
                    "action": "create",
                    **conversion,
                    "label_reviewed": True,
                    "data": {**data, "source": "manual_label", "food_state": "as_sold"},
                },
            },
        )
        assert duplicate.status_code == 201, duplicate.text
        saved = approve(client, preview.json())
        assert saved.status_code == 200, saved.text
        assert approve(client, preview.json()).status_code == 200
        assert approve(client, duplicate.json()).status_code == 409
        assert client.get(proposal_url, headers=OWNER).status_code == 410
        assert len(client.get("/api/v1/diet/personal-foods", headers=OWNER).json()["items"]) == 1
        assert (
            client.get(
                "/api/v1/diet/logs?start_date=2026-01-01&end_date=2026-01-01", headers=OWNER
            ).json()["items"]
            == []
        )
        for media_id in media_ids:
            assert (
                client.delete(f"/api/v1/diet/media/{media_id}", headers=headers()).status_code
                == 409
            )
        stolen = {**data, "source": "manual_label", "food_state": "as_sold"}
        assert (
            client.post(
                "/api/v1/diet/previews",
                headers={**OTHER, "Idempotency-Key": "stolen"},
                json={
                    "operation": "personal_food_create",
                    "arguments": {"action": "create", "data": stolen},
                },
            ).status_code
            == 404
        )


def test_label_conversion_requires_explicit_review_and_create_action() -> None:
    from uuid import uuid4

    from tests.unit.test_diet_foods import food_input

    from nutripilot.application.personal_food_service import PersonalFoodMutation

    for fields in (
        {"label_draft_id": uuid4()},
        {"label_reviewed": True},
        {"label_draft_id": uuid4(), "label_draft_version": 1},
    ):
        with pytest.raises(ValidationError, match="explicitly reviewed"):
            PersonalFoodMutation(action="create", data=food_input(), **fields)


def test_unsupported_label_basis_does_not_become_per_100g() -> None:
    label = label_output()
    label["basis"] = "unsupported"
    assert ParsedLabel.model_validate(label).basis == "unsupported"
