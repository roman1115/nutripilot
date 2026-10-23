"""Small indexed history adapters for the single-instance application."""

from __future__ import annotations

from threading import Lock

from nutripilot.application.history import (
    ConversationMessage,
    ConversationSummary,
)
from nutripilot.infrastructure.run_store import SQLiteRunStore


class InMemoryConversationStore:
    def __init__(self) -> None:
        self._items: dict[str, tuple[int, list[ConversationMessage]]] = {}
        self._lock = Lock()

    def append(self, user_id: int, thread_id: str, message: ConversationMessage) -> None:
        with self._lock:
            owner, items = self._items.setdefault(thread_id, (user_id, []))
            if owner != user_id:
                raise PermissionError("Conversation owner mismatch")
            if all(item.id != message.id for item in items):
                items.append(message.model_copy(deep=True))

    def list_threads(
        self, user_id: int, query: str, limit: int, offset: int
    ) -> list[ConversationSummary]:
        with self._lock:
            summaries = [
                ConversationSummary(
                    thread_id=thread_id,
                    title=next(
                        (item.content[:80] for item in items if item.role == "user"), "新对话"
                    ),
                    updated_at=items[-1].created_at,
                    message_count=len(items),
                )
                for thread_id, (owner, items) in self._items.items()
                if owner == user_id
                and items
                and (
                    not query or any(query.casefold() in item.content.casefold() for item in items)
                )
            ]
        return sorted(summaries, key=lambda item: item.updated_at, reverse=True)[
            offset : offset + limit
        ]

    def messages(
        self, user_id: int, thread_id: str, limit: int, offset: int
    ) -> list[ConversationMessage]:
        with self._lock:
            owner, items = self._items.get(thread_id, (user_id, []))
            if owner != user_id:
                return []
            return [item.model_copy(deep=True) for item in items[offset : offset + limit]]


class SQLiteConversationStore:
    def __init__(self, store: SQLiteRunStore) -> None:
        self.store = store
        with store._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS conversations (
                    thread_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS conversations_owner_date
                    ON conversations(user_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS conversation_messages (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT UNIQUE NOT NULL,
                    thread_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS messages_thread_sequence
                    ON conversation_messages(thread_id, sequence);
            """)

    def append(self, user_id: int, thread_id: str, message: ConversationMessage) -> None:
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO conversations VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (thread_id, user_id, message.content[:80], message.created_at.isoformat()),
            )
            owner = connection.execute(
                "SELECT user_id FROM conversations WHERE thread_id=?", (thread_id,)
            ).fetchone()
            if not owner or owner[0] != user_id:
                raise PermissionError("Conversation owner mismatch")
            inserted = connection.execute(
                "INSERT INTO conversation_messages(id, thread_id, content, payload) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(id) DO NOTHING",
                (message.id, thread_id, message.content, message.model_dump_json()),
            )
            if inserted.rowcount:
                connection.execute(
                    "UPDATE conversations SET updated_at=? WHERE thread_id=?",
                    (message.created_at.isoformat(), thread_id),
                )

    def list_threads(
        self, user_id: int, query: str, limit: int, offset: int
    ) -> list[ConversationSummary]:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self.store._connect() as connection:
            rows = connection.execute(
                "SELECT c.thread_id, c.title, c.updated_at, COUNT(m.id) "
                "FROM conversations c JOIN conversation_messages m ON m.thread_id=c.thread_id "
                "WHERE c.user_id=? AND (?='' OR EXISTS (SELECT 1 FROM conversation_messages s "
                "WHERE s.thread_id=c.thread_id AND s.content LIKE ? ESCAPE '\\')) "
                "GROUP BY c.thread_id ORDER BY c.updated_at DESC, c.thread_id LIMIT ? OFFSET ?",
                (user_id, query, f"%{escaped}%", limit, offset),
            ).fetchall()
        return [
            ConversationSummary(
                thread_id=row[0], title=row[1], updated_at=row[2], message_count=row[3]
            )
            for row in rows
        ]

    def messages(
        self, user_id: int, thread_id: str, limit: int, offset: int
    ) -> list[ConversationMessage]:
        with self.store._connect() as connection:
            rows = connection.execute(
                "SELECT m.payload FROM conversation_messages m "
                "JOIN conversations c ON c.thread_id=m.thread_id "
                "WHERE c.user_id=? AND c.thread_id=? ORDER BY m.sequence LIMIT ? OFFSET ?",
                (user_id, thread_id, limit, offset),
            ).fetchall()
        return [ConversationMessage.model_validate_json(row[0]) for row in rows]
