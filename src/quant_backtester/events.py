from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._:/-]{0,63}$")


def normalize_symbol(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("symbol must be a string")
    symbol = value.strip().upper()
    if not SYMBOL_PATTERN.fullmatch(symbol):
        raise ValueError(
            "symbol must contain 1 to 64 uppercase letters, digits, or . _ : / - characters"
        )
    return symbol


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        return 1 if self == Side.BUY else -1


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"


class OrderIntent(StrEnum):
    STRATEGY = "STRATEGY"
    STOP_LOSS = "STOP_LOSS"
    RISK_LIQUIDATION = "RISK_LIQUIDATION"


class TimeInForce(StrEnum):
    GTC = "GTC"
    DAY = "DAY"
    IOC = "IOC"


class OrderStatus(StrEnum):
    CREATED = "CREATED"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


def as_utc(value: datetime) -> datetime:
    """Normalize event timestamps; legacy naive inputs are explicitly interpreted as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True)
class MarketEvent:
    timestamp: datetime
    symbol: str
    mid: float
    bid: float | None = None
    ask: float | None = None
    spread_bps: float | None = None  # used if bid/ask absent
    volume: float | None = None  # per-tick available volume (shares)

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", as_utc(self.timestamp))
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        if not math.isfinite(self.mid) or self.mid <= 0:
            raise ValueError(f"mid must be finite and > 0, got {self.mid}")
        if self.bid is not None and (not math.isfinite(self.bid) or self.bid <= 0):
            raise ValueError(f"bid must be finite and > 0, got {self.bid}")
        if self.ask is not None and (not math.isfinite(self.ask) or self.ask <= 0):
            raise ValueError(f"ask must be finite and > 0, got {self.ask}")
        if self.bid is not None and self.ask is not None and self.ask < self.bid:
            raise ValueError(f"ask must be >= bid, got bid={self.bid} ask={self.ask}")
        if self.spread_bps is not None and (
            not math.isfinite(self.spread_bps) or self.spread_bps < 0
        ):
            raise ValueError(f"spread_bps must be finite and >= 0, got {self.spread_bps}")
        if self.volume is not None and (not math.isfinite(self.volume) or self.volume < 0):
            raise ValueError(f"volume must be finite and >= 0, got {self.volume}")


@dataclass(frozen=True)
class SignalEvent:
    timestamp: datetime
    symbol: str
    side: Side

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", as_utc(self.timestamp))
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "side", Side(self.side))


@dataclass(frozen=True)
class OrderEvent:
    timestamp: datetime
    symbol: str
    side: Side
    quantity: int
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    intent: OrderIntent = OrderIntent.STRATEGY
    reduce_only: bool = False
    stop_price: float | None = None
    time_in_force: TimeInForce = TimeInForce.GTC
    client_order_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", as_utc(self.timestamp))
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "side", Side(self.side))
        object.__setattr__(self, "order_type", OrderType(self.order_type))
        object.__setattr__(self, "intent", OrderIntent(self.intent))
        object.__setattr__(self, "time_in_force", TimeInForce(self.time_in_force))
        if self.quantity <= 0:
            raise ValueError(f"quantity must be > 0, got {self.quantity}")
        if self.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT):
            if self.limit_price is None:
                raise ValueError(f"{self.order_type} orders require limit_price")
            if not math.isfinite(self.limit_price) or self.limit_price <= 0:
                raise ValueError(f"limit_price must be finite and > 0, got {self.limit_price}")
        elif self.limit_price is not None:
            raise ValueError(f"{self.order_type} orders must not define limit_price")
        if self.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
            if self.stop_price is None:
                raise ValueError(f"{self.order_type} orders require stop_price")
            if not math.isfinite(self.stop_price) or self.stop_price <= 0:
                raise ValueError(f"stop_price must be finite and > 0, got {self.stop_price}")
        elif self.stop_price is not None:
            raise ValueError(f"{self.order_type} orders must not define stop_price")
        if self.client_order_id is not None and not self.client_order_id.strip():
            raise ValueError("client_order_id must not be empty")


@dataclass(frozen=True)
class FillEvent:
    timestamp: datetime
    symbol: str
    side: Side
    quantity: int
    fill_price: float
    commission: float
    slippage: float  # signed (positive = cost vs mid for BUY, benefit vs mid for SELL negative)
    order_id: str | None = None
    execution_id: str | None = None
    mid_price: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", as_utc(self.timestamp))
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "side", Side(self.side))
        if self.quantity <= 0:
            raise ValueError(f"quantity must be > 0, got {self.quantity}")
        if not math.isfinite(self.fill_price) or self.fill_price <= 0:
            raise ValueError(f"fill_price must be finite and > 0, got {self.fill_price}")
        if not math.isfinite(self.commission) or self.commission < 0:
            raise ValueError(f"commission must be finite and >= 0, got {self.commission}")
        if not math.isfinite(self.slippage):
            raise ValueError(f"slippage must be finite, got {self.slippage}")
        if self.mid_price is not None and (
            not math.isfinite(self.mid_price) or self.mid_price <= 0
        ):
            raise ValueError(f"mid_price must be finite and > 0, got {self.mid_price}")


@dataclass(frozen=True)
class EquityPoint:
    timestamp: datetime
    equity: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", as_utc(self.timestamp))
        if not math.isfinite(self.equity):
            raise ValueError(f"equity must be finite, got {self.equity}")
