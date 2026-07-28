from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

import quant_backtester.backtest as backtest_module
from quant_backtester.config import (
    BacktestConfig,
    ExecutionConfig,
    MicrostructureConfig,
    RiskConfig,
)
from quant_backtester.events import MarketEvent, OrderEvent, Side, SignalEvent
from quant_backtester.execution.simulated_execution import SimulatedExecutionHandler
from quant_backtester.portfolio.simple_portfolio import MultiAssetPortfolio


def _execution_config(*, latency_events: int, tick_volume: float = 1_000.0) -> ExecutionConfig:
    return ExecutionConfig(
        default_spread_bps=0.0,
        impact_bps_per_unit=0.0,
        micro=MicrostructureConfig(
            latency_events=latency_events,
            default_tick_volume=tick_volume,
            max_participation_rate=1.0,
            queue_ahead_fraction=0.0,
            base_fill_probability=1.0,
        ),
    )


def test_final_tick_fill_is_included_in_reported_equity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    base = datetime(2020, 1, 1)

    class ThreeTicks:
        def __init__(self, _path: str) -> None:
            pass

        def stream(self) -> Iterator[MarketEvent]:
            yield MarketEvent(base, "A", 100.0, bid=99.0, ask=101.0, volume=1_000.0)
            yield MarketEvent(
                base + timedelta(days=1),
                "A",
                110.0,
                bid=109.0,
                ask=111.0,
                volume=1_000.0,
            )
            yield MarketEvent(
                base + timedelta(days=2),
                "A",
                100.0,
                bid=99.0,
                ask=101.0,
                volume=1_000.0,
            )

    monkeypatch.setattr(backtest_module, "CSVDataHandler", ThreeTicks)
    cfg = BacktestConfig(
        symbols=("A",),
        csv_path="unused.csv",
        out_dir=str(tmp_path),
        short_window=1,
        long_window=2,
        commission_per_trade=1.0,
        execution=_execution_config(latency_events=1),
        risk=RiskConfig(max_drawdown_pct=1.0),
    )

    result = backtest_module.run_backtest(cfg, persist=False)

    assert result["total_commission"] == pytest.approx(1.0)
    assert result["total_slippage_cost"] == pytest.approx(100.0)
    assert result["final_equity"] == pytest.approx(99_899.0)
    assert result["total_return"] == pytest.approx(-0.00101)
    assert result["max_drawdown"] == pytest.approx(0.00101)

    extra = cast(dict[str, object], result["extra"])
    assert extra["pending_order_count"] == 1


@pytest.mark.parametrize(
    ("side", "limit_price", "expected_price"),
    [
        (Side.BUY, 101.05, 101.05),
        (Side.SELL, 98.95, 98.95),
    ],
)
def test_limit_fill_price_never_crosses_limit(
    side: Side, limit_price: float, expected_price: float
) -> None:
    cfg = ExecutionConfig(
        impact_bps_per_unit=100.0,
        impact_volume=1_000.0,
        micro=MicrostructureConfig(
            latency_events=0,
            default_tick_volume=1_000.0,
            max_participation_rate=1.0,
            queue_ahead_fraction=0.0,
            base_fill_probability=1.0,
        ),
    )
    execution = SimulatedExecutionHandler(commission_per_trade=0.0, cfg=cfg, rng_seed=1)
    market = MarketEvent(
        datetime(2020, 1, 1),
        "A",
        100.0,
        bid=99.0,
        ask=101.0,
        volume=1_000.0,
    )
    order_id = execution.submit(
        OrderEvent(
            market.timestamp,
            "A",
            side,
            100,
            order_type="LIMIT",
            limit_price=limit_price,
        )
    )

    fills = execution.on_market(market)

    assert len(fills) == 1
    assert fills[0].order_id == order_id
    assert fills[0].quantity == 50
    assert fills[0].fill_price == pytest.approx(expected_price)
    assert execution.remaining_quantity(order_id) == 50
    if side == Side.BUY:
        assert fills[0].fill_price <= limit_price
    else:
        assert fills[0].fill_price >= limit_price


