"""Store monetary and price values with fixed decimal precision."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONEY = sa.Numeric(precision=38, scale=8)
FLOAT = sa.Float()

TABLE_COLUMNS = {
    "runs": (
        "initial_cash",
        "final_equity",
        "total_commission",
        "total_slippage_cost",
    ),
    "run_orders": ("limit_price", "stop_price"),
    "run_fills": ("fill_price", "mid_price", "commission", "slippage"),
    "equity_snapshots": ("equity",),
}


def upgrade() -> None:
    for table_name, column_names in TABLE_COLUMNS.items():
        with op.batch_alter_table(table_name) as batch:
            for column_name in column_names:
                batch.alter_column(
                    column_name,
                    existing_type=FLOAT,
                    type_=MONEY,
                    existing_nullable=column_name in {"limit_price", "stop_price", "mid_price"},
                )


def downgrade() -> None:
    for table_name, column_names in reversed(tuple(TABLE_COLUMNS.items())):
        with op.batch_alter_table(table_name) as batch:
            for column_name in reversed(column_names):
                batch.alter_column(
                    column_name,
                    existing_type=MONEY,
                    type_=FLOAT,
                    existing_nullable=column_name in {"limit_price", "stop_price", "mid_price"},
                )
