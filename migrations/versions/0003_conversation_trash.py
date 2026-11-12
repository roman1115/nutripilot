"""Keep deleted conversations recoverable for seven days."""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_conversation_trash"
down_revision: str | None = "0002_durable_chat"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE TABLE IF NOT EXISTS chat_thread_trash ("
        "thread_id TEXT PRIMARY KEY REFERENCES chat_threads(thread_id),"
        "deleted_at DOUBLE PRECISION NOT NULL, purging BOOLEAN NOT NULL DEFAULT FALSE)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS chat_thread_trash_expiry ON chat_thread_trash(deleted_at)"
    )
    op.execute("CREATE TABLE IF NOT EXISTS chat_history_imports (thread_id TEXT PRIMARY KEY)")


def downgrade() -> None:
    op.drop_table("chat_thread_trash")
    op.drop_table("chat_history_imports")
