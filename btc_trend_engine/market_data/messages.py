"""Normalized market events (Trend_Engine.md §5.1, §6.2).

Every event retains the exchange timestamp, the local receive timestamp and —
where the venue supplies one — the sequence number.  Prices and sizes are
``Decimal`` end to end: the venue mixes three level encodings and scientific
notation (assumption A2), and float round-tripping money is how books drift.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

SCHEMA_VERSION = "1.0.0"


class EventType(enum.StrEnum):
    TRADE = "trade"
    L2_SNAPSHOT = "l2_snapshot"
    L2_UPDATE = "l2_update"
    TICKER = "ticker"
    MARK_PRICE = "mark_price"
    FUNDING = "funding"
    CANDLE = "candle"


class NormalizationError(ValueError):
    """The raw payload is malformed; fail closed rather than guess."""


def parse_decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise NormalizationError(f"{name} must be numeric, got {value!r}")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise NormalizationError(f"{name} is not a valid decimal: {value!r}") from exc
    if not result.is_finite():
        raise NormalizationError(f"{name} must be finite, got {value!r}")
    return result


def parse_exchange_timestamp(value: Any, name: str = "timestamp") -> datetime:
    """Delta timestamps are integer microseconds since the epoch (A1)."""
    if isinstance(value, bool) or value is None:
        raise NormalizationError(f"{name} missing")
    try:
        micros = int(value)
    except (TypeError, ValueError) as exc:
        raise NormalizationError(f"{name} is not an integer: {value!r}") from exc
    # Sanity band: 2015-01-01 .. 2100-01-01 in microseconds.
    if not 1_420_070_400_000_000 <= micros <= 4_102_444_800_000_000:
        raise NormalizationError(f"{name} out of plausible range: {micros}")
    return datetime.fromtimestamp(micros / 1_000_000, tz=timezone.utc)


@dataclass(frozen=True, slots=True)
class Level:
    price: Decimal
    size: Decimal


@dataclass(frozen=True, slots=True)
class MarketEvent:
    event_type: EventType
    symbol: str
    exchange_timestamp: datetime
    receive_timestamp: datetime
    sequence_no: int | None
    payload: dict[str, Any]
    schema_version: str = SCHEMA_VERSION

    def to_record(self) -> dict[str, Any]:
        """JSON-serialisable form for the append-only event store."""
        return {
            "v": self.schema_version,
            "type": self.event_type.value,
            "symbol": self.symbol,
            "exchange_ts_us": int(self.exchange_timestamp.timestamp() * 1_000_000),
            "receive_ts_us": int(self.receive_timestamp.timestamp() * 1_000_000),
            "seq": self.sequence_no,
            "payload": self.payload,
        }


@dataclass(frozen=True, slots=True)
class Trade:
    symbol: str
    price: Decimal
    size: Decimal
    buyer_is_taker: bool
    exchange_timestamp: datetime


@dataclass(frozen=True, slots=True)
class BookDelta:
    """One l2_updates message, snapshot or incremental."""

    symbol: str
    action: str  # "snapshot" | "update"
    sequence_no: int
    checksum: int | None
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    exchange_timestamp: datetime


@dataclass(frozen=True, slots=True)
class Candle:
    symbol: str
    resolution: str
    start: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    closed: bool = True
    trade_count: int = 0

    def merged_with_trade(self, price: Decimal, size: Decimal) -> "Candle":
        return Candle(
            symbol=self.symbol,
            resolution=self.resolution,
            start=self.start,
            open=self.open,
            high=max(self.high, price),
            low=min(self.low, price),
            close=price,
            volume=self.volume + size,
            closed=False,
            trade_count=self.trade_count + 1,
        )
