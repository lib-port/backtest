from __future__ import annotations

from dataclasses import dataclass, field

from quant_backtester.config import RiskConfig
from quant_backtester.events import FillEvent, Side


@dataclass
class Position:
    quantity: int = 0
    avg_cost: float = 0.0  # average entry cost per share

    def update_on_fill(self, fill: FillEvent) -> None:
        signed_qty = fill.quantity if fill.side == Side.BUY else -fill.quantity
        new_qty = self.quantity + signed_qty

        # If position flips sign or goes to zero, reset avg cost on the remainder.
        if new_qty == 0:
            self.quantity = 0
            self.avg_cost = 0.0
            return

        # If adding in same direction, update weighted average cost
        if (
            self.quantity == 0
            or (self.quantity > 0 and signed_qty > 0)
            or (self.quantity < 0 and signed_qty < 0)
        ):
            total_cost = self.avg_cost * abs(self.quantity) + fill.fill_price * abs(signed_qty)
            self.quantity = new_qty
            self.avg_cost = total_cost / abs(new_qty)
            return

        # Reducing or flipping: keep avg cost for remaining shares if not flipped
        # If flipped, set avg cost to fill price for new position direction.
        if (self.quantity > 0 and new_qty > 0) or (self.quantity < 0 and new_qty < 0):
            self.quantity = new_qty
            # avg_cost unchanged
            return

        # Flipped sign
        self.quantity = new_qty
        self.avg_cost = fill.fill_price


@dataclass
class RiskState:
    trading_halted: bool = False
    halt_reason: str | None = None


@dataclass
class MultiAssetPortfolio:
    initial_cash: float
    risk_cfg: RiskConfig

    cash: float = field(init=False)
    positions: dict[str, Position] = field(default_factory=dict, init=False)
    last_mid: dict[str, float] = field(default_factory=dict, init=False)
    equity_curve: list[float] = field(default_factory=list, init=False)
    peak_equity: float = field(default=0.0, init=False)
    _equity: float = field(default=0.0, init=False)
    risk_state: RiskState = field(default_factory=RiskState, init=False)

    # simple accounting
    total_commission: float = field(default=0.0, init=False)
    total_slippage_cost: float = field(default=0.0, init=False)  # absolute cost vs mid

    def __post_init__(self) -> None:
        self.cash = float(self.initial_cash)
        self.peak_equity = self.initial_cash
        self._equity = self.initial_cash

    def get_position(self, symbol: str) -> Position:
        if symbol not in self.positions:
            self.positions[symbol] = Position()
        return self.positions[symbol]

    def can_place_order(self, symbol: str, side: Side, qty: int) -> bool:
        pos = self.get_position(symbol)
        signed = qty if side == Side.BUY else -qty
        proposed = pos.quantity + signed
        return abs(proposed) <= self.risk_cfg.max_position_per_symbol

    def on_fill(self, fill: FillEvent) -> None:
        pos = self.get_position(fill.symbol)
        old_qty = pos.quantity
        signed_qty = fill.quantity if fill.side == Side.BUY else -fill.quantity

        # cash movement: BUY decreases cash, SELL increases cash
        cash_delta = -(fill.fill_price * signed_qty) - fill.commission
        self.cash += cash_delta

        self.total_commission += fill.commission
        # slippage cost vs mid: BUY pays +slip, SELL pays -slip (cost is side.sign * slip)
        # Convert to absolute cost in currency:
        self.total_slippage_cost += fill.slippage * signed_qty  # signed_qty carries side

        pos.update_on_fill(fill)

        # Keep equity updated in O(1) when mid price is available.
        mid = self.last_mid.get(fill.symbol)
        if mid is None:
            self._equity = self._recompute_equity()
            return
        qty_delta = pos.quantity - old_qty
        self._equity += cash_delta + (qty_delta * mid)

    def mark_to_market(self, symbol: str, mid: float) -> float:
        prev_mid = self.last_mid.get(symbol)
        self.last_mid[symbol] = mid

        pos = self.positions.get(symbol)
        qty = pos.quantity if pos is not None else 0
        if prev_mid is None:
            self._equity += qty * mid
        else:
            self._equity += qty * (mid - prev_mid)

        equity = self._equity
        self.equity_curve.append(equity)
        self.peak_equity = max(self.peak_equity, equity)

        # max drawdown halt
        if self.peak_equity > 0:
            dd = (self.peak_equity - equity) / self.peak_equity
            if dd >= self.risk_cfg.max_drawdown_pct and not self.risk_state.trading_halted:
                self.risk_state.trading_halted = True
                self.risk_state.halt_reason = f"Max drawdown reached: {dd:.2%}"

        return equity

    def equity(self) -> float:
        return float(self._equity)

    def _recompute_equity(self) -> float:
        total = self.cash
        for sym, pos in self.positions.items():
            mid = self.last_mid.get(sym)
            if mid is None:
                continue
            total += pos.quantity * mid
        return float(total)

    def check_stop_loss(self, symbol: str) -> Side | None:
        pos = self.positions.get(symbol)
        if pos is None or pos.quantity == 0:
            return None
        mid = self.last_mid.get(symbol)
        if mid is None or pos.avg_cost == 0:
            return None

        # If long: stop if mid <= avg_cost*(1-stop_loss_pct)
        # If short: stop if mid >= avg_cost*(1+stop_loss_pct)
        if pos.quantity > 0 and mid <= pos.avg_cost * (1.0 - self.risk_cfg.stop_loss_pct):
            return Side.SELL
        if pos.quantity < 0 and mid >= pos.avg_cost * (1.0 + self.risk_cfg.stop_loss_pct):
            return Side.BUY
        return None
