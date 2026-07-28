"""Add a transactional outbox for recoverable artifact publication."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("run_jobs") as batch:
        batch.drop_constraint("ck_run_jobs_status", type_="check")
        batch.create_check_constraint(
            "ck_run_jobs_status",
            "status IN ('QUEUED', 'RUNNING', 'WAITING_ARTIFACT', "
            "'SUCCEEDED', 'FAILED', 'CANCELED')",
        )

    op.create_table(
        "artifact_publications",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("uri", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('PENDING', 'PUBLISHED')",
            name="ck_artifact_publications_status",
        ),
        sa.CheckConstraint(
            "size_bytes > 0 AND attempts >= 0",
            name="ck_artifact_publications_values",
        ),
        sa.CheckConstraint(
            "(status = 'PUBLISHED' AND uri IS NOT NULL AND published_at IS NOT NULL) "
            "AND next_attempt_at IS NULL "
            "OR (status = 'PENDING' AND uri IS NULL AND published_at IS NULL "
            "AND next_attempt_at IS NOT NULL)",
            name="ck_artifact_publications_completion",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.run_id"],
            name="fk_artifact_publications_tenant_run",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id",
            "kind",
            name="uq_artifact_publications_run_kind",
        ),
    )
    op.create_index(
        "ix_artifact_publications_run_id",
        "artifact_publications",
        ["run_id"],
    )
    op.create_index(
        "ix_artifact_publications_status_updated",
        "artifact_publications",
        ["status", "updated_at"],
    )

    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE artifact_publications ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE artifact_publications FORCE ROW LEVEL SECURITY")
        op.execute(
            """
            CREATE POLICY artifact_publications_tenant_isolation
            ON artifact_publications
            USING (tenant_id = current_setting('qbt.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('qbt.tenant_id', true))
            """
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP POLICY IF EXISTS artifact_publications_tenant_isolation ON artifact_publications"
        )
        op.execute("ALTER TABLE artifact_publications NO FORCE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE artifact_publications DISABLE ROW LEVEL SECURITY")
    op.drop_index(
        "ix_artifact_publications_status_updated",
        table_name="artifact_publications",
    )
    op.drop_index(
        "ix_artifact_publications_run_id",
        table_name="artifact_publications",
    )
    op.drop_table("artifact_publications")
    with op.batch_alter_table("run_jobs") as batch:
        batch.drop_constraint("ck_run_jobs_status", type_="check")
        batch.create_check_constraint(
            "ck_run_jobs_status",
            "status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELED')",
        )