@pytest.mark.parametrize(
    ("side", "limit_price"),
    [
        (Side.BUY, 101.0),
        (Side.SELL, 99.0),
    ],
)
def test_limit_order_waits_when_impact_has_no_price_headroom(
    side: Side, limit_price: float
) -> None:
    cfg = ExecutionConfig(
        impact_bps_per_unit=100.0,
        impact_volume=1_000.0,
        micro=MicrostructureConfig(
            latency_events=0,
            default_tick_volume=1_000.0,
            max_participation_rate=1.0,
            queue_ahead_fraction=0.0,
            base_fill_probability=1.0,
        ),
    )
    execution = SimulatedExecutionHandler(commission_per_trade=0.0, cfg=cfg, rng_seed=1)
    market = MarketEvent(
        datetime(2020, 1, 1),
        "A",
        100.0,
        bid=99.0,
        ask=101.0,
        volume=1_000.0,
    )
    order_id = execution.submit(
        OrderEvent(
            market.timestamp,
            "A",
            side,
            100,
            order_type="LIMIT",
            limit_price=limit_price,
        )
    )

    assert execution.on_market(market) == []
    assert execution.remaining_quantity(order_id) == 100


def test_partial_limit_fills_remain_bounded_across_ticks() -> None:
    cfg = ExecutionConfig(
        impact_bps_per_unit=100.0,
        impact_volume=1_000.0,
        micro=MicrostructureConfig(
            latency_events=0,
            default_tick_volume=50.0,
            max_participation_rate=1.0,
            queue_ahead_fraction=0.0,
            base_fill_probability=1.0,
        ),
    )
    execution = SimulatedExecutionHandler(commission_per_trade=0.0, cfg=cfg, rng_seed=1)
    market = MarketEvent(
        datetime(2020, 1, 1),
        "A",
        100.0,
        bid=99.0,
        ask=101.0,
        volume=50.0,
    )
    order_id = execution.submit(
        OrderEvent(
            market.timestamp,
            "A",
            Side.BUY,
            100,
            order_type="LIMIT",
            limit_price=101.05,
        )
    )

    first_fill = execution.on_market(market)
    second_fill = execution.on_market(market)

    fills = [*first_fill, *second_fill]
    assert [fill.quantity for fill in fills] == [50, 50]
    assert all(fill.fill_price <= 101.05 for fill in fills)
    assert execution.remaining_quantity(order_id) is None


@pytest.mark.parametrize(
    ("side", "non_marketable_limit", "marketable_limit"),
    [
        (Side.BUY, 100.99, 101.0),
        (Side.SELL, 99.01, 99.0),
    ],
)
def test_limit_touch_uses_synthetic_quote_when_quotes_are_absent(
    side: Side, non_marketable_limit: float, marketable_limit: float
) -> None:
    cfg = ExecutionConfig(
        default_spread_bps=200.0,
        impact_bps_per_unit=0.0,
        impact_volume=1_000.0,
        micro=MicrostructureConfig(
            latency_events=0,
            default_tick_volume=1_000.0,
            max_participation_rate=1.0,
            queue_ahead_fraction=0.0,
            base_fill_probability=1.0,
        ),
    )
    market = MarketEvent(
        datetime(2020, 1, 1),
        "A",
        100.0,
        spread_bps=200.0,
        volume=1_000.0,
    )

    waiting = SimulatedExecutionHandler(commission_per_trade=0.0, cfg=cfg, rng_seed=1)
    waiting.submit(
        OrderEvent(
            market.timestamp,
            "A",
            side,
            100,
            order_type="LIMIT",
            limit_price=non_marketable_limit,
        )
    )
    assert waiting.on_market(market) == []

    filling = SimulatedExecutionHandler(commission_per_trade=0.0, cfg=cfg, rng_seed=1)
    filling.submit(
        OrderEvent(
            market.timestamp,
            "A",
            side,
            100,
            order_type="LIMIT",
            limit_price=marketable_limit,
        )
    )
    fills = filling.on_market(market)

    assert len(fills) == 1
    if side == Side.BUY:
        assert fills[0].fill_price <= marketable_limit
    else:
        assert fills[0].fill_price >= marketable_limit


