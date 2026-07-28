from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterable
from dataclasses import replace
from datetime import date
from itertools import product
from pathlib import Path
from typing import cast

import pandas as pd

from quant_backtester.backtest import run_backtest, run_to_model
from quant_backtester.config import BacktestConfig
from quant_backtester.data.csv_data_handler import CSVDataHandler, InMemoryDataHandler
from quant_backtester.events import MarketEvent
from quant_backtester.persistence.db import Database
from quant_backtester.persistence.models import Run
from quant_backtester.results import result_hash_from_mapping

logger = logging.getLogger(__name__)

RESULT_COLUMNS = [
    "run_id",
    "run_name",
    "symbols",
    "short_window",
    "long_window",
    "initial_cash",
    "final_equity",
    "total_return",
    "annualized_return",
    "annualized_volatility",
    "sharpe",
    "sortino",
    "calmar",
    "max_drawdown",
    "total_commission",
    "total_slippage_cost",
    "realized_pnl",
    "halted",
    "halt_reason",
    "created_at",
    "completed_at",
    "result_hash",
    "extra",
]


def _number(result: dict[str, object], key: str) -> float:
    value = result[key]
    if isinstance(value, (int, float, str)):
        return float(value)
    raise TypeError(f"Result field {key!r} is not numeric")


def _integer(result: dict[str, object], key: str) -> int:
    return int(_number(result, key))


def _valid_param_pairs(
    short_windows: Iterable[int],
    long_windows: Iterable[int],
) -> list[tuple[int, int]]:
    return sorted(
        {
            (short_window, long_window)
            for short_window, long_window in product(short_windows, long_windows)
            if short_window > 0 and short_window < long_window
        }
    )


def _load_events(cfg: BacktestConfig) -> tuple[MarketEvent, ...]:
    return tuple(CSVDataHandler(cfg.csv_path, cfg.data).stream())


def _persist_models(database: Database | None, pending: list[Run]) -> None:
    if database is not None and pending:
        database.insert_runs_bulk(pending)
    pending.clear()


def run_parameter_sweep(
    cfg: BacktestConfig,
    short_windows: list[int],
    long_windows: list[int],
    export_csv: str | None = None,
    *,
    persist: bool = True,
) -> pd.DataFrame:
    database = Database(cfg.database_url) if persist else None
    try:
        return _run_parameter_sweep_impl(
            cfg,
            short_windows,
            long_windows,
            export_csv,
            database=database,
        )
    finally:
        if database is not None:
            database.close()


def _run_parameter_sweep_impl(
    cfg: BacktestConfig,
    short_windows: list[int],
    long_windows: list[int],
    export_csv: str | None,
    *,
    database: Database | None,
) -> pd.DataFrame:
    destination = Path(export_csv or (Path(cfg.out_dir) / "sweep_results.csv"))
    valid_pairs = _valid_param_pairs(short_windows, long_windows)
    events = _load_events(cfg)
    source = InMemoryDataHandler(events)
    if database is not None:
        database.prepare_schema()

    logger.info(
        "Sweep started",
        extra={
            "event": {
                "run_name": cfg.run_name,
                "parameter_count": len(valid_pairs),
                "persist": database is not None,
            }
        },
    )
    results: list[dict[str, object]] = []
    pending_models: list[Run] = []
    for short_window, long_window in valid_pairs:
        run_cfg = replace(
            cfg,
            short_window=short_window,
            long_window=long_window,
            run_name=f"{cfg.run_name}-sw{short_window}-lw{long_window}",
        )
        result = run_backtest(
            run_cfg,
            persist=False,
            data_source=source,
            write_artifact=False,
        )
        results.append(result)
        if database is not None:
            pending_models.append(run_to_model(result))
            if len(pending_models) >= 500:
                _persist_models(database, pending_models)
    _persist_models(database, pending_models)

    if results:
        frame = pd.DataFrame(results).sort_values(
            ["sharpe", "sortino", "total_return"],
            ascending=False,
        )
    else:
        frame = pd.DataFrame(columns=RESULT_COLUMNS)
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
    destination.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(destination, index=False)
    logger.info(
        "Sweep completed",
        extra={"event": {"run_count": len(results), "export_csv": str(destination)}},
    )
    return frame


def run_walk_forward(
    cfg: BacktestConfig,
    short_windows: list[int],
    long_windows: list[int],
    train_days: int,
    test_days: int,
    step_days: int,
    export_csv: str | None = None,
    *,
    purge_days: int = 0,
    embargo_days: int = 0,
    warmup_days: int | None = None,
    allow_test_overlap: bool = False,
    persist: bool = True,
) -> tuple[pd.DataFrame, dict[str, object]]:
    database = Database(cfg.database_url) if persist else None
    try:
        return _run_walk_forward_impl(
            cfg,
            short_windows,
            long_windows,
            train_days,
            test_days,
            step_days,
            export_csv,
            purge_days=purge_days,
            embargo_days=embargo_days,
            warmup_days=warmup_days,
            allow_test_overlap=allow_test_overlap,
            database=database,
        )
    finally:
        if database is not None:
            database.close()


