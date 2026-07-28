"""Add fenced leases, retry scheduling, and lifecycle invariants."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("run_jobs") as batch:
        batch.add_column(
            sa.Column(
                "lease_generation",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch.add_column(sa.Column("last_heartbeat_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("next_attempt_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("failure_class", sa.String(length=32)))
        batch.add_column(sa.Column("public_error_code", sa.String(length=64)))
        batch.add_column(sa.Column("internal_error_ref", sa.String(length=64)))
        batch.add_column(sa.Column("provider_job_id", sa.String(length=256)))

    op.execute(
        sa.text(
            "UPDATE run_jobs SET "
            "lease_generation = CASE WHEN status = 'RUNNING' THEN 1 ELSE 0 END, "
            "last_heartbeat_at = CASE WHEN status = 'RUNNING' THEN updated_at ELSE NULL END, "
            "next_attempt_at = CASE WHEN status = 'QUEUED' THEN created_at ELSE NULL END"
        )
    )

    with op.batch_alter_table("run_jobs") as batch:
        batch.create_index(
            "ix_run_jobs_status_next_attempt",
            ["status", "next_attempt_at"],
        )
        batch.create_check_constraint(
            "ck_run_jobs_lease_state",
            "(status = 'RUNNING' AND worker_id IS NOT NULL "
            "AND lease_expires_at IS NOT NULL AND lease_generation > 0) "
            "OR (status != 'RUNNING' AND worker_id IS NULL AND lease_expires_at IS NULL)",
        )
        batch.create_check_constraint(
            "ck_run_jobs_queued_schedule",
            "status != 'QUEUED' OR next_attempt_at IS NOT NULL",
        )
        batch.create_check_constraint(
            "ck_run_jobs_terminal_completion",
            "status NOT IN ('SUCCEEDED', 'FAILED', 'CANCELED') OR completed_at IS NOT NULL",
        )
        batch.create_check_constraint(
            "ck_run_jobs_success_result",
            "status != 'SUCCEEDED' OR result_run_id IS NOT NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("run_jobs") as batch:
        batch.drop_constraint("ck_run_jobs_success_result", type_="check")
        batch.drop_constraint("ck_run_jobs_terminal_completion", type_="check")
        batch.drop_constraint("ck_run_jobs_queued_schedule", type_="check")
        batch.drop_constraint("ck_run_jobs_lease_state", type_="check")
        batch.drop_index("ix_run_jobs_status_next_attempt")
        batch.drop_column("provider_job_id")
        batch.drop_column("internal_error_ref")
        batch.drop_column("public_error_code")
        batch.drop_column("failure_class")
        batch.drop_column("next_attempt_at")
        batch.drop_column("last_heartbeat_at")
        batch.drop_column("lease_generation")
