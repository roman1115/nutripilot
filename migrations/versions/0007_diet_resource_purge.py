"""Retain deletion evidence for every diet resource, including legacy plan IDs."""

from collections.abc import Sequence

from alembic import op

from nutripilot.infrastructure.diet_schema import DIET_SCHEMA_V4

revision: str = "0007_diet_resource_purge"
down_revision: str | None = "0006_diet_privacy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for statement in DIET_SCHEMA_V4:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Do not remove deletion evidence or truncate legacy plan identities.")