def test_delayed_partial_stop_submits_once_and_finishes_flat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    base = datetime(2020, 1, 1)
    portfolios: list[MultiAssetPortfolio] = []
    executions: list[TrackingExecution] = []

    class NineTicks:
        def __init__(self, _path: str) -> None:
            pass

        def stream(self) -> Iterator[MarketEvent]:
            prices = (100.0, 100.0, 100.0, 100.0, 90.0, 100.0, 90.0, 90.0, 90.0)
            for day, price in enumerate(prices):
                yield MarketEvent(
                    base + timedelta(days=day),
                    "A",
                    price,
                    spread_bps=0.0,
                    volume=50.0,
                )

    class BuyThenQueueAnother:
        def __init__(self, **_kwargs: object) -> None:
            self.events_seen = 0

        def on_market(self, market: MarketEvent) -> SignalEvent | None:
            self.events_seen += 1
            if self.events_seen in (1, 4):
                return SignalEvent(market.timestamp, market.symbol, Side.BUY)
            return None

    class TrackingExecution(SimulatedExecutionHandler):
        def __post_init__(self) -> None:
            super().__post_init__()
            self.submitted_orders: list[OrderEvent] = []
            executions.append(self)

        def submit(self, order: OrderEvent) -> str:
            self.submitted_orders.append(order)
            return super().submit(order)

    real_portfolio = backtest_module.MultiAssetPortfolio

    def capture_portfolio(*, initial_cash: float, risk_cfg: RiskConfig) -> MultiAssetPortfolio:
        portfolio = real_portfolio(initial_cash=initial_cash, risk_cfg=risk_cfg)
        portfolios.append(portfolio)
        return portfolio

    monkeypatch.setattr(backtest_module, "CSVDataHandler", NineTicks)
    monkeypatch.setattr(backtest_module, "MovingAverageCrossStrategy", BuyThenQueueAnother)
    monkeypatch.setattr(backtest_module, "SimulatedExecutionHandler", TrackingExecution)
    monkeypatch.setattr(backtest_module, "MultiAssetPortfolio", capture_portfolio)

    cfg = BacktestConfig(
        symbols=("A",),
        csv_path="unused.csv",
        out_dir=str(tmp_path),
        short_window=1,
        long_window=2,
        trade_quantity=100,
        commission_per_trade=0.0,
        execution=_execution_config(latency_events=2, tick_volume=50.0),
        risk=RiskConfig(stop_loss_pct=0.05, max_drawdown_pct=1.0),
    )

    result = backtest_module.run_backtest(cfg, persist=False)

    portfolio = portfolios[0]
    position = portfolio.get_position("A")
    stop_orders = [order for order in executions[0].submitted_orders if order.intent == "STOP_LOSS"]
    strategy_orders = [
        order for order in executions[0].submitted_orders if order.intent == "STRATEGY"
    ]
    assert position.quantity == 0
    assert len(stop_orders) == 1
    assert len(strategy_orders) == 2
    extra = cast(dict[str, object], result["extra"])
    assert extra["pending_order_count"] == 0
    assert extra["active_stop_symbols"] == []


def test_reduce_only_orders_cannot_reverse_a_position() -> None:
    execution = SimulatedExecutionHandler(
        commission_per_trade=0.0,
        cfg=_execution_config(latency_events=0),
        rng_seed=1,
    )
    market = MarketEvent(
        datetime(2020, 1, 1),
        "A",
        100.0,
        spread_bps=0.0,
        volume=1_000.0,
    )
    first_id = execution.submit(
        OrderEvent(
            market.timestamp,
            "A",
            Side.SELL,
            100,
            intent="STOP_LOSS",
            reduce_only=True,
        )
    )
    second_id = execution.submit(
        OrderEvent(
            market.timestamp,
            "A",
            Side.SELL,
            100,
            intent="STOP_LOSS",
            reduce_only=True,
        )
    )

    fills = execution.on_market(market, position_quantity=100)

    assert sum(fill.quantity for fill in fills) == 100
    assert fills[0].order_id == first_id
    assert not execution.is_pending(first_id)
    assert not execution.is_pending(second_id)
