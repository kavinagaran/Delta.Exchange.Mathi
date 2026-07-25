"""Order-book state machine: snapshot/update application, sequence-gap
invalidation, rebuild counting, checksum flag semantics (§6.3, A2b)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from btc_trend_engine.market_data.messages import BookDelta, Level
from btc_trend_engine.market_data.orderbook import (
    ApplyResult,
    BookState,
    OrderBook,
)

TS = datetime(2026, 7, 25, 10, 0, tzinfo=timezone.utc)


def _delta(action: str, seq: int, bids=(), asks=(), checksum=None) -> BookDelta:
    return BookDelta(
        symbol="BTCUSD", action=action, sequence_no=seq, checksum=checksum,
        bids=tuple(Level(Decimal(p), Decimal(s)) for p, s in bids),
        asks=tuple(Level(Decimal(p), Decimal(s)) for p, s in asks),
        exchange_timestamp=TS,
    )


def _snapshot(seq=100):
    return _delta("snapshot", seq,
                  bids=[("63935", "10"), ("63934", "20")],
                  asks=[("63936", "5"), ("63937", "8")])


def test_updates_before_snapshot_are_rejected():
    book = OrderBook("BTCUSD")
    assert book.state is BookState.AWAITING_SNAPSHOT
    assert book.apply(_delta("update", 5)) is ApplyResult.REJECTED_NOT_VALID
    assert not book.is_valid


def test_snapshot_then_contiguous_updates_apply():
    book = OrderBook("BTCUSD")
    assert book.apply(_snapshot(100)) is ApplyResult.SNAPSHOT_APPLIED
    assert book.is_valid
    assert book.apply(_delta("update", 101, bids=[("63935", "12")])) is ApplyResult.APPLIED
    assert book.best_bid() == Level(Decimal("63935"), Decimal("12"))
    assert book.best_ask() == Level(Decimal("63936"), Decimal("5"))
    assert book.sequence_no == 101


def test_zero_size_removes_a_level():
    book = OrderBook("BTCUSD")
    book.apply(_snapshot(100))
    book.apply(_delta("update", 101, bids=[("63935", "0")]))
    assert book.best_bid() == Level(Decimal("63934"), Decimal("20"))


def test_sequence_gap_invalidates_and_counts():
    book = OrderBook("BTCUSD")
    book.apply(_snapshot(100))
    result = book.apply(_delta("update", 103))  # expected 101
    assert result is ApplyResult.REJECTED_SEQUENCE_GAP
    assert book.state is BookState.INVALID
    assert book.gap_count == 1
    # Views fail closed while invalid.
    assert book.best_bid() is None
    assert book.top() is None
    assert book.depth(5) == ((), ())


def test_updates_after_a_gap_stay_rejected_until_fresh_snapshot():
    book = OrderBook("BTCUSD")
    book.apply(_snapshot(100))
    book.apply(_delta("update", 103))
    assert book.apply(_delta("update", 104)) is ApplyResult.REJECTED_NOT_VALID
    assert book.apply(_snapshot(200)) is ApplyResult.SNAPSHOT_APPLIED
    assert book.is_valid
    assert book.rebuild_count == 2


def test_duplicate_sequence_is_a_gap_not_a_silent_reapply():
    book = OrderBook("BTCUSD")
    book.apply(_snapshot(100))
    book.apply(_delta("update", 101))
    assert book.apply(_delta("update", 101)) is ApplyResult.REJECTED_SEQUENCE_GAP


def test_wrong_symbol_raises():
    book = OrderBook("BTCUSD")
    with pytest.raises(ValueError):
        book.apply(BookDelta(symbol="ETHUSD", action="snapshot", sequence_no=1,
                             checksum=None, bids=(), asks=(),
                             exchange_timestamp=TS))


def test_checksum_flag_without_verifier_is_a_configuration_error():
    # A2b: enabling validation without a confirmed algorithm must fail loudly
    # at construction, not silently skip validation at runtime.
    with pytest.raises(ValueError, match="A2b"):
        OrderBook("BTCUSD", validate_checksums=True)


def test_checksum_mismatch_invalidates_when_verifier_supplied():
    book = OrderBook("BTCUSD", validate_checksums=True,
                     checksum_fn=lambda b: 42)
    assert book.apply(_delta("snapshot", 100, bids=[("1", "1")],
                             checksum=42)) is ApplyResult.SNAPSHOT_APPLIED
    result = book.apply(_delta("update", 101, checksum=999))
    assert result is ApplyResult.REJECTED_CHECKSUM
    assert book.state is BookState.INVALID


def test_depth_view_orders_bids_desc_asks_asc():
    book = OrderBook("BTCUSD")
    book.apply(_snapshot(100))
    bids, asks = book.depth(2)
    assert [lv.price for lv in bids] == [Decimal("63935"), Decimal("63934")]
    assert [lv.price for lv in asks] == [Decimal("63936"), Decimal("63937")]


def test_disconnect_invalidate_clears_book():
    book = OrderBook("BTCUSD")
    book.apply(_snapshot(100))
    book.invalidate("ws disconnect")
    assert book.state is BookState.INVALID
    assert book.level_counts() == (0, 0)
