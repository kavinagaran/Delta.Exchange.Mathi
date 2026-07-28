"""Reconstruct the derivatives feature block over history.

``features.pipeline.derivatives_features`` reads a *live* ``v2/ticker``
payload, so the 10% ``derivatives_context`` weight has never been tested --
there was no obvious historical source for funding, open interest or basis.

There is one: Delta serves auxiliary series through the same public candle
endpoint under prefixed symbols, verified against the live venue --

    MARK:BTCUSD      mark price
    .DEXBTUSD        spot index
    FUNDING:BTCUSD   funding rate
    OI:BTCUSD        open interest, in contracts

This module turns those into the exact keys ``signals.score`` consumes, as of
any instant, so the component can be measured like any other.

Two fidelity notes, because a study that quietly differs from production
measures the wrong thing:

* **Look-ahead.** An hourly candle covering [H, H+1) is not knowable until
  H+1, so a lookup at time *t* uses the newest candle that had already closed
  at or before *t*. Never the bar in progress.
* **`funding_percentile` does not exist in production.** ``producer`` calls
  ``derivatives_features(ticker)`` without ``funding_history``, and
  ``percentile_rank`` needs 8+ samples, so the key is never set on the live
  box. This module can produce it (``include_funding_percentile=True``) to
  measure the component *as designed*, and omit it to measure the component
  *as running*. The difference between those two is itself a finding.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..features import indicators as ind
from ..market_data.messages import Candle

# Symbols verified against api.india.delta.exchange (see module docstring).
MARK_SYMBOL = "MARK:BTCUSD"
SPOT_SYMBOL = ".DEXBTUSD"
FUNDING_SYMBOL = "FUNDING:BTCUSD"
OI_SYMBOL = "OI:BTCUSD"

DEFAULT_FUNDING_WINDOW = 720        # 30 days of hourly samples
OI_CHANGE_HOURS = 6                 # matches the venue's oi_change_usd_6h


@dataclass(frozen=True, slots=True)
class _Series:
    """Closed-bar values with an as-of lookup that cannot see the future."""

    times: tuple[datetime, ...]     # bar START times, ascending
    values: tuple[float, ...]
    bar_seconds: int

    @classmethod
    def from_candles(cls, candles: Sequence[Candle], bar_seconds: int) -> "_Series":
        ordered = sorted(candles, key=lambda c: c.start)
        return cls(tuple(c.start for c in ordered),
                   tuple(float(c.close) for c in ordered), bar_seconds)

    def index_as_of(self, when: datetime) -> int | None:
        """Index of the newest bar that had fully closed by ``when``."""
        cutoff = when - timedelta(seconds=self.bar_seconds)
        position = bisect_right(self.times, cutoff)
        return position - 1 if position else None

    def value_as_of(self, when: datetime) -> float | None:
        index = self.index_as_of(when)
        return None if index is None else self.values[index]

    def history_as_of(self, when: datetime, window: int) -> list[float]:
        index = self.index_as_of(when)
        if index is None:
            return []
        return list(self.values[max(0, index + 1 - window):index + 1])


class DerivativesHistory:
    """Callable: ``when -> {feature: value}`` for the derivatives block."""

    def __init__(
        self,
        *,
        mark: Sequence[Candle] = (),
        spot: Sequence[Candle] = (),
        funding: Sequence[Candle] = (),
        open_interest: Sequence[Candle] = (),
        bar_seconds: int = 3600,
        funding_window: int = DEFAULT_FUNDING_WINDOW,
        include_funding_percentile: bool = True,
    ) -> None:
        self._mark = _Series.from_candles(mark, bar_seconds)
        self._spot = _Series.from_candles(spot, bar_seconds)
        self._funding = _Series.from_candles(funding, bar_seconds)
        self._oi = _Series.from_candles(open_interest, bar_seconds)
        self._bar_seconds = bar_seconds
        self._funding_window = funding_window
        self._include_funding_percentile = include_funding_percentile

    def __call__(self, when: datetime) -> dict[str, float]:
        out: dict[str, float] = {}

        mark = self._mark.value_as_of(when)
        spot = self._spot.value_as_of(when)
        if mark is not None and spot:
            out["mark_spot_basis_pct"] = (mark - spot) / spot * 100.0

        funding = self._funding.value_as_of(when)
        if funding is not None:
            out["funding_rate"] = funding
            if self._include_funding_percentile:
                history = self._funding.history_as_of(when, self._funding_window)
                rank = ind.percentile_rank(history, funding)
                if rank is not None:
                    out["funding_percentile"] = rank

        # Production reads oi_change_usd_6h / oi_value_usd, i.e. the change in
        # *notional* OI. Rebuild that from contracts x mark rather than using
        # the contract count alone, whose ratio differs whenever price moved.
        index = self._oi.index_as_of(when)
        if index is not None and mark is not None:
            back = index - (OI_CHANGE_HOURS * 3600) // self._bar_seconds
            if back >= 0:
                past_mark = self._mark.value_as_of(
                    when - timedelta(hours=OI_CHANGE_HOURS))
                now_usd = self._oi.values[index] * mark
                if past_mark is not None and now_usd:
                    past_usd = self._oi.values[back] * past_mark
                    out["oi_change_6h_pct"] = (now_usd - past_usd) / now_usd * 100.0
        return out
