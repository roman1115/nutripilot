"""Private downloads and permanent removal must fail closed and reject old approvals."""

from __future__ import annotations

import csv
import io
import json
import time
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from tests.integration.test_diet_api import TOKEN_A, TOKEN_B, payload

from nutripilot.api.app import create_app
from nutripilot.application import build_container
from nutripilot.application.diet_privacy import csv_bytes
from nutripilot.config import Settings
from nutripilot.domain.diet import DietError

NOW = datetime(2026, 11, 12, tzinfo=UTC)
OWNER = {"Authorization": f"Bearer {TOKEN_A}"}
OTHER = {"Authorization": f"Bearer {TOKEN_B}"}


def headers(version: int | None = None) -> dict[str, str]:
    return {
        **OWNER,
        "Idempotency-Key": uuid4().hex,
        **({"If-Match": f'"{version}"'} if version is not None else {}),
    }


def approve(client: TestClient, preview: dict, *, purge: bool = False):
    prefix = "/purge" if purge else ""
    return client.post(
        f"/api/v1/diet{prefix}/previews/{preview['id']}/confirm",
        json={
            "approved": True,
            "preview_version": preview["version"],
            "content_hash": preview.get("preview_hash", preview.get("content_hash")),
        },
        headers=headers(preview["version"]),
    )


def finished(client: TestClient, job: dict) -> dict:
    for _ in range(250):
        response = client.get(f"/api/v1/diet/jobs/{job['id']}", headers=OWNER)
        assert response.status_code == 200, response.text
        result = response.json()
        if result["state"] not in ("queued", "running"):
            assert result["state"] == "succeeded", result
            return result
        time.sleep(0.02)
    pytest.fail(f"Task did not finish: {job['id']}")


@pytest.fixture
def active(tmp_path: Path):
    container = build_container(
        settings=Settings(
            _env_file=None,
            app_mode="test",
            user_timezone="UTC",
            api_access_tokens={TOKEN_A: 1, TOKEN_B: 2},
            diet_media_root=str(tmp_path / "media"),
            diet_export_root=str(tmp_path / "exports"),
        )
    )
    container.diet.clock = lambda: NOW
    container.diet.intake.clock = lambda: NOW
    container.diet.records.clock = lambda: NOW
    container.diet.personal_foods.clock = lambda: NOW
    container.diet.plans.clock = lambda: NOW
    return container


