from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from quant_backtester.config import DataConfig
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


def test_csv_rejects_invalid_optional_values(tmp_path: Path) -> None:
    path = tmp_path / "invalid_optional.csv"
    pd.DataFrame(
        [{"date": "2020-01-01", "symbol": "AAPL", "mid": 100.0, "volume": "unknown"}]
    ).to_csv(path, index=False)
    with pytest.raises(ValueError, match="Invalid volume"):
        list(CSVDataHandler(str(path)).stream())


def test_csv_strict_schema_rejects_unexpected_columns(tmp_path: Path) -> None:
    path = tmp_path / "unexpected.csv"
    pd.DataFrame(
        [{"date": "2020-01-01", "symbol": "AAPL", "mid": 100.0, "secret": "ignored"}]
    ).to_csv(path, index=False)
    with pytest.raises(ValueError, match="unexpected columns"):
        list(CSVDataHandler(str(path)).stream())

    events = list(
        CSVDataHandler(
            str(path),
            DataConfig(strict_schema=False),
        ).stream()
    )
    assert len(events) == 1


def test_csv_rejects_empty_and_bounds_unsorted_buffer(tmp_path: Path) -> None:
    empty = tmp_path / "empty.csv"
    empty.write_text("date,symbol,mid\n", encoding="utf-8")
    with pytest.raises(ValueError, match="at least one data row"):
        list(CSVDataHandler(str(empty)).stream())

    unsorted = tmp_path / "too_many_unsorted.csv"
    pd.DataFrame(
        [
            {"date": "2020-01-02", "symbol": "AAPL", "mid": 101.0},
            {"date": "2020-01-01", "symbol": "AAPL", "mid": 100.0},
        ]
    ).to_csv(unsorted, index=False)
    with pytest.raises(ValueError, match="max_events=1"):
        list(
            CSVDataHandler(
                str(unsorted),
                DataConfig(require_sorted=False, max_events=1, chunksize=1),
            ).stream()
        )
