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
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from ..features.pipeline import compute_timeframe_features, derivatives_features
from ..market_data.messages import Candle
from .forecast import forecast_from_closes
from .regime import Regime, RegimeClassifier
from .score import compute_score
from .snapshot import SignalConfig, SignalHysteresis, build_snapshot

log = logging.getLogger(__name__)

# Contract timeframe roles (§30 production baseline).  Every decision is
# still committed only after a closed 5m candle; 30m replaces the legacy 4h
# structural layer to make the score responsive to intraday trend changes.
STRUCTURAL, PRIMARY, SETUP, TRIGGER = "1h", "30m", "15m", "5m"
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

        # No funding_history argument, and that omission is now deliberate.
        #
        # Without it `derivatives_features` cannot compute funding_percentile,
        # so `_derivatives_context`'s crowding term never fires and the
        # component reduces to OI change plus basis. That was originally an
        # accident -- the history was simply never wired up -- but measurement
        # says the accident is the better signal. Over 114k bars
        # (docs/signal-study.md) derivatives_context scores IC +0.035 at 120m
        # and +0.054 at 240m WITHOUT the percentile, both |t| > 2, against
        # +0.017 and +0.027 WITH it, neither significant. Adding the crowding
        # term roughly halves the only component in the model whose IC is
        # positive at all.
        #
        # So do not "finish" this by passing a funding history. If that is ever
        # revisited, re-run `scripts/signal_study.py --derivatives` (with and
        # without `--as-running`) and let the ICs decide, rather than
        # completing the feature because it looks unfinished.
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
            trigger=features[TRIGGER],
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

    # ── provisional live view (display only) ─────────────────────────────
    def produce_live(
        self,
        *,
        now: datetime,
        candles: Mapping[str, Sequence[Candle]],
        forming: Candle | None,
        data_quality: str,
        ticker: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """A continuously-updating score for display. **Never a decision.**

        Deliberately returns a DIFFERENT SHAPE from ``produce`` — no
        ``signal_id``, no ``entry_allowed``, no ``gates``. The dashboard's
        order path requires those fields, so it is structurally incapable of
        consuming this, which is the point: the committed score is idempotent
        per closed candle (``completed_candle_signal_key`` dedupes on it), and
        a repainting score in that path would let one candle fire two entries
        as the score crossed a band and came back.

        The forming candle is appended to the closed trigger series, so this
        moves within the bar. All other score inputs, including derivatives
        context, match the committed pipeline. That makes the preview answer
        "what would commit if this forming candle closed now?" without making
        it eligible to drive orders.
        """
        trigger = list(candles.get(TRIGGER) or [])
        if forming is not None:
            trigger = [*trigger, forming]
        if not trigger:
            return None

        features = {
            role: compute_timeframe_features(
                role, trigger if role == TRIGGER else list(candles.get(role) or []))
            for role in (STRUCTURAL, PRIMARY, SETUP, TRIGGER)
        }
        score = compute_score(
            structural=features[STRUCTURAL], primary=features[PRIMARY],
            setup=features[SETUP], trigger=features[TRIGGER],
            derivatives=derivatives_features(ticker or {}),
        )
        # ``live_score`` remains the one-decimal value used by the dial.  The
        # separate chart value keeps enough precision for a faithful OHLC
        # preview without changing the committed decision or any trade path.
        live_score = score.trend_score if score.trend_score is not None else 0.0
        chart_score = (score.raw_trend_score
                       if score.raw_trend_score is not None else live_score)
        from . import zones

        committed = self.latest() or {}
        return {
            "symbol": self.symbol,
            "provisional": True,
            "as_of": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "live_score": round(live_score, 1),
            "chart_score": round(chart_score, 4),
            "live_zone": zones.zone_for_score(live_score),
            "data_quality": data_quality,
            "forming_candle_start": (
                forming.start.strftime("%Y-%m-%dT%H:%M:%SZ") if forming else None),
            # The committed decision this is previewing away from, so a reader
            # can always see both numbers and tell which one trades.
            "committed_signal_id": committed.get("signal_id"),
            "committed_candle_close_utc": committed.get("candle_close_utc"),
            "committed_score": committed.get("trend_score"),
            "committed_zone": committed.get("zone"),
        }

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
             "passed": abs(score) > self.config.entry_score,
             "detail": None if abs(score) > self.config.entry_score
             else f"|score| {abs(score):.1f} <= {self.config.entry_score}"},
            # The risk lock is genuinely not evaluable here: risk/ needs an
            # account's data_dir and kill-switch state, and the engine holds
            # neither (ADR 0001 — it never reads users/).  Mark it deferred
            # rather than green: the dashboard must show the final per-user
            # result immediately before a paper or LIVE mutation.
            {"name": "risk_lock_clear", "label": "RISK CHECK AT EXECUTION",
             "passed": False, "required": False, "status": "DEFERRED",
             "detail": "not evaluated by the engine; verified per account at entry"},
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

    def restore_history(self, snapshots: Sequence[Mapping[str, Any]]) -> int:
        """Rehydrate only validated committed state after an engine restart.

        The neutral MOVE confirmation deliberately still requires adjacent
        five-minute timestamps, so old/stale rows cannot confirm a new entry.
        Restoring the last valid regime and hysteresis direction prevents a
        harmless process restart from changing a current directional reading.
        """
        restored = [dict(row) for row in snapshots if isinstance(row, Mapping)]
        restored.sort(key=lambda row: str(row.get("candle_close_utc") or ""))
        self._history = restored[-self._max_history:]
        if not self._history:
            return 0

        latest = self._history[-1]
        direction = latest.get("direction")
        if direction in (-1, 0, 1):
            self.hysteresis.direction = int(direction)
        try:
            self.classifier.restore(str(latest["regime"]))
        except (KeyError, ValueError):
            pass
        try:
            self._regime_since = datetime.fromisoformat(
                str(latest.get("regime_since") or "").replace("Z", "+00:00")
            ).astimezone(timezone.utc)
        except ValueError:
            self._regime_since = None
        return len(self._history)


def spread_bps_from(best_bid: float | None, best_ask: float | None) -> float | None:
    if not best_bid or not best_ask or best_bid <= 0 or best_ask <= 0:
        return None
    mid = (best_bid + best_ask) / 2.0
    if mid <= 0:
        return None
    return (best_ask - best_bid) / mid * 10_000.0


ProducerFactory = Callable[[str], SnapshotProducer]
