from datetime import datetime

from quant_backtester.config import ExecutionConfig, MicrostructureConfig
from quant_backtester.events import MarketEvent, OrderEvent, Side
from quant_backtester.execution.simulated_execution import SimulatedExecutionHandler


def test_slippage_signs_and_latency_fill() -> None:
    exec_cfg = ExecutionConfig(
        default_spread_bps=10.0,
        impact_bps_per_unit=2.0,
        impact_volume=1000.0,
        micro=MicrostructureConfig(
            latency_events=1, default_tick_volume=1000.0, max_participation_rate=1.0
        ),
    )
    ex = SimulatedExecutionHandler(commission_per_trade=1.0, cfg=exec_cfg, rng_seed=1)

    mkt = MarketEvent(
        timestamp=datetime(2020, 1, 1), symbol="AAPL", mid=100.0, spread_bps=10.0, volume=1000.0
    )
    ex.submit(OrderEvent(timestamp=mkt.timestamp, symbol="AAPL", side=Side.BUY, quantity=100))
    fills0 = ex.on_market(
        mkt
    )  # becomes eligible after 1 tick; first on_market advances tick to 1 and can fill
    assert len(fills0) == 1
    buy = fills0[0]
    assert buy.fill_price > mkt.mid
    assert buy.slippage > 0

    ex.submit(OrderEvent(timestamp=mkt.timestamp, symbol="AAPL", side=Side.SELL, quantity=100))
    fills1 = ex.on_market(mkt)
    assert len(fills1) == 1
    sell = fills1[0]
    assert sell.fill_price < mkt.mid
    assert sell.slippage < 0
