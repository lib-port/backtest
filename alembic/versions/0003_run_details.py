"""Persist orders, fills, valuations, and balanced journal entries."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "run_orders",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("order_id", sa.String(length=128), nullable=False),
        sa.Column("client_order_id", sa.String(length=256), nullable=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("order_type", sa.String(length=16), nullable=False),
        sa.Column("limit_price", sa.Float(), nullable=True),
        sa.Column("stop_price", sa.Float(), nullable=True),
        sa.Column("time_in_force", sa.String(length=8), nullable=False),
        sa.Column("intent", sa.String(length=32), nullable=False),
        sa.Column("reduce_only", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("filled_quantity", sa.Integer(), nullable=False),
        sa.Column("remaining_quantity", sa.Integer(), nullable=False),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "order_id"),
    )
    op.create_index("ix_run_orders_run_id", "run_orders", ["run_id"])

    op.create_table(
        "run_fills",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("execution_id", sa.String(length=128), nullable=False),
        sa.Column("order_id", sa.String(length=128), nullable=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("fill_price", sa.Float(), nullable=False),
        sa.Column("mid_price", sa.Float(), nullable=True),
        sa.Column("commission", sa.Float(), nullable=False),
        sa.Column("slippage", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "execution_id"),
    )
    op.create_index("ix_run_fills_run_id", "run_fills", ["run_id"])

    op.create_table(
        "equity_snapshots",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("equity", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "sequence"),
    )
    op.create_index("ix_equity_snapshots_run_id", "equity_snapshots", ["run_id"])

    op.create_table(
        "run_journal_entries",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("transaction_id", sa.String(length=256), nullable=False),
        sa.Column("debit_account", sa.String(length=256), nullable=False),
        sa.Column("credit_account", sa.String(length=256), nullable=False),
        sa.Column("amount", sa.Numeric(precision=38, scale=8), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "sequence"),
    )
    op.create_index(
        "ix_run_journal_entries_run_id",
        "run_journal_entries",
        ["run_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_run_journal_entries_run_id", table_name="run_journal_entries")
    op.drop_table("run_journal_entries")
    op.drop_index("ix_equity_snapshots_run_id", table_name="equity_snapshots")
    op.drop_table("equity_snapshots")
    op.drop_index("ix_run_fills_run_id", table_name="run_fills")
    op.drop_table("run_fills")
    op.drop_index("ix_run_orders_run_id", table_name="run_orders")
    op.drop_table("run_orders")
