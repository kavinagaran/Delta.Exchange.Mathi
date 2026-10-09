"""Normalization: the three observed level encodings, Decimal discipline,
microsecond timestamps, and fail-closed rejection of malformed payloads."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from btc_trend_engine.market_data.messages import (
    EventType,
    NormalizationError,
    parse_decimal,
    parse_exchange_timestamp,
)
from btc_trend_engine.market_data.normalizer import (
    decimal_str,
    normalize_ws_message,
    parse_level,
    rest_book_levels,
    rest_candle,
    to_book_delta,
    to_trades,
)

RECV = datetime(2026, 7, 25, 10, 0, tzinfo=timezone.utc)
TS_US = 1_784_968_499_014_486  # observed live timestamp (microseconds)


def test_positional_ws_level_parses_to_decimal():
    level = parse_level(["63935.0", "2487"], "bids[0]")
    assert level.price == Decimal("63935.0")
    assert level.size == Decimal("2487")


def test_rest_dict_level_parses_including_scientific_notation_depth():
    # REST book rows look like {"size": 1688, "depth": "3.38E+3", "price": "192.0"}
    level = parse_level({"size": 1688, "depth": "3.38E+3", "price": "192.0"}, "buy[0]")
    assert level.price == Decimal("192.0")
    assert level.size == Decimal("1688")


def test_scientific_notation_survives_decimal_round_trip():
    assert parse_decimal("3.38E+3", "depth") == Decimal("3380")
    assert decimal_str(Decimal("3.38E+3")) == "3380"


def test_unknown_level_shape_fails_closed():
    with pytest.raises(NormalizationError):
        parse_level("63935.0:2487", "bids[0]")


def test_exchange_timestamp_is_microseconds_utc():
    ts = parse_exchange_timestamp(TS_US)
    assert ts.tzinfo == timezone.utc
    assert ts.year == 2026


@pytest.mark.parametrize("bad", [None, "not-a-number", True, 1_000])
def test_implausible_timestamps_are_rejected(bad):
    with pytest.raises(NormalizationError):
        parse_exchange_timestamp(bad)


def test_l2_snapshot_normalizes_with_sequence_and_checksum():
    raw = {"type": "l2_updates", "action": "snapshot", "symbol": "BTCUSD",
           "sequence_no": 7766170, "cs": 2918930439, "timestamp": TS_US,
           "bids": [["63935.0", "2487"]], "asks": [["63936.0", "100"]]}
    event = normalize_ws_message(raw, RECV)
    assert event is not None and event.event_type is EventType.L2_UPDATE
    delta = to_book_delta(event)
    assert delta.action == "snapshot"
    assert delta.sequence_no == 7766170
    assert delta.checksum == 2918930439
    assert delta.bids[0].price == Decimal("63935.0")


def test_l2_unknown_action_fails_closed():
    raw = {"type": "l2_updates", "action": "resync", "symbol": "BTCUSD",
           "sequence_no": 1, "timestamp": TS_US, "bids": [], "asks": []}
    event = normalize_ws_message(raw, RECV)
    with pytest.raises(NormalizationError):
        to_book_delta(event)


def test_l2_missing_sequence_fails_closed():
    raw = {"type": "l2_updates", "action": "update", "symbol": "BTCUSD",
           "timestamp": TS_US, "bids": [], "asks": []}
    event = normalize_ws_message(raw, RECV)
    with pytest.raises(NormalizationError):
        to_book_delta(event)


def test_mark_symbol_prefix_is_stripped():
    raw = {"type": "mark_price", "symbol": "MARK:BTCUSD",
           "timestamp": TS_US, "price": "63941.6"}
    event = normalize_ws_message(raw, RECV)
    assert event is not None and event.symbol == "BTCUSD"


def test_non_market_messages_are_ignored_not_errors():
    assert normalize_ws_message({"type": "subscriptions"}, RECV) is None


def test_trade_requires_explicit_buyer_role():
    raw = {"type": "all_trades", "symbol": "BTCUSD", "timestamp": TS_US,
           "price": "63940", "size": "10"}
    event = normalize_ws_message(raw, RECV)
    with pytest.raises(NormalizationError):
        to_trades(event)


def test_trade_and_snapshot_forms_both_normalize():
    single = normalize_ws_message(
        {"type": "all_trades", "symbol": "BTCUSD", "timestamp": TS_US,
         "price": "63940", "size": "10", "buyer_role": "taker"}, RECV)
    trades = to_trades(single)
    assert len(trades) == 1 and trades[0].buyer_is_taker

    snapshot = normalize_ws_message(
        {"type": "all_trades_snapshot", "symbol": "BTCUSD", "timestamp": TS_US,
         "trades": [
             {"price": "63940", "size": "10", "buyer_role": "maker",
              "timestamp": TS_US},
             {"price": "63941", "size": "5", "buyer_role": "taker",
              "timestamp": TS_US + 1000},
         ]}, RECV)
    both = to_trades(snapshot)
    assert [t.buyer_is_taker for t in both] == [False, True]


def test_trades_snapshot_has_no_envelope_timestamp_and_uses_newest_trade():
    """Observed live: the backfill frame carries only symbol/trades/type.
    Rejecting it would drop 50 real trades at every subscribe and reconnect."""
    raw = {"type": "all_trades_snapshot", "symbol": "BTCUSD", "trades": [
        {"buyer_role": "maker", "price": "63895.0", "seller_role": "taker",
         "size": 1, "timestamp": TS_US},
        {"buyer_role": "taker", "price": 63896, "seller_role": "maker",
         "size": 1, "timestamp": TS_US - 82_000_000},
    ]}
    event = normalize_ws_message(raw, RECV)
    assert event is not None
    assert event.exchange_timestamp == parse_exchange_timestamp(TS_US)
    trades = to_trades(event)
    # price arrives as both string and bare int; both must reach Decimal.
    assert [t.price for t in trades] == [Decimal("63895.0"), Decimal("63896")]
    assert [t.buyer_is_taker for t in trades] == [False, True]


def test_trades_snapshot_without_usable_timestamps_still_fails_closed():
    for trades in ([], [{"buyer_role": "taker", "price": "1", "size": 1}]):
        with pytest.raises(NormalizationError):
            normalize_ws_message(
                {"type": "all_trades_snapshot", "symbol": "BTCUSD",
                 "trades": trades}, RECV)


def test_rest_candle_epoch_seconds_and_decimal_fields():
    candle = rest_candle(
        {"time": 1784968500, "open": "63900", "high": "64000.5",
         "low": "63850", "close": "63950", "volume": "123.45"},
        "BTCUSD", "5m")
    assert candle.start == datetime.fromtimestamp(1784968500, tz=timezone.utc)
    assert candle.high == Decimal("64000.5")
    assert candle.closed


def test_rest_book_levels_parse_both_sides():
    bids, asks = rest_book_levels({
        "buy": [{"price": "192.0", "size": 1688, "depth": "1688"}],
        "sell": [{"price": "195.0", "size": 1345, "depth": "1345"}],
    })
    assert bids[0].price == Decimal("192.0")
    assert asks[0].size == Decimal("1345")
