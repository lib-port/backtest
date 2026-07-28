from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from quant_backtester.events import normalize_symbol


def _require_range(name: str, value: float, lo: float, hi: float) -> None:
    if not (lo <= value <= hi):
        raise ValueError(f"{name} must be in [{lo}, {hi}], got {value}")


def _require_positive(name: str, value: float) -> None:
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and > 0, got {value}")


def _require_non_negative(name: str, value: float) -> None:
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and >= 0, got {value}")


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


DuplicatePolicy = Literal["error", "last"]


@dataclass(frozen=True)
class DataConfig:
    """Input validation and normalization policy."""

    source_timezone: str = "UTC"
    duplicate_policy: DuplicatePolicy = "error"
    chunksize: int = 50_000
    require_sorted: bool = True
    max_events: int = 2_000_000
    strict_schema: bool = True
    require_all_symbols: bool = True

    def __post_init__(self) -> None:
        try:
            ZoneInfo(self.source_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown source_timezone: {self.source_timezone}") from exc
        if self.duplicate_policy not in ("error", "last"):
            raise ValueError(f"Unsupported duplicate_policy: {self.duplicate_policy}")
        if not 1 <= self.chunksize <= 1_000_000:
            raise ValueError(f"chunksize must be in [1, 1000000], got {self.chunksize}")
        if not 1 <= self.max_events <= 50_000_000:
            raise ValueError(f"max_events must be in [1, 50000000], got {self.max_events}")


@dataclass(frozen=True)
class MetricsConfig:
    """Performance metric assumptions recorded with every run."""

    annual_risk_free_rate: float = 0.0
    periods_per_year: float | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.annual_risk_free_rate) or self.annual_risk_free_rate <= -1.0:
            raise ValueError("annual_risk_free_rate must be finite and > -1")
        if self.periods_per_year is not None:
            _require_positive("periods_per_year", self.periods_per_year)


@dataclass(frozen=True)
class RiskConfig:
    max_position_per_symbol: int = 1_000
    stop_loss_pct: float = 0.05  # 5% stop from avg cost
    max_drawdown_pct: float = 0.20  # halt if equity drawdown exceeds this
    max_gross_exposure: float | None = None
    max_net_exposure: float | None = None
    max_order_notional: float | None = None
    max_leverage: float = 1.0
    max_daily_loss_pct: float | None = None
    allow_short: bool = True

    def __post_init__(self) -> None:
        if self.max_position_per_symbol <= 0:
            raise ValueError(
                f"max_position_per_symbol must be > 0, got {self.max_position_per_symbol}"
            )
        _require_range("stop_loss_pct", self.stop_loss_pct, 0.0, 1.0)
        _require_range("max_drawdown_pct", self.max_drawdown_pct, 0.0, 1.0)
        if self.max_gross_exposure is not None:
            _require_positive("max_gross_exposure", self.max_gross_exposure)
        if self.max_net_exposure is not None:
            _require_positive("max_net_exposure", self.max_net_exposure)
        if self.max_order_notional is not None:
            _require_positive("max_order_notional", self.max_order_notional)
        _require_positive("max_leverage", self.max_leverage)
        if self.max_leverage > 100:
            raise ValueError("max_leverage must be <= 100")
        if self.max_daily_loss_pct is not None:
            _require_range("max_daily_loss_pct", self.max_daily_loss_pct, 0.0, 1.0)


PendingOrderPolicy = Literal["cancel", "retain"]
PositionEndPolicy = Literal["mark_to_market", "liquidate", "require_flat"]


@dataclass(frozen=True)
class EndOfRunConfig:
    pending_order_policy: PendingOrderPolicy = "cancel"
    position_policy: PositionEndPolicy = "mark_to_market"

    def __post_init__(self) -> None:
        if self.pending_order_policy not in ("cancel", "retain"):
            raise ValueError(f"Unsupported pending_order_policy: {self.pending_order_policy}")
        if self.position_policy not in ("mark_to_market", "liquidate", "require_flat"):
            raise ValueError(f"Unsupported position_policy: {self.position_policy}")


