from __future__ import annotations

from datetime import datetime

from quant_backtester.config import ExecutionConfig, MicrostructureConfig
from quant_backtester.events import MarketEvent, OrderEvent, Side
from quant_backtester.execution.simulated_execution import SimulatedExecutionHandler


def test_market_order_partially_fills_across_ticks() -> None:
    cfg = ExecutionConfig(
        micro=MicrostructureConfig(
            latency_events=0,
            default_tick_volume=100.0,
            max_participation_rate=0.5,  # max 50 shares per tick
            queue_ahead_fraction=0.0,
            base_fill_probability=1.0,
        )
    )
    ex = SimulatedExecutionHandler(commission_per_trade=0.0, cfg=cfg, rng_seed=1)
    ts = datetime(2020, 1, 1)
    mkt = MarketEvent(timestamp=ts, symbol="AAPL", mid=100.0, spread_bps=0.0, volume=100.0)
    ex.submit(OrderEvent(timestamp=ts, symbol="AAPL", side=Side.BUY, quantity=120))

    fills1 = ex.on_market(mkt)
    fills2 = ex.on_market(mkt)
    fills3 = ex.on_market(mkt)

    total_qty = sum(f.quantity for f in [*fills1, *fills2, *fills3])
    assert total_qty == 120


def test_commission_charged_once_for_partially_filled_order() -> None:
    cfg = ExecutionConfig(
        micro=MicrostructureConfig(
            latency_events=0,
            default_tick_volume=100.0,
            max_participation_rate=0.5,
            queue_ahead_fraction=0.0,
            base_fill_probability=1.0,
        )
    )
    ex = SimulatedExecutionHandler(commission_per_trade=2.5, cfg=cfg, rng_seed=1)
    ts = datetime(2020, 1, 1)
    mkt = MarketEvent(timestamp=ts, symbol="AAPL", mid=100.0, spread_bps=0.0, volume=100.0)
    ex.submit(OrderEvent(timestamp=ts, symbol="AAPL", side=Side.BUY, quantity=120))

    fills = [*ex.on_market(mkt), *ex.on_market(mkt), *ex.on_market(mkt)]

    assert sum(f.quantity for f in fills) == 120
    assert sum(f.commission for f in fills) == 2.5
