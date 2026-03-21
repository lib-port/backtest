import json
import math
import sqlite3
from pathlib import Path

from quant_backtester.backtest import run_backtest
from quant_backtester.config import BacktestConfig


def test_smoke_run_sqlite(tmp_path: Path) -> None:
    db_path = tmp_path / "smoke_runs.db"
    cfg = BacktestConfig(
        symbols=("AAPL", "MSFT"),
        csv_path="data/sample_prices.csv",
        database_url=f"sqlite:///{db_path}",
        out_dir=str(tmp_path / "runs"),
        run_name="test",
        short_window=5,
        long_window=10,
    )
    res = run_backtest(cfg, persist=True)
    assert float(res["final_equity"]) > 0
    assert not math.isnan(float(res["total_return"]))
    assert not math.isnan(float(res["sharpe"]))
    assert not math.isnan(float(res["max_drawdown"]))
    assert 0.0 <= float(res["max_drawdown"]) <= 1.0

    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("SELECT COUNT(*) FROM runs")
    count = int(cur.fetchone()[0])
    cur.execute("SELECT symbols FROM runs ORDER BY id DESC LIMIT 1")
    symbols_raw = str(cur.fetchone()[0])
    con.close()
    assert count >= 1
    assert json.loads(symbols_raw) == ["AAPL", "MSFT"]
