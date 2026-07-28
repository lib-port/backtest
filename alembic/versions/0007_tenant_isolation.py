"""Enforce same-tenant references and install PostgreSQL RLS policies."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_TABLES = ("runs", "dataset_versions", "run_jobs", "audit_events")
RUN_CHILD_TABLES = (
    "run_artifacts",
    "run_orders",
    "run_fills",
    "equity_snapshots",
    "run_journal_entries",
)


def upgrade() -> None:
    with op.batch_alter_table("dataset_versions") as batch:
        batch.create_unique_constraint(
            "uq_dataset_versions_tenant_id",
            ["tenant_id", "id"],
        )
    with op.batch_alter_table("runs") as batch:
        batch.create_unique_constraint(
            "uq_runs_tenant_run_id",
            ["tenant_id", "run_id"],
        )
        batch.create_foreign_key(
            "fk_runs_tenant_dataset_version",
            "dataset_versions",
            ["tenant_id", "dataset_version_id"],
            ["tenant_id", "id"],
            ondelete="RESTRICT",
        )
    with op.batch_alter_table("run_jobs") as batch:
        batch.create_foreign_key(
            "fk_run_jobs_tenant_result_run",
            "runs",
            ["tenant_id", "result_run_id"],
            ["tenant_id", "run_id"],
            ondelete="RESTRICT",
        )

    if op.get_bind().dialect.name != "postgresql":
        return

    for table in TENANT_TABLES:
        op.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
        op.execute(sa.text(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY'))
        op.execute(
            sa.text(
                f'CREATE POLICY "{table}_tenant_isolation" ON "{table}" '
                "USING (tenant_id = current_setting('qbt.tenant_id', true)) "
                "WITH CHECK (tenant_id = current_setting('qbt.tenant_id', true))"
            )
        )
    for table in RUN_CHILD_TABLES:
        op.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
        op.execute(sa.text(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY'))
        predicate = (
            "EXISTS (SELECT 1 FROM runs AS tenant_run "
            f'WHERE tenant_run.run_id = "{table}".run_id '
            "AND tenant_run.tenant_id = current_setting('qbt.tenant_id', true))"
        )
        op.execute(
            sa.text(
                f'CREATE POLICY "{table}_tenant_isolation" ON "{table}" '
                f"USING ({predicate}) WITH CHECK ({predicate})"
            )
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table in RUN_CHILD_TABLES:
            op.execute(sa.text(f'DROP POLICY IF EXISTS "{table}_tenant_isolation" ON "{table}"'))
            op.execute(sa.text(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY'))
            op.execute(sa.text(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY'))
        for table in TENANT_TABLES:
            op.execute(sa.text(f'DROP POLICY IF EXISTS "{table}_tenant_isolation" ON "{table}"'))
            op.execute(sa.text(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY'))
            op.execute(sa.text(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY'))

    with op.batch_alter_table("run_jobs") as batch:
        batch.drop_constraint("fk_run_jobs_tenant_result_run", type_="foreignkey")
    with op.batch_alter_table("runs") as batch:
        batch.drop_constraint("fk_runs_tenant_dataset_version", type_="foreignkey")
        batch.drop_constraint("uq_runs_tenant_run_id", type_="unique")
    with op.batch_alter_table("dataset_versions") as batch:
        batch.drop_constraint("uq_dataset_versions_tenant_id", type_="unique")