@dataclass(frozen=True)
class BacktestConfig:
    symbols: tuple[str, ...]
    initial_cash: float = 100_000.0
    trade_quantity: int = 100
    commission_per_trade: float = 1.0
    short_window: int = 20
    long_window: int = 50
    csv_path: str = "data/sample_prices.csv"
    dataset_id: str | None = None
    dataset_sha256: str | None = None
    dataset_schema_version: int | None = None
    run_name: str = "default"
    out_dir: str = "runs"
    database_url: str = "sqlite:///runs/runs.db"
    engine_semantics_version: str = "3.0"
    max_persisted_equity_points: int = 100_000

    execution: ExecutionConfig = ExecutionConfig()
    risk: RiskConfig = RiskConfig()
    data: DataConfig = DataConfig()
    metrics: MetricsConfig = MetricsConfig()
    end_of_run: EndOfRunConfig = EndOfRunConfig()

    def __post_init__(self) -> None:
        if not self.symbols:
            raise ValueError("symbols must not be empty")
        if len(self.symbols) > 1_000:
            raise ValueError("symbols must contain at most 1000 instruments")
        normalized_symbols = tuple(
            dict.fromkeys(normalize_symbol(symbol) for symbol in self.symbols)
        )
        object.__setattr__(self, "symbols", normalized_symbols)
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
        if not self.csv_path.strip() or len(self.csv_path) > 2_048:
            raise ValueError("csv_path must contain 1 to 2048 characters")
        if self.dataset_sha256 is not None and (
            len(self.dataset_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.dataset_sha256)
        ):
            raise ValueError("dataset_sha256 must be a lowercase SHA-256 digest")
        if self.dataset_id is not None:
            try:
                canonical_dataset_id = str(uuid.UUID(self.dataset_id))
            except (ValueError, AttributeError) as exc:
                raise ValueError("dataset_id must be a lowercase UUID") from exc
            if self.dataset_id != canonical_dataset_id:
                raise ValueError("dataset_id must be a canonical lowercase UUID")
        if self.dataset_schema_version is not None and self.dataset_schema_version != 1:
            raise ValueError("Only dataset_schema_version=1 is supported")
        if not self.run_name.strip() or len(self.run_name) > 128:
            raise ValueError("run_name must contain 1 to 128 characters")
        if not self.out_dir.strip():
            raise ValueError("out_dir must not be empty")
        if not self.database_url.strip():
            raise ValueError("database_url must not be empty")
        if not self.engine_semantics_version.strip() or len(self.engine_semantics_version) > 32:
            raise ValueError("engine_semantics_version must contain 1 to 32 characters")
        if self.max_persisted_equity_points < 2:
            raise ValueError("max_persisted_equity_points must be >= 2")

    def ensure_outdir(self) -> Path:
        p = Path(self.out_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def to_dict(self, *, redact_secrets: bool = True) -> dict[str, object]:
        payload = dataclasses.asdict(self)
        if redact_secrets:
            payload["database_url"] = redact_database_url(self.database_url)
        return payload

    def fingerprint(self) -> str:
        """Stable hash of behavior-affecting configuration, excluding secrets and paths."""
        payload = self.to_dict(redact_secrets=True)
        payload.pop("run_name", None)
        payload.pop("out_dir", None)
        payload.pop("database_url", None)
        payload.pop("csv_path", None)
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def enforce_compute_limits(
    config: BacktestConfig,
    *,
    max_symbols: int,
    max_events: int,
    max_unsorted_events: int,
    max_equity_points: int,
    require_strict_schema: bool = False,
) -> None:
    """Apply deployment-controlled limits to an otherwise valid run configuration."""
    if min(max_symbols, max_events, max_unsorted_events, max_equity_points) <= 0:
        raise ValueError("Compute limits must be positive")
    if len(config.symbols) > max_symbols:
        raise ValueError(f"symbols exceeds the configured limit of {max_symbols}")
    if config.data.max_events > max_events:
        raise ValueError(f"data.max_events exceeds the configured limit of {max_events}")
    if not config.data.require_sorted and config.data.max_events > max_unsorted_events:
        raise ValueError(
            f"Unsorted data exceeds the configured in-memory event limit of {max_unsorted_events}"
        )
    if config.max_persisted_equity_points > max_equity_points:
        raise ValueError(
            f"max_persisted_equity_points exceeds the configured limit of {max_equity_points}"
        )
    if config.long_window > config.data.max_events:
        raise ValueError("long_window must not exceed data.max_events")
    if config.trade_quantity > config.risk.max_position_per_symbol:
        raise ValueError("trade_quantity must not exceed risk.max_position_per_symbol")
    if require_strict_schema and not config.data.strict_schema:
        raise ValueError("Strict dataset schemas are required by this deployment")


def redact_database_url(url: str) -> str:
    """Remove credentials without ever logging the original URL."""
    try:
        parsed = urlsplit(url)
        if not parsed.scheme:
            return "<redacted-database-url>"
        hostname = parsed.hostname or ""
        if ":" in hostname:
            hostname = f"[{hostname}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        username = parsed.username or ""
        password = ":***" if parsed.password is not None else ""
        credentials = f"{username}{password}@" if parsed.username is not None else ""
        netloc = f"{credentials}{hostname}{port}"
        query = urlencode(
            [
                (
                    key,
                    "***"
                    if any(marker in key.lower() for marker in ("password", "secret", "token"))
                    else value,
                )
                for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            ]
        )
        return urlunsplit((parsed.scheme, netloc, parsed.path, query, parsed.fragment))
    except ValueError:
        return "<redacted-database-url>"


def database_url_from_env(default: str = "sqlite:///runs/runs.db") -> str:
    explicit = os.getenv("DATABASE_URL")
    if explicit:
        return explicit
    host = os.getenv("QBT_DB_HOST")
    if not host:
        return default
    username = os.getenv("QBT_DB_USERNAME")
    password = os.getenv("QBT_DB_PASSWORD")
    database = os.getenv("QBT_DB_NAME")
    if not all((username, password, database)):
        raise ValueError("QBT_DB_HOST requires QBT_DB_USERNAME, QBT_DB_PASSWORD, and QBT_DB_NAME")
    port = int(os.getenv("QBT_DB_PORT", "5432"))
    url = (
        f"postgresql+psycopg://{quote(username or '', safe='')}:{quote(password or '', safe='')}"
        f"@{host}:{port}/{quote(database or '', safe='')}"
    )
    sslmode = os.getenv("QBT_DB_SSLMODE")
    return f"{url}?sslmode={quote(sslmode, safe='')}" if sslmode else url


def backtest_config_from_mapping(
    value: Mapping[str, object],
    *,
    database_url: str | None = None,
    out_dir: str | None = None,
) -> BacktestConfig:
    """Strict parser shared by API and worker boundaries."""

    allowed = {field.name for field in dataclasses.fields(BacktestConfig)}
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"Unknown backtest config fields: {sorted(unknown)}")

    raw_symbols = value.get("symbols")
    if isinstance(raw_symbols, str):
        symbols = tuple(item.strip().upper() for item in raw_symbols.split(",") if item.strip())
    elif isinstance(raw_symbols, (list, tuple)):
        symbols = tuple(str(item).strip().upper() for item in raw_symbols if str(item).strip())
    else:
        raise ValueError("symbols must be a string or list")

    execution_mapping = _nested_mapping(value, "execution")
    micro_mapping = _nested_mapping(execution_mapping, "micro")
    risk_mapping = _nested_mapping(value, "risk")
    data_mapping = _nested_mapping(value, "data")
    metrics_mapping = _nested_mapping(value, "metrics")
    end_of_run_mapping = _nested_mapping(value, "end_of_run")
    _reject_unknown(
        execution_mapping,
        {
            "default_spread_bps",
            "impact_bps_per_unit",
            "impact_volume",
            "rng_seed",
            "micro",
        },
        "execution",
    )
    _reject_unknown(
        micro_mapping,
        {
            "latency_events",
            "default_tick_volume",
            "max_participation_rate",
            "queue_ahead_fraction",
            "base_fill_probability",
        },
        "execution.micro",
    )
    _reject_unknown(
        risk_mapping,
        {
            "max_position_per_symbol",
            "stop_loss_pct",
            "max_drawdown_pct",
            "max_gross_exposure",
            "max_net_exposure",
            "max_order_notional",
            "max_leverage",
            "max_daily_loss_pct",
            "allow_short",
        },
        "risk",
    )
    _reject_unknown(
        data_mapping,
        {
            "source_timezone",
            "duplicate_policy",
            "chunksize",
            "require_sorted",
            "max_events",
            "strict_schema",
            "require_all_symbols",
        },
        "data",
    )
    _reject_unknown(
        metrics_mapping,
        {"annual_risk_free_rate", "periods_per_year"},
        "metrics",
    )
    _reject_unknown(
        end_of_run_mapping,
        {"pending_order_policy", "position_policy"},
        "end_of_run",
    )

    micro = MicrostructureConfig(
        latency_events=_mapping_int(micro_mapping, "latency_events", 1),
        default_tick_volume=_mapping_float(micro_mapping, "default_tick_volume", 5_000.0),
        max_participation_rate=_mapping_float(
            micro_mapping,
            "max_participation_rate",
            0.2,
        ),
        queue_ahead_fraction=_mapping_float(micro_mapping, "queue_ahead_fraction", 0.7),
        base_fill_probability=_mapping_float(micro_mapping, "base_fill_probability", 0.8),
    )
    execution = ExecutionConfig(
        default_spread_bps=_mapping_float(execution_mapping, "default_spread_bps", 5.0),
        impact_bps_per_unit=_mapping_float(execution_mapping, "impact_bps_per_unit", 2.0),
        impact_volume=_mapping_float(execution_mapping, "impact_volume", 10_000.0),
        rng_seed=_mapping_int(execution_mapping, "rng_seed", 7),
        micro=micro,
    )
    raw_max_gross = risk_mapping.get("max_gross_exposure")
    raw_max_net = risk_mapping.get("max_net_exposure")
    raw_max_order = risk_mapping.get("max_order_notional")
    raw_daily_loss = risk_mapping.get("max_daily_loss_pct")
    risk = RiskConfig(
        max_position_per_symbol=_mapping_int(
            risk_mapping,
            "max_position_per_symbol",
            1_000,
        ),
        stop_loss_pct=_mapping_float(risk_mapping, "stop_loss_pct", 0.05),
        max_drawdown_pct=_mapping_float(risk_mapping, "max_drawdown_pct", 0.20),
        max_gross_exposure=(
            None if raw_max_gross is None else _object_float(raw_max_gross, "max_gross_exposure")
        ),
        max_net_exposure=(
            None if raw_max_net is None else _object_float(raw_max_net, "max_net_exposure")
        ),
        max_order_notional=(
            None if raw_max_order is None else _object_float(raw_max_order, "max_order_notional")
        ),
        max_leverage=_mapping_float(risk_mapping, "max_leverage", 1.0),
        max_daily_loss_pct=(
            None if raw_daily_loss is None else _object_float(raw_daily_loss, "max_daily_loss_pct")
        ),
        allow_short=_mapping_bool(risk_mapping, "allow_short", True),
    )
    data = DataConfig(
        source_timezone=str(data_mapping.get("source_timezone", "UTC")),
        duplicate_policy=cast(
            DuplicatePolicy,
            str(data_mapping.get("duplicate_policy", "error")),
        ),
        chunksize=_mapping_int(data_mapping, "chunksize", 50_000),
        require_sorted=_mapping_bool(data_mapping, "require_sorted", True),
        max_events=_mapping_int(data_mapping, "max_events", 2_000_000),
        strict_schema=_mapping_bool(data_mapping, "strict_schema", True),
        require_all_symbols=_mapping_bool(data_mapping, "require_all_symbols", True),
    )
    raw_periods = metrics_mapping.get("periods_per_year")
    metrics = MetricsConfig(
        annual_risk_free_rate=_mapping_float(
            metrics_mapping,
            "annual_risk_free_rate",
            0.0,
        ),
        periods_per_year=(
            None if raw_periods is None else _object_float(raw_periods, "periods_per_year")
        ),
    )
    end_of_run = EndOfRunConfig(
        pending_order_policy=cast(
            PendingOrderPolicy,
            str(end_of_run_mapping.get("pending_order_policy", "cancel")),
        ),
        position_policy=cast(
            PositionEndPolicy,
            str(end_of_run_mapping.get("position_policy", "mark_to_market")),
        ),
    )
    return BacktestConfig(
        symbols=symbols,
        initial_cash=_mapping_float(value, "initial_cash", 100_000.0),
        trade_quantity=_mapping_int(value, "trade_quantity", 100),
        commission_per_trade=_mapping_float(value, "commission_per_trade", 1.0),
        short_window=_mapping_int(value, "short_window", 20),
        long_window=_mapping_int(value, "long_window", 50),
        csv_path=str(value.get("csv_path", "data/sample_prices.csv")),
        dataset_id=None if value.get("dataset_id") is None else str(value["dataset_id"]),
        dataset_sha256=(
            None if value.get("dataset_sha256") is None else str(value["dataset_sha256"])
        ),
        dataset_schema_version=(
            None
            if value.get("dataset_schema_version") is None
            else _mapping_int(value, "dataset_schema_version", 1)
        ),
        run_name=str(value.get("run_name", "run")),
        out_dir=out_dir or str(value.get("out_dir", "runs")),
        database_url=database_url or str(value.get("database_url", "sqlite:///runs/runs.db")),
        engine_semantics_version=str(value.get("engine_semantics_version", "3.0")),
        max_persisted_equity_points=_mapping_int(
            value,
            "max_persisted_equity_points",
            100_000,
        ),
        execution=execution,
        risk=risk,
        data=data,
        metrics=metrics,
        end_of_run=end_of_run,
    )


