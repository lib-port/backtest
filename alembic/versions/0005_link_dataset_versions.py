"""Link persisted runs to their registered immutable dataset version."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("dataset_version_id", sa.String(length=36), nullable=True))
        batch.create_foreign_key(
            "fk_runs_dataset_version_id",
            "dataset_versions",
            ["dataset_version_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_index("ix_runs_dataset_version_id", ["dataset_version_id"])


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.drop_index("ix_runs_dataset_version_id")
        batch.drop_constraint("fk_runs_dataset_version_id", type_="foreignkey")
        batch.drop_column("dataset_version_id")
