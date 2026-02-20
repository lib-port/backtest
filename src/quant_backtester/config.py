from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _require_range(name: str, value: float, lo: float, hi: float) -> None:
    if not (lo <= value <= hi):
        raise ValueError(f"{name} must be in [{lo}, {hi}], got {value}")


def _require_positive(name: str, value: float) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be > 0, got {value}")


def _require_non_negative(name: str, value: float) -> None:
    if value < 0:
        raise ValueError(f"{name} must be >= 0, got {value}")


@dataclass(frozen=True)
class MicrostructureConfig:
    # Latency: fills occur after this many market events for the same symbol.
    latency_events: int = 1

    # If market data has no volume column, assume this available volume per tick.
    default_tick_volume: float = 5_000.0

    # For MARKET orders, max fraction of tick volume that can be consumed (rest waits next tick).
    max_participation_rate: float = 0.2  # 20%

    # For LIMIT orders, model queue position: fraction of resting liquidity ahead of us.
    # Higher => less likely to fill quickly.
    queue_ahead_fraction: float = 0.7

    # Probability of getting filled when price is at/through our limit, after queueing.
    base_fill_probability: float = 0.8

    def __post_init__(self) -> None:
        if self.latency_events < 0:
            raise ValueError(f"latency_events must be >= 0, got {self.latency_events}")
        _require_positive("default_tick_volume", self.default_tick_volume)
        _require_range("max_participation_rate", self.max_participation_rate, 0.0, 1.0)
        _require_range("queue_ahead_fraction", self.queue_ahead_fraction, 0.0, 1.0)
        _require_range("base_fill_probability", self.base_fill_probability, 0.0, 1.0)


@dataclass(frozen=True)
class ExecutionConfig:
    # If bid/ask absent, use spread_bps or this fallback.
    default_spread_bps: float = 5.0

    # Impact model: impact = impact_bps_per_unit * (qty / impact_volume)
    impact_bps_per_unit: float = 2.0
    impact_volume: float = 10_000.0  # "liquidity" scale; bigger => less impact
    rng_seed: int = 7

    micro: MicrostructureConfig = MicrostructureConfig()

    def __post_init__(self) -> None:
        _require_non_negative("default_spread_bps", self.default_spread_bps)
        _require_non_negative("impact_bps_per_unit", self.impact_bps_per_unit)
        _require_positive("impact_volume", self.impact_volume)


@dataclass(frozen=True)
class RiskConfig:
    max_position_per_symbol: int = 1_000
    stop_loss_pct: float = 0.05  # 5% stop from avg cost
    max_drawdown_pct: float = 0.20  # halt if equity drawdown exceeds this

    def __post_init__(self) -> None:
        if self.max_position_per_symbol <= 0:
            raise ValueError(
                f"max_position_per_symbol must be > 0, got {self.max_position_per_symbol}"
            )
        _require_range("stop_loss_pct", self.stop_loss_pct, 0.0, 1.0)
        _require_range("max_drawdown_pct", self.max_drawdown_pct, 0.0, 1.0)


@dataclass(frozen=True)
class BacktestConfig:
    symbols: tuple[str, ...]
    initial_cash: float = 100_000.0
    trade_quantity: int = 100
    commission_per_trade: float = 1.0
    short_window: int = 20
    long_window: int = 50
    csv_path: str = "data/sample_prices.csv"
    run_name: str = "default"
    out_dir: str = "runs"
    database_url: str = "sqlite:///runs/runs.db"

    execution: ExecutionConfig = ExecutionConfig()
    risk: RiskConfig = RiskConfig()

    def __post_init__(self) -> None:
        if not self.symbols:
            raise ValueError("symbols must not be empty")
        if any(not s.strip() for s in self.symbols):
            raise ValueError("symbols must not contain empty values")
        _require_positive("initial_cash", self.initial_cash)
        _require_positive("trade_quantity", float(self.trade_quantity))
        _require_non_negative("commission_per_trade", self.commission_per_trade)
        if self.short_window <= 0:
            raise ValueError(f"short_window must be > 0, got {self.short_window}")
        if self.long_window <= 0:
            raise ValueError(f"long_window must be > 0, got {self.long_window}")
        if self.short_window >= self.long_window:
            raise ValueError(
                f"short_window must be < long_window, got {self.short_window} and {self.long_window}"
            )
        if not self.csv_path.strip():
            raise ValueError("csv_path must not be empty")
        if not self.run_name.strip():
            raise ValueError("run_name must not be empty")
        if not self.out_dir.strip():
            raise ValueError("out_dir must not be empty")
        if not self.database_url.strip():
            raise ValueError("database_url must not be empty")

    def ensure_outdir(self) -> Path:
        p = Path(self.out_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p


def load_config_file(path: str) -> dict[str, Any]:
    p = Path(path)
    suffix = p.suffix.lower()
    raw = p.read_text(encoding="utf-8")
    if suffix == ".json":
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            raise ValueError("Top-level JSON config must be an object")
        return obj
    if suffix in (".yml", ".yaml"):
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - depends on optional install
            raise ValueError("YAML config requires PyYAML; install `pyyaml`") from exc
        obj = yaml.safe_load(raw)
        if not isinstance(obj, dict):
            raise ValueError("Top-level YAML config must be a mapping")
        return obj
    raise ValueError(f"Unsupported config extension '{suffix}'. Use .json, .yml, or .yaml")
