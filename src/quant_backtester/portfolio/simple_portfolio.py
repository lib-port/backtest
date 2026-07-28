from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal

from quant_backtester.config import RiskConfig
from quant_backtester.events import EquityPoint, FillEvent, Side, as_utc

ACCOUNTING_QUANTUM = Decimal("0.00000001")


def _decimal(value: float | int | str | Decimal) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _money(value: Decimal) -> Decimal:
    return value.quantize(ACCOUNTING_QUANTUM, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True)
class JournalEntry:
    """A balanced monetary journal entry created from an execution."""

    timestamp: dt.datetime
    transaction_id: str
    debit_account: str
    credit_account: str
    amount: Decimal
    symbol: str | None = None


@dataclass
class Position:
    quantity: int = 0
    _avg_cost: Decimal = field(default=Decimal("0"), repr=False)
    _realized_pnl: Decimal = field(default=Decimal("0"), repr=False)

    @property
    def avg_cost(self) -> float:
        return float(self._avg_cost)

    @property
    def realized_pnl(self) -> float:
        return float(self._realized_pnl)

    def update_on_fill(self, fill: FillEvent) -> Decimal:
        signed_qty = fill.quantity * fill.side.sign
        old_qty = self.quantity
        new_qty = old_qty + signed_qty
        fill_price = _decimal(fill.fill_price)
        realized = Decimal("0")

        if old_qty != 0 and old_qty * signed_qty < 0:
            closed_quantity = min(abs(old_qty), abs(signed_qty))
            if old_qty > 0:
                realized = (fill_price - self._avg_cost) * closed_quantity
            else:
                realized = (self._avg_cost - fill_price) * closed_quantity
            self._realized_pnl = _money(self._realized_pnl + realized)

        if new_qty == 0:
            self.quantity = 0
            self._avg_cost = Decimal("0")
            return _money(realized)

        adding_same_direction = (
            old_qty == 0 or (old_qty > 0 and signed_qty > 0) or (old_qty < 0 and signed_qty < 0)
        )
        if adding_same_direction:
            total_cost = self._avg_cost * abs(old_qty) + fill_price * abs(signed_qty)
            self.quantity = new_qty
            self._avg_cost = _money(total_cost / abs(new_qty))
            return _money(realized)

        if old_qty * new_qty > 0:
            self.quantity = new_qty
            return _money(realized)

        # A single fill closed the old position and opened one in the other direction.
        self.quantity = new_qty
        self._avg_cost = _money(fill_price)
        return _money(realized)


@dataclass
class RiskState:
    trading_halted: bool = False
    halt_reason: str | None = None


