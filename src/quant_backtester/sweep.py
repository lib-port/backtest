from __future__ import annotations

import logging
from collections import Counter
from dataclasses import replace
from itertools import product
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from quant_backtester.backtest import run_backtest, run_to_model
from quant_backtester.config import BacktestConfig
from quant_backtester.persistence.db import Database

logger = logging.getLogger(__name__)


def _valid_param_pairs(short_windows: list[int], long_windows: list[int]) -> list[tuple[int, int]]:
    return [(sw, lw) for sw, lw in product(short_windows, long_windows) if sw < lw]


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
    valid_pairs = _valid_param_pairs(short_windows, long_windows)
    db = Database(cfg.database_url)
    db.create_tables()
    pending_models = []
    insert_chunk_size = 500
    for sw, lw in valid_pairs:
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


def run_walk_forward(
    cfg: BacktestConfig,
    short_windows: list[int],
    long_windows: list[int],
    train_days: int,
    test_days: int,
    step_days: int,
    export_csv: str | None = None,
) -> tuple[pd.DataFrame, dict[str, object]]:
    if train_days <= 0:
        raise ValueError("train_days must be > 0")
    if test_days <= 0:
        raise ValueError("test_days must be > 0")
    if step_days <= 0:
        raise ValueError("step_days must be > 0")

    valid_pairs = _valid_param_pairs(short_windows, long_windows)
    if not valid_pairs:
        raise ValueError("No valid parameter pairs: require short_window < long_window")

    if export_csv is None:
        export_csv = str(Path(cfg.out_dir) / "walk_forward_results.csv")

    market_df = pd.read_csv(cfg.csv_path)
    if "date" not in market_df.columns:
        raise ValueError("CSV missing required column: date")
    market_df["date"] = pd.to_datetime(market_df["date"], utc=False, errors="coerce")
    if market_df["date"].isna().any():
        raise ValueError("CSV contains invalid date values")
    market_df = market_df.sort_values("date")

    unique_dates = sorted(market_df["date"].drop_duplicates().tolist())
    total_days = len(unique_dates)

    logger.info(
        "Walk-forward started",
        extra={
            "event": {
                "run_name": cfg.run_name,
                "short_grid_size": len(short_windows),
                "long_grid_size": len(long_windows),
                "train_days": train_days,
                "test_days": test_days,
                "step_days": step_days,
                "total_days": total_days,
            }
        },
    )

    rows: list[dict[str, object]] = []
    db = Database(cfg.database_url)
    db.create_tables()
    pending_models = []
    insert_chunk_size = 200

    with TemporaryDirectory(prefix="walk_forward_", dir=cfg.out_dir) as tmp_dir:
        cursor = 0
        window_idx = 1
        while cursor + train_days + test_days <= total_days:
            train_dates = unique_dates[cursor : cursor + train_days]
            test_dates = unique_dates[cursor + train_days : cursor + train_days + test_days]
            cursor += step_days

            train_df = market_df[market_df["date"].isin(train_dates)]
            test_df = market_df[market_df["date"].isin(test_dates)]
            if train_df.empty or test_df.empty:
                window_idx += 1
                continue

            train_csv = Path(tmp_dir) / f"train_window_{window_idx}.csv"
            test_csv = Path(tmp_dir) / f"test_window_{window_idx}.csv"
            train_df.to_csv(train_csv, index=False)
            test_df.to_csv(test_csv, index=False)

            best_train_result: dict[str, object] | None = None
            best_pair: tuple[int, int] | None = None

            for sw, lw in valid_pairs:
                train_cfg = replace(
                    cfg,
                    csv_path=str(train_csv),
                    short_window=sw,
                    long_window=lw,
                    run_name=f"{cfg.run_name}-wf{window_idx}-train-sw{sw}-lw{lw}",
                )
                train_result = run_backtest(train_cfg, persist=False)
                train_score = (
                    float(train_result["total_return"]),
                    float(train_result["sharpe"]),
                    -float(train_result["max_drawdown"]),
                )
                if best_train_result is None:
                    best_train_result = train_result
                    best_pair = (sw, lw)
                    continue
                current_best_score = (
                    float(best_train_result["total_return"]),
                    float(best_train_result["sharpe"]),
                    -float(best_train_result["max_drawdown"]),
                )
                if train_score > current_best_score:
                    best_train_result = train_result
                    best_pair = (sw, lw)

            assert best_train_result is not None
            assert best_pair is not None

            test_cfg = replace(
                cfg,
                csv_path=str(test_csv),
                short_window=best_pair[0],
                long_window=best_pair[1],
                run_name=f"{cfg.run_name}-wf{window_idx}-oos-sw{best_pair[0]}-lw{best_pair[1]}",
            )
            oos_result = run_backtest(test_cfg, persist=False)
            oos_extra = dict(oos_result["extra"]) if isinstance(oos_result.get("extra"), dict) else {}
            oos_extra["walk_forward"] = {
                "window_index": window_idx,
                "train_start": train_dates[0].isoformat(),
                "train_end": train_dates[-1].isoformat(),
                "test_start": test_dates[0].isoformat(),
                "test_end": test_dates[-1].isoformat(),
                "best_short_window": best_pair[0],
                "best_long_window": best_pair[1],
                "in_sample_total_return": float(best_train_result["total_return"]),
                "in_sample_sharpe": float(best_train_result["sharpe"]),
                "in_sample_max_drawdown": float(best_train_result["max_drawdown"]),
            }
            oos_result["extra"] = oos_extra

            pending_models.append(run_to_model(oos_result))
            if len(pending_models) >= insert_chunk_size:
                db.insert_runs_bulk(pending_models)
                pending_models.clear()

            rows.append(
                {
                    "window_index": window_idx,
                    "train_start": train_dates[0].date().isoformat(),
                    "train_end": train_dates[-1].date().isoformat(),
                    "test_start": test_dates[0].date().isoformat(),
                    "test_end": test_dates[-1].date().isoformat(),
                    "best_short_window": best_pair[0],
                    "best_long_window": best_pair[1],
                    "train_total_return": float(best_train_result["total_return"]),
                    "train_sharpe": float(best_train_result["sharpe"]),
                    "train_max_drawdown": float(best_train_result["max_drawdown"]),
                    "oos_total_return": float(oos_result["total_return"]),
                    "oos_sharpe": float(oos_result["sharpe"]),
                    "oos_max_drawdown": float(oos_result["max_drawdown"]),
                    "oos_halted": bool(oos_result["halted"]),
                    "oos_halt_reason": oos_result["halt_reason"],
                    "oos_run_name": oos_result["run_name"],
                }
            )
            window_idx += 1

    if pending_models:
        db.insert_runs_bulk(pending_models)

    df = pd.DataFrame(rows).sort_values("window_index") if rows else pd.DataFrame()
    Path(export_csv).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(export_csv, index=False)

    if df.empty:
        summary: dict[str, object] = {
            "window_count": 0,
            "stability_score": 0.0,
            "most_common_short_window": None,
            "most_common_long_window": None,
            "oos_compounded_return": 0.0,
            "oos_mean_return": 0.0,
            "oos_mean_sharpe": 0.0,
            "oos_max_drawdown": 0.0,
        }
        logger.warning(
            "Walk-forward produced no windows",
            extra={"event": {"run_name": cfg.run_name, "export_csv": export_csv}},
        )
        return df, summary

    pair_counter = Counter(
        (int(row["best_short_window"]), int(row["best_long_window"])) for row in rows
    )
    (common_sw, common_lw), common_count = pair_counter.most_common(1)[0]

    oos_returns = df["oos_total_return"].astype(float)
    oos_sharpes = df["oos_sharpe"].astype(float)
    oos_drawdowns = df["oos_max_drawdown"].astype(float)

    summary = {
        "window_count": int(len(df)),
        "stability_score": float(common_count / len(df)),
        "most_common_short_window": int(common_sw),
        "most_common_long_window": int(common_lw),
        "oos_compounded_return": float((1.0 + oos_returns).prod() - 1.0),
        "oos_mean_return": float(oos_returns.mean()),
        "oos_mean_sharpe": float(oos_sharpes.mean()),
        "oos_max_drawdown": float(oos_drawdowns.max()),
    }

    logger.info(
        "Walk-forward completed",
        extra={
            "event": {
                "run_name": cfg.run_name,
                "window_count": len(df),
                "stability_score": summary["stability_score"],
                "export_csv": export_csv,
            }
        },
    )
    return df, summary
