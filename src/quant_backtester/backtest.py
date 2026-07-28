from __future__ import annotations

import datetime as dt
import hashlib
import logging
from collections.abc import Callable, Iterable
from dataclasses import asdict
from itertools import groupby
from pathlib import Path
from typing import Any, cast

from quant_backtester.config import BacktestConfig, DataConfig
from quant_backtester.data.csv_data_handler import CSVDataHandler
from quant_backtester.events import (
    EquityPoint,
    MarketEvent,
    OrderEvent,
    OrderIntent,
    OrderType,
    Side,
)
from quant_backtester.execution.simulated_execution import SimulatedExecutionHandler
from quant_backtester.persistence.db import Database
from quant_backtester.persistence.models import (
    EquitySnapshot,
    Run,
    RunArtifact,
    RunFill,
    RunJournalEntry,
    RunOrder,
)
from quant_backtester.portfolio.simple_portfolio import MultiAssetPortfolio
from quant_backtester.ports import MarketDataSource, Strategy
from quant_backtester.results import (
    ArtifactStore,
    BacktestResult,
    LocalArtifactStore,
    build_manifest,
    canonical_json,
    new_run_id,
)
from quant_backtester.strategy.moving_average import MovingAverageCrossStrategy
from quant_backtester.utils.metrics import summarize_performance

logger = logging.getLogger(__name__)


class BacktestCancelled(RuntimeError):
    pass


def _float_value(value: object) -> float:
    if isinstance(value, (int, float, str)):
        return float(value)
    raise TypeError(f"Expected a numeric value, got {type(value).__name__}")


def run_to_model(result: dict[str, object], *, tenant_id: str = "default") -> Run:
    data = cast(dict[str, Any], result)
    raw_symbols = data.get("symbols")
    symbols: list[str]
    if isinstance(raw_symbols, (list, tuple)):
        symbols = [str(symbol) for symbol in raw_symbols]
    elif isinstance(raw_symbols, str):
        symbols = [symbol.strip() for symbol in raw_symbols.split(",") if symbol.strip()]
    else:
        symbols = []

    extra = data.get("extra")
    effective_config = _mapping_value(extra, "effective_config")
    return Run(
        run_id=str(data.get("run_id") or new_run_id()),
        tenant_id=tenant_id,
        dataset_version_id=(
            str(effective_config["dataset_id"])
            if effective_config.get("dataset_id") is not None
            else None
        ),
        run_name=str(data["run_name"]),
        status="SUCCEEDED",
        completed_at=dt.datetime.fromisoformat(str(data["completed_at"])),
        symbols=symbols,
        short_window=int(_float_value(data["short_window"])),
        long_window=int(_float_value(data["long_window"])),
        initial_cash=_float_value(data["initial_cash"]),
        final_equity=_float_value(data["final_equity"]),
        total_return=_float_value(data["total_return"]),
        sharpe=_float_value(data["sharpe"]),
        max_drawdown=_float_value(data["max_drawdown"]),
        total_commission=_float_value(data["total_commission"]),
        total_slippage_cost=_float_value(data["total_slippage_cost"]),
        halted=bool(data["halted"]),
        halt_reason=str(data["halt_reason"]) if data["halt_reason"] is not None else None,
        result_hash=str(data.get("result_hash") or ""),
        config_hash=_manifest_value(extra, "config_hash"),
        data_hash=_manifest_value(extra, "dataset_hash"),
        engine_version=_manifest_value(extra, "engine_version"),
        config=effective_config,
        manifest=_mapping_value(extra, "manifest"),
        extra=dict(extra) if isinstance(extra, dict) else {},
    )


def _mapping_value(value: object, key: str) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    nested = value.get(key)
    return dict(nested) if isinstance(nested, dict) else {}


def _manifest_value(value: object, key: str) -> str:
    manifest = _mapping_value(value, "manifest")
    raw = manifest.get(key)
    return str(raw) if raw is not None else ""


def _default_data_source(cfg: BacktestConfig) -> MarketDataSource:
    # Preserve the simple one-argument constructor as an integration seam used
    # by callers and tests while allowing non-default validation policies.
    if cfg.data == DataConfig():
        return CSVDataHandler(cfg.csv_path)
    return CSVDataHandler(cfg.csv_path, cfg.data)


