from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from quant_backtester.backtest import run_backtest
from quant_backtester.config import (
    BacktestConfig,
    ExecutionConfig,
    MicrostructureConfig,
    RiskConfig,
    load_config_file,
)
from quant_backtester.logging_utils import configure_logging
from quant_backtester.sweep import run_parameter_sweep


def _parse_symbols(s: str) -> tuple[str, ...]:
    syms = [x.strip().upper() for x in s.split(",") if x.strip()]
    if not syms:
        raise argparse.ArgumentTypeError("No symbols provided")
    return tuple(dict.fromkeys(syms))  # preserve order, de-dup


def _parse_grid(raw: str) -> list[int]:
    values = [int(x) for x in raw.split(",") if x.strip()]
    if not values:
        raise ValueError("Grid values must not be empty")
    return values


def _pick(cli_value: Any, file_value: Any, default_value: Any) -> Any:
    if cli_value is not None:
        return cli_value
    if file_value is not None:
        return file_value
    return default_value


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="quant_backtester")
    sub = p.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=None, help="Path to .json/.yml/.yaml config")
    common.add_argument("--dry-run", action="store_true", help="Validate config and exit")
    common.add_argument("--log-level", default="INFO")
    common.add_argument("--json-logs", action="store_true")
    common.add_argument("--csv", required=False, default=None, help="Path to CSV market data")
    common.add_argument(
        "--symbols",
        type=_parse_symbols,
        required=False,
        default=None,
        help="Comma-separated symbols",
    )
    common.add_argument("--db", default=None)
    common.add_argument("--out", default=None)
    common.add_argument("--run-name", default=None)
    common.add_argument(
        "--no-persist",
        action="store_true",
        help="Do not persist run results to the database",
    )

    # execution config
    common.add_argument("--default-spread-bps", type=float, default=None)
    common.add_argument("--impact-bps-per-unit", type=float, default=None)
    common.add_argument("--impact-volume", type=float, default=None)
    common.add_argument("--rng-seed", type=int, default=None)
    common.add_argument("--commission", type=float, default=None)
    common.add_argument("--qty", type=int, default=None)
    common.add_argument("--cash", type=float, default=None)

    # microstructure config
    common.add_argument("--latency-events", type=int, default=None)
    common.add_argument("--default-tick-volume", type=float, default=None)
    common.add_argument("--max-participation", type=float, default=None)
    common.add_argument("--queue-ahead", type=float, default=None)
    common.add_argument("--base-fill-prob", type=float, default=None)

    # risk config
    common.add_argument("--max-pos", type=int, default=None)
    common.add_argument("--stop-loss", type=float, default=None)
    common.add_argument("--max-dd", type=float, default=None)

    run = sub.add_parser("run", parents=[common], help="Run a single backtest")
    run.add_argument("--short", type=int, default=None)
    run.add_argument("--long", type=int, default=None)

    sweep = sub.add_parser("sweep", parents=[common], help="Run a parameter sweep")
    sweep.add_argument("--short-grid", default=None, help="Comma-separated short windows")
    sweep.add_argument("--long-grid", default=None, help="Comma-separated long windows")
    sweep.add_argument("--export-csv", default=None)

    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    configure_logging(level=args.log_level, json_logs=args.json_logs)

    file_cfg: dict[str, Any] = {}
    if args.config:
        try:
            file_cfg = load_config_file(args.config)
        except Exception as exc:
            parser.error(f"Failed to load config file: {exc}")

    file_exec = file_cfg.get("execution", {})
    file_micro = file_exec.get("micro", {}) if isinstance(file_exec, dict) else {}
    file_risk = file_cfg.get("risk", {})
    if not isinstance(file_exec, dict):
        parser.error("'execution' in config file must be a mapping")
    if not isinstance(file_micro, dict):
        parser.error("'execution.micro' in config file must be a mapping")
    if not isinstance(file_risk, dict):
        parser.error("'risk' in config file must be a mapping")

    symbols = args.symbols
    if symbols is None:
        raw_symbols = file_cfg.get("symbols")
        if isinstance(raw_symbols, str):
            symbols = _parse_symbols(raw_symbols)
        elif isinstance(raw_symbols, list):
            symbols = tuple(str(x).strip().upper() for x in raw_symbols if str(x).strip())
    if not symbols:
        parser.error("Missing symbols. Pass --symbols or provide 'symbols' in --config")

    csv_path = _pick(args.csv, file_cfg.get("csv_path"), None)
    if not isinstance(csv_path, str) or not csv_path.strip():
        parser.error("Missing CSV path. Pass --csv or provide 'csv_path' in --config")

    run_name = _pick(args.run_name, file_cfg.get("run_name"), "run")
    out_dir = _pick(args.out, file_cfg.get("out_dir"), "runs")
    db_url = _pick(
        args.db, file_cfg.get("database_url"), os.getenv("DATABASE_URL", "sqlite:///runs/runs.db")
    )

    try:
        micro_cfg = MicrostructureConfig(
            latency_events=int(_pick(args.latency_events, file_micro.get("latency_events"), 1)),
            default_tick_volume=float(
                _pick(args.default_tick_volume, file_micro.get("default_tick_volume"), 5_000.0)
            ),
            max_participation_rate=float(
                _pick(args.max_participation, file_micro.get("max_participation_rate"), 0.2)
            ),
            queue_ahead_fraction=float(
                _pick(args.queue_ahead, file_micro.get("queue_ahead_fraction"), 0.7)
            ),
            base_fill_probability=float(
                _pick(args.base_fill_prob, file_micro.get("base_fill_probability"), 0.8)
            ),
        )
        exec_cfg = ExecutionConfig(
            default_spread_bps=float(
                _pick(args.default_spread_bps, file_exec.get("default_spread_bps"), 5.0)
            ),
            impact_bps_per_unit=float(
                _pick(args.impact_bps_per_unit, file_exec.get("impact_bps_per_unit"), 2.0)
            ),
            impact_volume=float(
                _pick(args.impact_volume, file_exec.get("impact_volume"), 10_000.0)
            ),
            rng_seed=int(_pick(args.rng_seed, file_exec.get("rng_seed"), 7)),
            micro=micro_cfg,
        )
        risk_cfg = RiskConfig(
            max_position_per_symbol=int(
                _pick(args.max_pos, file_risk.get("max_position_per_symbol"), 1_000)
            ),
            stop_loss_pct=float(_pick(args.stop_loss, file_risk.get("stop_loss_pct"), 0.05)),
            max_drawdown_pct=float(_pick(args.max_dd, file_risk.get("max_drawdown_pct"), 0.20)),
        )
    except (TypeError, ValueError) as exc:
        parser.error(f"Invalid execution/risk config: {exc}")

    short_window = int(_pick(getattr(args, "short", None), file_cfg.get("short_window"), 20))
    long_window = int(_pick(getattr(args, "long", None), file_cfg.get("long_window"), 50))
    try:
        cfg = BacktestConfig(
            symbols=symbols,
            initial_cash=float(_pick(args.cash, file_cfg.get("initial_cash"), 100_000.0)),
            trade_quantity=int(_pick(args.qty, file_cfg.get("trade_quantity"), 100)),
            commission_per_trade=float(
                _pick(args.commission, file_cfg.get("commission_per_trade"), 1.0)
            ),
            short_window=short_window,
            long_window=long_window,
            csv_path=csv_path,
            run_name=str(run_name),
            out_dir=str(out_dir),
            database_url=str(db_url),
            execution=exec_cfg,
            risk=risk_cfg,
        )
    except ValueError as exc:
        parser.error(str(exc))

    Path(cfg.out_dir).mkdir(parents=True, exist_ok=True)

    short_raw: object | None = None
    long_raw: object | None = None
    if args.cmd == "sweep":
        short_raw = _pick(args.short_grid, file_cfg.get("short_grid"), "10,20,30")
        long_raw = _pick(args.long_grid, file_cfg.get("long_grid"), "50,100,150")

    if args.dry_run:
        effective_config: dict[str, object] = {
            "cmd": str(args.cmd),
            "symbols": list(cfg.symbols),
            "initial_cash": cfg.initial_cash,
            "trade_quantity": cfg.trade_quantity,
            "commission_per_trade": cfg.commission_per_trade,
            "short_window": cfg.short_window,
            "long_window": cfg.long_window,
            "csv_path": cfg.csv_path,
            "run_name": cfg.run_name,
            "out_dir": cfg.out_dir,
            "database_url": cfg.database_url,
            "persist": not bool(args.no_persist),
            "execution": asdict(cfg.execution),
            "risk": asdict(cfg.risk),
        }
        if args.cmd == "sweep":
            effective_config["short_grid"] = short_raw
            effective_config["long_grid"] = long_raw
            effective_config["export_csv"] = args.export_csv or str(
                Path(cfg.out_dir) / "sweep_results.csv"
            )

        print("Config valid.")
        print(json.dumps(effective_config, indent=2, sort_keys=True))
        return

    if args.cmd == "run":
        res = run_backtest(cfg, persist=not bool(args.no_persist))
        for k in [
            "run_name",
            "symbols",
            "final_equity",
            "total_return",
            "sharpe",
            "max_drawdown",
            "halted",
            "halt_reason",
        ]:
            print(f"{k}: {res.get(k)}")
        return

    if args.cmd == "sweep":
        assert short_raw is not None
        assert long_raw is not None
        try:
            if isinstance(short_raw, list):
                short_grid = [int(x) for x in short_raw]
            else:
                short_grid = _parse_grid(str(short_raw))
            if isinstance(long_raw, list):
                long_grid = [int(x) for x in long_raw]
            else:
                long_grid = _parse_grid(str(long_raw))
        except ValueError as exc:
            parser.error(f"Invalid sweep grid: {exc}")
        df = run_parameter_sweep(cfg, short_grid, long_grid, export_csv=args.export_csv)
        print(df.head(10).to_string(index=False))
        print(f"Saved sweep CSV to: {args.export_csv or (Path(cfg.out_dir) / 'sweep_results.csv')}")
        return

    raise SystemExit("Unknown command")


if __name__ == "__main__":
    main()
