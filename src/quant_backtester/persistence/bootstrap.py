from __future__ import annotations

import argparse
import os
import re
from typing import Literal, cast

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from quant_backtester.config import database_url_from_env

SAFE_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{2,62}$")
RuntimeRole = Literal["api", "worker", "reporting"]
ROLE_TABLE_PRIVILEGES: dict[RuntimeRole, dict[str, tuple[str, ...]]] = {
    "api": {
        "alembic_version": ("SELECT",),
        "runs": ("SELECT",),
        "dataset_versions": ("SELECT", "INSERT"),
        "run_jobs": ("SELECT", "INSERT"),
        "audit_events": ("INSERT",),
    },
    "worker": {
        "alembic_version": ("SELECT",),
        "runs": ("SELECT", "INSERT"),
        "dataset_versions": ("SELECT",),
        "run_jobs": ("SELECT", "UPDATE"),
        "audit_events": ("INSERT",),
        "run_artifacts": ("SELECT", "INSERT"),
        "artifact_publications": ("SELECT", "INSERT", "UPDATE"),
        "run_orders": ("SELECT", "INSERT"),
        "run_fills": ("SELECT", "INSERT"),
        "equity_snapshots": ("SELECT", "INSERT"),
        "run_journal_entries": ("SELECT", "INSERT"),
    },
    "reporting": {
        "alembic_version": ("SELECT",),
        "runs": ("SELECT",),
        "dataset_versions": ("SELECT",),
        "run_jobs": ("SELECT",),
        "audit_events": ("SELECT",),
        "run_artifacts": ("SELECT",),
        "artifact_publications": ("SELECT",),
        "run_orders": ("SELECT",),
        "run_fills": ("SELECT",),
        "equity_snapshots": ("SELECT",),
        "run_journal_entries": ("SELECT",),
    },
}
API_JOB_UPDATE_COLUMNS = (
    "status",
    "cancel_requested",
    "next_attempt_at",
    "updated_at",
    "completed_at",
)


def bootstrap_application_role(
    *,
    admin_database_url: str,
    application_username: str,
    application_password: str,
    role_kind: RuntimeRole = "api",
) -> None:
    """Create or rotate one purpose-specific, least-privilege runtime role."""
    if make_url(admin_database_url).get_backend_name() != "postgresql":
        raise ValueError("Database role bootstrap requires PostgreSQL")
    if not SAFE_IDENTIFIER.fullmatch(application_username):
        raise ValueError("Application database username must be a safe PostgreSQL identifier")
    if len(application_password) < 24:
        raise ValueError("Application database password must contain at least 24 characters")
    if role_kind not in ROLE_TABLE_PRIVILEGES:
        raise ValueError("role_kind must be api, worker, or reporting")

    from psycopg import sql

    engine = create_engine(admin_database_url, future=True, pool_pre_ping=True)
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            exists = bool(
                connection.scalar(
                    text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role)"),
                    {"role": application_username},
                )
            )
            cursor = connection.connection.cursor()
            try:
                role = sql.Identifier(application_username)
                password = sql.Literal(application_password)
                if exists:
                    cursor.execute(
                        sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}").format(role, password)
                    )
                else:
                    cursor.execute(
                        sql.SQL(
                            "CREATE ROLE {} LOGIN PASSWORD {} "
                            "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
                            "NOINHERIT NOBYPASSRLS CONNECTION LIMIT 50"
                        ).format(role, password)
                    )
                cursor.execute(
                    sql.SQL(
                        "ALTER ROLE {} NOSUPERUSER NOCREATEDB NOCREATEROLE "
                        "NOREPLICATION NOINHERIT {} CONNECTION LIMIT 50"
                    ).format(
                        role,
                        sql.SQL("BYPASSRLS" if role_kind == "worker" else "NOBYPASSRLS"),
                    )
                )
                cursor.execute(
                    sql.SQL("ALTER ROLE {} SET search_path = public, pg_catalog").format(role)
                )
                cursor.execute(sql.SQL("ALTER ROLE {} SET lock_timeout = '10s'").format(role))
                cursor.execute(
                    sql.SQL("ALTER ROLE {} SET idle_in_transaction_session_timeout = '30s'").format(
                        role
                    )
                )
                statement_timeout = "120s" if role_kind == "worker" else "30s"
                cursor.execute(
                    sql.SQL("ALTER ROLE {} SET statement_timeout = {}").format(
                        role,
                        sql.Literal(statement_timeout),
                    )
                )
                database_name = make_url(admin_database_url).database
                if not database_name:
                    raise ValueError("Admin database URL must select a database")
                database = sql.Identifier(database_name)
                cursor.execute(sql.SQL("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
                cursor.execute(
                    sql.SQL("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {}").format(
                        role
                    )
                )
                cursor.execute(
                    sql.SQL(
                        "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {}"
                    ).format(role)
                )
                cursor.execute(
                    sql.SQL("REVOKE TEMPORARY ON DATABASE {} FROM {}").format(database, role)
                )
                cursor.execute(
                    sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                        database,
                        role,
                    )
                )
                cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
                for table_name, privileges in ROLE_TABLE_PRIVILEGES[role_kind].items():
                    cursor.execute(
                        sql.SQL("GRANT {} ON TABLE {} TO {}").format(
                            sql.SQL(", ").join(sql.SQL(privilege) for privilege in privileges),
                            sql.Identifier(table_name),
                            role,
                        )
                    )
                if role_kind == "api":
                    cursor.execute(
                        sql.SQL("GRANT UPDATE ({}) ON TABLE run_jobs TO {}").format(
                            sql.SQL(", ").join(
                                sql.Identifier(column) for column in API_JOB_UPDATE_COLUMNS
                            ),
                            role,
                        )
                    )
                if role_kind == "worker":
                    cursor.execute(
                        sql.SQL("GRANT USAGE, SELECT ON SEQUENCE runs_id_seq TO {}").format(role)
                    )
                # Schema changes must be followed by an explicit bootstrap run;
                # future objects are intentionally not granted implicitly.
                cursor.execute(
                    sql.SQL(
                        "ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM {}"
                    ).format(role)
                )
                cursor.execute(
                    sql.SQL(
                        "ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM {}"
                    ).format(role)
                )
            finally:
                cursor.close()
    finally:
        engine.dispose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quant-backtester-db-bootstrap")
    parser.add_argument(
        "--role",
        choices=("api", "worker", "reporting"),
        default="api",
        help="Purpose-specific role to create or rotate.",
    )
    parser.add_argument(
        "--username",
        help="Runtime database username; defaults to QBT_<ROLE>_DB_USERNAME.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    role_kind = cast(RuntimeRole, args.role)
    environment_prefix = f"QBT_{role_kind.upper()}_DB"
    username = args.username or os.getenv(f"{environment_prefix}_USERNAME")
    password = os.getenv(f"{environment_prefix}_PASSWORD")
    if not username or not password:
        raise SystemExit(
            f"{environment_prefix}_USERNAME and {environment_prefix}_PASSWORD are required"
        )
    bootstrap_application_role(
        admin_database_url=database_url_from_env(),
        application_username=str(username),
        application_password=password,
        role_kind=role_kind,
    )


if __name__ == "__main__":
    main()
