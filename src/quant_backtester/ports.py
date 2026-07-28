from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

from quant_backtester.events import FillEvent, MarketEvent, OrderEvent, SignalEvent


class MarketDataSource(Protocol):
    def stream(self) -> Iterator[MarketEvent]: ...


class Strategy(Protocol):
    def on_market(self, event: MarketEvent) -> SignalEvent | None: ...


class ExecutionVenue(Protocol):
    def submit(self, order: OrderEvent) -> str: ...

    def cancel(self, order_id: str) -> bool: ...

    def on_market(
        self,
        market: MarketEvent,
        *,
        position_quantity: int | None = None,
    ) -> list[FillEvent]: ...

    def pending_order_count(self) -> int: ...

    def pending_signed_quantity(self, symbol: str) -> int: ...
