from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from quant_backtester.events import MarketEvent, Side, SignalEvent


@dataclass
class MovingAverageCrossStrategy:
    symbols: tuple[str, ...]
    short_window: int
    long_window: int

    def __post_init__(self) -> None:
        if self.short_window <= 0 or self.long_window <= 0:
            raise ValueError("Windows must be positive")
        if self.short_window >= self.long_window:
            raise ValueError("short_window must be < long_window")

        self._long_prices: dict[str, deque[float]] = {
            s: deque(maxlen=self.long_window) for s in self.symbols
        }
        self._short_prices: dict[str, deque[float]] = {
            s: deque(maxlen=self.short_window) for s in self.symbols
        }
        self._long_sums: dict[str, float] = {s: 0.0 for s in self.symbols}
        self._short_sums: dict[str, float] = {s: 0.0 for s in self.symbols}
        self._last_signal: dict[str, Side | None] = {s: None for s in self.symbols}

    def on_market(self, event: MarketEvent) -> SignalEvent | None:
        if event.symbol not in self._long_prices:
            return None

        symbol = event.symbol
        px = event.mid

        long_q = self._long_prices[symbol]
        long_sum = self._long_sums[symbol]
        if len(long_q) == self.long_window:
            long_sum -= long_q[0]
        long_q.append(px)
        long_sum += px
        self._long_sums[symbol] = long_sum

        short_q = self._short_prices[symbol]
        short_sum = self._short_sums[symbol]
        if len(short_q) == self.short_window:
            short_sum -= short_q[0]
        short_q.append(px)
        short_sum += px
        self._short_sums[symbol] = short_sum

        if len(long_q) < self.long_window:
            return None

        short_ma = short_sum / self.short_window
        long_ma = long_sum / self.long_window

        last = self._last_signal[symbol]
        if short_ma > long_ma and last != Side.BUY:
            self._last_signal[symbol] = Side.BUY
            return SignalEvent(timestamp=event.timestamp, symbol=symbol, side=Side.BUY)

        if short_ma < long_ma and last != Side.SELL:
            self._last_signal[symbol] = Side.SELL
            return SignalEvent(timestamp=event.timestamp, symbol=symbol, side=Side.SELL)

        return None
