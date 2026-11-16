"""Exercise the offline command orchestration against new disposable databases only."""

import argparse
import os
from datetime import timedelta
from uuid import uuid4

import pytest
from scripts import backup_diet
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from tests.integration.test_diet_persistence import (
    ANCHOR,
    DATABASE_URL,
    NOW,
    UnknownFoodResolver,
    intake_request,
)
from tests.unit.test_diet_media import image_bytes

from nutripilot.application.diet_service import DietService
from nutripilot.application.intake_service import IntakeService
from nutripilot.config import Settings
from nutripilot.domain.diet_jobs import MediaAsset
from nutripilot.infrastructure.diet_deletions import DeletionJournal
from nutripilot.infrastructure.diet_media import PrivateFiles
from nutripilot.infrastructure.diet_schema import (
    DIET_SCHEMA_V1,
    DIET_SCHEMA_V2,
    DIET_SCHEMA_V3,
    DIET_SCHEMA_V4,
)
from nutripilot.infrastructure.diet_store import PostgresDietStore

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("NUTRIPILOT_TEST_EXTERNAL") != "1" or os.getenv("NUTRIPILOT_TEST_BACKUP") != "1",
        reason="Enable external and backup acceptance for disposable-database restore.",
    ),
]


async def test_offline_backup_command_restores_media_and_replays_current_deletions(
    tmp_path,
    monkeypatch,
) -> None:
    admin = create_async_engine(DATABASE_URL, isolation_level="AUTOCOMMIT")
    names = [f"test_diet_cli_{uuid4().hex}" for _ in range(2)]
    engines = [create_async_engine(admin.url.set(database=name)) for name in names]
    created = []
    try:
        async with admin.connect() as connection:
            for name in names:
                await connection.execute(text(f'CREATE DATABASE "{name}"'))
                created.append(name)
        source, target = engines
        async with source.begin() as connection:
            await connection.execute(
                text(
                    "CREATE TABLE meal_plans(id TEXT PRIMARY KEY,user_id BIGINT NOT NULL,"
                    "idempotency_key TEXT NOT NULL,payload JSONB NOT NULL,"
                    "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),UNIQUE(user_id,idempotency_key))"
                )
            )
            for statement in (*DIET_SCHEMA_V1, *DIET_SCHEMA_V2, *DIET_SCHEMA_V3, *DIET_SCHEMA_V4):
                await connection.execute(text(statement))
        journal = DeletionJournal(tmp_path / "journal")
        media = PrivateFiles(tmp_path / "source-media")
        content = image_bytes()
        asset = MediaAsset(
            id=uuid4(),
            user_id=1,
            kind="intake_image",
            storage_key=media.write(content, "png"),
            content_sha256="a" * 64,
            mime="image/png",
            bytes=len(content),
            width=16,
            height=12,
            state="ready",
            created_at=NOW,
            updated_at=NOW,
            expires_at=NOW + timedelta(hours=24),
        )
        store = PostgresDietStore(source, journal)
        service = IntakeService(
            store, UnknownFoodResolver(), default_timezone="UTC", clock=lambda: NOW
        )
        async with store.transaction(1, default_timezone="UTC") as tx:
            await tx.save_media(asset)
        draft = await service.prepare(1, intake_request(), idempotency_key="cli", anchor=ANCHOR)
        draft = draft.model_copy(update={"media_ids": [asset.id], "channel": "image"})
        async with store.transaction(1, default_timezone="UTC") as tx:
            await tx.save_draft(draft)
        saved = await service.confirm(1, draft.id, version=1, approved_hash=draft.preview_hash)
        settings = Settings(
            _env_file=None,
            database_url=source.url.render_as_string(hide_password=False),
            diet_media_root=str(media.root),
            diet_deletion_journal_root=str(journal.files.root),
        )
        monkeypatch.setattr(backup_diet, "Settings", lambda: settings)
        arguments = argparse.Namespace(
            command="backup",
            bundle=tmp_path / "bundle",
            container="nutripilot-postgres-1",
            database_user="nutripilot",
            offline=True,
        )
        assert (await backup_diet.execute(arguments))["status"] == "backup_complete"
        changes = DietService(store, default_timezone="UTC", clock=lambda: NOW)
        deletion = await changes.prepare_delete_restore(
            1, saved.result_refs[0].uuid, base_version=1, action="delete", idempotency_key="delete"
        )
        await changes.confirm(1, deletion.id, version=1, approved_hash=deletion.content_hash)
        async with store.transaction(1, default_timezone="UTC") as tx:
            await tx.purge_log(saved.result_refs[0].uuid, NOW)
            await tx.save_media(asset.model_copy(update={"state": "purge_pending"}))
            await tx.advance_epoch()
        media.delete(asset.storage_key)
        restored_media = PrivateFiles(tmp_path / "restored-media")
        settings = settings.model_copy(
            update={
                "database_url": target.url.render_as_string(hide_password=False),
                "diet_media_root": str(restored_media.root),
            }
        )
        arguments.command = "restore"
        result = await backup_diet.execute(arguments)
        assert result["status"] == "restore_replayed" and result["deletions_replayed"] >= 3
        assert not (journal.files.root / "restore_pending").exists()
        async with PostgresDietStore(target).transaction(1, default_timezone="UTC") as tx:
            assert await tx.get_log(saved.result_refs[0].uuid) is None
            assert await tx.get_tombstone("log", saved.result_refs[0].uuid)
            assert (await tx.get_media(asset.id)).state == "purged"
        assert not list(restored_media.root.iterdir())
        restored_service = IntakeService(
            PostgresDietStore(target),
            UnknownFoodResolver(),
            default_timezone="UTC",
            clock=lambda: NOW,
        )
        replay = await restored_service.confirm(
            1, draft.id, version=1, approved_hash=draft.preview_hash
        )
        assert replay.result_status == "purged"
        with pytest.raises(ValueError, match="must be empty"):
            await backup_diet.execute(arguments)
    finally:
        for engine in engines:
            await engine.dispose()
        async with admin.connect() as connection:
            for name in created:
                await connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        await admin.dispose()
