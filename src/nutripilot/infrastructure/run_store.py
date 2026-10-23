"""Durable local trace and evaluation records for a single API instance."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

from nutripilot.evaluation.models import EvalReport
from nutripilot.tracing.models import TraceEvent


class SQLiteRunStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS trace_events (
                    trace_id TEXT NOT NULL,
                    step_order INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (trace_id, step_order)
                );
                CREATE TABLE IF NOT EXISTS evaluation_reports (
                    run_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );
            """)
        self.path.chmod(0o600)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path, timeout=5)) as connection, connection:
            yield connection

    def append_trace(self, fields: dict[str, Any]) -> TraceEvent:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT COALESCE(MAX(step_order), 0) FROM trace_events WHERE trace_id=?",
                (fields["trace_id"],),
            ).fetchone()
            event = TraceEvent(step_order=int(row[0]) + 1, **fields)
            connection.execute(
                "INSERT INTO trace_events VALUES (?, ?, ?)",
                (event.trace_id, event.step_order, event.model_dump_json()),
            )
        return event

    def get_trace(self, trace_id: str) -> list[TraceEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM trace_events WHERE trace_id=? ORDER BY step_order",
                (trace_id,),
            )
            return [TraceEvent.model_validate_json(row[0]) for row in rows]

    def save_report(self, report: EvalReport) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO evaluation_reports VALUES (?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET payload=excluded.payload",
                (report.run_id, report.model_dump_json(exclude_computed_fields=True)),
            )

    def get_report(self, run_id: str) -> EvalReport | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM evaluation_reports WHERE run_id=?", (run_id,)
            ).fetchone()
        return EvalReport.model_validate_json(row[0]) if row else None

    def interrupt_running_reports(self) -> None:
        with self._connect() as connection:
            rows = connection.execute("SELECT payload FROM evaluation_reports").fetchall()
            for row in rows:
                report = EvalReport.model_validate_json(row[0])
                if report.status == "running":
                    interrupted = report.model_copy(
                        update={
                            "status": "interrupted",
                            "metrics": {**report.metrics, "process_interrupted": 1.0},
                        }
                    )
                    connection.execute(
                        "UPDATE evaluation_reports SET payload=? WHERE run_id=?",
                        (interrupted.model_dump_json(exclude_computed_fields=True), report.run_id),
                    )
