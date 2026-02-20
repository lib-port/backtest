from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from numbers import Real

import pandas as pd

from quant_backtester.events import MarketEvent


@dataclass(frozen=True)
class CSVDataHandler:
    """
    CSV columns:
      - date, symbol, mid
      - optional: bid, ask
      - optional: spread_bps (used if bid/ask absent)
      - optional: volume (available per-tick volume for execution simulation)

    Emits MarketEvent in chronological order.
    """

    csv_path: str

    @staticmethod
    def _to_optional_float(value: object) -> float | None:
        if value is None or pd.isna(value):
            return None
        if isinstance(value, (Real, str)):
            return float(value)
        return None

    @staticmethod
    def _to_required_float(value: object, *, name: str, row_num: int) -> float:
        if not isinstance(value, (Real, str)):
            raise ValueError(f"Invalid {name} at row {row_num}: {value!r}")
        try:
            val = float(value)
        except ValueError as exc:
            raise ValueError(f"Invalid {name} at row {row_num}: {value!r}") from exc
        if not math.isfinite(val):
            raise ValueError(f"Invalid {name} at row {row_num}: must be finite, got {val!r}")
        return val

    def stream(self) -> Iterator[MarketEvent]:
        df = pd.read_csv(self.csv_path)
        required = {"date", "symbol", "mid"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"CSV missing columns: {sorted(missing)}")

        raw_dates = df["date"].copy()
        df["date"] = pd.to_datetime(df["date"], utc=False, errors="coerce")
        invalid_dates = df["date"].isna()
        if invalid_dates.any():
            first_bad_idx = int(df.index[invalid_dates][0]) + 1
            bad_value = raw_dates.iloc[first_bad_idx - 1]
            raise ValueError(f"Invalid date at row {first_bad_idx}: {bad_value!r}")
        df = df.sort_values("date")

        has_bid = "bid" in df.columns
        has_ask = "ask" in df.columns
        has_spread_bps = "spread_bps" in df.columns
        has_volume = "volume" in df.columns

        idx = {name: i for i, name in enumerate(df.columns)}
        i_date = idx["date"]
        i_symbol = idx["symbol"]
        i_mid = idx["mid"]
        i_bid = idx["bid"] if has_bid else -1
        i_ask = idx["ask"] if has_ask else -1
        i_spread_bps = idx["spread_bps"] if has_spread_bps else -1
        i_volume = idx["volume"] if has_volume else -1

        for row_num, row in enumerate(df.itertuples(index=False, name=None), start=1):
            ts_raw = row[i_date]
            ts: datetime = ts_raw.to_pydatetime()
            bid_raw = row[i_bid] if has_bid else None
            ask_raw = row[i_ask] if has_ask else None
            spread_bps_raw = row[i_spread_bps] if has_spread_bps else None
            vol_raw = row[i_volume] if has_volume else None

            mid = self._to_required_float(row[i_mid], name="mid", row_num=row_num)
            if mid <= 0:
                raise ValueError(f"Invalid mid at row {row_num}: must be > 0, got {mid}")

            bid = self._to_optional_float(bid_raw)
            ask = self._to_optional_float(ask_raw)
            spread_bps = self._to_optional_float(spread_bps_raw)
            vol = self._to_optional_float(vol_raw)

            if bid is not None and not math.isfinite(bid):
                raise ValueError(f"Invalid bid at row {row_num}: must be finite, got {bid}")
            if ask is not None and not math.isfinite(ask):
                raise ValueError(f"Invalid ask at row {row_num}: must be finite, got {ask}")
            if bid is not None and bid <= 0:
                raise ValueError(f"Invalid bid at row {row_num}: must be > 0, got {bid}")
            if ask is not None and ask <= 0:
                raise ValueError(f"Invalid ask at row {row_num}: must be > 0, got {ask}")
            if bid is not None and ask is not None and ask < bid:
                raise ValueError(
                    f"Invalid quote at row {row_num}: ask ({ask}) must be >= bid ({bid})"
                )
            if spread_bps is not None and not math.isfinite(spread_bps):
                raise ValueError(
                    f"Invalid spread_bps at row {row_num}: must be finite, got {spread_bps}"
                )
            if spread_bps is not None and spread_bps < 0:
                raise ValueError(
                    f"Invalid spread_bps at row {row_num}: must be >= 0, got {spread_bps}"
                )
            if vol is not None and not math.isfinite(vol):
                raise ValueError(f"Invalid volume at row {row_num}: must be finite, got {vol}")
            if vol is not None and vol < 0:
                raise ValueError(f"Invalid volume at row {row_num}: must be >= 0, got {vol}")
            yield MarketEvent(
                timestamp=ts,
                symbol=str(row[i_symbol]),
                mid=mid,
                bid=bid,
                ask=ask,
                spread_bps=spread_bps,
                volume=vol,
            )
