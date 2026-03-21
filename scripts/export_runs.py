from __future__ import annotations

import logging
import os

import pandas as pd
from sqlalchemy import create_engine, text

from quant_backtester.logging_utils import configure_logging

DB = os.getenv("DATABASE_URL", "sqlite:///runs/runs.db")
logger = logging.getLogger(__name__)


def main() -> None:
    configure_logging(level="INFO", json_logs=bool(os.getenv("CI")))
    logger.info("Export started", extra={"event": {"database_url": DB}})
    eng = create_engine(DB, future=True)
    with eng.connect() as c:
        rows = c.execute(text("SELECT * FROM runs ORDER BY created_at DESC")).mappings().all()
    df = pd.DataFrame(rows)
    out = "runs/runs_export.csv"
    os.makedirs("runs", exist_ok=True)
    df.to_csv(out, index=False)
    print(f"Exported {len(df)} rows to {out}")
    logger.info("Export completed", extra={"event": {"rows": len(df), "out": out}})


if __name__ == "__main__":
    main()