def _event_digest_update(digest: Any, event: MarketEvent) -> None:
    digest.update(
        canonical_json(
            {
                "timestamp": event.timestamp,
                "symbol": event.symbol,
                "mid": event.mid,
                "bid": event.bid,
                "ask": event.ask,
                "spread_bps": event.spread_bps,
                "volume": event.volume,
            }
        ).encode("utf-8")
    )
    digest.update(b"\n")


def run_backtest(
    cfg: BacktestConfig,
    persist: bool = True,
    *,
    data_source: MarketDataSource | None = None,
    strategy: Strategy | None = None,
    execution: SimulatedExecutionHandler | None = None,
    portfolio: MultiAssetPortfolio | None = None,
    warmup_events: Iterable[MarketEvent] = (),
    run_id: str | None = None,
    write_artifact: bool | None = None,
    cancel_requested: Callable[[], bool] | None = None,
    tenant_id: str = "default",
    artifact_store: ArtifactStore | None = None,
    persistence: Database | None = None,
) -> dict[str, object]:
    """Run one deterministic simulation.

    Infrastructure is injected through ports so the simulation can be exercised
    without files or a database. The compatibility return value remains a JSON-
    serializable mapping; internally a typed ``BacktestResult`` is used.
    """

    started_at = dt.datetime.now(dt.UTC)
    resolved_run_id = run_id or new_run_id()
    should_write_artifact = persist if write_artifact is None else write_artifact
    output_directory = (
        cfg.ensure_outdir()
        if should_write_artifact and artifact_store is None
        else Path(cfg.out_dir)
    )
    logger.info(
        "Backtest started",
        extra={
            "event": {
                "run_id": resolved_run_id,
                "run_name": cfg.run_name,
                "symbols": cfg.symbols,
                "persist": persist,
            }
        },
    )

    database: Database | None = None
    if persist:
        database = persistence or Database(cfg.database_url)
        database.prepare_schema()

    source = data_source or _default_data_source(cfg)
    selected_strategy = strategy or MovingAverageCrossStrategy(
        symbols=cfg.symbols,
        short_window=cfg.short_window,
        long_window=cfg.long_window,
    )
    selected_execution = execution or SimulatedExecutionHandler(
        commission_per_trade=cfg.commission_per_trade,
        cfg=cfg.execution,
        rng_seed=cfg.execution.rng_seed,
    )
    selected_portfolio = portfolio or MultiAssetPortfolio(
        initial_cash=cfg.initial_cash,
        risk_cfg=cfg.risk,
    )

    for warmup_event in warmup_events:
        if warmup_event.symbol in cfg.symbols:
            selected_strategy.on_market(warmup_event)

    active_liquidations: dict[str, str] = {}
    dataset_digest = hashlib.sha256()

    grouped_events = groupby(source.stream(), key=lambda market: market.timestamp)
    for timestamp, timestamp_events_iter in grouped_events:
        if cancel_requested is not None and cancel_requested():
            raise BacktestCancelled(f"Backtest {resolved_run_id} was canceled")
        timestamp_events = tuple(timestamp_events_iter)
        for market in timestamp_events:
            _event_digest_update(dataset_digest, market)
            if market.symbol in cfg.symbols:
                selected_portfolio.update_market_price(market.symbol, market.mid)

        flattened_by_liquidation: set[str] = set()
        for market in timestamp_events:
            if market.symbol not in cfg.symbols:
                continue
            active_before = active_liquidations.get(market.symbol)
            position_before = selected_portfolio.get_position(market.symbol).quantity
            fills = selected_execution.on_market(
                market,
                position_quantity=position_before,
            )
            for fill in fills:
                selected_portfolio.on_fill(fill)

            position_after = selected_portfolio.get_position(market.symbol).quantity
            if active_before is not None:
                if position_after == 0:
                    selected_execution.cancel(active_before)
                    active_liquidations.pop(market.symbol, None)
                    flattened_by_liquidation.add(market.symbol)
                elif not selected_execution.is_pending(active_before):
                    active_liquidations.pop(market.symbol, None)

        selected_portfolio.snapshot_equity(timestamp)

        if selected_portfolio.risk_state.trading_halted:
            _ensure_risk_liquidations(
                timestamp_events,
                selected_execution,
                selected_portfolio,
                active_liquidations,
            )
            continue

        for market in timestamp_events:
            if market.symbol not in cfg.symbols or market.symbol in flattened_by_liquidation:
                continue

            active_order_id = active_liquidations.get(market.symbol)
            if active_order_id is not None and selected_execution.is_pending(active_order_id):
                continue

            stop_side = selected_portfolio.check_stop_loss(market.symbol)
            if stop_side is not None:
                position = selected_portfolio.get_position(market.symbol)
                if position.quantity != 0:
                    selected_execution.cancel_symbol_orders(
                        market.symbol,
                        intent=OrderIntent.STRATEGY,
                    )
                    liquidation = OrderEvent(
                        timestamp=market.timestamp,
                        symbol=market.symbol,
                        side=stop_side,
                        quantity=abs(position.quantity),
                        order_type=OrderType.MARKET,
                        intent=OrderIntent.STOP_LOSS,
                        reduce_only=True,
                        client_order_id=(
                            f"{resolved_run_id}:stop:{market.symbol}:{market.timestamp.isoformat()}"
                        ),
                    )
                    active_liquidations[market.symbol] = selected_execution.submit(liquidation)
                continue

            signal = selected_strategy.on_market(market)
            pending_quantity = selected_execution.pending_signed_quantity(market.symbol)
            if signal and selected_portfolio.can_place_order(
                signal.symbol,
                signal.side,
                cfg.trade_quantity,
                pending_signed_quantity=pending_quantity,
            ):
                selected_execution.submit(
                    OrderEvent(
                        timestamp=signal.timestamp,
                        symbol=signal.symbol,
                        side=signal.side,
                        quantity=cfg.trade_quantity,
                        order_type=OrderType.MARKET,
                        intent=OrderIntent.STRATEGY,
                    )
                )

    final_equity = selected_portfolio.equity()
    if selected_portfolio.equity_curve and selected_portfolio.equity_curve[-1] != final_equity:
        raise RuntimeError("Final equity snapshot is out of sync with portfolio state")
    selected_portfolio.assert_reconciled()

    performance = summarize_performance(
        selected_portfolio.equity_points,
        initial_equity=cfg.initial_cash,
        configured_periods_per_year=cfg.metrics.periods_per_year,
        annual_risk_free_rate=cfg.metrics.annual_risk_free_rate,
    )
    manifest = build_manifest(
        cfg,
        run_id=resolved_run_id,
        dataset_hash=dataset_digest.hexdigest(),
        strategy=type(selected_strategy).__qualname__,
    )
    extra_payload: dict[str, object] = {
        "execution": asdict(cfg.execution),
        "risk": asdict(cfg.risk),
        "metrics": asdict(cfg.metrics),
        "data": asdict(cfg.data),
        "pending_order_count": selected_execution.pending_order_count(),
        "active_liquidation_symbols": sorted(active_liquidations),
        # Kept for compatibility with the safety regression suite.
        "active_stop_symbols": sorted(active_liquidations),
        "equity_observation_count": performance.observation_count,
        "periods_per_year": performance.periods_per_year,
        "journal_entry_count": len(selected_portfolio.journal),
        "max_persisted_equity_points": cfg.max_persisted_equity_points,
        "effective_config": cfg.to_dict(redact_secrets=True),
        "manifest": manifest.to_dict(),
    }
    completed_at = dt.datetime.now(dt.UTC)
    typed_result = BacktestResult(
        run_id=resolved_run_id,
        run_name=cfg.run_name,
        symbols=cfg.symbols,
        short_window=cfg.short_window,
        long_window=cfg.long_window,
        initial_cash=cfg.initial_cash,
        final_equity=final_equity,
        total_return=performance.total_return,
        sharpe=performance.sharpe,
        max_drawdown=performance.max_drawdown,
        total_commission=selected_portfolio.total_commission,
        total_slippage_cost=selected_portfolio.total_slippage_cost,
        realized_pnl=selected_portfolio.realized_pnl,
        halted=selected_portfolio.risk_state.trading_halted,
        halt_reason=selected_portfolio.risk_state.halt_reason,
        created_at=started_at,
        completed_at=completed_at,
        extra=extra_payload,
    )
    result = typed_result.to_dict()

    artifact_descriptor = None
    if should_write_artifact:
        selected_artifact_store = artifact_store or LocalArtifactStore(output_directory)
        artifact_descriptor = selected_artifact_store.write_result(
            typed_result,
            manifest=manifest,
        )

    if persist:
        assert database is not None
        model = run_to_model(result, tenant_id=tenant_id)
        _attach_run_details(
            model,
            selected_execution,
            selected_portfolio,
            max_equity_points=cfg.max_persisted_equity_points,
        )
        if artifact_descriptor is not None:
            model.artifacts.append(
                RunArtifact(
                    kind="result",
                    uri=artifact_descriptor.uri,
                    content_hash=artifact_descriptor.content_hash,
                    size_bytes=artifact_descriptor.size_bytes,
                )
            )
        database.insert_run(model)

    logger.info(
        "Backtest completed",
        extra={
            "event": {
                "run_id": resolved_run_id,
                "run_name": cfg.run_name,
                "result_hash": typed_result.result_hash,
                "final_equity": final_equity,
                "total_return": performance.total_return,
                "halted": selected_portfolio.risk_state.trading_halted,
            }
        },
    )
    return result


