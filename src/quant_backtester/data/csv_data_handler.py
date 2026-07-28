from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from numbers import Real
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

import pandas as pd

from quant_backtester.config import DataConfig
from quant_backtester.events import MarketEvent


@dataclass(frozen=True)
class InMemoryDataHandler:
    """Immutable event view used by tests, sweeps, and walk-forward evaluation."""

    events: tuple[MarketEvent, ...]

    def __init__(self, events: Iterable[MarketEvent]) -> None:
        normalized = tuple(events)
        previous: datetime | None = None
        seen: set[tuple[datetime, str]] = set()
        for event in normalized:
            if previous is not None and event.timestamp < previous:
                raise ValueError("Market events must be ordered by timestamp")
            key = (event.timestamp, event.symbol)
            if key in seen:
                raise ValueError(f"Duplicate market event for {event.symbol} at {event.timestamp}")
            seen.add(key)
            previous = event.timestamp
        object.__setattr__(self, "events", normalized)

    def stream(self) -> Iterator[MarketEvent]:
        yield from self.events


@dataclass(frozen=True)
class CSVDataHandler:
    """Validated, bounded-memory CSV market-data reader.

    Rows must be sorted by timestamp by default. Naive source timestamps are
    interpreted in ``source_timezone`` and all emitted events are UTC.
    """

    csv_path: str
    config: DataConfig = DataConfig()

    @property
    def dataset_hash(self) -> str:
        digest = hashlib.sha256()
        with Path(self.csv_path).open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        digest.update(repr(self.config).encode("utf-8"))
        return digest.hexdigest()

    @staticmethod
    def _to_optional_float(value: object) -> float | None:
        if value is None or pd.isna(value):
            return None
        if isinstance(value, (Real, str)):
            try:
                return float(value)
            except ValueError:
                return None
        return None

    @staticmethod
    def _to_required_float(value: object, *, name: str, row_num: int) -> float:
        if not isinstance(value, (Real, str)):
            raise ValueError(f"Invalid {name} at row {row_num}: {value!r}")
        try:
            converted = float(value)
        except ValueError as exc:
            raise ValueError(f"Invalid {name} at row {row_num}: {value!r}") from exc
        if not math.isfinite(converted):
            raise ValueError(f"Invalid {name} at row {row_num}: must be finite, got {converted!r}")
        return converted

    def _timestamp(self, value: object, *, row_num: int) -> datetime:
        try:
            timestamp = pd.Timestamp(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid date at row {row_num}: {value!r}") from exc
        if pd.isna(timestamp):
            raise ValueError(f"Invalid date at row {row_num}: {value!r}")
        try:
            if timestamp.tzinfo is None:
                timestamp = timestamp.tz_localize(ZoneInfo(self.config.source_timezone))
            timestamp = timestamp.tz_convert(UTC)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid date at row {row_num}: {value!r}") from exc
        return cast(datetime, timestamp.to_pydatetime())

    def _event_from_row(
        self,
        row: Mapping[str, object],
        *,
        row_num: int,
    ) -> MarketEvent:
        timestamp = self._timestamp(row["date"], row_num=row_num)
        symbol = str(row["symbol"]).strip().upper()
        if not symbol or symbol == "NAN":
            raise ValueError(f"Invalid symbol at row {row_num}: {row['symbol']!r}")

        mid = self._to_required_float(row["mid"], name="mid", row_num=row_num)
        if mid <= 0:
            raise ValueError(f"Invalid mid at row {row_num}: must be > 0, got {mid}")

        bid = self._to_optional_float(row.get("bid"))
        ask = self._to_optional_float(row.get("ask"))
        spread_bps = self._to_optional_float(row.get("spread_bps"))
        volume = self._to_optional_float(row.get("volume"))

        if bid is not None and (not math.isfinite(bid) or bid <= 0):
            raise ValueError(f"Invalid bid at row {row_num}: must be finite and > 0, got {bid}")
        if ask is not None and (not math.isfinite(ask) or ask <= 0):
            raise ValueError(f"Invalid ask at row {row_num}: must be finite and > 0, got {ask}")
        if bid is not None and ask is not None and ask < bid:
            raise ValueError(f"Invalid quote at row {row_num}: ask ({ask}) must be >= bid ({bid})")
        if spread_bps is not None and (not math.isfinite(spread_bps) or spread_bps < 0):
            raise ValueError(
                f"Invalid spread_bps at row {row_num}: must be finite and >= 0, got {spread_bps}"
            )
        if volume is not None and (not math.isfinite(volume) or volume < 0):
            raise ValueError(
                f"Invalid volume at row {row_num}: must be finite and >= 0, got {volume}"
            )

        return MarketEvent(
            timestamp=timestamp,
            symbol=symbol,
            mid=mid,
            bid=bid,
            ask=ask,
            spread_bps=spread_bps,
            volume=volume,
        )

    def _chunks(self) -> Iterator[pd.DataFrame]:
        if self.config.require_sorted:
            yield from pd.read_csv(self.csv_path, chunksize=self.config.chunksize)
            return
        frame = pd.read_csv(self.csv_path)
        if "date" not in frame.columns:
            yield frame
            return
        parsed = pd.to_datetime(frame["date"], errors="coerce", utc=True)
        frame = frame.assign(_parsed_date=parsed).sort_values("_parsed_date", kind="stable")
        yield frame.drop(columns=["_parsed_date"])

    def stream(self) -> Iterator[MarketEvent]:
        previous_timestamp: datetime | None = None
        current_timestamp: datetime | None = None
        timestamp_bucket: dict[str, MarketEvent] = {}
        row_num = 0
        columns_checked = False

        for frame in self._chunks():
            if not columns_checked:
                required = {"date", "symbol", "mid"}
                missing = required - set(frame.columns)
                if missing:
                    raise ValueError(f"CSV missing columns: {sorted(missing)}")
                columns_checked = True

            for raw_row in frame.to_dict(orient="records"):
                row_num += 1
                if row_num > self.config.max_events:
                    raise ValueError(f"CSV exceeds configured max_events={self.config.max_events}")
                event = self._event_from_row(raw_row, row_num=row_num)
                if (
                    self.config.require_sorted
                    and previous_timestamp is not None
                    and event.timestamp < previous_timestamp
                ):
                    raise ValueError(
                        "CSV must be sorted by date; "
                        f"row {row_num} has {event.timestamp.isoformat()} after "
                        f"{previous_timestamp.isoformat()}"
                    )

                if current_timestamp is None:
                    current_timestamp = event.timestamp
                if event.timestamp != current_timestamp:
                    yield from timestamp_bucket.values()
                    timestamp_bucket.clear()
                    current_timestamp = event.timestamp

                if event.symbol in timestamp_bucket and self.config.duplicate_policy == "error":
                    raise ValueError(
                        f"Duplicate market event for {event.symbol} at "
                        f"{event.timestamp.isoformat()} (row {row_num})"
                    )
                timestamp_bucket[event.symbol] = event
                previous_timestamp = event.timestamp

        if not columns_checked:
            # pandas raises EmptyDataError for a zero-byte file before this point,
            # but a header-only file is a valid empty dataset.
            return
        yield from timestamp_bucket.values()
