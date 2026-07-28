from __future__ import annotations

import argparse
import os
import re

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from quant_backtester.config import database_url_from_env

SAFE_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{2,62}$")


def bootstrap_application_role(
    *,
    admin_database_url: str,
    application_username: str,
    application_password: str,
) -> None:
    """Create or rotate the least-privilege runtime role after migrations."""
    if make_url(admin_database_url).get_backend_name() != "postgresql":
        raise ValueError("Database role bootstrap requires PostgreSQL")
    if not SAFE_IDENTIFIER.fullmatch(application_username):
        raise ValueError("Application database username must be a safe PostgreSQL identifier")
    if len(application_password) < 24:
        raise ValueError("Application database password must contain at least 24 characters")

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
                            "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION"
                        ).format(role, password)
                    )
                database_name = make_url(admin_database_url).database
                if not database_name:
                    raise ValueError("Admin database URL must select a database")
                cursor.execute(
                    sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                        sql.Identifier(database_name),
                        role,
                    )
                )
                cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
                cursor.execute(
                    sql.SQL(
                        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}"
                    ).format(role)
                )
                cursor.execute(
                    sql.SQL(
                        "GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO {}"
                    ).format(role)
                )
                cursor.execute(
                    sql.SQL(
                        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}"
                    ).format(role)
                )
                cursor.execute(
                    sql.SQL(
                        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                        "GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {}"
                    ).format(role)
                )
            finally:
                cursor.close()
    finally:
        engine.dispose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quant-backtester-db-bootstrap")
    parser.add_argument(
        "--username",
        default=os.getenv("QBT_APP_DB_USERNAME"),
        help="Runtime database username; defaults to QBT_APP_DB_USERNAME.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    password = os.getenv("QBT_APP_DB_PASSWORD")
    if not args.username or not password:
        raise SystemExit("QBT_APP_DB_USERNAME and QBT_APP_DB_PASSWORD are required")
    bootstrap_application_role(
        admin_database_url=database_url_from_env(),
        application_username=str(args.username),
        application_password=password,
    )


if __name__ == "__main__":
    main()