def test_export_private_manifest_csv_and_epoch_revocation(active) -> None:
    with TestClient(create_app(active)) as client:
        request = payload()
        request["entry"]["items"][0]["food"]["name"] = "=FORMULA()"
        draft = client.post("/api/v1/diet/drafts", json=request, headers=headers()).json()
        assert approve(client, draft).status_code == 200
        response = client.post(
            "/api/v1/diet/exports",
            headers=headers(),
            json={
                "start_date": "2026-11-12",
                "end_date": "2026-11-12",
            },
        )
        assert response.status_code == 202, response.text
        job = finished(client, response.json())
        assert "storage_key" not in json.dumps(job)
        assert "lease_owner" not in job and "request_hash" not in job
        url = f"/api/v1/diet/exports/{job['id']}/content"
        assert client.get(url, headers=OTHER).status_code == 404
        assert client.get(url).status_code == 401
        downloaded = client.get(url, headers=OWNER)
        assert downloaded.status_code == 200, downloaded.text
        assert downloaded.headers["cache-control"] == "no-store"
        with zipfile.ZipFile(io.BytesIO(downloaded.content)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["record_count"] == 1 and manifest["complete"]
            for name, meta in manifest["files"].items():
                import hashlib

                assert hashlib.sha256(archive.read(name)).hexdigest() == meta["sha256"]
            rows = list(csv.DictReader(io.StringIO(archive.read("items.csv").decode("utf-8-sig"))))
            assert rows[0]["name"] == "'=FORMULA()"
            assert (
                json.loads(archive.read("sources.json"))["logs"][0]["items"][0]["name_snapshot"]
                == "=FORMULA()"
            )
        active.diet.clock = lambda: NOW + timedelta(hours=25)
        assert client.get(url, headers=OWNER).status_code == 410
        active.diet.clock = lambda: NOW
        changed = client.post("/api/v1/diet/drafts", json=payload(), headers=headers()).json()
        assert approve(client, changed).status_code == 200
        assert client.get(url, headers=OWNER).status_code == 410
        assert client.get("/api/v1/diet/jobs", headers=OTHER).json()["items"] == []


def test_purge_destroys_copies_rejects_replay_and_has_dedicated_approval(active) -> None:
    with TestClient(create_app(active)) as client:
        draft = client.post("/api/v1/diet/drafts", json=payload(), headers=headers()).json()
        saved = approve(client, draft).json()
        record_id = saved["result_refs"][0]["id"]
        response = client.post(
            "/api/v1/diet/purge/previews", json={"record_ids": [record_id]}, headers=headers()
        )
        assert response.status_code == 422
        deletion = client.post(
            "/api/v1/diet/previews",
            json={
                "operation": "log_delete",
                "arguments": {
                    "action": "delete",
                    "record_id": record_id,
                    "base_version": 1,
                    "evidence": payload()["evidence"],
                },
            },
            headers=headers(1),
        ).json()
        assert approve(client, deletion).status_code == 200
        preview = client.post(
            "/api/v1/diet/purge/previews", json={"record_ids": [record_id]}, headers=headers()
        ).json()
        assert "历史聊天" in preview["content"]["history_notice"]
        assert approve(client, preview).status_code == 422
        confirmation = approve(client, preview, purge=True)
        assert confirmation.status_code == 202, confirmation.text
        completed = finished(client, confirmation.json())
        assert approve(client, preview, purge=True).json()["id"] == completed["id"]
        assert client.get(f"/api/v1/diet/logs/{record_id}", headers=OWNER).status_code == 410
        assert client.get(f"/api/v1/diet/logs/{record_id}", headers=OTHER).status_code == 404
        assert approve(client, draft).status_code == 410
        assert approve(client, deletion).status_code == 410
        assert client.get(f"/api/v1/diet/drafts/{draft['id']}", headers=OWNER).status_code == 410
        data = active.diet.store._users[1]
        assert not data.logs and not data.drafts and not data.revisions
        assert len(data.tombstones) >= 3


def test_image_upload_owner_boundary_no_implicit_model_consent(active) -> None:
    from tests.unit.test_diet_media import image_bytes

    with TestClient(create_app(active)) as client:
        uploaded = client.post(
            "/api/v1/diet/media",
            headers=headers(),
            files={
                "files": ("meal.png", image_bytes(), "image/png"),
            },
        )
        assert uploaded.status_code == 201, uploaded.text
        asset = uploaded.json()["items"][0]
        assert "storage_key" not in asset
        url = f"/api/v1/diet/media/{asset['id']}/content"
        assert client.get(url, headers=OWNER).status_code == 200
        assert client.get(url, headers=OTHER).status_code == 404
        assert (
            client.delete(
                f"/api/v1/diet/media/{asset['id']}",
                headers={
                    **OTHER,
                    "Idempotency-Key": "delete-other",
                },
            ).status_code
            == 404
        )
        stolen = client.post(
            "/api/v1/diet/drafts",
            headers={
                **OTHER,
                "Idempotency-Key": "other-image",
            },
            json={"mode": "image", "media_ids": [asset["id"]]},
        )
        assert stolen.status_code == 404
        draft_response = client.post(
            "/api/v1/diet/drafts",
            headers=headers(),
            json={
                "mode": "image",
                "media_ids": [asset["id"]],
                "send_images_authorized": False,
            },
        )
        assert draft_response.status_code == 201, draft_response.text
        draft = draft_response.json()
        assert draft["job_id"] is None
        assert client.get("/api/v1/diet/jobs", headers=OWNER).json()["items"] == []
        assert (
            client.delete(f"/api/v1/diet/media/{asset['id']}", headers=headers()).status_code == 409
        )
        denied = client.post(
            f"/api/v1/diet/drafts/{draft['id']}/parse",
            headers=headers(1),
            json={
                "kind": "image",
                "media_ids": [asset["id"]],
                "send_images_authorized": False,
            },
        )
        assert denied.status_code == 403, denied.text
        invalid = client.post(
            "/api/v1/diet/media",
            headers=headers(),
            files={
                "files": ("fake.png", b"not-a-png", "image/png"),
            },
        )
        assert invalid.status_code == 415


def test_batch_upload_is_idempotent_atomic_and_never_revives_removed_media(active) -> None:
    from tests.unit.test_diet_media import image_bytes

    with TestClient(create_app(active)) as client:
        key = {**OWNER, "Idempotency-Key": "batch"}
        files = [
            ("files", ("one.png", image_bytes(), "image/png")),
            ("files", ("two.jpg", image_bytes("JPEG"), "image/jpeg")),
        ]
        first = client.post("/api/v1/diet/media", headers=key, files=files)
        assert first.status_code == 201, first.text
        assert client.post("/api/v1/diet/media", headers=key, files=files).json() == first.json()
        assert len(list(active.diet_parsing.media.root.glob("*.png"))) == 2
        changed = client.post("/api/v1/diet/media", headers=key, files=files[:1])
        assert changed.status_code == 409
        invalid = client.post(
            "/api/v1/diet/media",
            headers=headers(),
            files=[
                files[0],
                ("files", ("bad.png", b"broken", "image/png")),
            ],
        )
        assert invalid.status_code == 415
        assert len(list(active.diet_parsing.media.root.glob("*.png"))) == 2
        media_id = first.json()["items"][0]["id"]
        assert client.delete(f"/api/v1/diet/media/{media_id}", headers=headers()).status_code == 202
        assert client.post("/api/v1/diet/media", headers=key, files=files).status_code == 410
        assert len(list(active.diet_parsing.media.root.glob("*.png"))) == 1


@pytest.mark.parametrize("value", ["=1", "+cmd", "-1", "@x", "  =1", "\ttext", "\ntext"])
def test_csv_formula_injection(value: str) -> None:
    row = next(csv.DictReader(io.StringIO(csv_bytes([{"text": value}]).decode("utf-8-sig"))))
    assert row["text"] == "'" + value


@pytest.mark.asyncio
async def test_retention_cleans_expired_private_data_and_preserves_committed_drafts(active) -> None:
    from tests.unit.test_diet_media import image_bytes

    from nutripilot.application.diet_parsing import ImageDraft, TextDraft
    from nutripilot.application.diet_privacy import DietPrivacyService
    from nutripilot.application.runtime import start_container, stop_container
    from nutripilot.domain.diet import SubmissionAnchor
    from nutripilot.tools.local.diet_models import CreateIntake

    await start_container(active)
    try:
        await active.chat_worker.close()
        await active.chat_worker_task
        parser = active.diet_parsing
        assert parser
        asset = await parser.upload(1, image_bytes(), "image/png", "intake_image")
        draft = await parser.create(1, ImageDraft(mode="image", media_ids=[asset.id]), key="photo")
        text = await parser.create(1, TextDraft(mode="text", text="private text"), key="text")
        request = payload()
        request.pop("mode")
        request.pop("channel")
        request["action"] = "create"
        committed = await active.diet.intake.prepare(
            1,
            CreateIntake.model_validate(request),
            idempotency_key="committed",
            anchor=SubmissionAnchor.capture(NOW, "UTC"),
        )
        await active.diet.confirm(1, committed.id, version=1, approved_hash=committed.preview_hash)
        assert parser.media.read(asset.storage_key)
        active.diet.clock = lambda: NOW + timedelta(hours=25)
        privacy = DietPrivacyService(parser)
        await privacy.cleanup()
        async with active.diet.store.transaction(1, default_timezone="UTC") as tx:
            assert await tx.get_draft(draft.id) is None
            assert await tx.get_tombstone("draft", draft.id)
            assert await tx.get_draft(text.id) is None
            assert (await tx.get_draft(committed.id)).state == "committed"
            cleaned = await tx.get_media(asset.id)
            assert cleaned.state == "purged"
        assert not (parser.media.root / asset.storage_key).exists()
        await privacy.cleanup()
        active.diet.records.clock = active.diet.clock
        async with active.diet.store.transaction(1, default_timezone="UTC") as tx:
            log = (await tx.list_logs(NOW.date(), NOW.date()))[0]
        deletion = await active.diet.records.prepare_delete_restore(
            1, log.id, base_version=log.version, action="delete", idempotency_key="retention-delete"
        )
        await active.diet.confirm(1, deletion.id, version=1, approved_hash=deletion.content_hash)
        deleted_at = active.diet.clock()
        active.diet.clock = lambda: deleted_at + timedelta(days=7, seconds=-1)
        for _ in range(2):
            await privacy.cleanup()
        assert (await active.diet.records.get_log(1, log.id)).deleted_at is not None
        active.diet.clock = lambda: deleted_at + timedelta(days=7)
        for _ in range(2):
            await privacy.cleanup()
        with pytest.raises(DietError) as exc:
            await active.diet.records.get_log(1, log.id)
        assert exc.value.code == "RESOURCE_PURGED"
        assert not active.diet.store._users[1].revisions
    finally:
        await stop_container(active)
