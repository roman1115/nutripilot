"""Add persistent conversations, tasks, and replay events without removing legacy data."""

from collections.abc import Sequence

from alembic import op

revision: str = "0002_durable_chat"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

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
)


def upgrade() -> None:
    for statement in SCHEMA:
        op.execute(statement)


def downgrade() -> None:
    for name in ("chat_events", "chat_runs", "chat_messages", "chat_threads", "chat_workers"):
        op.drop_table(name)
