"""Persist richer risk-adjusted performance metrics."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

METRIC_COLUMNS = (
    "annualized_return",
    "annualized_volatility",
    "sortino",
    "calmar",
)


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        for column_name in METRIC_COLUMNS:
            batch.add_column(
                sa.Column(
                    column_name,
                    sa.Float(),
                    nullable=False,
                    server_default=sa.text("0"),
                )
            )
    with op.batch_alter_table("runs") as batch:
        for column_name in METRIC_COLUMNS:
            batch.alter_column(column_name, server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        for column_name in reversed(METRIC_COLUMNS):
            batch.drop_column(column_name)