def _run_walk_forward_impl(
    cfg: BacktestConfig,
    short_windows: list[int],
    long_windows: list[int],
    train_days: int,
    test_days: int,
    step_days: int,
    export_csv: str | None,
    *,
    purge_days: int,
    embargo_days: int,
    warmup_days: int | None,
    allow_test_overlap: bool,
    database: Database | None,
) -> tuple[pd.DataFrame, dict[str, object]]:
    _validate_walk_forward(
        train_days=train_days,
        test_days=test_days,
        step_days=step_days,
        purge_days=purge_days,
        embargo_days=embargo_days,
        warmup_days=warmup_days,
        allow_test_overlap=allow_test_overlap,
    )
    valid_pairs = _valid_param_pairs(short_windows, long_windows)
    if not valid_pairs:
        raise ValueError("No valid parameter pairs: require 0 < short_window < long_window")

    destination = Path(export_csv or (Path(cfg.out_dir) / "walk_forward_results.csv"))
    events = _load_events(cfg)
    events_by_date: dict[date, list[MarketEvent]] = {}
    for event in events:
        events_by_date.setdefault(event.timestamp.date(), []).append(event)
    unique_dates = sorted(events_by_date)
    resolved_warmup_days = (
        max(long_window for _, long_window in valid_pairs) if warmup_days is None else warmup_days
    )

    if database is not None:
        database.prepare_schema()

    logger.info(
        "Walk-forward started",
        extra={
            "event": {
                "run_name": cfg.run_name,
                "parameter_count": len(valid_pairs),
                "train_days": train_days,
                "test_days": test_days,
                "step_days": step_days,
                "purge_days": purge_days,
                "embargo_days": embargo_days,
                "warmup_days": resolved_warmup_days,
                "total_days": len(unique_dates),
                "persist": database is not None,
            }
        },
    )

    rows: list[dict[str, object]] = []
    pending_models: list[Run] = []
    cursor = 0
    window_index = 1
    required_days = train_days + purge_days + test_days

    while cursor + required_days <= len(unique_dates):
        train_dates = unique_dates[cursor : cursor + train_days]
        test_start = cursor + train_days + purge_days
        test_dates = unique_dates[test_start : test_start + test_days]
        train_events = _events_for_dates(events_by_date, train_dates)
        test_events = _events_for_dates(events_by_date, test_dates)
        if not train_events or not test_events:
            cursor += step_days + embargo_days
            window_index += 1
            continue

        best_result: dict[str, object] | None = None
        best_pair: tuple[int, int] | None = None
        for short_window, long_window in valid_pairs:
            train_cfg = replace(
                cfg,
                short_window=short_window,
                long_window=long_window,
                run_name=(
                    f"{cfg.run_name}-wf{window_index}-train-sw{short_window}-lw{long_window}"
                ),
            )
            candidate = run_backtest(
                train_cfg,
                persist=False,
                data_source=InMemoryDataHandler(train_events),
                write_artifact=False,
            )
            if best_result is None or _score(candidate) > _score(best_result):
                best_result = candidate
                best_pair = (short_window, long_window)

        assert best_result is not None
        assert best_pair is not None
        warmup_start = max(0, test_start - resolved_warmup_days)
        warmup_dates_view = unique_dates[warmup_start:test_start]
        warmup_events_view = _events_for_dates(events_by_date, warmup_dates_view)
        test_cfg = replace(
            cfg,
            short_window=best_pair[0],
            long_window=best_pair[1],
            run_name=(f"{cfg.run_name}-wf{window_index}-oos-sw{best_pair[0]}-lw{best_pair[1]}"),
        )
        out_of_sample = run_backtest(
            test_cfg,
            persist=False,
            data_source=InMemoryDataHandler(test_events),
            warmup_events=warmup_events_view,
            write_artifact=False,
        )
        raw_extra = out_of_sample.get("extra")
        out_of_sample_extra = dict(raw_extra) if isinstance(raw_extra, dict) else {}
        out_of_sample_extra["walk_forward"] = {
            "window_index": window_index,
            "train_start": train_dates[0].isoformat(),
            "train_end": train_dates[-1].isoformat(),
            "test_start": test_dates[0].isoformat(),
            "test_end": test_dates[-1].isoformat(),
            "purge_days": purge_days,
            "embargo_days": embargo_days,
            "warmup_days": resolved_warmup_days,
            "best_short_window": best_pair[0],
            "best_long_window": best_pair[1],
            "in_sample_total_return": _number(best_result, "total_return"),
            "in_sample_sharpe": _number(best_result, "sharpe"),
            "in_sample_sortino": _number(best_result, "sortino"),
            "in_sample_max_drawdown": _number(best_result, "max_drawdown"),
        }
        out_of_sample["extra"] = out_of_sample_extra
        out_of_sample["result_hash"] = result_hash_from_mapping(out_of_sample)

        if database is not None:
            pending_models.append(run_to_model(out_of_sample))
            if len(pending_models) >= 200:
                _persist_models(database, pending_models)

        rows.append(
            {
                "window_index": window_index,
                "train_start": train_dates[0].isoformat(),
                "train_end": train_dates[-1].isoformat(),
                "test_start": test_dates[0].isoformat(),
                "test_end": test_dates[-1].isoformat(),
                "best_short_window": best_pair[0],
                "best_long_window": best_pair[1],
                "train_total_return": _number(best_result, "total_return"),
                "train_sharpe": _number(best_result, "sharpe"),
                "train_sortino": _number(best_result, "sortino"),
                "train_max_drawdown": _number(best_result, "max_drawdown"),
                "oos_total_return": _number(out_of_sample, "total_return"),
                "oos_sharpe": _number(out_of_sample, "sharpe"),
                "oos_sortino": _number(out_of_sample, "sortino"),
                "oos_max_drawdown": _number(out_of_sample, "max_drawdown"),
                "oos_halted": bool(out_of_sample["halted"]),
                "oos_halt_reason": out_of_sample["halt_reason"],
                "oos_run_name": str(out_of_sample["run_name"]),
                "oos_run_id": str(out_of_sample["run_id"]),
                "oos_result_hash": str(out_of_sample["result_hash"]),
            }
        )
        cursor += step_days + embargo_days
        window_index += 1

    _persist_models(database, pending_models)
    frame = pd.DataFrame(rows).sort_values("window_index") if rows else pd.DataFrame()
    destination.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(destination, index=False)
    summary = _walk_forward_summary(frame, rows)
    logger.info(
        "Walk-forward completed",
        extra={
            "event": {
                "run_name": cfg.run_name,
                "window_count": len(frame),
                "stability_score": summary["stability_score"],
                "export_csv": str(destination),
            }
        },
    )
    return frame, summary


