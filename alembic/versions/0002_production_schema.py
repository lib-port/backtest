"""Add run provenance, durable jobs, artifacts, datasets, and audit records."""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _normalize_symbols(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip().upper() for item in value if str(item).strip()]
    raw = str(value or "").strip()
    if not raw:
        return []
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, list):
        return [str(item).strip().upper() for item in decoded if str(item).strip()]
    return [item.strip().upper() for item in raw.split(",") if item.strip()]


def upgrade() -> None:
    connection = op.get_bind()

    op.add_column("runs", sa.Column("symbols_v2", sa.JSON(), nullable=True))
    runs = sa.table(
        "runs",
        sa.column("id", sa.Integer()),
        sa.column("symbols", sa.String()),
        sa.column("symbols_v2", sa.JSON()),
    )
    for row in connection.execute(sa.select(runs.c.id, runs.c.symbols)):
        connection.execute(
            runs.update()
            .where(runs.c.id == row.id)
            .values(symbols_v2=_normalize_symbols(row.symbols))
        )

    with op.batch_alter_table("runs") as batch:
        batch.drop_column("symbols")
        batch.alter_column(
            "symbols_v2",
            new_column_name="symbols",
            existing_type=sa.JSON(),
            nullable=False,
        )
        batch.add_column(sa.Column("run_id", sa.String(length=36), nullable=True))
        batch.add_column(
            sa.Column(
                "tenant_id",
                sa.String(length=128),
                nullable=False,
                server_default="default",
            )
        )
        batch.add_column(
            sa.Column(
                "status",
                sa.String(length=32),
                nullable=False,
                server_default="SUCCEEDED",
            )
        )
        batch.add_column(sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(
            sa.Column("result_hash", sa.String(length=64), nullable=False, server_default="")
        )
        batch.add_column(
            sa.Column("config_hash", sa.String(length=64), nullable=False, server_default="")
        )
        batch.add_column(
            sa.Column("data_hash", sa.String(length=64), nullable=False, server_default="")
        )
        batch.add_column(
            sa.Column("engine_version", sa.String(length=32), nullable=False, server_default="")
        )
        batch.add_column(
            sa.Column("config", sa.JSON(), nullable=False, server_default=sa.text("'{}'"))
        )
        batch.add_column(
            sa.Column("manifest", sa.JSON(), nullable=False, server_default=sa.text("'{}'"))
        )
        batch.alter_column(
            "created_at",
            existing_type=sa.DateTime(),
            type_=sa.DateTime(timezone=True),
            existing_nullable=False,
        )
        batch.alter_column(
            "halted",
            existing_type=sa.Integer(),
            type_=sa.Boolean(),
            existing_nullable=False,
            postgresql_using="halted <> 0",
        )

    legacy_rows = connection.execute(sa.select(runs.c.id)).all()
    for row in legacy_rows:
        connection.execute(
            sa.text("UPDATE runs SET run_id = :run_id WHERE id = :id"),
            {"run_id": f"legacy-{row.id:029d}", "id": row.id},
        )

    with op.batch_alter_table("runs") as batch:
        batch.alter_column(
            "run_id",
            existing_type=sa.String(length=36),
            nullable=False,
        )
        batch.create_unique_constraint("uq_runs_run_id", ["run_id"])
        batch.create_index("ix_runs_created_at", ["created_at"])
        batch.create_index("ix_runs_run_name", ["run_name"])
        batch.create_index("ix_runs_tenant_created_at", ["tenant_id", "created_at"])
        batch.create_index("ix_runs_tenant_status", ["tenant_id", "status"])

    op.create_table(
        "dataset_versions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("source_uri", sa.Text(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "content_hash"),
    )
    op.create_table(
        "run_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("result_run_id", sa.String(length=36), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("worker_id", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["result_run_id"], ["runs.run_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "idempotency_key"),
    )
    op.create_index(
        "ix_run_jobs_status_created_at",
        "run_jobs",
        ["status", "created_at"],
    )
    op.create_table(
        "run_artifacts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("uri", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "kind", "uri"),
    )
    op.create_index("ix_run_artifacts_run_id", "run_artifacts", ["run_id"])
    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("actor_id", sa.String(length=256), nullable=False),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.String(length=128), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_audit_tenant_created_at",
        "audit_events",
        ["tenant_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_tenant_created_at", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index("ix_run_artifacts_run_id", table_name="run_artifacts")
    op.drop_table("run_artifacts")
    op.drop_index("ix_run_jobs_status_created_at", table_name="run_jobs")
    op.drop_table("run_jobs")
    op.drop_table("dataset_versions")

    op.add_column("runs", sa.Column("symbols_v1", sa.String(length=512), nullable=True))
    connection = op.get_bind()
    runs = sa.table(
        "runs",
        sa.column("id", sa.Integer()),
        sa.column("symbols", sa.JSON()),
        sa.column("symbols_v1", sa.String()),
    )
    for row in connection.execute(sa.select(runs.c.id, runs.c.symbols)):
        connection.execute(
            runs.update()
            .where(runs.c.id == row.id)
            .values(symbols_v1=",".join(_normalize_symbols(row.symbols)))
        )

    with op.batch_alter_table("runs") as batch:
        batch.drop_index("ix_runs_tenant_status")
        batch.drop_index("ix_runs_tenant_created_at")
        batch.drop_index("ix_runs_run_name")
        batch.drop_index("ix_runs_created_at")
        batch.drop_constraint("uq_runs_run_id", type_="unique")
        for column in (
            "manifest",
            "config",
            "engine_version",
            "data_hash",
            "config_hash",
            "result_hash",
            "completed_at",
            "status",
            "tenant_id",
            "run_id",
            "symbols",
        ):
            batch.drop_column(column)
        batch.alter_column(
            "symbols_v1",
            new_column_name="symbols",
            existing_type=sa.String(length=512),
            nullable=False,
        )
        batch.alter_column(
            "created_at",
            existing_type=sa.DateTime(timezone=True),
            type_=sa.DateTime(),
            existing_nullable=False,
        )
        batch.alter_column(
            "halted",
            existing_type=sa.Boolean(),
            type_=sa.Integer(),
            existing_nullable=False,
            postgresql_using="CASE WHEN halted THEN 1 ELSE 0 END",
        )
