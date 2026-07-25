"""TrendSnapshot assembly per docs/trend-snapshot-contract.md v1.0.0 (frozen).

Also owns signal hysteresis (§12.3) and entry gating (§12.1/§12.2).  Direction
here is *signal* direction under hysteresis — it can hold +1 while the raw
score decays to +30 — whereas the regime classifier runs its own thresholds.

Reason codes (§12.5) are a public contract: stable, machine-readable, snapshot
tested.  Every gate always appears in ``gates`` with a pass/fail and detail.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from ..features.pipeline import FEATURE_SET_VERSION, TimeframeFeatures
from .regime import NON_TRADEABLE, Regime
from .score import ScoreResult

SCHEMA_VERSION = "1.0.0"
MODEL_VERSION = "trend-rules-v1.0.0"


@dataclass(frozen=True, slots=True)
class SignalConfig:
    entry_score: float = 65.0       # §12.3 / §30
    hold_score: float = 25.0
    minimum_confidence: float = 0.62
    ttl_seconds: int = 300
    forecast_horizon_seconds: int = 900
    max_spread_bps: float = 8.0     # §20 execution.maximum_spread_bps


class SignalHysteresis:
    """-1 / 0 / +1 signal direction with distinct enter and hold thresholds."""

    def __init__(self, config: SignalConfig) -> None:
        self._config = config
        self.direction = 0

    def update(self, trend_score: float | None) -> int:
        if trend_score is None:
            self.direction = 0
            return 0
        config = self._config
        if self.direction > 0:
            if trend_score <= config.hold_score:
                self.direction = 0
        elif self.direction < 0:
            if trend_score >= -config.hold_score:
                self.direction = 0
        if self.direction == 0:
            if trend_score >= config.entry_score:
                self.direction = 1
            elif trend_score <= -config.entry_score:
                self.direction = -1
        return self.direction


def confidence_from(score: ScoreResult, timeframes_aligned: int) -> float:
    """Transparent confidence: |score| strength × input completeness × the
    fraction of timeframes agreeing with the score's sign.  Not a calibrated
    probability and not presented as one (§10.4 calibration is Phase 6)."""
    if score.trend_score is None:
        return 0.0
    strength = min(abs(score.trend_score) / 100.0, 1.0)
    completeness = min(score.available_weight / 1.0, 1.0)
    alignment = timeframes_aligned / 4.0
    return round(0.25 + 0.75 * (0.5 * strength + 0.2 * completeness
                                + 0.3 * alignment), 3)


def _timeframe_bias(features: TimeframeFeatures) -> int:
    distance = features.get("ema_distance_atr")
    if distance is None:
        return 0
    if distance > 0.15:
        return 1
    if distance < -0.15:
        return -1
    return 0


def build_reason_codes(*, regime: Regime, direction: int,
                       timeframe_biases: Mapping[str, int],
                       setup: TimeframeFeatures,
                       gates: list[dict[str, Any]]) -> list[str]:
    codes: list[str] = []
    for timeframe, bias in timeframe_biases.items():
        label = timeframe.upper()
        if bias > 0:
            codes.append(f"{label}_TREND_UP")
        elif bias < 0:
            codes.append(f"{label}_TREND_DOWN")
    breakout = setup.get("breakout_direction") or 0.0
    if breakout > 0:
        codes.append("15M_BREAKOUT_CONFIRMED")
    elif breakout < 0:
        codes.append("15M_BREAKDOWN_CONFIRMED")
    if regime is Regime.RANGE:
        codes.append("REGIME_RANGE")
    if regime is Regime.HIGH_VOL_SHOCK:
        codes.append("HIGH_VOL_SHOCK")
    if regime is Regime.LOW_LIQUIDITY:
        codes.append("LOW_LIQUIDITY")
    if regime is Regime.DEGRADED:
        codes.append("DATA_DEGRADED")
    for gate in gates:
        if not gate["passed"]:
            codes.append(f"GATE_{gate['name'].upper()}_FAILED")
    if direction == 0 and regime not in NON_TRADEABLE:
        codes.append("SCORE_BELOW_ENTRY_THRESHOLD")
    return codes


def signal_id_for(symbol: str, candle_close_utc: str) -> str:
    """Deterministic per contract: same inputs, same id (invariant 7)."""
    payload = json.dumps(
        [symbol, candle_close_utc, FEATURE_SET_VERSION, MODEL_VERSION],
        separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_snapshot(
    *,
    symbol: str,
    now: datetime,
    candle_close: datetime,
    regime: Regime,
    regime_since: datetime,
    score: ScoreResult,
    direction: int,
    structural: TimeframeFeatures,
    primary: TimeframeFeatures,
    setup: TimeframeFeatures,
    trigger: TimeframeFeatures,
    timeframe_closes: Mapping[str, datetime],
    gates: list[dict[str, Any]],
    data_quality: str,
    config: SignalConfig,
    forecast: Mapping[str, float | None] | None = None,
) -> dict[str, Any]:
    timeframe_features = {"4h": structural, "1h": primary,
                          "15m": setup, "5m": trigger}
    biases = {tf: _timeframe_bias(features)
              for tf, features in timeframe_features.items()}
    aligned = (sum(1 for b in biases.values() if b > 0) if direction > 0
               else sum(1 for b in biases.values() if b < 0) if direction < 0
               else 0)
    confidence = confidence_from(score, aligned)

    gates_passed = all(g["passed"] for g in gates)
    entry_allowed = bool(
        data_quality == "OK"
        and regime not in NON_TRADEABLE
        and direction != 0
        and confidence >= config.minimum_confidence
        and gates_passed
    )
    # Contract invariants 1 and 2 enforced structurally, not just tested.
    if data_quality != "OK" or regime is Regime.DEGRADED:
        entry_allowed = False

    trigger_atr = trigger.get("atr")
    trigger_close = None
    invalidation_price: str | None = None
    suggested_stop_bps: float | None = None
    forecast = forecast or {}
    if trigger_atr is not None and direction != 0:
        swing_key = "last_swing_low" if direction > 0 else "last_swing_high"
        swing = trigger.get(swing_key)
        if swing is not None:
            invalidation_price = f"{swing:.1f}"
    volatility_bps = forecast.get("forecast_volatility_bps")
    if volatility_bps is not None:
        suggested_stop_bps = round(1.5 * volatility_bps, 1)  # §13.2 mid-range

    reason_codes = build_reason_codes(
        regime=regime, direction=direction, timeframe_biases=biases,
        setup=setup, gates=gates)

    return {
        "schema_version": SCHEMA_VERSION,
        "symbol": symbol,
        "timestamp": _iso(now),
        "candle_close_utc": _iso(candle_close),
        "signal_id": signal_id_for(symbol, _iso(candle_close)),
        "regime": regime.value,
        "regime_since": _iso(regime_since),
        "direction": direction,
        "trend_score": score.trend_score if score.trend_score is not None else 0.0,
        "confidence": confidence,
        "forecast_horizon_seconds": config.forecast_horizon_seconds,
        "expected_return_bps": forecast.get("expected_return_bps"),
        "expected_absolute_move_bps": forecast.get("expected_absolute_move_bps"),
        "forecast_volatility_bps": volatility_bps,
        "jump_probability": forecast.get("jump_probability"),
        "invalidation_price": invalidation_price,
        "suggested_stop_bps": suggested_stop_bps,
        "entry_allowed": entry_allowed,
        "signal_ttl_seconds": config.ttl_seconds,
        "components": [
            {"name": c.name, "weight": c.weight, "score": c.score,
             "available": c.available}
            for c in score.components
        ],
        "timeframes": [
            {"timeframe": tf, "bias": biases[tf],
             "score": _timeframe_display_score(timeframe_features[tf]),
             "closed_candle_utc": _iso(timeframe_closes[tf])}
            for tf in ("4h", "1h", "15m", "5m")
        ],
        "gates": gates,
        "reason_codes": reason_codes,
        "data_quality": data_quality,
        "feature_set_version": FEATURE_SET_VERSION,
        "model_version": MODEL_VERSION,
    }


def _timeframe_display_score(features: TimeframeFeatures) -> float | None:
    distance = features.get("ema_distance_atr")
    if distance is None:
        return None
    import math

    return round(100.0 * math.tanh(distance / 1.5), 1)