def _events_for_dates(
    events_by_date: dict[date, list[MarketEvent]],
    dates: Iterable[date],
) -> tuple[MarketEvent, ...]:
    return tuple(event for day in dates for event in events_by_date.get(day, ()))


def _score(result: dict[str, object]) -> tuple[float, float, float, float]:
    return (
        _number(result, "sharpe"),
        _number(result, "sortino"),
        -_number(result, "max_drawdown"),
        _number(result, "total_return"),
    )


def _validate_walk_forward(
    *,
    train_days: int,
    test_days: int,
    step_days: int,
    purge_days: int,
    embargo_days: int,
    warmup_days: int | None,
    allow_test_overlap: bool,
) -> None:
    for name, value in (
        ("train_days", train_days),
        ("test_days", test_days),
        ("step_days", step_days),
    ):
        if value <= 0:
            raise ValueError(f"{name} must be > 0")
    for name, value in (("purge_days", purge_days), ("embargo_days", embargo_days)):
        if value < 0:
            raise ValueError(f"{name} must be >= 0")
    if warmup_days is not None and warmup_days < 0:
        raise ValueError("warmup_days must be >= 0")
    if not allow_test_overlap and step_days < test_days:
        raise ValueError(
            "step_days must be >= test_days unless allow_test_overlap is explicitly enabled"
        )


def _walk_forward_summary(
    frame: pd.DataFrame,
    rows: list[dict[str, object]],
) -> dict[str, object]:
    if frame.empty:
        return {
            "window_count": 0,
            "stability_score": 0.0,
            "most_common_short_window": None,
            "most_common_long_window": None,
            "oos_compounded_return": 0.0,
            "oos_mean_return": 0.0,
            "oos_mean_sharpe": 0.0,
            "oos_mean_sortino": 0.0,
            "oos_max_drawdown": 0.0,
        }

    pair_counter = Counter(
        (
            cast(int, row["best_short_window"]),
            cast(int, row["best_long_window"]),
        )
        for row in rows
    )
    (common_short, common_long), common_count = pair_counter.most_common(1)[0]
    returns = frame["oos_total_return"].astype(float)
    sharpes = frame["oos_sharpe"].astype(float)
    sortinos = frame["oos_sortino"].astype(float)
    drawdowns = frame["oos_max_drawdown"].astype(float)
    return {
        "window_count": int(len(frame)),
        "stability_score": float(common_count / len(frame)),
        "most_common_short_window": common_short,
        "most_common_long_window": common_long,
        "oos_compounded_return": float((1.0 + returns).prod() - 1.0),
        "oos_mean_return": float(returns.mean()),
        "oos_mean_sharpe": float(sharpes.mean()),
        "oos_mean_sortino": float(sortinos.mean()),
        "oos_max_drawdown": float(drawdowns.max()),
    }
