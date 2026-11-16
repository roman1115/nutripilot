"""Keep plan timestamps explicit without modifying the frozen 0004 migration."""

from collections.abc import Sequence

from alembic import op

from nutripilot.infrastructure.diet_schema import DIET_SCHEMA_V2

revision: str = "0005_diet_plan_timestamps"
down_revision: str | None = "0004_diet_management"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for statement in DIET_SCHEMA_V2:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Restore a verified backup instead of removing diet resource metadata.")
