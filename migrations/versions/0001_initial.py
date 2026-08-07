"""Create NutriPilot application tables.

Revision ID: 0001_initial
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "meals",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "dietary_guidelines",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "user_profiles",
        sa.Column("user_id", sa.BigInteger(), primary_key=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "meal_plans",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_meal_plans_idempotency"),
    )
    op.create_table(
        "user_feedback",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_user_feedback_idempotency"),
    )
    op.create_table(
        "trace_runs",
        sa.Column("trace_id", sa.Text(), primary_key=True),
        sa.Column("thread_id", sa.Text(), nullable=False, index=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "metadata", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "trace_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("trace_id", sa.Text(), nullable=False, index=True),
        sa.Column("step_order", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("component", sa.Text(), nullable=False),
        sa.Column(
            "payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("duration_ms", sa.Float(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("trace_id", "step_order", name="uq_trace_events_order"),
    )
    op.create_table(
        "eval_runs",
        sa.Column("run_id", sa.Text(), primary_key=True),
        sa.Column("dataset_version", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "metrics", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "eval_results",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Text(), nullable=False, index=True),
        sa.Column("case_id", sa.Text(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column(
            "scores", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "details", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.UniqueConstraint("run_id", "case_id", name="uq_eval_results_case"),
    )
    op.create_table(
        "prompt_versions",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column(
            "metadata", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("name", "version"),
    )


def downgrade() -> None:
    for table in (
        "prompt_versions",
        "eval_results",
        "eval_runs",
        "trace_events",
        "trace_runs",
        "user_feedback",
        "meal_plans",
        "user_profiles",
        "dietary_guidelines",
        "meals",
    ):
        op.drop_table(table)
