from __future__ import annotations

from datetime import datetime

import pytest

hypothesis = pytest.importorskip("hypothesis")
given = hypothesis.given
settings = hypothesis.settings
st = hypothesis.strategies

from quant_backtester.config import RiskConfig  # noqa: E402
from quant_backtester.events import FillEvent, Side  # noqa: E402
from quant_backtester.portfolio.simple_portfolio import MultiAssetPortfolio  # noqa: E402


@settings(max_examples=50, deadline=None)
@given(
    fills=st.lists(
        st.tuples(
            st.sampled_from([Side.BUY, Side.SELL]),
            st.integers(min_value=1, max_value=200),
            st.floats(min_value=1.0, max_value=1_000.0, allow_nan=False, allow_infinity=False),
            st.floats(min_value=0.0, max_value=5.0, allow_nan=False, allow_infinity=False),
        ),
        min_size=1,
        max_size=30,
    )
)
def test_equity_matches_cash_plus_mark_to_market(
    fills: list[tuple[Side, int, float, float]],
) -> None:
    pf = MultiAssetPortfolio(initial_cash=100_000.0, risk_cfg=RiskConfig())
    symbol = "AAPL"
    ts = datetime(2020, 1, 1)

    for side, qty, px, commission in fills:
        pf.on_fill(
            FillEvent(
                timestamp=ts,
                symbol=symbol,
                side=side,
                quantity=qty,
                fill_price=px,
                commission=commission,
                slippage=0.0,
            )
        )
        pf.mark_to_market(symbol, px)

        pos = pf.get_position(symbol)
        expected = pf.cash + (pos.quantity * px)
        assert abs(pf.equity() - expected) < 1e-6
