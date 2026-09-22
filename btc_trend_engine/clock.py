"""Injectable clock (Trend_Engine.md §28: dependency injection for time).

Every module takes a ``Clock`` so tests control time exactly and no production
calculation can accidentally mix wall-clock reads mid-computation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Current time as an aware UTC datetime."""
        ...

    def monotonic(self) -> float:
        """Monotonic seconds for interval measurement."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        import time

        return time.monotonic()


class FixedClock:
    """Test clock advanced explicitly."""

    def __init__(self, start: datetime, monotonic_start: float = 0.0) -> None:
        if start.tzinfo is None:
            raise ValueError("FixedClock requires an aware datetime")
        self._now = start.astimezone(timezone.utc)
        self._mono = monotonic_start

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        from datetime import timedelta

        self._now += timedelta(seconds=seconds)
        self._mono += seconds
