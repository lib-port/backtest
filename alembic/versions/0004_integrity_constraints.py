"""Add database-enforced state and value invariants."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.create_check_constraint(
            "ck_runs_valid_configuration",
            "initial_cash > 0 AND short_window > 0 AND long_window > short_window",
        )
        batch.create_check_constraint(
            "ck_runs_valid_result",
            "total_commission >= 0 AND status IN ('SUCCEEDED', 'FAILED', 'CANCELED')",
        )
    with op.batch_alter_table("dataset_versions") as batch:
        batch.create_check_constraint(
            "ck_dataset_versions_schema",
            "schema_version >= 1",
        )
    with op.batch_alter_table("run_artifacts") as batch:
        batch.create_check_constraint("ck_run_artifacts_size", "size_bytes >= 0")
    with op.batch_alter_table("run_orders") as batch:
        batch.create_check_constraint(
            "ck_run_orders_quantities",
            "quantity > 0 AND filled_quantity >= 0 AND remaining_quantity >= 0 "
            "AND filled_quantity + remaining_quantity = quantity",
        )
        batch.create_check_constraint(
            "ck_run_orders_status",
            "status IN ('CREATED', 'ACCEPTED', 'PARTIALLY_FILLED', 'FILLED', "
            "'CANCELED', 'REJECTED', 'EXPIRED')",
        )
    with op.batch_alter_table("run_fills") as batch:
        batch.create_check_constraint(
            "ck_run_fills_values",
            "quantity > 0 AND fill_price > 0 AND commission >= 0",
        )
    with op.batch_alter_table("equity_snapshots") as batch:
        batch.create_check_constraint("ck_equity_snapshots_sequence", "sequence > 0")
    with op.batch_alter_table("run_journal_entries") as batch:
        batch.create_check_constraint(
            "ck_run_journal_entries_values",
            "sequence > 0 AND amount >= 0",
        )
    with op.batch_alter_table("run_jobs") as batch:
        batch.create_index(
            "ix_run_jobs_tenant_created_at",
            ["tenant_id", "created_at"],
        )
        batch.create_check_constraint(
            "ck_run_jobs_status",
            "status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELED')",
        )
        batch.create_check_constraint(
            "ck_run_jobs_attempts",
            "attempts >= 0 AND max_attempts >= 1 AND max_attempts <= 10 "
            "AND attempts <= max_attempts",
        )


def downgrade() -> None:
    with op.batch_alter_table("run_jobs") as batch:
        batch.drop_constraint("ck_run_jobs_attempts", type_="check")
        batch.drop_constraint("ck_run_jobs_status", type_="check")
        batch.drop_index("ix_run_jobs_tenant_created_at")
    with op.batch_alter_table("run_journal_entries") as batch:
        batch.drop_constraint("ck_run_journal_entries_values", type_="check")
    with op.batch_alter_table("equity_snapshots") as batch:
        batch.drop_constraint("ck_equity_snapshots_sequence", type_="check")
    with op.batch_alter_table("run_fills") as batch:
        batch.drop_constraint("ck_run_fills_values", type_="check")
    with op.batch_alter_table("run_orders") as batch:
        batch.drop_constraint("ck_run_orders_status", type_="check")
        batch.drop_constraint("ck_run_orders_quantities", type_="check")
    with op.batch_alter_table("run_artifacts") as batch:
        batch.drop_constraint("ck_run_artifacts_size", type_="check")
    with op.batch_alter_table("dataset_versions") as batch:
        batch.drop_constraint("ck_dataset_versions_schema", type_="check")
    with op.batch_alter_table("runs") as batch:
        batch.drop_constraint("ck_runs_valid_result", type_="check")
        batch.drop_constraint("ck_runs_valid_configuration", type_="check")
