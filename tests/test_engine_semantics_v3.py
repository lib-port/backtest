from __future__ import annotations

import datetime as dt
import math
from dataclasses import replace

import pytest

from quant_backtester.backtest import run_backtest
from quant_backtester.config import (
    BacktestConfig,
    EndOfRunConfig,
    ExecutionConfig,
    MicrostructureConfig,
    RiskConfig,
)
from quant_backtester.data.csv_data_handler import InMemoryDataHandler
from quant_backtester.events import (
    EquityPoint,
    FillEvent,
    MarketEvent,
    OrderEvent,
    OrderType,
    Side,
    SignalEvent,
)
from quant_backtester.execution.simulated_execution import SimulatedExecutionHandler
from quant_backtester.portfolio.simple_portfolio import MultiAssetPortfolio
from quant_backtester.utils.metrics import summarize_performance


class BuyOnce:
    def __init__(self) -> None:
        self.sent = False

    def warmup(self, _market: MarketEvent) -> None:
        pass

    def on_market(self, market: MarketEvent) -> SignalEvent | None:
        if self.sent:
            return None
        self.sent = True
        return SignalEvent(market.timestamp, market.symbol, Side.BUY)


def _probabilistic_execution() -> SimulatedExecutionHandler:
    return SimulatedExecutionHandler(
        commission_per_trade=0.0,
        cfg=ExecutionConfig(
            impact_bps_per_unit=0.0,
            micro=MicrostructureConfig(
                latency_events=0,
                default_tick_volume=1_000.0,
                max_participation_rate=1.0,
                queue_ahead_fraction=0.5,
                base_fill_probability=0.8,
            ),
        ),
        rng_seed=123,
    )


def test_queue_rng_is_independent_between_symbols() -> None:
    timestamp = dt.datetime(2025, 1, 1, tzinfo=dt.UTC)
    a_market = MarketEvent(timestamp, "AAPL", 100.0, bid=99.0, ask=101.0, volume=1_000)
    b_market = MarketEvent(timestamp, "MSFT", 200.0, bid=199.0, ask=201.0, volume=1_000)
    a_order = OrderEvent(
        timestamp,
        "AAPL",
        Side.BUY,
        10,
        order_type=OrderType.LIMIT,
        limit_price=101.0,
        client_order_id="stable-a-order",
    )

    isolated = _probabilistic_execution()
    isolated.submit(a_order)
    isolated_result = isolated.on_market(a_market)

    with_unrelated_activity = _probabilistic_execution()
    with_unrelated_activity.submit(
        OrderEvent(
            timestamp,
            "MSFT",
            Side.BUY,
            10,
            order_type=OrderType.LIMIT,
            limit_price=201.0,
            client_order_id="unrelated-b-order",
        )
    )
    with_unrelated_activity.on_market(b_market)
    with_unrelated_activity.submit(a_order)
    combined_result = with_unrelated_activity.on_market(a_market)

    assert bool(combined_result) == bool(isolated_result)
    if isolated_result:
        assert combined_result[0].fill_price == isolated_result[0].fill_price
        assert combined_result[0].quantity == isolated_result[0].quantity


def test_end_of_run_liquidation_closes_positions_and_records_costs() -> None:
    start = dt.datetime(2025, 1, 1, tzinfo=dt.UTC)
    events = (
        MarketEvent(start, "AAPL", 100.0, bid=99.5, ask=100.5, volume=1_000),
        MarketEvent(
            start + dt.timedelta(days=1),
            "AAPL",
            110.0,
            bid=109.5,
            ask=110.5,
            volume=1_000,
        ),
    )
    config = BacktestConfig(
        symbols=("AAPL",),
        short_window=1,
        long_window=2,
        trade_quantity=10,
        commission_per_trade=1.0,
        end_of_run=EndOfRunConfig(position_policy="liquidate"),
        execution=ExecutionConfig(
            impact_bps_per_unit=0.0,
            micro=MicrostructureConfig(
                latency_events=0,
                max_participation_rate=1.0,
                queue_ahead_fraction=0.0,
                base_fill_probability=1.0,
            ),
        ),
    )

    result = run_backtest(
        config,
        persist=False,
        data_source=InMemoryDataHandler(events),
        strategy=BuyOnce(),
    )
    extra = result["extra"]
    assert isinstance(extra, dict)
    assert extra["open_positions"] == {}
    assert extra["end_liquidation_count"] == 1
    assert result["total_commission"] == pytest.approx(2.0)