def _attach_run_details(
    run: Run,
    execution: SimulatedExecutionHandler,
    portfolio: MultiAssetPortfolio,
    *,
    max_equity_points: int,
) -> None:
    for snapshot in execution.order_snapshots():
        order = snapshot.order
        run.orders.append(
            RunOrder(
                order_id=snapshot.order_id,
                client_order_id=order.client_order_id,
                timestamp=order.timestamp,
                symbol=order.symbol,
                side=order.side.value,
                quantity=order.quantity,
                order_type=order.order_type.value,
                limit_price=order.limit_price,
                stop_price=order.stop_price,
                time_in_force=order.time_in_force.value,
                intent=order.intent.value,
                reduce_only=order.reduce_only,
                status=snapshot.status.value,
                filled_quantity=snapshot.filled_quantity,
                remaining_quantity=snapshot.remaining_quantity,
                rejection_reason=snapshot.rejection_reason,
            )
        )
    for sequence, fill in enumerate(execution.fills, start=1):
        run.fills.append(
            RunFill(
                execution_id=fill.execution_id or f"anonymous-{sequence}",
                order_id=fill.order_id,
                timestamp=fill.timestamp,
                symbol=fill.symbol,
                side=fill.side.value,
                quantity=fill.quantity,
                fill_price=fill.fill_price,
                mid_price=fill.mid_price,
                commission=fill.commission,
                slippage=fill.slippage,
            )
        )
    for sequence, point in _sampled_equity_points(
        portfolio.equity_points,
        max_points=max_equity_points,
    ):
        run.equity_snapshots.append(
            EquitySnapshot(
                sequence=sequence,
                timestamp=point.timestamp,
                equity=point.equity,
            )
        )
    for sequence, entry in enumerate(portfolio.journal, start=1):
        run.journal_entries.append(
            RunJournalEntry(
                sequence=sequence,
                timestamp=entry.timestamp,
                transaction_id=entry.transaction_id,
                debit_account=entry.debit_account,
                credit_account=entry.credit_account,
                amount=entry.amount,
                symbol=entry.symbol,
            )
        )


