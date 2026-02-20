from __future__ import annotations

import math
from typing import cast

import numpy as np
import numpy.typing as npt


def returns_from_equity(equity: list[float]) -> npt.NDArray[np.float64]:
    if len(equity) < 2:
        return np.array([], dtype=np.float64)
    eq = np.array(equity, dtype=float)
    return cast(npt.NDArray[np.float64], (np.diff(eq) / eq[:-1]).astype(np.float64))


def sharpe_ratio(daily_returns: npt.NDArray[np.float64], trading_days: int = 252) -> float:
    if daily_returns.size == 0:
        return 0.0
    mu = float(np.mean(daily_returns))
    sigma = float(np.std(daily_returns, ddof=1)) if daily_returns.size > 1 else 0.0
    if sigma == 0.0:
        return 0.0
    return (mu / sigma) * math.sqrt(trading_days)


def max_drawdown(equity: list[float]) -> float:
    if not equity:
        return 0.0
    peak = equity[0]
    mdd = 0.0
    for x in equity:
        peak = max(peak, x)
        dd = (peak - x) / peak if peak != 0 else 0.0
        mdd = max(mdd, dd)
    return mdd
