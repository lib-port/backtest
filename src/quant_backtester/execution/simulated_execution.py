from __future__ import annotations

import hashlib
import math
from collections import deque
from dataclasses import dataclass, field

from quant_backtester.config import ExecutionConfig
from quant_backtester.events import (
    FillEvent,
    MarketEvent,
    OrderEvent,
    OrderIntent,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
)


@dataclass
class _PendingOrder:
    order_id: str
    order: OrderEvent
    submitted_tick: int
    remaining: int
    commission_charged: bool = False
    stop_triggered: bool = False
    random_sequence: int = 0
    fill_sequence: int = 0
    rng_identity: str = ""


@dataclass(frozen=True)
class OrderSnapshot:
    order_id: str
    order: OrderEvent
    status: OrderStatus
    filled_quantity: int
    remaining_quantity: int
    rejection_reason: str | None = None


@dataclass
class _OrderRecord:
    order: OrderEvent
    status: OrderStatus
    filled_quantity: int
    remaining_quantity: int
    rejection_reason: str | None = None


@dataclass
class SimulatedExecutionHandler:
    """
    Richer market microstructure simulation:

    - **Latency**: order becomes eligible to fill after N market events (per symbol).
    - **Partial fills**: limited by tick volume and participation rate; remaining quantity carries forward.
    - **Queue position** (LIMIT orders): fill probability reduced by queue_ahead_fraction; may not fill even if
      price touches the limit.
    - **Price protection**: LIMIT prices remain hard boundaries after spread and impact.
    - **Order lifecycle**: stable IDs, cancellation, remaining quantity, and reduce-only fills.
    - Slippage model still applies: executable quote + linear impact (relative to mid).

    Random queue decisions use a keyed, versioned stream so activity in one
    symbol does not perturb another symbol's fill path.
    """

    commission_per_trade: float
    cfg: ExecutionConfig
    rng_seed: int = 7

    _tick_index: dict[str, int] = field(default_factory=dict, init=False)
    _pending: dict[str, deque[_PendingOrder]] = field(default_factory=dict, init=False)
    _symbol_order_sequences: dict[str, int] = field(default_factory=dict, init=False)
    _records: dict[str, _OrderRecord] = field(default_factory=dict, init=False)
    _client_order_ids: dict[str, str] = field(default_factory=dict, init=False)
    _fill_history: list[FillEvent] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        # Retained as an extension hook for custom execution adapters.
        pass

    def submit(self, order: OrderEvent) -> str:
        if order.client_order_id is not None:
            existing_id = self._client_order_ids.get(order.client_order_id)
            if existing_id is not None:
                existing = self._records[existing_id]
                if existing.order != order:
                    raise ValueError(
                        f"client_order_id {order.client_order_id!r} was reused for a different order"
                    )
                return existing_id

        sym = order.symbol
        if sym not in self._pending:
            self._pending[sym] = deque()
        tick = self._tick_index.get(sym, 0)
        symbol_sequence = self._symbol_order_sequences.get(sym, 0) + 1
        self._symbol_order_sequences[sym] = symbol_sequence
        symbol_key = hashlib.sha256(sym.encode("utf-8")).hexdigest()[:12]
        order_id = f"sim-{symbol_key}-{symbol_sequence}"
        self._records[order_id] = _OrderRecord(
            order=order,
            status=OrderStatus.ACCEPTED,
            filled_quantity=0,
            remaining_quantity=order.quantity,
        )
        if order.client_order_id is not None:
            self._client_order_ids[order.client_order_id] = order_id
        self._pending[sym].append(
            _PendingOrder(
                order_id=order_id,
                order=order,
                submitted_tick=tick,
                remaining=order.quantity,
                rng_identity=order.client_order_id or f"{sym}:{symbol_sequence}",
            )
        )
        return order_id

    def cancel(self, order_id: str) -> bool:
        for sym, pending_orders in tuple(self._pending.items()):
            for pending in pending_orders:
                if pending.order_id != order_id:
                    continue
                pending_orders.remove(pending)
                record = self._records[order_id]
                record.status = OrderStatus.CANCELED
                record.remaining_quantity = pending.remaining
                if not pending_orders:
                    self._pending.pop(sym, None)
                return True
        return False

    def cancel_symbol_orders(self, symbol: str, *, intent: OrderIntent | None = None) -> list[str]:
        pending_orders = self._pending.get(symbol)
        if not pending_orders:
            return []

        cancelled: list[str] = []
        retained: deque[_PendingOrder] = deque()
        while pending_orders:
            pending = pending_orders.popleft()
            if intent is None or pending.order.intent == intent:
                cancelled.append(pending.order_id)
                record = self._records[pending.order_id]
                record.status = OrderStatus.CANCELED
                record.remaining_quantity = pending.remaining
            else:
                retained.append(pending)

        if retained:
            self._pending[symbol] = retained
        else:
            self._pending.pop(symbol, None)
        return cancelled

    def cancel_all(self) -> list[str]:
        cancelled: list[str] = []
        for symbol in tuple(self._pending):
            cancelled.extend(self.cancel_symbol_orders(symbol))
        return cancelled

    def is_pending(self, order_id: str) -> bool:
        return any(
            pending.order_id == order_id
            for pending_orders in self._pending.values()
            for pending in pending_orders
        )

    def remaining_quantity(self, order_id: str) -> int | None:
        for pending_orders in self._pending.values():
            for pending in pending_orders:
                if pending.order_id == order_id:
                    return pending.remaining
        return None

    def order_snapshot(self, order_id: str) -> OrderSnapshot | None:
        record = self._records.get(order_id)
        if record is None:
            return None
        return OrderSnapshot(
            order_id=order_id,
            order=record.order,
            status=record.status,
            filled_quantity=record.filled_quantity,
            remaining_quantity=record.remaining_quantity,
            rejection_reason=record.rejection_reason,
        )

    def order_snapshots(self) -> tuple[OrderSnapshot, ...]:
        return tuple(
            OrderSnapshot(
                order_id=order_id,
                order=record.order,
                status=record.status,
                filled_quantity=record.filled_quantity,
                remaining_quantity=record.remaining_quantity,
                rejection_reason=record.rejection_reason,
            )
            for order_id, record in self._records.items()
        )

    @property
    def fills(self) -> tuple[FillEvent, ...]:
        return tuple(self._fill_history)

    def pending_order_count(self) -> int:
        return sum(len(pending_orders) for pending_orders in self._pending.values())

    def pending_signed_quantity(
        self,
        symbol: str,
        *,
        intent: OrderIntent | None = None,
    ) -> int:
        return sum(
            pending.remaining * pending.order.side.sign
            for pending in self._pending.get(symbol, ())
            if intent is None or pending.order.intent == intent
        )

    def on_market(
        self, market: MarketEvent, *, position_quantity: int | None = None
    ) -> list[FillEvent]:
        sym = market.symbol
        self._tick_index[sym] = self._tick_index.get(sym, 0) + 1
        tick = self._tick_index[sym]
        fills: list[FillEvent] = []
        simulated_position = position_quantity

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
            record = self._records[pending.order_id]

            if (
                pending.order.time_in_force == TimeInForce.DAY
                and market.timestamp.date() > pending.order.timestamp.date()
            ):
                q.popleft()
                record.status = OrderStatus.EXPIRED
                record.remaining_quantity = pending.remaining
                continue

            if tick - pending.submitted_tick < eligible_after:
                # not eligible yet; rotate to preserve FIFO among ineligible
                q.rotate(-1)
                continue

            if pending.order.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
                if not pending.stop_triggered:
                    pending.stop_triggered = self._stop_is_triggered(pending.order, market)
                if not pending.stop_triggered:
                    q.rotate(-1)
                    continue

            if remaining_capacity <= 0:
                if pending.order.time_in_force == TimeInForce.IOC:
                    q.popleft()
                    record.status = OrderStatus.CANCELED
                    record.remaining_quantity = pending.remaining
                    continue
                # Keep walking the original queue so eligible IOC orders behind
                # this resting order are canceled on their first opportunity.
                q.rotate(-1)
                continue

            # Decide if order can fill (especially for LIMIT and STOP_LIMIT)
            if pending.order.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT):
                if pending.order.limit_price is None:
                    # invalid limit -> skip
                    q.popleft()
                    record.status = OrderStatus.REJECTED
                    record.rejection_reason = "missing limit price"
                    continue
                if not self._limit_is_touching(pending.order, market):
                    if pending.order.time_in_force == TimeInForce.IOC:
                        q.popleft()
                        record.status = OrderStatus.CANCELED
                        record.remaining_quantity = pending.remaining
                    else:
                        # keep waiting
                        q.rotate(-1)
                    continue
                # Queue position: reduce fill probability
                p_fill = self.cfg.micro.base_fill_probability * (
                    1.0 - self.cfg.micro.queue_ahead_fraction
                )
                pending.random_sequence += 1
                if self._keyed_random(pending, market, tick) > p_fill:
                    if pending.order.time_in_force == TimeInForce.IOC:
                        q.popleft()
                        record.status = OrderStatus.CANCELED
                        record.remaining_quantity = pending.remaining
                    else:
                        q.rotate(-1)
                    continue

            # Determine fill quantity (partial fills)
            fill_qty = min(pending.remaining, remaining_capacity)
            if pending.order.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT):
                fill_qty = self._limit_constrained_quantity(
                    pending.order,
                    market,
                    fill_qty,
                )
                if fill_qty <= 0:
                    # The quote touches, but modeled impact would cross the limit.
                    if pending.order.time_in_force == TimeInForce.IOC:
                        q.popleft()
                        record.status = OrderStatus.CANCELED
                        record.remaining_quantity = pending.remaining
                    else:
                        q.rotate(-1)
                    continue
            if pending.order.reduce_only:
                if simulated_position is None:
                    raise ValueError(
                        "position_quantity is required when processing reduce-only orders"
                    )
                if simulated_position > 0 and pending.order.side == Side.SELL:
                    fill_qty = min(fill_qty, simulated_position)
                elif simulated_position < 0 and pending.order.side == Side.BUY:
                    fill_qty = min(fill_qty, abs(simulated_position))
                else:
                    # The position is already flat or the order would increase exposure.
                    q.popleft()
                    record.status = OrderStatus.CANCELED
                    record.remaining_quantity = pending.remaining
                    continue
            if fill_qty <= 0:
                break

            commission = 0.0 if pending.commission_charged else self.commission_per_trade
            pending.fill_sequence += 1
            fill = self._fill(
                pending.order_id,
                pending.order,
                market,
                fill_qty,
                commission=commission,
                fill_sequence=pending.fill_sequence,
            )
            fills.append(fill)
            self._fill_history.append(fill)
            pending.commission_charged = True

            pending.remaining -= fill_qty
            remaining_capacity -= fill_qty
            record.filled_quantity += fill_qty
            record.remaining_quantity = pending.remaining
            if simulated_position is not None:
                simulated_position += fill_qty * pending.order.side.sign

            reduced_to_flat = pending.order.reduce_only and simulated_position == 0
            if pending.remaining <= 0:
                q.popleft()
                record.remaining_quantity = 0
                record.status = OrderStatus.FILLED
            elif reduced_to_flat:
                q.popleft()
                record.status = OrderStatus.CANCELED
            elif pending.order.time_in_force == TimeInForce.IOC:
                q.popleft()
                record.status = OrderStatus.CANCELED
            else:
                # partially filled, move to end (others may get a chance next tick)
                record.status = OrderStatus.PARTIALLY_FILLED
                q.rotate(-1)

        if not q:
            self._pending.pop(sym, None)
        return fills

    def force_liquidation(
        self,
        market: MarketEvent,
        *,
        position_quantity: int,
        client_order_id: str,
    ) -> FillEvent | None:
        """Close a position at the final executable quote, bypassing latency and volume.

        This models an explicit end-of-run liquidation auction. Spread, impact,
        and commission still apply and the resulting order/fill are persisted.
        """
        if position_quantity == 0:
            return None
        self.cancel_symbol_orders(market.symbol)
        order = OrderEvent(
            timestamp=market.timestamp,
            symbol=market.symbol,
            side=Side.SELL if position_quantity > 0 else Side.BUY,
            quantity=abs(position_quantity),
            order_type=OrderType.MARKET,
            intent=OrderIntent.RISK_LIQUIDATION,
            reduce_only=True,
            client_order_id=client_order_id,
        )
        order_id = self.submit(order)
        pending_orders = self._pending[market.symbol]
        pending = next(item for item in pending_orders if item.order_id == order_id)
        pending_orders.remove(pending)
        if not pending_orders:
            self._pending.pop(market.symbol, None)
        fill = self._fill(
            order_id,
            order,
            market,
            order.quantity,
            commission=self.commission_per_trade,
            fill_sequence=1,
        )
        record = self._records[order_id]
        record.status = OrderStatus.FILLED
        record.filled_quantity = order.quantity
        record.remaining_quantity = 0
        self._fill_history.append(fill)
        return fill

    def _keyed_random(
        self,
        pending: _PendingOrder,
        market: MarketEvent,
        tick: int,
    ) -> float:
        key = (
            f"qbt-rng-v2|{self.rng_seed}|{pending.rng_identity}|"
            f"{market.timestamp.isoformat()}|{tick}|{pending.random_sequence}|"
            f"{pending.remaining}"
        )
        raw = int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:8], "big")
        return raw / 2**64

    def _stop_is_triggered(self, order: OrderEvent, market: MarketEvent) -> bool:
        assert order.stop_price is not None
        if order.side == Side.BUY:
            return market.mid >= order.stop_price
        return market.mid <= order.stop_price

    def _limit_is_touching(self, order: OrderEvent, market: MarketEvent) -> bool:
        # BUY limit fills if limit >= executable ask; SELL if limit <= executable bid.
        limit = float(order.limit_price) if order.limit_price is not None else None
        if limit is None:
            return False
        reference = self._execution_reference_price(order, market)
        if order.side == Side.BUY:
            return limit >= reference
        return limit <= reference

    def _limit_constrained_quantity(
        self,
        order: OrderEvent,
        market: MarketEvent,
        proposed_quantity: int,
    ) -> int:
        assert order.limit_price is not None  # validated by OrderEvent
        impact_per_share = (
            market.mid * self.cfg.impact_bps_per_unit / max(self.cfg.impact_volume, 1.0) / 10_000.0
        )
        if impact_per_share <= 0:
            return proposed_quantity

        reference = self._execution_reference_price(order, market)
        if order.side == Side.BUY:
            price_headroom = order.limit_price - reference
        else:
            price_headroom = reference - order.limit_price

        # Include a scale-aware tolerance so an exact integral boundary is not
        # lost to floating-point representation. The final price is still capped.
        tolerance = max(abs(reference), abs(order.limit_price), 1.0) * 1e-12
        available_headroom = max(0.0, price_headroom + tolerance)
        if impact_per_share * proposed_quantity <= available_headroom:
            return proposed_quantity
        ratio = available_headroom / impact_per_share
        if not math.isfinite(ratio):
            return proposed_quantity
        max_quantity = math.floor(ratio)
        return min(proposed_quantity, max_quantity)

    def _effective_spread(self, market: MarketEvent) -> float:
        if market.bid is not None and market.ask is not None and market.ask >= market.bid:
            return market.ask - market.bid
        spread_bps = (
            market.spread_bps if market.spread_bps is not None else self.cfg.default_spread_bps
        )
        return market.mid * (spread_bps / 10_000.0)

    def _execution_reference_price(self, order: OrderEvent, market: MarketEvent) -> float:
        if order.side == Side.BUY and market.ask is not None:
            return market.ask
        if order.side == Side.SELL and market.bid is not None:
            return market.bid

        half_spread = 0.5 * self._effective_spread(market)
        side_sign = order.side.sign
        return market.mid + side_sign * half_spread

    def _fill(
        self,
        order_id: str,
        order: OrderEvent,
        market: MarketEvent,
        qty: int,
        commission: float,
        fill_sequence: int,
    ) -> FillEvent:
        impact_bps = self.cfg.impact_bps_per_unit * (qty / max(self.cfg.impact_volume, 1.0))
        impact = market.mid * (impact_bps / 10_000.0)

        side_sign = order.side.sign
        reference = self._execution_reference_price(order, market)
        price = reference + side_sign * impact

        # A limit is a hard execution boundary, even after modeled market impact.
        if order.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT):
            assert order.limit_price is not None  # validated by OrderEvent
            if order.side == Side.BUY:
                price = min(price, order.limit_price)
            else:
                price = max(price, order.limit_price)

        slippage = price - market.mid

        execution_id = f"{order_id}-fill-{fill_sequence}"
        return FillEvent(
            timestamp=market.timestamp,
            symbol=order.symbol,
            side=order.side,
            quantity=qty,
            fill_price=price,
            commission=commission,
            slippage=slippage,
            order_id=order_id,
            execution_id=execution_id,
            mid_price=market.mid,
        )
