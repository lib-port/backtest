from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from quant_backtester.data.csv_data_handler import CSVDataHandler


def test_csv_missing_required_columns(tmp_path: Path) -> None:
    p = tmp_path / "bad.csv"
    pd.DataFrame({"date": ["2020-01-01"], "symbol": ["AAPL"]}).to_csv(p, index=False)
    with pytest.raises(ValueError, match="CSV missing columns"):
        list(CSVDataHandler(str(p)).stream())


def test_csv_optional_columns_with_nulls(tmp_path: Path) -> None:
    p = tmp_path / "ok.csv"
    pd.DataFrame(
        [
            {"date": "2020-01-01", "symbol": "AAPL", "mid": 100.0, "bid": None, "ask": None},
            {"date": "2020-01-02", "symbol": "AAPL", "mid": 101.0, "spread_bps": None},
        ]
    ).to_csv(p, index=False)
    events = list(CSVDataHandler(str(p)).stream())
    assert len(events) == 2
    assert events[0].bid is None
    assert events[0].ask is None


def test_csv_rejects_non_positive_mid(tmp_path: Path) -> None:
    p = tmp_path / "bad_mid.csv"
    pd.DataFrame([{"date": "2020-01-01", "symbol": "AAPL", "mid": 0.0}]).to_csv(p, index=False)
    with pytest.raises(ValueError, match="Invalid mid"):
        list(CSVDataHandler(str(p)).stream())


def test_csv_rejects_negative_volume(tmp_path: Path) -> None:
    p = tmp_path / "bad_volume.csv"
    pd.DataFrame([{"date": "2020-01-01", "symbol": "AAPL", "mid": 100.0, "volume": -1.0}]).to_csv(
        p, index=False
    )
    with pytest.raises(ValueError, match="Invalid volume"):
        list(CSVDataHandler(str(p)).stream())


def test_csv_rejects_crossed_quotes(tmp_path: Path) -> None:
    p = tmp_path / "bad_quote.csv"
    pd.DataFrame(
        [{"date": "2020-01-01", "symbol": "AAPL", "mid": 100.0, "bid": 101.0, "ask": 100.0}]
    ).to_csv(p, index=False)
    with pytest.raises(ValueError, match="ask .* >= bid"):
        list(CSVDataHandler(str(p)).stream())


def test_csv_rejects_invalid_date(tmp_path: Path) -> None:
    p = tmp_path / "bad_date.csv"
    pd.DataFrame([{"date": "not-a-date", "symbol": "AAPL", "mid": 100.0}]).to_csv(p, index=False)
    with pytest.raises(ValueError, match="Invalid date"):
        list(CSVDataHandler(str(p)).stream())
