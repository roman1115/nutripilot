"""Offline media snapshots and deletion replay for PostgreSQL restore rehearsals."""

from __future__ import annotations

import asyncio
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import text

from nutripilot.domain.diet import DietError
from nutripilot.domain.diet_jobs import MediaAsset
from nutripilot.domain.diet_records import ResourceRef
from nutripilot.infrastructure.diet_deletions import DeletionJournal
from nutripilot.infrastructure.diet_media import PrivateFiles, file_sha256
from nutripilot.infrastructure.diet_store import PostgresDietStore, PostgresDietTransaction


def snapshot_media(source: PrivateFiles, destination: Path) -> dict[str, str]:
    destination.mkdir(mode=0o700)
    checksums = {}
    for path in sorted(source.root.iterdir()):
        if path.suffix == ".tmp":
            continue
        PrivateFiles.validate_key(path.name)
        with source.open_read(path.name) as reader, (destination / path.name).open("xb") as writer:
            os.fchmod(writer.fileno(), 0o600)
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
            reader.seek(0)
            checksums[path.name] = file_sha256(reader)
    return checksums


def restore_media(source: Path, checksums: dict[str, str], destination: PrivateFiles) -> None:
    if list(destination.root.iterdir()):
        raise DietError("RESTORE_TARGET_NOT_EMPTY", "媒体恢复目录必须为空。")
    source_files = PrivateFiles(source)
    for key, expected in checksums.items():
        with source_files.open_read(key) as reader:
            if file_sha256(reader) != expected:
                raise DietError("BACKUP_CORRUPTED", "媒体备份校验失败，禁止开放访问。")
    for key in checksums:
        with source_files.open_read(key) as reader, (destination.root / key).open("xb") as writer:
            os.fchmod(writer.fileno(), 0o600)
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())


async def replay_deletions(
    store: PostgresDietStore, journal: DeletionJournal, media: PrivateFiles
) -> int:
    """Call while API and Worker are stopped, using the latest independent journal."""
    store = PostgresDietStore(store.engine)
    count = 0
    for entry in journal.entries():
        async with store.transaction(entry.user_id, default_timezone="UTC") as tx:
            assert isinstance(tx, PostgresDietTransaction)
            connection = tx.connection
            if isinstance(entry, MediaAsset):
                asset = await tx.get_media(entry.id)
                if asset is not None and asset.storage_key != entry.storage_key:
                    raise DietError("RESTORE_CONFLICT", "媒体ID与独立删除日志不一致。")
                if await tx.media_referenced(entry.id):
                    raise DietError("RESTORE_CONFLICT", "待删除媒体仍有引用，禁止开放恢复数据。")
                await asyncio.to_thread(media.delete, entry.storage_key)
                if asset:
                    await tx.save_media(
                        asset.model_copy(
                            update={
                                "state": "purged",
                                "purged_at": entry.purged_at or entry.updated_at,
                                "version": max(asset.version, entry.version) + 1,
                                "updated_at": datetime.now(UTC),
                            }
                        )
                    )
            else:
                kind, identifier = entry.entity_type, entry.entity_id
                table = {
                    "log": "diet_logs",
                    "draft": "intake_drafts",
                    "preview": "diet_change_previews",
                    "plan": "meal_plans",
                    "planned_meal": "planned_meals",
                    "personal_food": "personal_foods",
                    "target": "nutrition_targets",
                }[kind]
                values = {
                    "owner": entry.user_id,
                    "id": identifier,
                    "now": entry.purged_at,
                    "kind": kind,
                    "version": entry.version,
                }
                row = (
                    await connection.execute(
                        text(f"SELECT version FROM {table} WHERE user_id=:owner AND id::TEXT=:id"),
                        values,
                    )
                ).first()
                if kind in ("draft", "preview"):
                    await connection.execute(
                        text(f"DELETE FROM {table} WHERE user_id=:owner AND id::TEXT=:id"), values
                    )
                    await connection.execute(
                        text(
                            "UPDATE operation_receipts SET result_status='purged' "
                            "WHERE user_id=:owner AND preview_id::TEXT=:id"
                        ),
                        values,
                    )
                elif row:
                    if kind != "target":
                        await connection.execute(
                            text(
                                f"UPDATE {table} SET deleted_at=:now "
                                "WHERE user_id=:owner AND id::TEXT=:id"
                            ),
                            values,
                        )
                    await tx.purge_resource(
                        ResourceRef(
                            kind=kind,
                            id=identifier if kind == "plan" else UUID(identifier),
                            version=row.version,
                        ),
                        entry.purged_at,
                    )
                await connection.execute(
                    text(
                        "INSERT INTO diet_purge_tombstones"
                        "(user_id,entity_type,entity_id,version,purged_at) "
                        "VALUES (:owner,:kind,:id,:version,:now) ON CONFLICT DO NOTHING"
                    ),
                    values,
                )
                await tx.advance_epoch(targets_changed=kind == "target")
            count += 1
    # Restored exports are stale even for users without a new deletion.
    async with store.engine.begin() as connection:
        if (
            await connection.execute(text("SELECT to_regclass('background_jobs') IS NOT NULL"))
        ).scalar_one():
            await connection.execute(text("UPDATE background_jobs SET artifact=NULL"))
    return count
