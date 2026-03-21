from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("run_name", sa.String(length=128), nullable=False),
        sa.Column("symbols", sa.String(length=512), nullable=False),
        sa.Column("short_window", sa.Integer(), nullable=False),
        sa.Column("long_window", sa.Integer(), nullable=False),
        sa.Column("initial_cash", sa.Float(), nullable=False),
        sa.Column("final_equity", sa.Float(), nullable=False),
        sa.Column("total_return", sa.Float(), nullable=False),
        sa.Column("sharpe", sa.Float(), nullable=False),
        sa.Column("max_drawdown", sa.Float(), nullable=False),
        sa.Column("total_commission", sa.Float(), nullable=False),
        sa.Column("total_slippage_cost", sa.Float(), nullable=False),
        sa.Column("halted", sa.Integer(), nullable=False),
        sa.Column("halt_reason", sa.String(length=256), nullable=True),
        sa.Column("extra", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )


def downgrade() -> None:
    op.drop_table("runs")
