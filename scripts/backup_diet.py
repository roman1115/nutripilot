"""Offline PostgreSQL + private media backup/restore; never restore the current deletion journal."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from nutripilot.config import Settings
from nutripilot.infrastructure.diet_backup import replay_deletions, restore_media, snapshot_media
from nutripilot.infrastructure.diet_deletions import DeletionJournal
from nutripilot.infrastructure.diet_media import PrivateFiles, file_sha256
from nutripilot.infrastructure.diet_store import PostgresDietStore
from nutripilot.paths import project_root


def run_pg(
    args: argparse.Namespace,
    tool: str,
    database: str,
    source: BinaryIO | int,
    target: BinaryIO | int,
) -> None:
    command = [
        "docker",
        "exec",
        "-i",
        args.container,
        tool,
        "-U",
        args.database_user,
        "-d",
        database,
    ]
    if tool == "pg_dump":
        command += ["--format=custom", "--no-owner", "--no-privileges"]
    else:
        command += ["--exit-on-error", "--no-owner", "--no-privileges"]
    completed = subprocess.run(command, stdin=source, stdout=target, stderr=subprocess.PIPE)
    if completed.returncode:
        raise RuntimeError(f"{tool} failed; backup/restore remains closed.")


async def execute(args: argparse.Namespace) -> dict[str, object]:
    settings = Settings()
    engine = create_async_engine(settings.database_url)
    journal_root = (
        Path(settings.diet_deletion_journal_root)
        if settings.diet_deletion_journal_root
        else project_root() / ".runtime/diet_deletions"
    )
    if args.command == "restore" and not (journal_root / "deletions.sqlite3").is_file():
        raise ValueError("Restore requires the latest existing independent deletion journal.")
    journal = DeletionJournal(journal_root)
    media = PrivateFiles(
        Path(settings.diet_media_root)
        if settings.diet_media_root
        else project_root() / ".runtime/diet_media"
    )
    database = engine.url.database
    if not database:
        raise ValueError("DATABASE_URL must name a database.")
    try:
        if args.command == "backup":
            journal.assert_ready()
            args.bundle.mkdir(mode=0o700, parents=False)
            dump = args.bundle / "database.dump"
            with dump.open("xb") as output:
                os.fchmod(output.fileno(), 0o600)
                await asyncio.to_thread(
                    run_pg, args, "pg_dump", database, subprocess.DEVNULL, output
                )
                output.flush()
                os.fsync(output.fileno())
            media_checksums = await asyncio.to_thread(snapshot_media, media, args.bundle / "media")
            with dump.open("rb") as source:
                checksum = file_sha256(source)
            now = datetime.now(UTC)
            manifest = {
                "schema": "diet-backup-v1",
                "database": database,
                "created_at": now.isoformat(),
                "destroy_after": (now + timedelta(days=30)).isoformat(),
                "database_sha256": checksum,
                "media": media_checksums,
                "deletion_journal": "use latest independent journal, never this snapshot",
            }
            with (args.bundle / "manifest.json").open("x", encoding="utf-8") as output:
                os.fchmod(output.fileno(), 0o600)
                json.dump(manifest, output, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            return {"status": "backup_complete", "media_files": len(media_checksums)}
        if args.bundle.is_symlink():
            raise ValueError("Backup bundle cannot be a symlink.")
        if (args.bundle / "manifest.json").is_symlink():
            raise ValueError("Backup manifest cannot be a symlink.")
        manifest = json.loads((args.bundle / "manifest.json").read_text())
        if manifest["schema"] != "diet-backup-v1":
            raise ValueError("Unsupported backup format.")
        if datetime.fromisoformat(manifest["destroy_after"]) <= datetime.now(UTC):
            raise ValueError("Backup retention expired; destroy it under the retention policy.")
        dump = args.bundle / "database.dump"
        if dump.is_symlink():
            raise ValueError("Database dump cannot be a symlink.")
        with dump.open("rb") as source:
            if file_sha256(source) != manifest["database_sha256"]:
                raise ValueError("Database dump checksum mismatch.")
        async with engine.connect() as connection:
            count = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM information_schema.tables "
                        "WHERE table_schema NOT IN ('pg_catalog','information_schema')"
                    )
                )
            ).scalar_one()
            if count:
                raise ValueError("Restore target database must be empty.")
        if list(media.root.iterdir()):
            raise ValueError("Restore target media directory must be empty.")
        pending = journal.files.root / "restore_pending"
        with pending.open("x") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write("Restore and deletion replay must complete before opening traffic.\n")
            output.flush()
            os.fsync(output.fileno())
        with dump.open("rb") as source:
            await asyncio.to_thread(
                run_pg, args, "pg_restore", database, source, subprocess.DEVNULL
            )
        await asyncio.to_thread(restore_media, args.bundle / "media", manifest["media"], media)
        replayed = await replay_deletions(PostgresDietStore(engine), journal, media)
        pending.unlink()
        return {"status": "restore_replayed", "deletions_replayed": replayed}
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("backup", "restore"))
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--container", default="nutripilot-postgres-1")
    parser.add_argument("--database-user", default="nutripilot")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Attest API and Worker are stopped and the source/target is offline.",
    )
    args = parser.parse_args()
    if not args.offline:
        parser.error("Stop API and Worker, then explicitly pass --offline.")
    try:
        print(json.dumps(asyncio.run(execute(args))))
    except Exception as exc:
        # Do not print connection credentials or private paths from driver exceptions.
        raise SystemExit(
            f"Backup/restore failed ({type(exc).__name__}); keep traffic stopped."
        ) from None


if __name__ == "__main__":
    main()
