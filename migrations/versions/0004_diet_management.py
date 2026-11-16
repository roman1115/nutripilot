"""Add diet-management storage without changing legacy read/write behavior."""

from collections.abc import Sequence

from alembic import op

from nutripilot.infrastructure.diet_schema import DIET_SCHEMA_V1

revision: str = "0004_diet_management"
down_revision: str | None = "0003_conversation_trash"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for statement in DIET_SCHEMA_V1:
        op.execute(statement)


def downgrade() -> None:
    # A downgrade must never silently destroy confirmed private records.
    raise RuntimeError(
        "Diet storage cannot be dropped automatically. "
        "Export new data and restore a verified backup."
    )
