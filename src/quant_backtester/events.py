from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        return 1 if self == Side.BUY else -1


@dataclass(frozen=True)
class MarketEvent:
    timestamp: datetime
    symbol: str
    mid: float
    bid: float | None = None
    ask: float | None = None
    spread_bps: float | None = None  # used if bid/ask absent
    volume: float | None = None  # per-tick available volume (shares)


@dataclass(frozen=True)
class SignalEvent:
    timestamp: datetime
    symbol: str
    side: Side


@dataclass(frozen=True)
class OrderEvent:
    timestamp: datetime
    symbol: str
    side: Side
    quantity: int
    order_type: Literal["MARKET", "LIMIT"] = "MARKET"
    limit_price: float | None = None


@dataclass(frozen=True)
class FillEvent:
    timestamp: datetime
    symbol: str
    side: Side
    quantity: int
    fill_price: float
    commission: float
    slippage: float  # signed (positive = cost vs mid for BUY, benefit vs mid for SELL negative)
