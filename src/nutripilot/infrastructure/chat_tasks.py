"""Transactional chat history, durable queue and replayable execution events."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

from nutripilot.application.chat_service import ThreadBusyError, ThreadNotFoundError
from nutripilot.application.history import ConversationMessage, ConversationSummary, HistoryMatch
from nutripilot.domain.execution import ExecutionOwnershipLost

ACTIVE = ("queued", "running", "cancelling")
TERMINAL = ("completed", "awaiting_confirmation", "failed", "cancelled")
TRASH_RETENTION_SECONDS = 7 * 24 * 60 * 60

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS chat_workers (
        queue TEXT PRIMARY KEY, heartbeat DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS chat_threads (
        thread_id TEXT PRIMARY KEY, user_id BIGINT NOT NULL, title TEXT NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL, snapshot TEXT)""",
    """CREATE INDEX IF NOT EXISTS chat_threads_owner ON chat_threads(user_id, updated_at)""",
    """CREATE TABLE IF NOT EXISTS chat_messages (
        id TEXT PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES chat_threads(thread_id),
        position BIGINT NOT NULL, payload TEXT NOT NULL, content TEXT NOT NULL,
        UNIQUE(thread_id, position))""",
    """CREATE TABLE IF NOT EXISTS chat_runs (
        id TEXT PRIMARY KEY, queue TEXT NOT NULL,
        thread_id TEXT NOT NULL REFERENCES chat_threads(thread_id), user_id BIGINT NOT NULL,
        request_key TEXT NOT NULL, request_hash TEXT NOT NULL, kind TEXT NOT NULL,
        state TEXT NOT NULL, status TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0,
        attempts INTEGER NOT NULL DEFAULT 0, created_at DOUBLE PRECISION NOT NULL,
        started_at DOUBLE PRECISION, updated_at DOUBLE PRECISION NOT NULL,
        deadline_at DOUBLE PRECISION, reserved_tokens INTEGER NOT NULL DEFAULT 0,
        last_event BIGINT NOT NULL DEFAULT 0, result TEXT, error TEXT,
        UNIQUE(user_id, request_key))""",
    """CREATE UNIQUE INDEX IF NOT EXISTS chat_runs_one_active ON chat_runs(thread_id)
        WHERE status IN ('queued', 'running', 'cancelling')""",
    """CREATE INDEX IF NOT EXISTS chat_runs_queue ON chat_runs(queue, status, created_at)""",
    """CREATE TABLE IF NOT EXISTS chat_events (
        run_id TEXT NOT NULL REFERENCES chat_runs(id), sequence BIGINT NOT NULL,
        event_type TEXT NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY(run_id, sequence))""",
    """CREATE TABLE IF NOT EXISTS chat_thread_trash (
        thread_id TEXT PRIMARY KEY REFERENCES chat_threads(thread_id),
        deleted_at DOUBLE PRECISION NOT NULL, purging BOOLEAN NOT NULL DEFAULT FALSE)""",
    """CREATE INDEX IF NOT EXISTS chat_thread_trash_expiry ON chat_thread_trash(deleted_at)""",
    """CREATE TABLE IF NOT EXISTS chat_history_imports (thread_id TEXT PRIMARY KEY)""",
)


class RequestConflictError(ValueError):
    pass


class QueueFullError(RuntimeError):
    pass


def _json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"),
        default=lambda item: (
            item.model_dump(mode="json") if isinstance(item, BaseModel) else str(item)
        ),
    )


