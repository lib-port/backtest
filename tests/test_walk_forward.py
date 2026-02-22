from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from quant_backtester.config import BacktestConfig
from quant_backtester.sweep import run_walk_forward


def test_walk_forward_exports_summary_and_persists_runs(tmp_path: Path) -> None:
    out_csv = tmp_path / "walk_forward.csv"
    db_path = tmp_path / "walk_forward.db"
    cfg = BacktestConfig(
        symbols=("AAPL", "MSFT"),
        csv_path="data/sample_prices.csv",
        database_url=f"sqlite:///{db_path}",
        out_dir=str(tmp_path),
        run_name="wf-test",
        short_window=5,
        long_window=10,
    )

    df, summary = run_walk_forward(
        cfg,
        short_windows=[5, 10],
        long_windows=[20, 30],
        train_days=30,
        test_days=15,
        step_days=15,
        export_csv=str(out_csv),
    )

    assert not df.empty
    assert out_csv.exists()
    assert "best_short_window" in df.columns
    assert "train_total_return" in df.columns
    assert "oos_total_return" in df.columns
    assert summary["window_count"] == len(df)
    assert 0.0 <= float(summary["stability_score"]) <= 1.0

    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("SELECT COUNT(*) FROM runs")
    run_count = int(cur.fetchone()[0])
    con.close()
    assert run_count == len(df)


def test_walk_forward_requires_valid_param_pairs(tmp_path: Path) -> None:
    cfg = BacktestConfig(
        symbols=("AAPL",),
        csv_path="data/sample_prices.csv",
        database_url=f"sqlite:///{tmp_path / 'runs.db'}",
        out_dir=str(tmp_path),
        run_name="wf-invalid",
        short_window=5,
        long_window=10,
    )
    with pytest.raises(ValueError, match="No valid parameter pairs"):
        run_walk_forward(
            cfg,
            short_windows=[20],
            long_windows=[10],
            train_days=10,
            test_days=5,
            step_days=5,
            export_csv=str(tmp_path / "walk_forward.csv"),
        )