def _nested_mapping(
    value: Mapping[str, object],
    key: str,
) -> Mapping[str, object]:
    nested = value.get(key, {})
    if not isinstance(nested, Mapping):
        raise ValueError(f"{key} must be a mapping")
    return cast(Mapping[str, object], nested)


def _object_float(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be numeric")
    if isinstance(value, (int, float, str)):
        try:
            converted = float(value)
        except ValueError as exc:
            raise ValueError(f"{name} must be numeric") from exc
        if math.isfinite(converted):
            return converted
    raise ValueError(f"{name} must be numeric")


def _mapping_float(
    value: Mapping[str, object],
    key: str,
    default: float,
) -> float:
    return _object_float(value.get(key, default), key)


def _mapping_int(
    value: Mapping[str, object],
    key: str,
    default: int,
) -> int:
    raw = value.get(key, default)
    if isinstance(raw, bool):
        raise ValueError(f"{key} must be an integer")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        if math.isfinite(raw) and raw.is_integer():
            return int(raw)
        raise ValueError(f"{key} must be an integer")
    if isinstance(raw, str):
        try:
            return int(raw)
        except ValueError as exc:
            raise ValueError(f"{key} must be an integer") from exc
    raise ValueError(f"{key} must be an integer")


def _mapping_bool(
    value: Mapping[str, object],
    key: str,
    default: bool,
) -> bool:
    raw = value.get(key, default)
    if isinstance(raw, bool):
        return raw
    raise ValueError(f"{key} must be a boolean")


def _reject_unknown(
    value: Mapping[str, object],
    allowed: set[str],
    path: str,
) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"Unknown {path} fields: {sorted(unknown)}")


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
