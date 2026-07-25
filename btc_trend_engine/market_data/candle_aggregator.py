"""Multi-timeframe candle aggregation from normalized trades (§7).

One base 1-minute series is built from trades; higher resolutions are rolled
up from it deterministically.  Only **closed** candles are ever emitted —
decisions use closed candles (§7 rule 6), and the same aggregation code will
drive the Phase 5 backtester (§28: production and backtest share calculation
code).

REST-bootstrapped history (already closed by definition) is seeded via
:meth:`seed_closed`, then live trades continue the series.  A gap between the
bootstrap and the first live trade is detected and reported rather than
papered over.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

from .messages import Candle, Trade
from .normalizer import resolution_seconds

CandleListener = Callable[[Candle], None]


def bucket_start(timestamp: datetime, resolution: str) -> datetime:
    seconds = resolution_seconds(resolution)
    epoch = int(timestamp.astimezone(timezone.utc).timestamp())
    return datetime.fromtimestamp(epoch - epoch % seconds, tz=timezone.utc)


class CandleSeries:
    """One symbol × one resolution. Emits a candle only when it closes."""

    def __init__(self, symbol: str, resolution: str,
                 on_close: CandleListener | None = None,
                 max_closed: int = 5000) -> None:
        self.symbol = symbol
        self.resolution = resolution
        self._seconds = resolution_seconds(resolution)
        self._on_close = on_close
        self._max_closed = max_closed
        self._closed: list[Candle] = []
        self._open: Candle | None = None
        self.gap_detected = False

    # ── input ────────────────────────────────────────────────────────────
    def seed_closed(self, candles: Iterable[Candle]) -> None:
        """Seed already-closed history (REST bootstrap), oldest first."""
        for candle in candles:
            if candle.resolution != self.resolution or candle.symbol != self.symbol:
                raise ValueError("seed candle does not match series identity")
            if not candle.closed:
                raise ValueError("seed_closed only accepts closed candles")
            if self._closed and candle.start <= self._closed[-1].start:
                continue  # idempotent overlap from a re-bootstrap
            if (self._closed
                    and candle.start != self._closed[-1].start
                    + timedelta(seconds=self._seconds)):
                self.gap_detected = True
            self._closed.append(candle)
        self._trim()

    def add_trade(self, trade: Trade) -> None:
        start = bucket_start(trade.exchange_timestamp, self.resolution)
        if self._open is None:
            self._begin(start, trade)
            return
        if start == self._open.start:
            self._open = self._open.merged_with_trade(trade.price, trade.size)
            return
        if start < self._open.start:
            return  # late trade for an already-rolled bucket; never rewrite
        self._close_open()
        self._begin(start, trade)

    def roll_clock(self, now: datetime) -> None:
        """Close the open candle when its bucket has fully elapsed, even if no
        new trade arrives — a quiet market must still produce closed candles."""
        if self._open is None:
            return
        if now >= self._open.start + timedelta(seconds=self._seconds):
            self._close_open()

    # ── internals ────────────────────────────────────────────────────────
    def _begin(self, start: datetime, trade: Trade) -> None:
        self._open = Candle(
            symbol=self.symbol, resolution=self.resolution, start=start,
            open=trade.price, high=trade.price, low=trade.price,
            close=trade.price, volume=trade.size, closed=False, trade_count=1,
        )

    def _close_open(self) -> None:
        assert self._open is not None
        closed = Candle(
            symbol=self._open.symbol, resolution=self._open.resolution,
            start=self._open.start, open=self._open.open, high=self._open.high,
            low=self._open.low, close=self._open.close,
            volume=self._open.volume, closed=True,
            trade_count=self._open.trade_count,
        )
        if self._closed and closed.start <= self._closed[-1].start:
            self._open = None
            return  # duplicate close after re-bootstrap; keep history append-only
        if (self._closed
                and closed.start != self._closed[-1].start
                + timedelta(seconds=self._seconds)):
            self.gap_detected = True
        self._closed.append(closed)
        self._trim()
        self._open = None
        if self._on_close is not None:
            self._on_close(closed)

    def _trim(self) -> None:
        if len(self._closed) > self._max_closed:
            del self._closed[: len(self._closed) - self._max_closed]

    # ── views ────────────────────────────────────────────────────────────
    def closed_candles(self) -> list[Candle]:
        return list(self._closed)

    def last_closed(self) -> Candle | None:
        return self._closed[-1] if self._closed else None


class CandleAggregator:
    """All configured resolutions for one symbol, fed from one trade stream."""

    def __init__(self, symbol: str, resolutions: Iterable[str],
                 on_close: Callable[[Candle], None] | None = None) -> None:
        self.symbol = symbol
        self.series: dict[str, CandleSeries] = {
            resolution: CandleSeries(symbol, resolution, on_close=on_close)
            for resolution in resolutions
        }

    def add_trade(self, trade: Trade) -> None:
        for series in self.series.values():
            series.add_trade(trade)

    def roll_clock(self, now: datetime) -> None:
        for series in self.series.values():
            series.roll_clock(now)

    def seed_closed(self, resolution: str, candles: Iterable[Candle]) -> None:
        self.series[resolution].seed_closed(candles)
