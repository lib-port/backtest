from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from quant_backtester.backtest import run_backtest
from quant_backtester.config import BacktestConfig
from quant_backtester.events import MarketEvent, Side
from quant_backtester.strategy.moving_average import MovingAverageCrossStrategy


@dataclass(frozen=True)
class _Events:
    events: tuple[MarketEvent, ...]

    def stream(self) -> Iterator[MarketEvent]:
        yield from self.events


def _event(day: int, mid: float) -> MarketEvent:
    return MarketEvent(
        timestamp=dt.datetime(2024, 1, day, tzinfo=dt.UTC),
        symbol="AAPL",
        mid=mid,
        spread_bps=0.0,
        volume=10_000.0,
    )


def test_operational_paths_and_names_do_not_change_result_hash(tmp_path: Path) -> None:
    common = {
        "symbols": ("AAPL",),
        "csv_path": "data/sample_prices.csv",
        "short_window": 2,
        "long_window": 3,
    }
    first = run_backtest(
        BacktestConfig(
            **common,
            run_name="first-name",
            out_dir=str(tmp_path / "one"),
            database_url=f"sqlite:///{tmp_path / 'one.db'}",
        ),
        persist=False,
        write_artifact=False,
    )
    second = run_backtest(
        BacktestConfig(
            **common,
            run_name="second-name",
            out_dir=str(tmp_path / "two"),
            database_url=f"sqlite:///{tmp_path / 'two.db'}",
        ),
        persist=False,
        write_artifact=False,
    )

    assert first["final_equity"] == second["final_equity"]
    assert first["result_hash"] == second["result_hash"]


def test_warmup_populates_indicators_without_suppressing_first_oos_signal() -> None:
    strategy = MovingAverageCrossStrategy(
        symbols=("AAPL",),
        short_window=1,
        long_window=2,
    )
    strategy.warmup(_event(1, 100.0))
    strategy.warmup(_event(2, 101.0))

    signal = strategy.on_market(_event(3, 102.0))

    assert signal is not None
    assert signal.side == Side.BUY


def test_warmup_data_is_bound_into_manifest_and_result_hash(tmp_path: Path) -> None:
    cfg = BacktestConfig(
        symbols=("AAPL",),
        csv_path="unused.csv",
        short_window=1,
        long_window=2,
        out_dir=str(tmp_path),
    )
    test_events = _Events((_event(3, 102.0), _event(4, 103.0)))

    first = run_backtest(
        cfg,
        persist=False,
        write_artifact=False,
        data_source=test_events,
        warmup_events=(_event(1, 100.0), _event(2, 101.0)),
    )
    second = run_backtest(
        cfg,
        persist=False,
        write_artifact=False,
        data_source=test_events,
        warmup_events=(_event(1, 90.0), _event(2, 91.0)),
    )

    first_manifest = first["extra"]["manifest"]  # type: ignore[index]
    second_manifest = second["extra"]["manifest"]  # type: ignore[index]
    assert first_manifest["warmup_event_count"] == 2  # type: ignore[index]
    assert first_manifest["warmup_hash"] != second_manifest["warmup_hash"]  # type: ignore[index]
    assert first["result_hash"] != second["result_hash"]
