from __future__ import annotations

import argparse
import logging
import os
import time
from pathlib import Path

import pandas as pd

from quant_backtester.backtest import run_backtest
from quant_backtester.config import BacktestConfig, ExecutionConfig
from quant_backtester.logging_utils import configure_logging

logger = logging.getLogger(__name__)


def make_data(path: Path, ticks: int, symbols: tuple[str, ...]) -> None:
    rows: list[dict[str, object]] = []
    ts = pd.Timestamp("2020-01-01")
    for i in range(ticks):
        for j, sym in enumerate(symbols):
            mid = 100.0 + (i * 0.01) + (j * 0.1)
            rows.append(
                {
                    "date": ts + pd.Timedelta(minutes=i),
                    "symbol": sym,
                    "mid": mid,
                    "spread_bps": 5.0,
                    "volume": 10_000.0,
                }
            )
    df = pd.DataFrame(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks", type=int, default=20_000)
    ap.add_argument("--symbols", default="AAPL,MSFT")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--max-seconds", type=float, default=None)
    ap.add_argument("--csv", default="runs/benchmark_input.csv")
    ap.add_argument("--rng-seed", type=int, default=7)
    args = ap.parse_args()
    configure_logging(level="INFO", json_logs=bool(os.getenv("CI")))

    symbols = tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip())
    if not symbols:
        raise SystemExit("No symbols provided")

    csv_path = Path(args.csv)
    make_data(csv_path, ticks=args.ticks, symbols=symbols)

    cfg = BacktestConfig(
        symbols=symbols,
        csv_path=str(csv_path),
        run_name="benchmark",
        short_window=20,
        long_window=50,
        out_dir="runs",
        database_url="sqlite:///runs/benchmark.db",
        execution=ExecutionConfig(rng_seed=args.rng_seed),
    )
    logger.info(
        "Benchmark started",
        extra={
            "event": {
                "ticks": args.ticks,
                "symbols": list(symbols),
                "repeats": args.repeats,
                "rng_seed": args.rng_seed,
                "csv": str(csv_path),
            }
        },
    )

    best = float("inf")
    for _ in range(args.repeats):
        t0 = time.perf_counter()
        run_backtest(cfg, persist=False)
        elapsed = time.perf_counter() - t0
        best = min(best, elapsed)

    print(f"best_seconds={best:.6f}")
    print(f"ticks={args.ticks}")
    print(f"symbols={len(symbols)}")
    print(f"rng_seed={args.rng_seed}")
    if args.max_seconds is not None and best > args.max_seconds:
        raise SystemExit(
            f"Performance check failed: best_seconds={best:.6f} exceeded max={args.max_seconds:.6f}"
        )
    logger.info("Benchmark completed", extra={"event": {"best_seconds": best}})


if __name__ == "__main__":
    main()
