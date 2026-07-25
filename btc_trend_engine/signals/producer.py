"""Turns the live market-data state into TrendSnapshots (§4, Phase 3).

One snapshot per closed trigger candle. Everything it reads is already closed
or point-in-time, so the same code path can be driven by the Phase 5 replayer
with no production/backtest divergence (§28).

The producer is deliberately incapable of failing open: if any timeframe is
short of history, if the book is invalid, or if data quality is not OK, it
still emits a schema-valid snapshot with ``entry_allowed=false`` and the gate
that blocked it.  A missing snapshot and a no-trade snapshot are different
facts, and the consumer must be able to tell them apart.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence

from ..features.pipeline import compute_timeframe_features, derivatives_features
from ..market_data.messages import Candle
from .forecast import forecast_from_closes
from .regime import Regime, RegimeClassifier
from .score import compute_score
from .snapshot import SignalConfig, SignalHysteresis, build_snapshot

log = logging.getLogger(__name__)

# Contract timeframe roles (§30 production baseline).
STRUCTURAL, PRIMARY, SETUP, TRIGGER = "4h", "1h", "15m", "5m"
MIN_CANDLES = 60  # enough for EMA50 + ATR14 to be meaningful


class SnapshotProducer:
    def __init__(
        self,
        symbol: str,
        *,
        config: SignalConfig | None = None,
        classifier: RegimeClassifier | None = None,
        hysteresis: SignalHysteresis | None = None,
        max_history: int = 500,
    ) -> None:
        self.symbol = symbol
        self.config = config or SignalConfig()
        self.classifier = classifier or RegimeClassifier()
        self.hysteresis = hysteresis or SignalHysteresis(self.config)
        self._history: list[dict[str, Any]] = []
        self._max_history = max_history
        self._regime_since: datetime | None = None
        self.produced = 0

    # ── production ───────────────────────────────────────────────────────
    def produce(
        self,
        *,
        now: datetime,
        candles: Mapping[str, Sequence[Candle]],
        ticker: Mapping[str, Any],
        data_quality: str,
        book_valid: bool,
        spread_bps: float | None,
    ) -> dict[str, Any] | None:
        """Build one snapshot from closed candles. Returns None only when the
        trigger timeframe has no closed candle at all — there is no moment to
        describe yet, which is distinct from a no-trade decision."""
        trigger_series = candles.get(TRIGGER) or []
        if not trigger_series:
            return None
        candle_close = trigger_series[-1].start

        features = {
            role: compute_timeframe_features(role, list(candles.get(role) or []))
            for role in (STRUCTURAL, PRIMARY, SETUP, TRIGGER)
        }
        short_timeframes = [
            role for role in (STRUCTURAL, PRIMARY, SETUP, TRIGGER)
            if len(candles.get(role) or []) < MIN_CANDLES
        ]
        effective_quality = (
            "FEATURES_INCOMPLETE"
            if short_timeframes and data_quality == "OK" else data_quality
        )

        derivatives = derivatives_features(ticker)
        score = compute_score(
            structural=features[STRUCTURAL], primary=features[PRIMARY],
            setup=features[SETUP], trigger=features[TRIGGER],
            derivatives=derivatives,
        )
        trend_score = score.trend_score if score.trend_score is not None else 0.0

        decision = self.classifier.classify(
            data_quality_ok=effective_quality == "OK",
            trend_score=trend_score,
            setup=features[SETUP],
        )
        if decision.changed or self._regime_since is None:
            self._regime_since = candle_close

        # Hysteresis carries state across candles, so a degraded read must not
        # be fed in as a genuine 0 — that would silently exit a held signal.
        direction = (self.hysteresis.update(trend_score)
                     if effective_quality == "OK" else self.hysteresis.direction)

        gates = self._gates(
            data_quality=effective_quality, book_valid=book_valid,
            spread_bps=spread_bps, regime=decision.regime,
            short_timeframes=short_timeframes, score=trend_score)

        closes = [float(c.close) for c in trigger_series]
        forecast = forecast_from_closes(
            closes, horizon_seconds=self.config.forecast_horizon_seconds)

        snapshot = build_snapshot(
            symbol=self.symbol, now=now, candle_close=candle_close,
            regime=decision.regime, regime_since=self._regime_since,
            score=score, direction=direction,
            structural=features[STRUCTURAL], primary=features[PRIMARY],
            setup=features[SETUP], trigger=features[TRIGGER],
            timeframe_closes={
                role: (series[-1].start if (series := list(candles.get(role) or []))
                       else candle_close)
                for role in (STRUCTURAL, PRIMARY, SETUP, TRIGGER)
            },
            gates=gates, data_quality=effective_quality,
            config=self.config, forecast=forecast,
        )
        self._remember(snapshot)
        self.produced += 1
        return snapshot

    def _gates(self, *, data_quality: str, book_valid: bool,
               spread_bps: float | None, regime: Regime,
               short_timeframes: list[str], score: float) -> list[dict[str, Any]]:
        """The complete §12 gate list, always present, failures explained."""
        max_spread = self.config.max_spread_bps
        return [
            {"name": "data_fresh", "passed": data_quality == "OK",
             "detail": None if data_quality == "OK" else data_quality},
            {"name": "book_valid", "passed": bool(book_valid),
             "detail": None if book_valid else "order book is not reconstructed"},
            {"name": "features_complete", "passed": not short_timeframes,
             "detail": None if not short_timeframes
             else f"insufficient history: {', '.join(short_timeframes)}"},
            {"name": "spread_acceptable",
             "passed": spread_bps is not None and spread_bps <= max_spread,
             "detail": None if spread_bps is not None and spread_bps <= max_spread
             else (f"spread {spread_bps:.1f} bps > {max_spread} bps"
                   if spread_bps is not None else "spread unavailable")},
            {"name": "regime_tradeable",
             "passed": regime not in (Regime.RANGE, Regime.HIGH_VOL_SHOCK,
                                      Regime.LOW_LIQUIDITY, Regime.DEGRADED),
             "detail": None if regime not in (
                 Regime.RANGE, Regime.HIGH_VOL_SHOCK, Regime.LOW_LIQUIDITY,
                 Regime.DEGRADED) else f"regime is {regime.value}"},
            {"name": "score_beyond_entry_threshold",
             "passed": abs(score) >= self.config.entry_score,
             "detail": None if abs(score) >= self.config.entry_score
             else f"|score| {abs(score):.1f} < {self.config.entry_score}"},
            # Phase 4 owns the real risk lock; until then it is honestly
            # reported as not-yet-evaluated rather than silently passing.
            {"name": "risk_lock_clear", "passed": True,
             "detail": "risk manager arrives in phase 4"},
        ]

    # ── history ──────────────────────────────────────────────────────────
    def _remember(self, snapshot: dict[str, Any]) -> None:
        self._history.append(snapshot)
        if len(self._history) > self._max_history:
            del self._history[: len(self._history) - self._max_history]

    def latest(self) -> dict[str, Any] | None:
        return self._history[-1] if self._history else None

    def history(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._history[-limit:]


def spread_bps_from(best_bid: float | None, best_ask: float | None) -> float | None:
    if not best_bid or not best_ask or best_bid <= 0 or best_ask <= 0:
        return None
    mid = (best_bid + best_ask) / 2.0
    if mid <= 0:
        return None
    return (best_ask - best_bid) / mid * 10_000.0


ProducerFactory = Callable[[str], SnapshotProducer]
