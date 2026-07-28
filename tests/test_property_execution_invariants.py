from __future__ import annotations

from datetime import datetime

import pytest

hypothesis = pytest.importorskip("hypothesis")
given = hypothesis.given
settings = hypothesis.settings
st = hypothesis.strategies

from quant_backtester.config import ExecutionConfig, MicrostructureConfig  # noqa: E402
from quant_backtester.events import MarketEvent, OrderEvent, Side  # noqa: E402
from quant_backtester.execution.simulated_execution import (  # noqa: E402
    SimulatedExecutionHandler,
)


@settings(max_examples=100, deadline=None)
@given(
    side=st.sampled_from([Side.BUY, Side.SELL]),
    mid=st.floats(
        min_value=10.0,
        max_value=10_000.0,
        allow_nan=False,
        allow_infinity=False,
    ),
    half_spread_fraction=st.floats(
        min_value=0.0,
        max_value=0.05,
        allow_nan=False,
        allow_infinity=False,
    ),
    limit_headroom_fraction=st.floats(
        min_value=0.0,
        max_value=0.05,
        allow_nan=False,
        allow_infinity=False,
    ),
    quantity=st.integers(min_value=1, max_value=10_000),
    impact_bps_per_unit=st.floats(
        min_value=0.0,
        max_value=1_000.0,
        allow_nan=False,
        allow_infinity=False,
    ),
)
def test_limit_fills_always_respect_price_boundary(
    side: Side,
    mid: float,
    half_spread_fraction: float,
    limit_headroom_fraction: float,
    quantity: int,
    impact_bps_per_unit: float,
) -> None:
    bid = mid * (1.0 - half_spread_fraction)
    ask = mid * (1.0 + half_spread_fraction)
    if side == Side.BUY:
        limit_price = ask * (1.0 + limit_headroom_fraction)
    else:
        limit_price = bid * (1.0 - limit_headroom_fraction)

    cfg = ExecutionConfig(
        impact_bps_per_unit=impact_bps_per_unit,
        impact_volume=1_000.0,
        micro=MicrostructureConfig(
            latency_events=0,
            default_tick_volume=float(quantity),
            max_participation_rate=1.0,
            queue_ahead_fraction=0.0,
            base_fill_probability=1.0,
        ),
    )
    execution = SimulatedExecutionHandler(commission_per_trade=0.0, cfg=cfg, rng_seed=1)
    market = MarketEvent(
        timestamp=datetime(2020, 1, 1),
        symbol="A",
        mid=mid,
        bid=bid,
        ask=ask,
        volume=float(quantity),
    )
    order_id = execution.submit(
        OrderEvent(
            timestamp=market.timestamp,
            symbol=market.symbol,
            side=side,
            quantity=quantity,
            order_type="LIMIT",
            limit_price=limit_price,
        )
    )

    fills = execution.on_market(market)

    if not fills:
        assert execution.remaining_quantity(order_id) == quantity
    for fill in fills:
        if side == Side.BUY:
            assert fill.fill_price <= limit_price
        else:
            assert fill.fill_price >= limit_price
