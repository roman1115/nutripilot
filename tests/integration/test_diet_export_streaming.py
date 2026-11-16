"""Batching, publication failures and orphan collection exercise the actual storage path."""

import csv
import hashlib
import io
import json
import os
import zipfile
from uuid import uuid4

import pytest
from tests.integration.test_diet_persistence import ANCHOR, intake_request
from tests.integration.test_diet_purge_export import NOW
from tests.integration.test_diet_purge_export import active as active
from tests.unit.test_diet_media import image_bytes

from nutripilot.application.diet_parsing import DietParsingService
from nutripilot.application.diet_privacy import DietPrivacyService
from nutripilot.domain.diet import DietError
from nutripilot.domain.diet_jobs import ExportRequest
from nutripilot.infrastructure import diet_export
from nutripilot.infrastructure.background_jobs import BackgroundJobStore
from nutripilot.infrastructure.chat_tasks import ChatTaskStore
from nutripilot.infrastructure.diet_store import MemoryDietTransaction
from nutripilot.model_gateway import FakeModelGateway


def privacy(active, tmp_path) -> DietPrivacyService:
    tasks = ChatTaskStore.create(f"sqlite:///{tmp_path / 'jobs.sqlite3'}", "default")
    tasks.initialize()
    jobs = BackgroundJobStore(tasks)
    jobs.initialize()
    parser = DietParsingService(active.diet, jobs, FakeModelGateway(), active.settings, None)
    return DietPrivacyService(parser)


@pytest.mark.asyncio
async def test_export_batches_sources_csv_and_checksum_without_snapshot(
    active, tmp_path, monkeypatch
) -> None:
    manager = privacy(active, tmp_path)
    try:
        for index in range(101):
            draft = await active.diet.intake.prepare(
                1, intake_request(), idempotency_key=str(index), anchor=ANCHOR
            )
            await active.diet.confirm(1, draft.id, version=1, approved_hash=draft.preview_hash)

        async def forbidden(*args, **kwargs):
            raise AssertionError("export must not load an entire snapshot")

        monkeypatch.setattr(MemoryDietTransaction, "export_snapshot", forbidden)
        request = ExportRequest(start_date=NOW.date(), end_date=NOW.date())
        job = await manager.submit_export(1, request, "export")
        artifact = await manager.execute_export(job)
        content = manager.files.read(artifact.storage_key)
        assert hashlib.sha256(content).hexdigest() == artifact.checksum
        assert len(content) == artifact.bytes
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            sources = json.loads(archive.read("sources.json"))
            assert len(sources["logs"]) == 101
            csv_data = archive.read("records.csv")
            assert csv_data.count(b"\xef\xbb\xbf") == 1
            rows = list(csv.DictReader(io.StringIO(csv_data.decode("utf-8-sig"))))
            assert len(rows) == len({row["id"] for row in rows}) == 101
            manifest = json.loads(archive.read("manifest.json"))
            for name, metadata in manifest["files"].items():
                assert metadata["bytes"] == len(archive.read(name))
                assert metadata["sha256"] == hashlib.sha256(archive.read(name)).hexdigest()
    finally:
        manager.jobs.chat.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [10, 500])
async def test_failed_archive_does_not_publish_partial_file(
    active, tmp_path, monkeypatch, limit
) -> None:
    manager = privacy(active, tmp_path)
    try:
        monkeypatch.setattr(diet_export, "EXPORT_LIMIT", limit)
        job = await manager.submit_export(
            1, ExportRequest(start_date=NOW.date(), end_date=NOW.date()), "limit"
        )
        with pytest.raises(DietError, match="200MiB"):
            await manager.execute_export(job)
        assert list(manager.files.root.iterdir()) == []
        assert manager.jobs.get(job.id, 1).artifact is None
    finally:
        manager.jobs.chat.close()


@pytest.mark.asyncio
async def test_orphan_collection_checks_authority_and_age_and_preserves_links(
    active, tmp_path, monkeypatch
) -> None:
    manager = privacy(active, tmp_path)
    try:
        files = manager.parser.media
        asset = await manager.parser.upload(1, image_bytes(), "image/png", "intake_image")
        old = NOW.timestamp() - 25 * 3600
        os.utime(files.root / asset.storage_key, (old, old))
        keys = [files.write(b"unreferenced", "png") for _ in range(102)]
        for key in keys:
            os.utime(files.root / key, (old, old))
        temporary = files.root / f"{uuid4().hex}.tmp"
        temporary.touch()
        os.utime(temporary, (old, old))
        young = files.write(b"young", "png")
        linked = files.root / f"{uuid4().hex}.png"
        linked.symlink_to(files.root / young)
        export = manager.files.write(b"orphan", "zip")
        os.utime(manager.files.root / export, (old, old))
        for _ in range(3):
            await manager.cleanup()
        assert (files.root / asset.storage_key).exists()
        assert (files.root / young).exists() and linked.is_symlink()
        assert not temporary.exists()
        assert all(not (files.root / key).exists() for key in keys)
        assert not (manager.files.root / export).exists()
        orphan = files.write(b"keep on unknown commit", "png")
        os.utime(files.root / orphan, (old, old))

        async def unavailable(key):
            raise ConnectionError("database unavailable")

        monkeypatch.setattr(active.diet.store, "media_key_exists", unavailable)
        with pytest.raises(ConnectionError):
            await manager.cleanup()
        assert (files.root / orphan).exists()
    finally:
        manager.jobs.chat.close()
