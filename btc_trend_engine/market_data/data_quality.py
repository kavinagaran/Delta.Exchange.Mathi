"""Feed staleness and clock-drift monitoring (§6.5).

Per-feed dynamic thresholds:

    warning_threshold = stale_multiplier × rolling median inter-message interval
    hard_threshold    = max(warning_threshold, stale_floor_seconds)

Verdicts are computed on demand from monotonic arrival times, so a wall-clock
jump cannot fake freshness.  Clock drift compares the exchange timestamp with
local receive time; sustained drift beyond the configured bound marks the feed
set DEGRADED (§3.2: block entries on clock drift).
"""

from __future__ import annotations

import enum
import statistics
from collections import deque
from dataclasses import dataclass

from ..clock import Clock
from ..config import DataQualityConfig


class Freshness(enum.StrEnum):
    NEVER = "never"        # no message seen yet
    FRESH = "fresh"
    WARNING = "warning"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class FeedVerdict:
    feed: str
    freshness: Freshness
    age_seconds: float | None
    warning_threshold: float
    hard_threshold: float


class FeedMonitor:
    def __init__(self, feed: str, config: DataQualityConfig, clock: Clock) -> None:
        self.feed = feed
        self._config = config
        self._clock = clock
        self._intervals: deque[float] = deque(maxlen=config.median_window)
        self._last_arrival: float | None = None

    def record(self) -> None:
        now = self._clock.monotonic()
        if self._last_arrival is not None:
            self._intervals.append(max(now - self._last_arrival, 1e-9))
        self._last_arrival = now

    def thresholds(self) -> tuple[float, float]:
        if len(self._intervals) >= 4:
            median = statistics.median(self._intervals)
        else:
            # Too few samples to estimate cadence; fall back to the floor so a
            # brand-new feed is neither instantly stale nor trusted forever.
            median = self._config.stale_floor_seconds / self._config.stale_multiplier
        warning = self._config.stale_multiplier * median
        hard = max(warning, self._config.stale_floor_seconds)
        return warning, hard

    def verdict(self) -> FeedVerdict:
        warning, hard = self.thresholds()
        if self._last_arrival is None:
            return FeedVerdict(self.feed, Freshness.NEVER, None, warning, hard)
        age = self._clock.monotonic() - self._last_arrival
        if age >= hard:
            freshness = Freshness.STALE
        elif age >= warning:
            freshness = Freshness.WARNING
        else:
            freshness = Freshness.FRESH
        return FeedVerdict(self.feed, freshness, age, warning, hard)


class ClockDriftMonitor:
    """Median of recent (receive − exchange) offsets, in milliseconds.

    The median of a window resists a single delayed packet; sustained skew —
    real clock drift or a systematically lagging feed — pushes the median past
    the bound.  Either way the timestamps cannot be trusted for decisions.
    """

    def __init__(self, config: DataQualityConfig, window: int = 64) -> None:
        self._max_drift_ms = config.max_clock_drift_ms
        self._offsets_ms: deque[float] = deque(maxlen=window)

    def record(self, exchange_ts_epoch: float, receive_ts_epoch: float) -> None:
        self._offsets_ms.append((receive_ts_epoch - exchange_ts_epoch) * 1000.0)

    def drift_ms(self) -> float | None:
        if len(self._offsets_ms) < 8:
            return None
        return statistics.median(self._offsets_ms)

    def in_sync(self) -> bool:
        """False only on *evidence* of drift; unknown is handled by feed
        freshness (a feed with <8 samples is NEVER/STALE there)."""
        drift = self.drift_ms()
        return drift is None or abs(drift) <= self._max_drift_ms


class DataQualityMonitor:
    """Aggregate verdict across feeds + clock + book validity."""

    def __init__(self, config: DataQualityConfig, clock: Clock,
                 feeds: list[str]) -> None:
        self._config = config
        self._clock = clock
        self.feeds: dict[str, FeedMonitor] = {
            feed: FeedMonitor(feed, config, clock) for feed in feeds
        }
        self.clock_drift = ClockDriftMonitor(config)

    def record(self, feed: str, exchange_ts_epoch: float | None = None) -> None:
        monitor = self.feeds.get(feed)
        if monitor is None:
            return
        monitor.record()
        if exchange_ts_epoch is not None:
            self.clock_drift.record(exchange_ts_epoch,
                                    self._clock.now().timestamp())

    def verdicts(self) -> dict[str, FeedVerdict]:
        return {feed: monitor.verdict() for feed, monitor in self.feeds.items()}

    def data_quality(self, *, book_valid: bool) -> str:
        """Overall label matching docs/trend-snapshot-contract.md."""
        if not self.clock_drift.in_sync():
            return "CLOCK_DRIFT"
        verdicts = self.verdicts()
        trade_like = [v for f, v in verdicts.items() if f in ("trades", "ticker")]
        if any(v.freshness in (Freshness.STALE, Freshness.NEVER) for v in trade_like):
            return "STALE_L1"
        l2 = verdicts.get("l2")
        if l2 is not None and l2.freshness in (Freshness.STALE, Freshness.NEVER):
            return "STALE_L2"
        if not book_valid:
            return "BOOK_INVALID"
        return "OK"
