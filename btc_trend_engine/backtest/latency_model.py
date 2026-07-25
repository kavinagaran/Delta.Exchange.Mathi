"""Decision-to-arrival latency (§15, plan Phase 5).

A signal is computed at a candle close; the resulting order reaches the venue
some milliseconds later, and the market has moved in between. Ignoring that
gap is one of the classic ways a backtest flatters itself, so latency is
modelled explicitly and is never zero by default.

Deterministic by construction: the jitter draw is seeded from the decision
timestamp, so replaying the same input twice produces the same latency. That
is what makes the §23.3 fixtures reproducible.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True, slots=True)
class LatencyModel:
    """Base latency plus bounded, deterministic jitter."""

    base_ms: float = 250.0
    jitter_ms: float = 150.0

    def __post_init__(self) -> None:
        if self.base_ms < 0:
            raise ValueError("base_ms must be non-negative")
        if self.jitter_ms < 0:
            raise ValueError("jitter_ms must be non-negative")

    def delay_ms(self, decision_time: datetime, seed: str = "") -> float:
        """Latency for one decision. Same inputs always give the same answer."""
        if self.jitter_ms == 0:
            return self.base_ms
        key = f"{decision_time.isoformat()}|{seed}".encode()
        digest = hashlib.sha256(key).digest()
        # First 4 bytes -> uniform [0, 1), scaled to +/- jitter_ms.
        unit = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF
        return max(0.0, self.base_ms + (unit * 2 - 1) * self.jitter_ms)

    def arrival_time(self, decision_time: datetime, seed: str = "") -> datetime:
        return decision_time + timedelta(
            milliseconds=self.delay_ms(decision_time, seed))


ZERO_LATENCY = LatencyModel(base_ms=0.0, jitter_ms=0.0)
"""Only for isolating a test from latency. Never a backtest default: a
zero-latency result is not achievable in production and must not be reported
as if it were."""
