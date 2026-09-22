"""L2 order-book reconstruction with sequence-continuity validation (§6.3).

State machine:

    AWAITING_SNAPSHOT ──snapshot──▶ VALID ──update(seq=last+1)──▶ VALID
            ▲                        │
            └────── invalidate ◀─────┘  (gap, checksum mismatch, bad payload)

While not VALID, order-flow features are unavailable and the owner must
resubscribe to obtain a fresh snapshot.  Sequence continuity and Delta's
documented top-10 CRC32 checksum are both verified whenever validation is
enabled.  A missing checksum is an integrity failure, never a silent bypass.
"""

from __future__ import annotations

import enum
import zlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Callable

from .messages import BookDelta, Level


class BookState(enum.StrEnum):
    AWAITING_SNAPSHOT = "awaiting_snapshot"
    VALID = "valid"
    INVALID = "invalid"


class ApplyResult(enum.StrEnum):
    APPLIED = "applied"
    SNAPSHOT_APPLIED = "snapshot_applied"
    REJECTED_SEQUENCE_GAP = "rejected_sequence_gap"
    REJECTED_CHECKSUM = "rejected_checksum"
    REJECTED_NOT_VALID = "rejected_not_valid"


ChecksumFn = Callable[["OrderBook"], int]


def delta_checksum(book: "OrderBook") -> int:
    """Delta ``ob_updates`` checksum over the top ten levels on each side.

    Delta documents ``asks|bids`` with price/size pairs joined as
    ``price:size``. Decimal's fixed-point rendering preserves meaningful
    trailing zeroes while avoiding an exponent representation in the checksum
    material. CRC32 is converted to an unsigned 32-bit integer because Python
    may otherwise expose a signed result on some platforms.
    """
    bids, asks = book.depth(10)

    def render(levels: tuple[Level, ...]) -> str:
        return ",".join(
            f"{format(level.price, 'f')}:{format(level.size, 'f')}"
            for level in levels
        )

    material = f"{render(asks)}|{render(bids)}"
    return zlib.crc32(material.encode("utf-8")) & 0xFFFFFFFF


@dataclass(frozen=True, slots=True)
class BookTop:
    bid: Level | None
    ask: Level | None
    sequence_no: int
    exchange_timestamp: datetime


class OrderBook:
    """Single-symbol book. Not thread-safe; owned by one asyncio task."""

    def __init__(
        self,
        symbol: str,
        *,
        validate_checksums: bool = False,
        checksum_fn: ChecksumFn | None = None,
    ) -> None:
        self.symbol = symbol
        self._validate_checksums = validate_checksums
        self._checksum_fn = checksum_fn or delta_checksum
        self._bids: dict[Decimal, Decimal] = {}
        self._asks: dict[Decimal, Decimal] = {}
        self._state = BookState.AWAITING_SNAPSHOT
        self._sequence_no = -1
        self._last_exchange_ts: datetime | None = None
        self.rebuild_count = 0
        self.gap_count = 0

    # ── state ────────────────────────────────────────────────────────────
    @property
    def state(self) -> BookState:
        return self._state

    @property
    def is_valid(self) -> bool:
        return self._state is BookState.VALID

    @property
    def sequence_no(self) -> int:
        return self._sequence_no

    def invalidate(self, reason: str = "") -> None:
        self._state = BookState.INVALID
        self._bids.clear()
        self._asks.clear()

    # ── application ──────────────────────────────────────────────────────
    def apply(self, delta: BookDelta) -> ApplyResult:
        if delta.symbol != self.symbol:
            raise ValueError(f"delta for {delta.symbol} applied to {self.symbol} book")
        if delta.action == "snapshot":
            return self._apply_snapshot(delta)
        return self._apply_update(delta)

    def _apply_snapshot(self, delta: BookDelta) -> ApplyResult:
        self._bids = {lv.price: lv.size for lv in delta.bids if lv.size > 0}
        self._asks = {lv.price: lv.size for lv in delta.asks if lv.size > 0}
        self._sequence_no = delta.sequence_no
        self._last_exchange_ts = delta.exchange_timestamp
        self._state = BookState.VALID
        self.rebuild_count += 1
        if not self._checksum_ok(delta):
            self.invalidate("checksum mismatch on snapshot")
            return ApplyResult.REJECTED_CHECKSUM
        return ApplyResult.SNAPSHOT_APPLIED

    def _apply_update(self, delta: BookDelta) -> ApplyResult:
        if self._state is not BookState.VALID:
            return ApplyResult.REJECTED_NOT_VALID
        if delta.sequence_no != self._sequence_no + 1:
            self.gap_count += 1
            self.invalidate(
                f"sequence gap: expected {self._sequence_no + 1}, got {delta.sequence_no}")
            return ApplyResult.REJECTED_SEQUENCE_GAP
        for side, levels in ((self._bids, delta.bids), (self._asks, delta.asks)):
            for level in levels:
                if level.size == 0:
                    side.pop(level.price, None)
                else:
                    side[level.price] = level.size
        self._sequence_no = delta.sequence_no
        self._last_exchange_ts = delta.exchange_timestamp
        if not self._checksum_ok(delta):
            self.invalidate("checksum mismatch on update")
            return ApplyResult.REJECTED_CHECKSUM
        return ApplyResult.APPLIED

    def _checksum_ok(self, delta: BookDelta) -> bool:
        if not self._validate_checksums:
            return True
        if delta.checksum is None:
            return False
        assert self._checksum_fn is not None
        return self._checksum_fn(self) == delta.checksum

    # ── views ────────────────────────────────────────────────────────────
    def best_bid(self) -> Level | None:
        if not self.is_valid or not self._bids:
            return None
        price = max(self._bids)
        return Level(price=price, size=self._bids[price])

    def best_ask(self) -> Level | None:
        if not self.is_valid or not self._asks:
            return None
        price = min(self._asks)
        return Level(price=price, size=self._asks[price])

    def top(self) -> BookTop | None:
        if not self.is_valid or self._last_exchange_ts is None:
            return None
        return BookTop(
            bid=self.best_bid(),
            ask=self.best_ask(),
            sequence_no=self._sequence_no,
            exchange_timestamp=self._last_exchange_ts,
        )

    def depth(self, levels: int) -> tuple[tuple[Level, ...], tuple[Level, ...]]:
        """Top-N (bids desc, asks asc). Empty tuples while not VALID."""
        if not self.is_valid:
            return (), ()
        bids = tuple(Level(p, self._bids[p])
                     for p in sorted(self._bids, reverse=True)[:levels])
        asks = tuple(Level(p, self._asks[p])
                     for p in sorted(self._asks)[:levels])
        return bids, asks

    def level_counts(self) -> tuple[int, int]:
        return len(self._bids), len(self._asks)
