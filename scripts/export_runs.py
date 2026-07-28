from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text

from quant_backtester.config import database_url_from_env, redact_database_url
from quant_backtester.logging_utils import configure_logging

logger = logging.getLogger(__name__)


def _safe_spreadsheet_value(value: object) -> object:
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
        return f"'{value}"
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export tenant-scoped run summaries to CSV.")
    parser.add_argument("--database-url", default=None)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--tenant-id")
    scope.add_argument("--all-tenants", action="store_true")
    parser.add_argument("--out", default="runs/runs_export.csv")
    parser.add_argument("--chunksize", type=int, default=10_000)
    parser.add_argument("--json-logs", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.chunksize <= 0:
        raise SystemExit("--chunksize must be > 0")
    configure_logging(level="INFO", json_logs=args.json_logs or bool(os.getenv("CI")))
    database_url = args.database_url or database_url_from_env()
    logger.info(
        "Export started",
        extra={
            "event": {
                "database_url": redact_database_url(database_url),
                "tenant_id": args.tenant_id if not args.all_tenants else "*",
            }
        },
    )

    query = "SELECT * FROM runs"
    params: dict[str, object] = {}
    if not args.all_tenants:
        query += " WHERE tenant_id = :tenant_id"
        params["tenant_id"] = args.tenant_id
    query += " ORDER BY created_at DESC"

    destination = Path(args.out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(f"{destination.suffix}.tmp")
    engine = create_engine(database_url, future=True, pool_pre_ping=True)
    row_count = 0
    wrote_header = False
    try:
        with engine.connect() as connection:
            chunks = pd.read_sql_query(
                text(query),
                connection,
                params=params,
                chunksize=args.chunksize,
            )
            for chunk in chunks:
                sanitized = chunk.map(_safe_spreadsheet_value)
                sanitized.to_csv(
                    temporary,
                    mode="a" if wrote_header else "w",
                    header=not wrote_header,
                    index=False,
                )
                wrote_header = True
                row_count += len(chunk)
        if not wrote_header:
            pd.DataFrame().to_csv(temporary, index=False)
        os.replace(temporary, destination)
    finally:
        engine.dispose()
        temporary.unlink(missing_ok=True)

    print(f"Exported {row_count} rows to {destination}")
    logger.info(
        "Export completed",
        extra={"event": {"rows": row_count, "out": str(destination)}},
    )


if __name__ == "__main__":
    main()
