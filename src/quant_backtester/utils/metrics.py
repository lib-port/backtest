from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import cast

import numpy as np
import numpy.typing as npt

from quant_backtester.events import EquityPoint

SECONDS_PER_YEAR = 365.2425 * 24 * 60 * 60


@dataclass(frozen=True)
class PerformanceMetrics:
    total_return: float
    sharpe: float
    max_drawdown: float
    periods_per_year: float
    observation_count: int


def returns_from_equity(equity: Sequence[float]) -> npt.NDArray[np.float64]:
    if len(equity) < 2:
        return np.array([], dtype=np.float64)
    values = np.array(equity, dtype=float)
    if not np.all(np.isfinite(values)):
        raise ValueError("equity must contain only finite values")
    if np.any(values[:-1] == 0):
        raise ValueError("cannot calculate returns after zero equity")
    return cast(
        npt.NDArray[np.float64],
        (np.diff(values) / values[:-1]).astype(np.float64),
    )


def infer_periods_per_year(timestamps: Sequence[datetime], fallback: float = 252.0) -> float:
    if len(timestamps) < 2:
        return fallback
    elapsed = (timestamps[-1] - timestamps[0]).total_seconds()
    if elapsed <= 0:
        raise ValueError("equity timestamps must be strictly increasing")
    return (len(timestamps) - 1) * SECONDS_PER_YEAR / elapsed


def sharpe_ratio(
    returns: npt.NDArray[np.float64],
    trading_days: float = 252.0,
    *,
    annual_risk_free_rate: float = 0.0,
) -> float:
    if returns.size == 0:
        return 0.0
    if not math.isfinite(trading_days) or trading_days <= 0:
        raise ValueError("trading_days must be finite and > 0")
    if not math.isfinite(annual_risk_free_rate) or annual_risk_free_rate <= -1:
        raise ValueError("annual_risk_free_rate must be finite and > -1")
    per_period_risk_free = (1.0 + annual_risk_free_rate) ** (1.0 / trading_days) - 1.0
    excess = returns - per_period_risk_free
    mean = float(np.mean(excess))
    sigma = float(np.std(excess, ddof=1)) if excess.size > 1 else 0.0
    if sigma == 0.0:
        return 0.0
    return (mean / sigma) * math.sqrt(trading_days)


def max_drawdown(equity: Sequence[float]) -> float:
    if not equity:
        return 0.0
    peak = equity[0]
    drawdown = 0.0
    for value in equity:
        peak = max(peak, value)
        current = (peak - value) / peak if peak != 0 else 0.0
        drawdown = max(drawdown, current)
    return drawdown


def summarize_performance(
    points: Sequence[EquityPoint],
    *,
    initial_equity: float,
    configured_periods_per_year: float | None = None,
    annual_risk_free_rate: float = 0.0,
) -> PerformanceMetrics:
    equity = [point.equity for point in points]
    timestamps = [point.timestamp for point in points]
    periods = configured_periods_per_year or infer_periods_per_year(timestamps)
    returns = returns_from_equity(equity)
    final_equity = equity[-1] if equity else initial_equity
    return PerformanceMetrics(
        total_return=(final_equity / initial_equity) - 1.0,
        sharpe=sharpe_ratio(
            returns,
            periods,
            annual_risk_free_rate=annual_risk_free_rate,
        ),
        max_drawdown=max_drawdown(equity),
        periods_per_year=periods,
        observation_count=len(points),
    )