class ChatTaskStore:
    def __init__(self, engine: Engine, queue: str = "default") -> None:
        self.engine = engine
        self.queue = queue
        self.postgres = engine.dialect.name == "postgresql"
        self._mutex = threading.Lock()

    @classmethod
    def create(cls, database_url: str | None = None, queue: str = "default") -> ChatTaskStore:
        if database_url:
            url = database_url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
            if url.startswith("postgresql://"):
                url = url.replace("postgresql://", "postgresql+psycopg://", 1)
            engine = create_engine(url, pool_pre_ping=True, pool_size=12, max_overflow=12)
        else:
            engine = create_engine(
                "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
            )
        return cls(engine, queue)

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        if self.postgres:
            with self.engine.begin() as connection:
                yield connection
        else:
            with self._mutex, self.engine.begin() as connection:
                yield connection

    def initialize(self) -> None:
        with self.transaction() as connection:
            for statement in SCHEMA:
                connection.execute(text(statement))

    def _schedule_lock(self, connection: Connection) -> None:
        if self.postgres:
            connection.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": self._lock_key("schedule")}
            )

    def _lock_key(self, purpose: str) -> int:
        return int.from_bytes(
            hashlib.sha256(f"nutripilot:{self.queue}:{purpose}".encode()).digest()[:8],
            signed=True,
        )

    def worker_lock(self) -> Connection | None:
        if not self.postgres:
            return self.engine.connect()
        connection = self.engine.connect()
        acquired = connection.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": self._lock_key("worker")}
        ).scalar()
        connection.commit()
        if not acquired:
            connection.close()
            return None
        return connection

    def release_worker(self, connection: Connection) -> None:
        try:
            if self.postgres and not connection.invalidated:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": self._lock_key("worker")}
                )
                connection.commit()
        finally:
            connection.close()

    def heartbeat(self, alive: bool = True) -> None:
        with self.transaction() as connection:
            connection.execute(
                text(
                    "INSERT INTO chat_workers(queue,heartbeat) VALUES (:queue,:now) "
                    "ON CONFLICT(queue) DO UPDATE SET heartbeat=excluded.heartbeat"
                ),
                {"queue": self.queue, "now": time.time() if alive else 0},
            )

    def worker_ready(self) -> bool:
        with self.transaction() as connection:
            heartbeat = connection.execute(
                text("SELECT heartbeat FROM chat_workers WHERE queue=:queue"),
                {"queue": self.queue},
            ).scalar()
            return heartbeat is not None and time.time() - heartbeat < 5

    def bundle(self, thread_id: str, user_id: int) -> dict[str, Any]:
        with self.transaction() as connection:
            suffix = " FOR SHARE" if self.postgres else ""
            thread = (
                connection.execute(
                    text(
                        "SELECT * FROM chat_threads WHERE thread_id=:id AND user_id=:user "
                        "AND NOT EXISTS (SELECT 1 FROM chat_thread_trash WHERE thread_id=:id)"
                        + suffix
                    ),
                    {"id": thread_id, "user": user_id},
                )
                .mappings()
                .first()
            )
            if thread is None:
                raise ThreadNotFoundError(thread_id)
            row = (
                connection.execute(
                    text(
                        "SELECT * FROM chat_runs WHERE thread_id=:id AND queue=:queue "
                        "ORDER BY created_at DESC,id DESC LIMIT 1"
                    ),
                    {"id": thread_id, "queue": self.queue},
                )
                .mappings()
                .first()
            )
            messages = (
                connection.execute(
                    text("SELECT payload FROM chat_messages WHERE thread_id=:id ORDER BY position"),
                    {"id": thread_id},
                )
                .scalars()
                .all()
            )
            return {
                "result": json.loads(thread["snapshot"]) if thread["snapshot"] else None,
                "run": self._public(dict(row)) if row else None,
                "messages": [json.loads(item) for item in messages],
            }

    @staticmethod
    def _owner(connection: Connection, user_id: int, thread_id: str) -> None:
        owner = connection.execute(
            text(
                "SELECT user_id FROM chat_threads WHERE thread_id=:id "
                "AND NOT EXISTS (SELECT 1 FROM chat_thread_trash WHERE thread_id=:id)"
            ), {"id": thread_id}
        ).scalar()
        if owner is None or owner != user_id:
            raise ThreadNotFoundError(thread_id)

    def _append(
        self, connection: Connection, user_id: int, thread_id: str, message: ConversationMessage
    ) -> None:
        now = message.created_at.timestamp()
        inserted_thread = connection.execute(
            text(
                "INSERT INTO chat_threads(thread_id,user_id,title,updated_at) "
                "VALUES (:id,:user,:title,:now) ON CONFLICT(thread_id) DO NOTHING "
                "RETURNING thread_id"
            ),
            {"id": thread_id, "user": user_id, "title": message.content[:80], "now": now},
        ).scalar()
        if inserted_thread and connection.execute(
            text("SELECT 1 FROM chat_history_imports WHERE thread_id=:id"), {"id": thread_id}
        ).first():
            raise ThreadNotFoundError(thread_id)
        if self.postgres:
            connection.execute(
                text("SELECT thread_id FROM chat_threads WHERE thread_id=:id FOR UPDATE"),
                {"id": thread_id},
            )
        self._owner(connection, user_id, thread_id)
        existing = connection.execute(
            text("SELECT thread_id,payload FROM chat_messages WHERE id=:id"), {"id": message.id},
        ).first()
        if existing is not None:
            if existing[0] != thread_id or ConversationMessage.model_validate_json(
                existing[1],
            ).model_dump(exclude={"position"}) != message.model_dump(exclude={"position"}):
                raise RequestConflictError("消息编号已被不同内容占用。")
            return
        position = connection.execute(
            text("SELECT COALESCE(MAX(position),0)+1 FROM chat_messages WHERE thread_id=:id"),
            {"id": thread_id},
        ).scalar_one()
        connection.execute(
            text("INSERT INTO chat_messages VALUES (:id,:thread,:position,:payload,:content)"),
            {
                "id": message.id,
                "thread": thread_id,
                "position": position,
                "payload": message.model_dump_json(),
                "content": message.content,
            },
        )
        connection.execute(
            text("UPDATE chat_threads SET updated_at=:now WHERE thread_id=:id"),
            {"now": now, "id": thread_id},
        )

    def append(self, user_id: int, thread_id: str, message: ConversationMessage) -> None:
        with self.transaction() as connection:
            self._append(connection, user_id, thread_id, message)

    def list_threads(
        self, user_id: int, query: str, limit: int, offset: int, *, trashed: bool = False
    ) -> list[ConversationSummary]:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        visibility = (
            "t.deleted_at > :cutoff AND t.purging=FALSE"
            if trashed else "t.thread_id IS NULL"
        )
        ordering = "t.deleted_at" if trashed else "c.updated_at"
        with self.transaction() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT c.thread_id,c.title,c.updated_at,t.deleted_at,COUNT(m.id) AS count "
                        "FROM chat_threads c JOIN chat_messages m ON m.thread_id=c.thread_id "
                        "LEFT JOIN chat_thread_trash t ON t.thread_id=c.thread_id "
                        f"WHERE c.user_id=:user AND {visibility} AND (:q='' OR EXISTS "
                        "(SELECT 1 FROM chat_messages s WHERE s.thread_id=c.thread_id "
                        "AND LOWER(s.content) LIKE LOWER(:pattern) ESCAPE '\\')) "
                        "GROUP BY c.thread_id,c.title,c.updated_at,t.deleted_at "
                        f"ORDER BY {ordering} DESC,c.thread_id LIMIT :limit OFFSET :offset"
                    ),
                    {
                        "user": user_id,
                        "q": query,
                        "pattern": f"%{escaped}%",
                        "limit": limit,
                        "offset": offset,
                        "cutoff": time.time() - TRASH_RETENTION_SECONDS,
                    },
                )
                .mappings()
                .all()
            )
            items = []
            for row in rows:
                status = connection.execute(
                    text(
                        "SELECT status FROM chat_runs WHERE thread_id=:id "
                        "ORDER BY created_at DESC,id DESC LIMIT 1"
                    ),
                    {"id": row["thread_id"]},
                ).scalar()
                items.append(
                    ConversationSummary(
                        thread_id=row["thread_id"],
                        title=row["title"],
                        updated_at=datetime.fromtimestamp(row["updated_at"], UTC),
                        message_count=row["count"],
                        run_status=status,
                        deleted_at=(
                            datetime.fromtimestamp(row["deleted_at"], UTC)
                            if row["deleted_at"] is not None else None
                        ),
                        expires_at=(
                            datetime.fromtimestamp(
                                row["deleted_at"] + TRASH_RETENTION_SECONDS, UTC
                            ) if row["deleted_at"] is not None else None
                        ),
                    )
                )
            return items

    def messages(
        self, user_id: int, thread_id: str, limit: int, offset: int
    ) -> list[ConversationMessage]:
        with self.transaction() as connection:
            self._owner(connection, user_id, thread_id)
            rows = (
                connection.execute(
                    text(
                        "SELECT payload,position FROM chat_messages WHERE thread_id=:id "
                        "ORDER BY position LIMIT :limit OFFSET :offset"
                    ),
                    {"id": thread_id, "limit": limit, "offset": offset},
                )
                .all()
            )
        return [
            ConversationMessage.model_validate_json(row[0]).model_copy(
                update={"position": row[1]}
            ) for row in rows
        ]

    def after_position(
        self, user_id: int, thread_id: str, after: int, limit: int
    ) -> list[ConversationMessage]:
        with self.transaction() as connection:
            self._owner(connection, user_id, thread_id)
            rows = connection.execute(
                text(
                    "SELECT payload,position FROM chat_messages "
                    "WHERE thread_id=:id AND position>:after ORDER BY position LIMIT :limit"
                ),
                {"id": thread_id, "after": after, "limit": limit},
            ).all()
        return [
            ConversationMessage.model_validate_json(row[0]).model_copy(
                update={"position": row[1]}
            ) for row in rows
        ]

    def search_messages(
        self, user_id: int, thread_id: str | None, query: str, limit: int
    ) -> list[HistoryMatch]:
        if not query.strip():
            return []
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self.transaction() as connection:
            if thread_id is not None:
                self._owner(connection, user_id, thread_id)
            rows = connection.execute(
                text(
                    "SELECT m.thread_id,m.payload,m.position FROM chat_messages m "
                    "JOIN chat_threads t ON t.thread_id=m.thread_id WHERE t.user_id=:user "
                    "AND NOT EXISTS (SELECT 1 FROM chat_thread_trash x "
                    "WHERE x.thread_id=t.thread_id) "
                    + ("AND t.thread_id=:thread " if thread_id is not None else "")
                    + "AND m.content LIKE :query ESCAPE '\\' "
                    "ORDER BY t.updated_at DESC,m.position DESC LIMIT :limit"
                ),
                {"user": user_id, "thread": thread_id, "query": f"%{escaped}%", "limit": limit},
            ).all()
        return [
            HistoryMatch(thread_id=row[0], message=ConversationMessage.model_validate_json(
                row[1],
            ).model_copy(update={"position": row[2]})) for row in rows
        ]

    def delete_thread(self, user_id: int, thread_id: str) -> None:
        with self.transaction() as connection:
            suffix = " FOR UPDATE" if self.postgres else ""
            thread = connection.execute(
                text(
                    "SELECT user_id FROM chat_threads WHERE thread_id=:id" + suffix
                ),
                {"id": thread_id},
            ).scalar()
            if thread is None or thread != user_id:
                raise ThreadNotFoundError(thread_id)
            active = connection.execute(
                text(
                    "SELECT 1 FROM chat_runs WHERE thread_id=:id "
                    "AND status IN ('queued','running','cancelling') LIMIT 1"
                ),
                {"id": thread_id},
            ).first()
            if active:
                raise ThreadBusyError("会话仍有任务正在执行，完成或停止后才能删除。")
            connection.execute(
                text(
                    "INSERT INTO chat_thread_trash(thread_id,deleted_at) VALUES (:id,:now) "
                    "ON CONFLICT(thread_id) DO NOTHING"
                ),
                {"id": thread_id, "now": time.time()},
            )

    def restore_thread(self, user_id: int, thread_id: str) -> None:
        with self.transaction() as connection:
            suffix = " FOR UPDATE OF c,t" if self.postgres else ""
            row = connection.execute(
                text(
                    "SELECT t.deleted_at,t.purging FROM chat_threads c "
                    "JOIN chat_thread_trash t ON t.thread_id=c.thread_id "
                    "WHERE c.thread_id=:id AND c.user_id=:user" + suffix
                ),
                {"id": thread_id, "user": user_id},
            ).mappings().first()
            if (
                row is None or row["purging"]
                or row["deleted_at"] <= time.time() - TRASH_RETENTION_SECONDS
            ):
                raise ThreadNotFoundError(thread_id)
            connection.execute(
                text("DELETE FROM chat_thread_trash WHERE thread_id=:id"), {"id": thread_id}
            )

    def claim_expired_threads(self, limit: int = 100) -> list[str]:
        with self.transaction() as connection:
            suffix = " FOR UPDATE SKIP LOCKED" if self.postgres else ""
            rows = connection.execute(
                text(
                    "SELECT thread_id FROM chat_thread_trash WHERE deleted_at<=:cutoff "
                    "ORDER BY deleted_at,thread_id LIMIT :limit" + suffix
                ),
                {"cutoff": time.time() - TRASH_RETENTION_SECONDS, "limit": limit},
            ).scalars().all()
            for thread_id in rows:
                connection.execute(
                    text("UPDATE chat_thread_trash SET purging=TRUE WHERE thread_id=:id"),
                    {"id": thread_id},
                )
            return list(rows)

    def purge_thread(self, thread_id: str) -> None:
        with self.transaction() as connection:
            suffix = " FOR UPDATE" if self.postgres else ""
            connection.execute(
                text("SELECT thread_id FROM chat_threads WHERE thread_id=:id" + suffix),
                {"id": thread_id},
            ).first()
            purging = connection.execute(
                text("SELECT purging FROM chat_thread_trash WHERE thread_id=:id" + suffix),
                {"id": thread_id},
            ).scalar()
            if not purging:
                return
            # Retire the opaque ID so an old URL/import cannot recreate a purged thread.
            connection.execute(
                text(
                    "INSERT INTO chat_history_imports(thread_id) VALUES (:id) "
                    "ON CONFLICT(thread_id) DO NOTHING"
                ),
                {"id": thread_id},
            )
            connection.execute(
                text(
                    "DELETE FROM chat_events WHERE run_id IN "
                    "(SELECT id FROM chat_runs WHERE thread_id=:id)"
                ),
                {"id": thread_id},
            )
            connection.execute(text("DELETE FROM chat_runs WHERE thread_id=:id"), {"id": thread_id})
            connection.execute(
                text("DELETE FROM chat_messages WHERE thread_id=:id"), {"id": thread_id}
            )
            connection.execute(
                text("DELETE FROM chat_thread_trash WHERE thread_id=:id"), {"id": thread_id}
            )
            connection.execute(
                text("DELETE FROM chat_threads WHERE thread_id=:id"), {"id": thread_id}
            )

    def submit(
        self,
        *,
        user_id: int,
        thread_id: str,
        request_key: str,
        request_hash: str,
        state: dict[str, Any],
        kind: str,
        message: ConversationMessage,
        max_queued: int,
    ) -> dict[str, Any]:
        now = time.time()
        try:
            with self.transaction() as connection:
                self._schedule_lock(connection)
                existing = (
                    connection.execute(
                        text("SELECT * FROM chat_runs WHERE user_id=:user AND request_key=:key"),
                        {"user": user_id, "key": request_key},
                    )
                    .mappings()
                    .first()
                )
                if existing:
                    if existing["request_hash"] != request_hash or existing["queue"] != self.queue:
                        raise RequestConflictError("同一请求编号不能提交不同内容。")
                    self._owner(connection, user_id, thread_id)
                    return self._public(dict(existing))
                count = connection.execute(
                    text(
                        "SELECT COUNT(*) FROM chat_runs WHERE queue=:queue AND user_id=:user "
                        "AND status IN ('queued','running','cancelling')"
                    ),
                    {"queue": self.queue, "user": user_id},
                ).scalar_one()
                if count >= max_queued:
                    raise QueueFullError("待处理任务已达上限，请等待完成或停止部分任务。")
                owner = connection.execute(
                    text("SELECT user_id FROM chat_threads WHERE thread_id=:id"),
                    {"id": thread_id},
                ).scalar()
                if owner is not None and owner != user_id:
                    raise ThreadNotFoundError(thread_id)
                busy = connection.execute(
                    text(
                        "SELECT id FROM chat_runs WHERE thread_id=:id "
                        "AND status IN ('queued','running','cancelling')"
                    ),
                    {"id": thread_id},
                ).scalar()
                if busy:
                    raise ThreadBusyError("该会话已有请求正在执行。")
                if kind == "confirmation":
                    snapshot = connection.execute(
                        text("SELECT snapshot FROM chat_threads WHERE thread_id=:id"),
                        {"id": thread_id},
                    ).scalar()
                    if snapshot:
                        saved = json.loads(snapshot)
                        if saved.get("turn_id") != state["turn_id"] or not saved.get(
                            "requires_confirmation"
                        ):
                            raise RequestConflictError("确认已失效，请重新读取当前预览。")
                self._append(connection, user_id, thread_id, message)
                connection.execute(
                    text(
                        "INSERT INTO chat_runs "
                        "(id,queue,thread_id,user_id,request_key,request_hash,kind,state,status,"
                        "created_at,updated_at) "
                        "VALUES (:id,:queue,:thread,:user,:key,:hash,:kind,:state,"
                        "'queued',:now,:now)"
                    ),
                    {
                        "id": state["run_id"],
                        "queue": self.queue,
                        "thread": thread_id,
                        "user": user_id,
                        "key": request_key,
                        "hash": request_hash,
                        "kind": kind,
                        "state": _json(state),
                        "now": now,
                    },
                )
                self._event(
                    connection,
                    state["run_id"],
                    "status",
                    {
                        "status": "queued",
                        "phase": "QUEUED",
                        "run_id": state["run_id"],
                        "thread_id": thread_id,
                        "turn_id": state["turn_id"],
                        "trace_id": state["trace_id"],
                    },
                )
                return self._get(connection, state["run_id"], user_id)
        except IntegrityError as exc:
            raise ThreadBusyError("该会话已有请求正在执行。") from exc

    @staticmethod
    def request_hash(value: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def find_request(self, user_id: int, key: str, request_hash: str) -> dict[str, Any] | None:
        with self.transaction() as connection:
            row = (
                connection.execute(
                    text("SELECT * FROM chat_runs WHERE user_id=:user AND request_key=:key"),
                    {"user": user_id, "key": key},
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            if row["request_hash"] != request_hash or row["queue"] != self.queue:
                raise RequestConflictError("同一请求编号不能提交不同内容。")
            self._owner(connection, user_id, row["thread_id"])
            return self._public(dict(row))

    @staticmethod
    def _public(row: dict[str, Any]) -> dict[str, Any]:
        state = json.loads(row["state"])
        return {
            "id": row["id"],
            "thread_id": row["thread_id"],
            "turn_id": state["turn_id"],
            "trace_id": state["trace_id"],
            "status": row["status"],
            "kind": row["kind"],
            "attempts": row["attempts"],
            "last_event": row["last_event"],
            "result": json.loads(row["result"]) if row["result"] else None,
            "error": row["error"],
            "created_at": row["created_at"],
        }

    def _get(self, connection: Connection, run_id: str, user_id: int) -> dict[str, Any]:
        row = (
            connection.execute(
                text("SELECT * FROM chat_runs WHERE id=:id AND user_id=:user AND queue=:queue"),
                {"id": run_id, "user": user_id, "queue": self.queue},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise ThreadNotFoundError(run_id)
        self._owner(connection, user_id, row["thread_id"])
        return self._public(dict(row))

    def get(self, run_id: str, user_id: int) -> dict[str, Any]:
        with self.transaction() as connection:
            return self._get(connection, run_id, user_id)

    def final_state(self, run_id: str, user_id: int) -> dict[str, Any]:
        with self.transaction() as connection:
            self._get(connection, run_id, user_id)
            value = connection.execute(
                text("SELECT state FROM chat_runs WHERE id=:id"), {"id": run_id}
            ).scalar_one()
            return cast("dict[str, Any]", json.loads(value))

    def thread_run(self, thread_id: str, user_id: int) -> dict[str, Any] | None:
        with self.transaction() as connection:
            self._owner(connection, user_id, thread_id)
            row = (
                connection.execute(
                    text(
                        "SELECT * FROM chat_runs WHERE thread_id=:id AND queue=:queue "
                        "ORDER BY created_at DESC,id DESC LIMIT 1"
                    ),
                    {"id": thread_id, "queue": self.queue},
                )
                .mappings()
                .first()
            )
            return self._public(dict(row)) if row else None

    def snapshot(self, thread_id: str, user_id: int) -> dict[str, Any] | None:
        with self.transaction() as connection:
            self._owner(connection, user_id, thread_id)
            value = connection.execute(
                text("SELECT snapshot FROM chat_threads WHERE thread_id=:id"), {"id": thread_id}
            ).scalar()
            return json.loads(value) if value else None

    def _event(
        self, connection: Connection, run_id: str, event_type: str, payload: dict[str, Any]
    ) -> None:
        sequence = connection.execute(
            text(
                "UPDATE chat_runs SET last_event=last_event+1,updated_at=:now "
                "WHERE id=:id RETURNING last_event"
            ),
            {"id": run_id, "now": time.time()},
        ).scalar_one()
        connection.execute(
            text("INSERT INTO chat_events VALUES (:id,:sequence,:type,:payload)"),
            {"id": run_id, "sequence": sequence, "type": event_type, "payload": _json(payload)},
        )

    def events(self, run_id: str, user_id: int, after: int) -> list[dict[str, Any]]:
        with self.transaction() as connection:
            self._get(connection, run_id, user_id)
            rows = connection.execute(
                text(
                    "SELECT sequence,event_type,payload FROM chat_events WHERE run_id=:id "
                    "AND sequence>:after ORDER BY sequence LIMIT 100"
                ),
                {"id": run_id, "after": after},
            ).all()
            return [{"id": row[0], "type": row[1], "data": json.loads(row[2])} for row in rows]

    def recover(self) -> None:
        with self.transaction() as connection:
            self._schedule_lock(connection)
            connection.execute(
                text(
                    "UPDATE chat_runs SET status=CASE WHEN status='cancelling' THEN "
                    "'cancelling' ELSE 'queued' END,version=version+1 "
                    "WHERE queue=:queue AND status IN ('running','cancelling')"
                ),
                {"queue": self.queue},
            )

    def claim(
        self, concurrency: int, per_user: int, deadline: float, excluded: tuple[str, ...] = ()
    ) -> dict[str, Any] | None:
        with self.transaction() as connection:
            self._schedule_lock(connection)
            active_rows = connection.execute(
                text(
                    "SELECT id,user_id,status FROM chat_runs WHERE queue=:queue "
                    "AND status IN ('running','cancelling')"
                ),
                {"queue": self.queue},
            ).all()
            running = [row[1] for row in active_rows if row[2] == "running" or row[0] in excluded]
            if len(running) >= concurrency:
                return None
            rows = (
                connection.execute(
                    text(
                        "SELECT * FROM chat_runs WHERE queue=:queue "
                        "AND status IN ('queued','cancelling') ORDER BY created_at,id"
                    ),
                    {"queue": self.queue},
                )
                .mappings()
                .all()
            )
            row = next(
                (
                    dict(row)
                    for row in rows
                    if row["id"] not in excluded and running.count(row["user_id"]) < per_user
                ),
                None,
            )
            if row is None:
                return None
            cancelled = row["status"] == "cancelling"
            now = time.time()
            row.update(
                version=row["version"] + 1,
                attempts=row["attempts"] + 1,
                deadline_at=row["deadline_at"] or now + deadline,
                status="cancelling" if cancelled else "running",
            )
            connection.execute(
                text(
                    "UPDATE chat_runs SET status=:status,version=:version,attempts=:attempts,"
                    "deadline_at=:deadline_at,started_at=COALESCE(started_at,:now) WHERE id=:id"
                ),
                {key: row[key] for key in ("id", "status", "version", "attempts", "deadline_at")}
                | {"now": now},
            )
            row["state"] = json.loads(row["state"])
            return row

    def _check(
        self, connection: Connection, run_id: str, version: int, *, exclusive: bool = False
    ) -> None:
        suffix = (" FOR UPDATE" if exclusive else " FOR SHARE") if self.postgres else ""
        row = connection.execute(
            text("SELECT version,status FROM chat_runs WHERE id=:id" + suffix), {"id": run_id}
        ).first()
        if not row or row[0] != version or row[1] not in ("running", "cancelling"):
            raise ExecutionOwnershipLost("Execution ownership has changed.")

    @contextmanager
    def fence(self, run_id: str, version: int) -> Iterator[None]:
        with self.transaction() as connection:
            self._check(connection, run_id, version)
            yield

    def progress(self, run_id: str, version: int, event_type: str, payload: dict[str, Any]) -> None:
        with self.transaction() as connection:
            self._check(connection, run_id, version, exclusive=True)
            self._event(connection, run_id, event_type, payload)

    def reserve(self, run_id: str, version: int, tokens: int, limit: int) -> None:
        with self.transaction() as connection:
            self._check(connection, run_id, version, exclusive=True)
            used = connection.execute(
                text("SELECT reserved_tokens FROM chat_runs WHERE id=:id"), {"id": run_id}
            ).scalar_one()
            if used + tokens > limit:
                from nutripilot.model_gateway.budget import ExecutionLimitExceeded

                raise ExecutionLimitExceeded("TOKEN_BUDGET_EXCEEDED")
            connection.execute(
                text("UPDATE chat_runs SET reserved_tokens=reserved_tokens+:tokens WHERE id=:id"),
                {"id": run_id, "tokens": tokens},
            )

    def settle(self, run_id: str, version: int, reserved: int, used: int) -> None:
        with self.transaction() as connection:
            self._check(connection, run_id, version, exclusive=True)
            connection.execute(
                text(
                    "UPDATE chat_runs SET reserved_tokens=reserved_tokens+:difference WHERE id=:id"
                ),
                {"id": run_id, "difference": used - reserved},
            )

    def cancel(self, run_id: str, user_id: int) -> dict[str, Any]:
        with self.transaction() as connection:
            row = self._get(connection, run_id, user_id)
            if row["status"] in ACTIVE:
                connection.execute(
                    text(
                        "UPDATE chat_runs SET status='cancelling' WHERE id=:id "
                        "AND status IN ('queued','running','cancelling')"
                    ),
                    {"id": run_id},
                )
            return self._get(connection, run_id, user_id)

    def finish(
        self,
        run_id: str,
        version: int,
        status: str,
        result: dict[str, Any],
        message: ConversationMessage,
        error: str | None = None,
        state: dict[str, Any] | None = None,
    ) -> None:
        with self.transaction() as connection:
            self._check(connection, run_id, version, exclusive=True)
            row = connection.execute(
                text("SELECT user_id,thread_id FROM chat_runs WHERE id=:id"), {"id": run_id}
            ).one()
            self._append(connection, row[0], row[1], message)
            connection.execute(
                text("UPDATE chat_threads SET snapshot=:snapshot WHERE thread_id=:thread"),
                {"snapshot": _json(result), "thread": row[1]},
            )
            self._event(connection, run_id, "completed", result | {"run_status": status})
            connection.execute(
                text(
                    "UPDATE chat_runs SET status=:status,result=:result,error=:error,"
                    "state=COALESCE(:state,state) WHERE id=:id"
                ),
                {
                    "id": run_id,
                    "status": status,
                    "result": _json(result),
                    "error": error,
                    "state": _json(state) if state is not None else None,
                },
            )

    def import_history(self, source: Any) -> dict[str, int]:
        """Import and verify the entire SQLite snapshot atomically; never overwrite conflicts."""
        counts = {"threads": 0, "messages": 0}
        with source._connect() as old:
            exists = old.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='conversations'"
            ).fetchone()
            if not exists:
                return counts
            backup = source.path.with_name(source.path.name + ".pre-durable-chat.bak")
            if not backup.exists():
                with tempfile.NamedTemporaryFile(
                    dir=source.path.parent, prefix=".chat-backup-", delete=False
                ) as staged:
                    temporary = Path(staged.name)
                try:
                    with sqlite3.connect(temporary) as destination:
                        old.backup(destination)
                    with suppress(FileExistsError):
                        os.link(temporary, backup)
                finally:
                    temporary.unlink(missing_ok=True)
            rows = old.execute(
                "SELECT c.user_id,c.thread_id,m.payload FROM conversations c "
                "JOIN conversation_messages m ON m.thread_id=c.thread_id ORDER BY m.sequence"
            ).fetchall()
        with self.transaction() as connection:
            imported = set(connection.execute(
                text("SELECT thread_id FROM chat_history_imports")
            ).scalars())
            visible = set(connection.execute(
                text(
                    "SELECT thread_id FROM chat_threads c WHERE NOT EXISTS "
                    "(SELECT 1 FROM chat_thread_trash t WHERE t.thread_id=c.thread_id)"
                )
            ).scalars())
            for user_id, thread_id, payload in rows:
                if thread_id in imported and thread_id not in visible:
                    continue
                self._append(
                    connection, user_id, thread_id, ConversationMessage.model_validate_json(payload)
                )
            for thread_id in {row[1] for row in rows}:
                connection.execute(
                    text(
                        "INSERT INTO chat_history_imports(thread_id) VALUES (:id) "
                        "ON CONFLICT(thread_id) DO NOTHING"
                    ),
                    {"id": thread_id},
                )
        counts["threads"] = len({row[1] for row in rows})
        counts["messages"] = len(rows)
        return counts

    def close(self) -> None:
        self.engine.dispose()