@dataclass
class MultiAssetPortfolio:
    """Fixed-precision portfolio accounting with replay-safe fills."""

    initial_cash: float
    risk_cfg: RiskConfig

    positions: dict[str, Position] = field(default_factory=dict, init=False)
    last_mid: dict[str, float] = field(default_factory=dict, init=False)
    equity_curve: list[float] = field(default_factory=list, init=False)
    equity_points: list[EquityPoint] = field(default_factory=list, init=False)
    peak_equity: float = field(default=0.0, init=False)
    risk_state: RiskState = field(default_factory=RiskState, init=False)
    journal: list[JournalEntry] = field(default_factory=list, init=False)

    _cash: Decimal = field(init=False, repr=False)
    _equity: Decimal = field(init=False, repr=False)
    _marks: dict[str, Decimal] = field(default_factory=dict, init=False, repr=False)
    _total_commission: Decimal = field(default=Decimal("0"), init=False, repr=False)
    _total_slippage_cost: Decimal = field(default=Decimal("0"), init=False, repr=False)
    _applied_execution_ids: set[str] = field(default_factory=set, init=False, repr=False)
    _anonymous_fill_sequence: int = field(default=1, init=False, repr=False)

    def __post_init__(self) -> None:
        self._cash = _money(_decimal(self.initial_cash))
        self._equity = self._cash
        self.peak_equity = float(self._equity)

    @property
    def cash(self) -> float:
        return float(self._cash)

    @property
    def total_commission(self) -> float:
        return float(self._total_commission)

    @property
    def total_slippage_cost(self) -> float:
        return float(self._total_slippage_cost)

    @property
    def realized_pnl(self) -> float:
        return float(
            sum((position._realized_pnl for position in self.positions.values()), Decimal(0))
        )

    def get_position(self, symbol: str) -> Position:
        normalized = symbol.strip().upper()
        if normalized not in self.positions:
            self.positions[normalized] = Position()
        return self.positions[normalized]

    def gross_exposure(self) -> float:
        gross = sum(
            abs(position.quantity) * self._marks.get(symbol, Decimal("0"))
            for symbol, position in self.positions.items()
        )
        return float(gross)

    def can_place_order(
        self,
        symbol: str,
        side: Side,
        qty: int,
        *,
        pending_signed_quantity: int = 0,
    ) -> bool:
        if qty <= 0 or self.risk_state.trading_halted:
            return False
        normalized = symbol.strip().upper()
        position = self.get_position(normalized)
        proposed = position.quantity + pending_signed_quantity + (qty * side.sign)
        if abs(proposed) > self.risk_cfg.max_position_per_symbol:
            return False

        mark = self._marks.get(normalized)
        if mark is None:
            return True
        current_symbol_exposure = abs(position.quantity + pending_signed_quantity) * mark
        proposed_symbol_exposure = abs(proposed) * mark
        proposed_gross = (
            _decimal(self.gross_exposure()) - current_symbol_exposure + proposed_symbol_exposure
        )
        if self.risk_cfg.max_gross_exposure is not None and proposed_gross > _decimal(
            self.risk_cfg.max_gross_exposure
        ):
            return False
        equity = max(self._equity, Decimal("0"))
        if equity == 0:
            return proposed_gross == 0
        return proposed_gross <= equity * _decimal(self.risk_cfg.max_leverage)

    def on_fill(self, fill: FillEvent) -> bool:
        """Apply a fill once. Returns False when an execution is a replay."""
        if fill.execution_id is not None:
            if fill.execution_id in self._applied_execution_ids:
                return False
            self._applied_execution_ids.add(fill.execution_id)
            transaction_id = fill.execution_id
        else:
            transaction_id = f"anonymous-fill-{self._anonymous_fill_sequence}"
            self._anonymous_fill_sequence += 1

        position = self.get_position(fill.symbol)
        signed_quantity = fill.quantity * fill.side.sign
        principal = _money(_decimal(fill.fill_price) * fill.quantity)
        commission = _money(_decimal(fill.commission))
        cash_delta = principal * (-1 if fill.side == Side.BUY else 1) - commission

        self._cash = _money(self._cash + cash_delta)
        self._total_commission = _money(self._total_commission + commission)
        slippage_cost = _decimal(fill.slippage) * signed_quantity
        self._total_slippage_cost = _money(self._total_slippage_cost + slippage_cost)
        realized = position.update_on_fill(fill)

        if fill.side == Side.BUY:
            self._post(
                fill.timestamp,
                transaction_id,
                f"inventory:{fill.symbol}",
                "cash",
                principal,
                fill.symbol,
            )
        else:
            self._post(
                fill.timestamp,
                transaction_id,
                "cash",
                f"inventory:{fill.symbol}",
                principal,
                fill.symbol,
            )
        if commission:
            self._post(
                fill.timestamp,
                f"{transaction_id}:commission",
                "expense:commission",
                "cash",
                commission,
                fill.symbol,
            )
        if realized:
            pnl_account = "income:realized-pnl" if realized > 0 else "expense:realized-pnl"
            offset = "inventory:pnl-offset"
            self._post(
                fill.timestamp,
                f"{transaction_id}:realized-pnl",
                offset if realized > 0 else pnl_account,
                pnl_account if realized > 0 else offset,
                abs(realized),
                fill.symbol,
            )

        # Recompute from the ledger-backed cash balance and current marks. This
        # deliberately favors exact reconciliation over an accumulation shortcut
        # whose per-event rounding can drift from the independently derived value.
        self._equity = self._recompute_equity()
        self.assert_reconciled()
        return True

    def _post(
        self,
        timestamp: dt.datetime,
        transaction_id: str,
        debit_account: str,
        credit_account: str,
        amount: Decimal,
        symbol: str | None,
    ) -> None:
        if amount < 0:
            raise ValueError("journal amount must not be negative")
        self.journal.append(
            JournalEntry(
                timestamp=as_utc(timestamp),
                transaction_id=transaction_id,
                debit_account=debit_account,
                credit_account=credit_account,
                amount=_money(amount),
                symbol=symbol,
            )
        )

    def update_market_price(self, symbol: str, mid: float) -> float:
        normalized = symbol.strip().upper()
        mark = _decimal(mid)
        if mark <= 0:
            raise ValueError(f"mid must be > 0, got {mid}")
        self._marks[normalized] = mark
        self.last_mid[normalized] = float(mark)

        self._equity = self._recompute_equity()
        self.assert_reconciled()
        return float(self._equity)

    def snapshot_equity(self, timestamp: dt.datetime | None = None) -> float:
        equity = float(self._equity)
        if timestamp is not None:
            point = EquityPoint(timestamp=timestamp, equity=equity)
            if self.equity_points and self.equity_points[-1].timestamp == point.timestamp:
                self.equity_points[-1] = point
                self.equity_curve[-1] = equity
            else:
                self.equity_points.append(point)
                self.equity_curve.append(equity)
        else:
            self.equity_curve.append(equity)
        self.peak_equity = max(self.peak_equity, equity)

        if self.peak_equity > 0:
            drawdown = (self.peak_equity - equity) / self.peak_equity
            if drawdown >= self.risk_cfg.max_drawdown_pct and not self.risk_state.trading_halted:
                self.risk_state.trading_halted = True
                self.risk_state.halt_reason = f"Max drawdown reached: {drawdown:.2%}"
        return equity

    def mark_to_market(
        self,
        symbol: str,
        mid: float,
        timestamp: dt.datetime | None = None,
    ) -> float:
        self.update_market_price(symbol, mid)
        return self.snapshot_equity(timestamp)

    def equity(self) -> float:
        return float(self._equity)

    def _recompute_equity(self) -> Decimal:
        total = self._cash
        for symbol, position in self.positions.items():
            mark = self._marks.get(symbol)
            if mark is not None:
                total += position.quantity * mark
        return _money(total)

    def assert_reconciled(self) -> None:
        expected = self._recompute_equity()
        if expected != self._equity:
            raise RuntimeError(
                f"Portfolio does not reconcile: tracked={self._equity} expected={expected}"
            )

    def check_stop_loss(self, symbol: str) -> Side | None:
        position = self.positions.get(symbol)
        if position is None or position.quantity == 0:
            return None
        mid = self._marks.get(symbol)
        if mid is None or position._avg_cost == 0:
            return None

        stop = _decimal(self.risk_cfg.stop_loss_pct)
        if position.quantity > 0 and mid <= position._avg_cost * (Decimal("1") - stop):
            return Side.SELL
        if position.quantity < 0 and mid >= position._avg_cost * (Decimal("1") + stop):
            return Side.BUY
        return None
