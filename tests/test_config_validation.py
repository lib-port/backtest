from __future__ import annotations

import pytest

from quant_backtester.config import (
    BacktestConfig,
    ExecutionConfig,
    MicrostructureConfig,
    RiskConfig,
)


def test_config_rejects_invalid_ranges() -> None:
    with pytest.raises(ValueError):
        MicrostructureConfig(max_participation_rate=1.5)
    with pytest.raises(ValueError):
        ExecutionConfig(impact_volume=0.0)
    with pytest.raises(ValueError):
        RiskConfig(stop_loss_pct=1.2)


def test_backtest_config_requires_valid_windows_and_symbols() -> None:
    with pytest.raises(ValueError):
        BacktestConfig(symbols=(), short_window=5, long_window=10)
    with pytest.raises(ValueError):
        BacktestConfig(symbols=("AAPL",), short_window=10, long_window=10)