def _sampled_equity_points(
    points: list[EquityPoint],
    *,
    max_points: int,
) -> list[tuple[int, EquityPoint]]:
    if len(points) <= max_points:
        return list(enumerate(points, start=1))
    last_index = len(points) - 1
    indices = {round(sample * last_index / (max_points - 1)) for sample in range(max_points)}
    return [(index + 1, points[index]) for index in sorted(indices)]


def _ensure_risk_liquidations(
    market_events: Iterable[MarketEvent],
    execution: SimulatedExecutionHandler,
    portfolio: MultiAssetPortfolio,
    active_liquidations: dict[str, str],
) -> None:
    for market in market_events:
        position = portfolio.get_position(market.symbol)
        if position.quantity == 0:
            active_id = active_liquidations.pop(market.symbol, None)
            if active_id is not None:
                execution.cancel(active_id)
            continue

        active_id = active_liquidations.get(market.symbol)
        if active_id is not None and execution.is_pending(active_id):
            continue

        execution.cancel_symbol_orders(market.symbol)
        side = OrderEvent(
            timestamp=market.timestamp,
            symbol=market.symbol,
            side=Side.SELL if position.quantity > 0 else Side.BUY,
            quantity=abs(position.quantity),
            order_type=OrderType.MARKET,
            intent=OrderIntent.RISK_LIQUIDATION,
            reduce_only=True,
            client_order_id=f"risk:{market.symbol}:{market.timestamp.isoformat()}",
        )
        active_liquidations[market.symbol] = execution.submit(side)
