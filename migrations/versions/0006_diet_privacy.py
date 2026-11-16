"""Keep content-free deletion evidence across backup recovery."""

from collections.abc import Sequence

from alembic import op

from nutripilot.infrastructure.diet_schema import DIET_SCHEMA_V3

revision: str = "0006_diet_privacy"
down_revision: str | None = "0005_diet_plan_timestamps"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for statement in DIET_SCHEMA_V3:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Do not remove purge tombstones; restore and replay deletion evidence.")
