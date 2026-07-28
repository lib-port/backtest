from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from quant_backtester.backtest import run_backtest
from quant_backtester.config import (
    BacktestConfig,
    DataConfig,
    ExecutionConfig,
    MicrostructureConfig,
    RiskConfig,
)
from quant_backtester.data.csv_data_handler import CSVDataHandler
from quant_backtester.events import (
    FillEvent,
    MarketEvent,
    OrderEvent,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
)
from quant_backtester.execution.simulated_execution import SimulatedExecutionHandler
from quant_backtester.portfolio.simple_portfolio import MultiAssetPortfolio


def _immediate_execution() -> SimulatedExecutionHandler:
    return SimulatedExecutionHandler(
        commission_per_trade=0.0,
        cfg=ExecutionConfig(
            impact_bps_per_unit=0.0,
            micro=MicrostructureConfig(
                latency_events=0,
                max_participation_rate=1.0,
                queue_ahead_fraction=0.0,
                base_fill_probability=1.0,
            ),
        ),
    )


def test_csv_normalizes_timezone_and_rejects_duplicates(tmp_path: Path) -> None:
    path = tmp_path / "prices.csv"
    pd.DataFrame(
        [
            {"date": "2025-01-01 10:00:00", "symbol": "aapl", "mid": 100},
            {"date": "2025-01-01 10:00:00", "symbol": "aapl", "mid": 101},
        ]
    ).to_csv(path, index=False)
    handler = CSVDataHandler(
        str(path),
        DataConfig(source_timezone="Australia/Sydney", duplicate_policy="error", chunksize=1),
    )
    with pytest.raises(ValueError, match="Duplicate market event"):
        list(handler.stream())

    last_wins = CSVDataHandler(
        str(path),
        DataConfig(source_timezone="Australia/Sydney", duplicate_policy="last", chunksize=1),
    )
    events = list(last_wins.stream())
    assert len(events) == 1
    assert events[0].timestamp.tzinfo is UTC
    assert events[0].mid == 101


def test_csv_rejects_out_of_order_rows(tmp_path: Path) -> None:
    path = tmp_path / "unsorted.csv"
    pd.DataFrame(
        [
            {"date": "2025-01-02", "symbol": "AAPL", "mid": 100},
            {"date": "2025-01-01", "symbol": "AAPL", "mid": 99},
        ]
    ).to_csv(path, index=False)
    with pytest.raises(ValueError, match="must be sorted"):
        list(CSVDataHandler(str(path)).stream())


def test_stop_and_ioc_order_lifecycle() -> None:
    execution = _immediate_execution()
    timestamp = datetime(2025, 1, 1, tzinfo=UTC)
    stop_id = execution.submit(
        OrderEvent(
            timestamp,
            "AAPL",
            Side.BUY,
            10,
            order_type=OrderType.STOP,
            stop_price=105.0,
            client_order_id="stop-1",
        )
    )
    assert execution.on_market(MarketEvent(timestamp, "AAPL", 100.0, volume=100.0)) == []
    fills = execution.on_market(
        MarketEvent(timestamp + timedelta(minutes=1), "AAPL", 106.0, volume=100.0)
    )
    assert len(fills) == 1
    assert execution.order_snapshot(stop_id).status == OrderStatus.FILLED  # type: ignore[union-attr]

    ioc_id = execution.submit(
        OrderEvent(
            timestamp,
            "AAPL",
            Side.BUY,
            10,
            order_type=OrderType.LIMIT,
            limit_price=90.0,
            time_in_force=TimeInForce.IOC,
        )
    )
    assert execution.on_market(MarketEvent(timestamp, "AAPL", 100.0, volume=100.0)) == []
    assert execution.order_snapshot(ioc_id).status == OrderStatus.CANCELED  # type: ignore[union-attr]


def test_untriggered_stop_ioc_waits_even_when_tick_has_no_capacity() -> None:
    execution = _immediate_execution()
    timestamp = datetime(2025, 1, 1, tzinfo=UTC)
    order_id = execution.submit(
        OrderEvent(
            timestamp,
            "AAPL",
            Side.SELL,
            10,
            order_type=OrderType.STOP,
            stop_price=95.0,
            time_in_force=TimeInForce.IOC,
        )
    )
    assert execution.on_market(MarketEvent(timestamp, "AAPL", 100.0, volume=0.0)) == []
    assert execution.order_snapshot(order_id).status == OrderStatus.ACCEPTED  # type: ignore[union-attr]


def test_ioc_behind_resting_order_cancels_when_tick_has_no_capacity() -> None:
    execution = _immediate_execution()
    timestamp = datetime(2025, 1, 1, tzinfo=UTC)
    execution.submit(OrderEvent(timestamp, "AAPL", Side.BUY, 10))
    ioc_id = execution.submit(
        OrderEvent(
            timestamp,
            "AAPL",
            Side.BUY,
            10,
            time_in_force=TimeInForce.IOC,
        )
    )

    assert execution.on_market(MarketEvent(timestamp, "AAPL", 100.0, volume=0.0)) == []
    assert execution.order_snapshot(ioc_id).status == OrderStatus.CANCELED  # type: ignore[union-attr]


def test_execution_id_replay_is_accounting_noop() -> None:
    portfolio = MultiAssetPortfolio(initial_cash=10_000.0, risk_cfg=RiskConfig())
    timestamp = datetime(2025, 1, 1, tzinfo=UTC)
    portfolio.update_market_price("AAPL", 100.0)
    fill = FillEvent(
        timestamp,
        "AAPL",
        Side.BUY,
        10,
        100.0,
        1.0,
        0.0,
        order_id="o-1",
        execution_id="e-1",
        mid_price=100.0,
    )
    assert portfolio.on_fill(fill)
    equity = portfolio.equity()
    assert not portfolio.on_fill(fill)
    assert portfolio.equity() == equity
    assert portfolio.get_position("AAPL").quantity == 10


def test_equity_has_one_observation_per_timestamp(tmp_path: Path) -> None:
    result = run_backtest(
        BacktestConfig(
            symbols=("AAPL", "MSFT"),
            csv_path="data/sample_prices.csv",
            out_dir=str(tmp_path),
            short_window=5,
            long_window=10,
        ),
        persist=False,
    )
    frame = pd.read_csv("data/sample_prices.csv")
    unique_timestamps = pd.to_datetime(frame["date"], utc=True).nunique()
    extra = result["extra"]
    assert isinstance(extra, dict)
    assert extra["equity_observation_count"] == unique_timestamps


def test_non_persistent_run_does_not_create_output_directory(tmp_path: Path) -> None:
    output = tmp_path / "must-not-exist"
    run_backtest(
        BacktestConfig(
            symbols=("AAPL",),
            csv_path="data/sample_prices.csv",
            out_dir=str(output),
            short_window=5,
            long_window=10,
        ),
        persist=False,
    )
    assert not output.exists()
