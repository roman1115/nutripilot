"""Independent deletion intents survive restoration of an older PostgreSQL backup."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

from nutripilot.domain.diet import DietError
from nutripilot.domain.diet_jobs import MediaAsset
from nutripilot.domain.diet_privacy import PurgeTombstone
from nutripilot.infrastructure.diet_media import PrivateFiles


class DeletionJournal:
    def __init__(self, root: Path) -> None:
        self.files = PrivateFiles(root)
        self.path = self.files.root / "deletions.sqlite3"
        if self.path.is_symlink():
            raise DietError("UNSAFE_MEDIA_STORAGE", "Deletion journal cannot use symlinks.")
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS deletions("
                "user_id INTEGER NOT NULL,kind TEXT NOT NULL,id TEXT NOT NULL,"
                "payload TEXT NOT NULL,PRIMARY KEY(user_id,kind,id))"
            )
        self.path.chmod(0o600)

    def assert_ready(self) -> None:
        if (self.files.root / "restore_pending").exists():
            raise DietError("RESTORE_PENDING", "恢复数据尚未完成删除日志重放，禁止开放访问。")

    def append(self, tombstones: list[PurgeTombstone], media: list[MediaAsset]) -> None:
        with closing(sqlite3.connect(self.path, timeout=15)) as connection, connection:
            connection.execute("PRAGMA synchronous=FULL")
            connection.executemany(
                "INSERT INTO deletions VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
                [
                    (row.user_id, row.entity_type, row.entity_id, row.model_dump_json())
                    for row in tombstones
                ]
                + [
                    (
                        row.user_id,
                        "media",
                        str(row.id),
                        # No food text, original image or model payload belongs in this ledger.
                        row.model_dump_json(),
                    )
                    for row in media
                ],
            )

    def entries(self) -> Iterator[PurgeTombstone | MediaAsset]:
        with closing(sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True)) as connection:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise DietError("DELETION_JOURNAL_CORRUPTED", "删除日志损坏，禁止开放恢复数据。")
            for kind, body in connection.execute(
                "SELECT kind,payload FROM deletions ORDER BY (kind='media'),user_id,kind,id"
            ):
                yield (
                    MediaAsset.model_validate_json(body)
                    if kind == "media"
                    else PurgeTombstone.model_validate_json(body)
                )
