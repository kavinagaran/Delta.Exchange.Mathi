"""Raw Delta India payloads → normalized events.

Handles the three observed level encodings (assumption A2):
- WS l2_updates snapshot/update: positional ``["63935.0", "2487"]``
- REST l2orderbook: ``{"price": "192.0", "size": 1688, "depth": "3.38E+3"}``

Anything malformed raises :class:`NormalizationError`; the caller records the
failure and fails closed.  Nothing here estimates or repairs data (§3.2).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from .messages import (
    BookDelta,
    Candle,
    EventType,
    Level,
    MarketEvent,
    NormalizationError,
    Trade,
    parse_decimal,
    parse_exchange_timestamp,
)

# WS message type → normalized event type.  Only types listed here are
# persisted; everything else (subscriptions acks, heartbeats) is ignored.
_WS_TYPE_MAP: dict[str, EventType] = {
    "all_trades": EventType.TRADE,
    "all_trades_snapshot": EventType.TRADE,
    "l2_updates": EventType.L2_UPDATE,
    "v2/ticker": EventType.TICKER,
    "mark_price": EventType.MARK_PRICE,
    "funding_rate": EventType.FUNDING,
    "candlestick_5m": EventType.CANDLE,
}


def parse_level(raw: Any, name: str) -> Level:
    """One price level in any of the venue's encodings."""
    if isinstance(raw, (list, tuple)):
        if len(raw) < 2:
            raise NormalizationError(f"{name}: positional level too short: {raw!r}")
        return Level(price=parse_decimal(raw[0], f"{name}.price"),
                     size=parse_decimal(raw[1], f"{name}.size"))
    if isinstance(raw, dict):
        price = raw.get("limit_price", raw.get("price"))
        return Level(price=parse_decimal(price, f"{name}.price"),
                     size=parse_decimal(raw.get("size"), f"{name}.size"))
    raise NormalizationError(f"{name}: unrecognised level shape: {type(raw).__name__}")


def _parse_levels(raw: Any, name: str) -> tuple[Level, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise NormalizationError(f"{name} must be a list")
    return tuple(parse_level(item, f"{name}[{i}]") for i, item in enumerate(raw))


def _newest_trade_timestamp(raw: dict[str, Any]) -> int:
    rows = raw.get("trades")
    if not isinstance(rows, list) or not rows:
        raise NormalizationError("all_trades_snapshot: empty or missing trades")
    stamps: list[int] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise NormalizationError(f"all_trades_snapshot: trade[{index}] is not an object")
        value = row.get("timestamp")
        if value is None:
            raise NormalizationError(f"all_trades_snapshot: trade[{index}] has no timestamp")
        stamps.append(int(value))
    return max(stamps)


def normalize_ws_message(
    raw: dict[str, Any], receive_timestamp: datetime
) -> MarketEvent | None:
    """One WS payload → MarketEvent, or None for non-market messages."""
    message_type = raw.get("type")
    event_type = _WS_TYPE_MAP.get(str(message_type))
    if event_type is None:
        return None
    symbol = str(raw.get("symbol") or "")
    if not symbol:
        raise NormalizationError(f"{message_type}: missing symbol")
    if symbol.startswith("MARK:"):
        symbol = symbol[len("MARK:"):]

    timestamp_field = raw.get("timestamp")
    if timestamp_field is None and event_type is EventType.CANDLE:
        timestamp_field = raw.get("last_updated")
    if timestamp_field is None and message_type == "all_trades_snapshot":
        # The backfill frame carries no envelope timestamp of its own; the
        # newest trade it contains is the moment it describes. Rejecting the
        # frame would silently discard the venue's trade backfill at every
        # subscribe, including after each reconnect.
        timestamp_field = _newest_trade_timestamp(raw)
    exchange_ts = parse_exchange_timestamp(timestamp_field)

    sequence = raw.get("sequence_no")
    sequence_no = int(sequence) if sequence is not None else None

    return MarketEvent(
        event_type=event_type,
        symbol=symbol,
        exchange_timestamp=exchange_ts,
        receive_timestamp=receive_timestamp,
        sequence_no=sequence_no,
        payload=raw,
    )


def to_book_delta(event: MarketEvent) -> BookDelta:
    raw = event.payload
    action = str(raw.get("action") or "")
    if action not in ("snapshot", "update"):
        raise NormalizationError(f"l2_updates: unknown action {action!r}")
    if event.sequence_no is None:
        raise NormalizationError("l2_updates: missing sequence_no")
    checksum_raw = raw.get("cs")
    return BookDelta(
        symbol=event.symbol,
        action=action,
        sequence_no=event.sequence_no,
        checksum=int(checksum_raw) if checksum_raw is not None else None,
        bids=_parse_levels(raw.get("bids"), "bids"),
        asks=_parse_levels(raw.get("asks"), "asks"),
        exchange_timestamp=event.exchange_timestamp,
    )


def to_trades(event: MarketEvent) -> list[Trade]:
    """all_trades carries one trade; all_trades_snapshot carries a list."""
    raw = event.payload
    if raw.get("type") == "all_trades_snapshot":
        rows = raw.get("trades")
        if not isinstance(rows, list):
            raise NormalizationError("all_trades_snapshot: missing trades list")
    else:
        rows = [raw]
    trades: list[Trade] = []
    for i, row in enumerate(rows):
        name = f"trade[{i}]"
        buyer_role = str(row.get("buyer_role") or "")
        if buyer_role not in ("taker", "maker"):
            raise NormalizationError(f"{name}: unknown buyer_role {buyer_role!r}")
        trades.append(Trade(
            symbol=event.symbol,
            price=parse_decimal(row.get("price"), f"{name}.price"),
            size=parse_decimal(row.get("size"), f"{name}.size"),
            buyer_is_taker=buyer_role == "taker",
            exchange_timestamp=parse_exchange_timestamp(
                row.get("timestamp"), f"{name}.timestamp"),
        ))
    return trades


_RESOLUTION_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400}


def resolution_seconds(resolution: str) -> int:
    try:
        return _RESOLUTION_SECONDS[resolution]
    except KeyError as exc:
        raise NormalizationError(f"unsupported resolution {resolution!r}") from exc


def rest_candle(row: dict[str, Any], symbol: str, resolution: str) -> Candle:
    """/v2/history/candles row → closed Candle. ``time`` is epoch seconds."""
    start_raw = row.get("time")
    if start_raw is None:
        raise NormalizationError("candle: missing time")
    start = parse_exchange_timestamp(int(start_raw) * 1_000_000, "candle.time")
    return Candle(
        symbol=symbol,
        resolution=resolution,
        start=start,
        open=parse_decimal(row.get("open"), "candle.open"),
        high=parse_decimal(row.get("high"), "candle.high"),
        low=parse_decimal(row.get("low"), "candle.low"),
        close=parse_decimal(row.get("close"), "candle.close"),
        volume=parse_decimal(row.get("volume") if row.get("volume") is not None else 0,
                             "candle.volume"),
        closed=True,
    )


def rest_book_levels(result: dict[str, Any]) -> tuple[tuple[Level, ...], tuple[Level, ...]]:
    """REST /v2/l2orderbook result → (bids, asks). ``buy``/``sell`` keys."""
    return (_parse_levels(result.get("buy"), "buy"),
            _parse_levels(result.get("sell"), "sell"))


def decimal_str(value: Decimal) -> str:
    """Canonical string form for persistence (no exponent surprises)."""
    return format(value.normalize(), "f")
