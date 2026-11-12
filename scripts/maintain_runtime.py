"""Back up local records or preview age-based observability cleanup."""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path


def backup_database(source: Path, destination: Path) -> None:
    """Use SQLite's snapshot API so WAL contents are included, without overwriting files."""

    source = source.resolve(strict=True)
    destination = destination.resolve()
    if source == destination:
        raise ValueError("Backup destination must differ from the source")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb"):
        pass
    try:
        destination.chmod(0o600)
        with (
            closing(sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)) as reader,
            closing(sqlite3.connect(destination)) as writer,
        ):
            if reader.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Source database integrity check failed")
            reader.backup(writer)
            if writer.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Backup database integrity check failed")
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def prune_records(source: Path, cutoff: datetime, *, apply: bool = False) -> dict[str, int]:
    """Delete only old finished traces/reports. Never delete conversations or active work."""

    path = source.resolve(strict=True)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        trace_ids = [
            row[0]
            for row in connection.execute(
                "SELECT trace_id FROM trace_events GROUP BY trace_id "
                "HAVING julianday(MAX(json_extract(payload, '$.created_at'))) < julianday(?) "
                "AND EXISTS (SELECT 1 FROM trace_events last "
                "WHERE last.trace_id=trace_events.trace_id "
                "AND last.step_order=(SELECT MAX(step_order) FROM trace_events latest "
                "WHERE latest.trace_id=trace_events.trace_id) "
                "AND json_extract(last.payload, '$.phase_after') "
                "IN ('COMPLETED','FAILED','BLOCKED','CLARIFY'))",
                (cutoff.isoformat(),),
            )
        ]
        run_ids = [
            row[0]
            for row in connection.execute(
                "SELECT run_id FROM evaluation_reports "
                "WHERE julianday(json_extract(payload, '$.created_at')) < julianday(?) "
                "AND json_extract(payload, '$.status') IN ('completed','interrupted','failed')",
                (cutoff.isoformat(),),
            )
        ]
        if apply:
            connection.executemany(
                "DELETE FROM trace_events WHERE trace_id=?", ((value,) for value in trace_ids)
            )
            connection.executemany(
                "DELETE FROM evaluation_reports WHERE run_id=?", ((value,) for value in run_ids)
            )
    return {"traces": len(trace_ids), "evaluation_reports": len(run_ids)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("backup", "restore"):
        command = subparsers.add_parser(name)
        command.add_argument("--source", type=Path, required=True)
        command.add_argument("--destination", type=Path, required=True)
    prune = subparsers.add_parser("prune")
    prune.add_argument("--source", type=Path, required=True)
    prune.add_argument("--days", type=int, default=30)
    prune.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.command in {"backup", "restore"}:
        backup_database(args.source, args.destination)
        print(json.dumps({"status": "verified", "destination": str(args.destination)}))
    else:
        if args.days < 1:
            parser.error("--days must be at least 1")
        counts = prune_records(
            args.source, datetime.now(UTC) - timedelta(days=args.days), apply=args.apply
        )
        print(json.dumps({"applied": args.apply, **counts}))


if __name__ == "__main__":
    main()
