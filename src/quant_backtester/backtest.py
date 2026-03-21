from __future__ import annotations

import datetime as dt
import logging
from dataclasses import asdict
from typing import Any, cast

from quant_backtester.config import BacktestConfig
from quant_backtester.data.csv_data_handler import CSVDataHandler
from quant_backtester.events import OrderEvent
from quant_backtester.execution.simulated_execution import SimulatedExecutionHandler
from quant_backtester.persistence.db import Database
from quant_backtester.persistence.models import Run
from quant_backtester.portfolio.simple_portfolio import MultiAssetPortfolio
from quant_backtester.strategy.moving_average import MovingAverageCrossStrategy
from quant_backtester.utils.metrics import max_drawdown, returns_from_equity, sharpe_ratio

logger = logging.getLogger(__name__)


def run_to_model(result: dict[str, object]) -> Run:
    data = cast(dict[str, Any], result)
    raw_symbols = data.get("symbols")
    symbols: list[str]
    if isinstance(raw_symbols, (list, tuple)):
        symbols = [str(s) for s in raw_symbols]
    elif isinstance(raw_symbols, str):
        symbols = [s.strip() for s in raw_symbols.split(",") if s.strip()]
    else:
        symbols = []

    return Run(
        run_name=str(data["run_name"]),
        symbols=symbols,
        short_window=int(data["short_window"]),
        long_window=int(data["long_window"]),
        initial_cash=float(data["initial_cash"]),
        final_equity=float(data["final_equity"]),
        total_return=float(data["total_return"]),
        sharpe=float(data["sharpe"]),
        max_drawdown=float(data["max_drawdown"]),
        total_commission=float(data["total_commission"]),
        total_slippage_cost=float(data["total_slippage_cost"]),
        halted=1 if bool(data["halted"]) else 0,
        halt_reason=str(data["halt_reason"]) if data["halt_reason"] is not None else None,
        extra=dict(data["extra"]) if isinstance(data["extra"], dict) else {},
    )


def run_backtest(cfg: BacktestConfig, persist: bool = True) -> dict[str, object]:
    cfg.ensure_outdir()
    logger.info(
        "Backtest started",
        extra={"event": {"run_name": cfg.run_name, "symbols": cfg.symbols, "persist": persist}},
    )

    db: Database | None = None
    if persist:
        # Alembic recommended for production, but create_all keeps dev UX smooth.
        db = Database(cfg.database_url)
        db.create_tables()

    data = CSVDataHandler(cfg.csv_path)
    strategy = MovingAverageCrossStrategy(
        symbols=cfg.symbols,
        short_window=cfg.short_window,
        long_window=cfg.long_window,
    )
    execution = SimulatedExecutionHandler(
        commission_per_trade=cfg.commission_per_trade,
        cfg=cfg.execution,
        rng_seed=cfg.execution.rng_seed,
    )
    portfolio = MultiAssetPortfolio(
        initial_cash=cfg.initial_cash,
        risk_cfg=cfg.risk,
    )

    for market in data.stream():
        if market.symbol not in cfg.symbols:
            continue

        portfolio.mark_to_market(market.symbol, market.mid)

        # Process fills from microstructure engine (latency/partial fills)
        fills = execution.on_market(market)
        for f in fills:
            portfolio.on_fill(f)

        # If drawdown halt is active, do not generate new trades
        if portfolio.risk_state.trading_halted:
            continue

        # Stop-loss check (generate liquidation order if triggered)
        stop_side = portfolio.check_stop_loss(market.symbol)
        if stop_side is not None:
            pos = portfolio.get_position(market.symbol)
            qty = abs(pos.quantity)
            if qty > 0:
                liquidation = OrderEvent(
                    timestamp=market.timestamp,
                    symbol=market.symbol,
                    side=stop_side,
                    quantity=qty,
                    order_type="MARKET",
                )
                execution.submit(liquidation)
            continue

        signal = strategy.on_market(market)
        if signal and portfolio.can_place_order(signal.symbol, signal.side, cfg.trade_quantity):
            order = OrderEvent(
                timestamp=signal.timestamp,
                symbol=signal.symbol,
                side=signal.side,
                quantity=cfg.trade_quantity,
                order_type="MARKET",
            )
            execution.submit(order)

    eq = portfolio.equity_curve
    rets = returns_from_equity(eq)

    final_equity = float(eq[-1]) if eq else cfg.initial_cash
    total_return = (final_equity / cfg.initial_cash) - 1.0
    sr = sharpe_ratio(rets)
    mdd = max_drawdown(eq)

    extra_payload: dict[str, object] = {
        "execution": asdict(cfg.execution),
        "risk": asdict(cfg.risk),
    }

    result: dict[str, object] = {
        "run_name": cfg.run_name,
        "symbols": list(cfg.symbols),
        "short_window": cfg.short_window,
        "long_window": cfg.long_window,
        "initial_cash": cfg.initial_cash,
        "final_equity": final_equity,
        "total_return": float(total_return),
        "sharpe": float(sr),
        "max_drawdown": float(mdd),
        "total_commission": float(portfolio.total_commission),
        "total_slippage_cost": float(portfolio.total_slippage_cost),
        "halted": bool(portfolio.risk_state.trading_halted),
        "halt_reason": portfolio.risk_state.halt_reason,
        "created_at": dt.datetime.now(dt.UTC).isoformat(),
        "extra": extra_payload,
    }

    if persist:
        assert db is not None
        with db.session() as s:
            s.add(run_to_model(result))
            s.commit()

    logger.info(
        "Backtest completed",
        extra={
            "event": {
                "run_name": cfg.run_name,
                "final_equity": final_equity,
                "total_return": float(total_return),
                "halted": bool(portfolio.risk_state.trading_halted),
            }
        },
    )
    return result
