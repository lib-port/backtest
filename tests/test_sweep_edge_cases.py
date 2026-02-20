from __future__ import annotations

from pathlib import Path

from quant_backtester.config import BacktestConfig
from quant_backtester.sweep import run_parameter_sweep


def test_sweep_returns_empty_dataframe_when_no_valid_pairs(tmp_path: Path) -> None:
    out_csv = tmp_path / "sweep.csv"
    cfg = BacktestConfig(
        symbols=("AAPL",),
        csv_path="data/sample_prices.csv",
        database_url=f"sqlite:///{tmp_path / 'runs.db'}",
        out_dir=str(tmp_path),
        run_name="sweep-empty",
        short_window=5,
        long_window=10,
    )

    df = run_parameter_sweep(
        cfg, short_windows=[20, 30], long_windows=[10], export_csv=str(out_csv)
    )

    assert df.empty
    assert "total_return" in df.columns
    assert out_csv.exists()
