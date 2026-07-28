"""Make security audit records append-only at the database boundary."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            CREATE FUNCTION qbt_reject_audit_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            SET search_path = pg_catalog
            AS $$
            BEGIN
                RAISE EXCEPTION 'audit_events is append-only' USING ERRCODE = '55000';
            END;
            $$
            """
        )
        op.execute(
            """
            CREATE TRIGGER audit_events_no_update
            BEFORE UPDATE ON audit_events
            FOR EACH ROW EXECUTE FUNCTION qbt_reject_audit_mutation()
            """
        )
        op.execute(
            """
            CREATE TRIGGER audit_events_no_delete
            BEFORE DELETE ON audit_events
            FOR EACH ROW EXECUTE FUNCTION qbt_reject_audit_mutation()
            """
        )
        return

    op.execute(
        """
        CREATE TRIGGER audit_events_no_update
        BEFORE UPDATE ON audit_events
        BEGIN
            SELECT RAISE(ABORT, 'audit_events is append-only');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_events_no_delete
        BEFORE DELETE ON audit_events
        BEGIN
            SELECT RAISE(ABORT, 'audit_events is append-only');
        END
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS audit_events_no_delete ON audit_events")
        op.execute("DROP TRIGGER IF EXISTS audit_events_no_update ON audit_events")
        op.execute("DROP FUNCTION IF EXISTS qbt_reject_audit_mutation()")
        return
    op.execute("DROP TRIGGER IF EXISTS audit_events_no_delete")
    op.execute("DROP TRIGGER IF EXISTS audit_events_no_update")
