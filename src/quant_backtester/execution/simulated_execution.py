from __future__ import annotations

import random
from collections import deque
from dataclasses import dataclass, field

from quant_backtester.config import ExecutionConfig
from quant_backtester.events import FillEvent, MarketEvent, OrderEvent


@dataclass
class _PendingOrder:
    order: OrderEvent
    submitted_tick: int
    remaining: int
    commission_charged: bool = False


@dataclass
class SimulatedExecutionHandler:
    """
    Richer market microstructure simulation:

    - **Latency**: order becomes eligible to fill after N market events (per symbol).
    - **Partial fills**: limited by tick volume and participation rate; remaining quantity carries forward.
    - **Queue position** (LIMIT orders): fill probability reduced by queue_ahead_fraction; may not fill even if
      price touches the limit.
    - Slippage model still applies: spread + linear impact (relative to mid).

    Notes:
    - This remains a **simulation**; it's deterministic if you set PYTHONHASHSEED and RNG seed externally.
    - For serious research, replace with historical L2 replay / queue model.
    """

    commission_per_trade: float
    cfg: ExecutionConfig
    rng_seed: int = 7

    _tick_index: dict[str, int] = field(default_factory=dict, init=False)
    _pending: dict[str, deque[_PendingOrder]] = field(default_factory=dict, init=False)
    _rng: random.Random = field(init=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.rng_seed)

    def submit(self, order: OrderEvent) -> None:
        sym = order.symbol
        if sym not in self._pending:
            self._pending[sym] = deque()
        tick = self._tick_index.get(sym, 0)
        self._pending[sym].append(
            _PendingOrder(order=order, submitted_tick=tick, remaining=order.quantity)
        )

    def on_market(self, market: MarketEvent) -> list[FillEvent]:
        sym = market.symbol
        self._tick_index[sym] = self._tick_index.get(sym, 0) + 1
        tick = self._tick_index[sym]
        fills: list[FillEvent] = []

        q = self._pending.get(sym)
        if not q:
            return fills

        eligible_after = self.cfg.micro.latency_events
        tick_volume = (
            market.volume if market.volume is not None else self.cfg.micro.default_tick_volume
        )
        max_fill_this_tick = int(max(0.0, tick_volume * self.cfg.micro.max_participation_rate))

        remaining_capacity = max_fill_this_tick

        # process in FIFO order
        for _ in range(len(q)):
            pending = q[0]
            if tick - pending.submitted_tick < eligible_after:
                # not eligible yet; rotate to preserve FIFO among ineligible
                q.rotate(-1)
                continue

            if remaining_capacity <= 0:
                break

            # Decide if order can fill (especially for LIMIT)
            if pending.order.order_type == "LIMIT":
                if pending.order.limit_price is None:
                    # invalid limit -> skip
                    q.popleft()
                    continue
                if not self._limit_is_touching(pending.order, market):
                    # keep waiting
                    q.rotate(-1)
                    continue
                # Queue position: reduce fill probability
                p_fill = self.cfg.micro.base_fill_probability * (
                    1.0 - self.cfg.micro.queue_ahead_fraction
                )
                if self._rng.random() > p_fill:
                    q.rotate(-1)
                    continue

            # Determine fill quantity (partial fills)
            fill_qty = min(pending.remaining, remaining_capacity)
            if fill_qty <= 0:
                break

            commission = 0.0 if pending.commission_charged else self.commission_per_trade
            fill = self._fill(pending.order, market, fill_qty, commission=commission)
            fills.append(fill)
            pending.commission_charged = True

            pending.remaining -= fill_qty
            remaining_capacity -= fill_qty

            if pending.remaining <= 0:
                q.popleft()
            else:
                # partially filled, move to end (others may get a chance next tick)
                q.rotate(-1)

        return fills

    def _limit_is_touching(self, order: OrderEvent, market: MarketEvent) -> bool:
        # BUY limit fills if limit >= ask (or >= mid if no ask)
        # SELL limit fills if limit <= bid (or <= mid if no bid)
        limit = float(order.limit_price) if order.limit_price is not None else None
        if limit is None:
            return False
        if order.side.value == "BUY":
            ref = market.ask if market.ask is not None else market.mid
            return limit >= ref
        ref = market.bid if market.bid is not None else market.mid
        return limit <= ref

    def _effective_spread(self, market: MarketEvent) -> float:
        if market.bid is not None and market.ask is not None and market.ask >= market.bid:
            return market.ask - market.bid
        spread_bps = (
            market.spread_bps if market.spread_bps is not None else self.cfg.default_spread_bps
        )
        return market.mid * (spread_bps / 10_000.0)

    def _fill(
        self, order: OrderEvent, market: MarketEvent, qty: int, commission: float
    ) -> FillEvent:
        spread = self._effective_spread(market)
        half_spread = 0.5 * spread

        impact_bps = self.cfg.impact_bps_per_unit * (qty / max(self.cfg.impact_volume, 1.0))
        impact = market.mid * (impact_bps / 10_000.0)

        side_sign = 1 if order.side.value == "BUY" else -1
        price = market.mid + side_sign * (half_spread + impact)
        slippage = price - market.mid

        return FillEvent(
            timestamp=market.timestamp,
            symbol=order.symbol,
            side=order.side,
            quantity=qty,
            fill_price=price,
            commission=commission,
            slippage=slippage,
        )