def test_require_flat_and_required_symbols_fail_closed() -> None:
    start = dt.datetime(2025, 1, 1, tzinfo=dt.UTC)
    events = (
        MarketEvent(start, "AAPL", 100.0, volume=1_000),
        MarketEvent(start + dt.timedelta(days=1), "AAPL", 101.0, volume=1_000),
    )
    config = BacktestConfig(
        symbols=("AAPL",),
        short_window=1,
        long_window=2,
        end_of_run=EndOfRunConfig(position_policy="require_flat"),
        execution=ExecutionConfig(
            impact_bps_per_unit=0.0,
            micro=MicrostructureConfig(
                latency_events=0,
                max_participation_rate=1.0,
                queue_ahead_fraction=0.0,
                base_fill_probability=1.0,
            ),
        ),
    )
    with pytest.raises(ValueError, match="requires a flat portfolio"):
        run_backtest(
            config,
            persist=False,
            data_source=InMemoryDataHandler(events),
            strategy=BuyOnce(),
        )

    with pytest.raises(ValueError, match="MSFT"):
        run_backtest(
            replace(config, symbols=("MSFT",)),
            persist=False,
            data_source=InMemoryDataHandler(events),
            strategy=BuyOnce(),
        )


def test_order_and_daily_risk_limits_fail_closed() -> None:
    timestamp = dt.datetime(2025, 1, 1, tzinfo=dt.UTC)
    portfolio = MultiAssetPortfolio(
        initial_cash=100_000,
        risk_cfg=RiskConfig(
            allow_short=False,
            max_order_notional=500,
            max_net_exposure=1_000,
            max_daily_loss_pct=0.004,
            max_drawdown_pct=1.0,
        ),
    )
    assert not portfolio.can_place_order("AAPL", Side.BUY, 1)
    portfolio.update_market_price("AAPL", 100)
    assert not portfolio.can_place_order("AAPL", Side.SELL, 1)
    assert portfolio.can_place_order("AAPL", Side.BUY, 5)
    assert not portfolio.can_place_order("AAPL", Side.BUY, 6)

    portfolio.on_fill(
        FillEvent(
            timestamp,
            "AAPL",
            Side.BUY,
            5,
            100,
            0,
            0,
            execution_id="risk-fill",
        )
    )
    portfolio.snapshot_equity(timestamp)
    portfolio.update_market_price("AAPL", 1)
    portfolio.snapshot_equity(timestamp + dt.timedelta(days=1))
    assert portfolio.risk_state.trading_halted
    assert "daily loss" in str(portfolio.risk_state.halt_reason).lower()


def test_richer_metrics_are_finite_and_risk_adjusted() -> None:
    start = dt.datetime(2025, 1, 1, tzinfo=dt.UTC)
    points = [
        EquityPoint(start + dt.timedelta(days=index), equity)
        for index, equity in enumerate((100.0, 102.0, 101.0, 104.0))
    ]
    metrics = summarize_performance(
        points,
        initial_equity=100.0,
        configured_periods_per_year=252.0,
        annual_risk_free_rate=0.02,
    )
    assert metrics.total_return == pytest.approx(0.04)
    assert metrics.annualized_return > metrics.total_return
    assert metrics.annualized_volatility > 0
    assert metrics.max_drawdown == pytest.approx(1 / 102)
    assert all(
        math.isfinite(value)
        for value in (
            metrics.sharpe,
            metrics.sortino,
            metrics.calmar,
        )
    )
