from __future__ import annotations

import logging
from dataclasses import replace
from itertools import product
from pathlib import Path

import pandas as pd

from quant_backtester.backtest import run_backtest, run_to_model
from quant_backtester.config import BacktestConfig
from quant_backtester.persistence.db import Database

logger = logging.getLogger(__name__)


def run_parameter_sweep(
    cfg: BacktestConfig,
    short_windows: list[int],
    long_windows: list[int],
    export_csv: str | None = None,
) -> pd.DataFrame:
    if export_csv is None:
        export_csv = str(Path(cfg.out_dir) / "sweep_results.csv")

    logger.info(
        "Sweep started",
        extra={
            "event": {
                "run_name": cfg.run_name,
                "short_grid_size": len(short_windows),
                "long_grid_size": len(long_windows),
            }
        },
    )
    results: list[dict[str, object]] = []
    db = Database(cfg.database_url)
    pending_models = []
    insert_chunk_size = 500
    for sw, lw in product(short_windows, long_windows):
        if sw >= lw:
            continue
        run_cfg = replace(
            cfg, short_window=sw, long_window=lw, run_name=f"{cfg.run_name}-sw{sw}-lw{lw}"
        )
        run_result = run_backtest(run_cfg, persist=False)
        results.append(run_result)
        pending_models.append(run_to_model(run_result))
        if len(pending_models) >= insert_chunk_size:
            db.insert_runs_bulk(pending_models)
            pending_models.clear()

    if pending_models:
        db.insert_runs_bulk(pending_models)

    if not results:
        logger.warning(
            "Sweep has no valid parameter pairs",
            extra={
                "event": {
                    "run_name": cfg.run_name,
                    "short_windows": short_windows,
                    "long_windows": long_windows,
                }
            },
        )
        df = pd.DataFrame(
            columns=[
                "run_name",
                "symbols",
                "short_window",
                "long_window",
                "initial_cash",
                "final_equity",
                "total_return",
                "sharpe",
                "max_drawdown",
                "total_commission",
                "total_slippage_cost",
                "halted",
                "halt_reason",
                "created_at",
                "extra",
            ]
        )
        Path(export_csv).parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(export_csv, index=False)
        return df

    df = pd.DataFrame(results)
    df = df.sort_values(["total_return", "sharpe"], ascending=False)
    Path(export_csv).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(export_csv, index=False)
    logger.info(
        "Sweep completed",
        extra={"event": {"run_count": len(results), "export_csv": export_csv}},
    )
    return df
