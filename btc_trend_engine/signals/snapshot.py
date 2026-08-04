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
from . import zones
from .regime import CALM_ADX_MAX, NON_TRADEABLE, Regime, is_calm_adx
from .score import ScoreResult

# 1.4.0 adds the raw committed 5-minute trigger ADX.  The trading controller
# consumes it for an explicit SHORT_MOVE invalidation exit; exposing the
# numeric evidence avoids brittle parsing of human-readable gate text.
SCHEMA_VERSION = "1.4.0"
# The model version participates in signal_id. v1.6.0 defines Calm ADX as an
# inclusive 5-minute reading at or below 20. Directional CE/PE entries remain
# independent of ADX.
MODEL_VERSION = "trend-rules-v1.6.0"

# These two v1 gates describe whether a *directional* entry is available. They
# remain in the public gate matrix for backward compatibility, but they are not
# applicable to SHORT_MOVE: RANGE and a score inside +/-30 are the definition
# of that zone rather than reasons to reject it.
_DIRECTIONAL_ONLY_GATE_NAMES = frozenset({
    "regime_tradeable",
    "score_beyond_entry_threshold",
})


@dataclass(frozen=True, slots=True)
class SignalConfig:
    # Operator spec: directional entry at |score| > 40, independent of ADX;
    # the neutral candidate
    # range is only |score| <= 30 and must persist for six closed 5m candles.
    # Every intermediate score is HOLD -- see signals/zones.py. Was 65/25;
    # entry threshold moved to match the zone spec so `direction` and `zone`
    # can never disagree about whether a directional trade is on.
    entry_score: float = 40.0
    hold_score: float = 30.0
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
            if trend_score > config.entry_score:
                self.direction = 1
            elif trend_score < -config.entry_score:
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
        if gate.get("required", True) and not gate["passed"]:
            codes.append(f"GATE_{gate['name'].upper()}_FAILED")
    if direction == 0 and regime not in NON_TRADEABLE:
        codes.append("SCORE_BELOW_ENTRY_THRESHOLD")
    return codes


def _zone_entry_gates(
    gates: list[dict[str, Any]],
    *,
    zone: str,
    score: float,
    regime: Regime,
    short_move_calm: bool,
    trigger_adx: float | None,
    confidence: float,
    config: SignalConfig,
) -> list[dict[str, Any]]:
    """Return the gate matrix in terms of the zone that can actually trade.

    The original two directional gates are accurate for CE/PE entries but
    misleading for a neutral SHORT_MOVE candidate: RANGE is valid there and a
    score of +10 should not be displayed as a failed ``|score| >= 40`` test.
    Keep the raw gates for the stable reason-code contract; this is the
    operator-facing entry matrix shown by the dashboard.
    """
    if zone in {zones.CE_2_ITM, zones.PE_2_ITM}:
        # The raw v1 ``regime_tradeable`` gate treats RANGE as untradeable.
        # RANGE can be caused solely by ADX <= 20, so retaining that gate here
        # would reintroduce the directional ADX requirement indirectly.  Keep
        # the genuinely unsafe regimes blocked and preserve all other shared
        # execution/data gates.
        shared = [
            dict(gate)
            for gate in gates
            if gate.get("name") != "regime_tradeable"
        ]
        regime_safe = regime.value not in zones.UNSAFE_REGIMES
        return [
            *shared,
            {
                "name": "regime_safe_for_directional_entry",
                "label": "REGIME SAFETY",
                "passed": regime_safe,
                "detail": (
                    "regime permits directional entry"
                    if regime_safe else
                    f"regime is {regime.value}; directional entry is blocked"
                ),
            },
            {
                "name": "directional_confidence",
                "label": "DIRECTIONAL CONFIDENCE",
                "passed": confidence >= config.minimum_confidence,
                "detail": (
                    f"confidence {confidence:.0%} meets the minimum "
                    f"{config.minimum_confidence:.0%}"
                    if confidence >= config.minimum_confidence else
                    f"confidence {confidence:.0%} is below the required "
                    f"{config.minimum_confidence:.0%}"
                ),
            },
        ]

    shared = [
        dict(gate)
        for gate in gates
        if gate.get("name") not in _DIRECTIONAL_ONLY_GATE_NAMES
    ]
    if zone == zones.HOLD:
        policy = zones.ZonePolicy()
        return [
            *shared,
            {
                "name": "hold_band",
                "label": "HOLD BAND — NO ENTRY",
                "passed": False,
                "detail": (
                    f"score {score:+.1f} is between "
                    f"±{policy.sideways_max_abs:g} and ±"
                    f"{policy.directional_entry_abs:g}; keep the open position"
                ),
            },
        ]

    policy = zones.ZonePolicy()
    regime_safe = regime.value not in zones.UNSAFE_REGIMES
    return [
        *shared,
        {
            "name": "calm_adx",
            "label": "CALM ADX",
            "passed": short_move_calm,
            "detail": (
                f"5m ADX {trigger_adx:.1f} is at or below {CALM_ADX_MAX:.0f}; calm market confirms SHORT_MOVE"
                if short_move_calm else
                (f"5m ADX {trigger_adx:.1f} must be at or below {CALM_ADX_MAX:.0f} before selling MOVE"
                 if trigger_adx is not None else
                 "5m ADX is unavailable; calm-market confirmation is required before selling MOVE")
            ),
        },
        {
            "name": "regime_safe_for_move",
            "label": "REGIME SAFE FOR MOVE",
            "passed": regime_safe,
            "detail": (
                "regime permits an ATM MOVE straddle"
                if regime_safe
                else f"regime is {regime.value}; MOVE entry is blocked"
            ),
        },
        {
            "name": "score_in_neutral_range",
            "label": "NEUTRAL SCORE RANGE",
            "passed": abs(score) <= policy.sideways_max_abs,
            "detail": (
                f"score {score:+.1f} is inside −{policy.sideways_max_abs:g} to "
                f"+{policy.sideways_max_abs:g}"
            ),
        },
    ]


def _required_gates_pass(gates: list[dict[str, Any]]) -> bool:
    """Ignore explicit execution-deferred checks in the engine decision.

    Account risk belongs to the per-user consumer and is deliberately absent
    from the credential-free engine.  It must be visible as deferred, never
    painted as an engine pass or allowed to block every public snapshot.
    """
    return all(gate.get("passed") for gate in gates
               if gate.get("required", True))


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
    stop_loss_configured: bool = True,
) -> dict[str, Any]:
    timeframe_features = {"1h": structural, "30m": primary,
                          "15m": setup, "5m": trigger}
    biases = {tf: _timeframe_bias(features)
              for tf, features in timeframe_features.items()}
    aligned = (sum(1 for b in biases.values() if b > 0) if direction > 0
               else sum(1 for b in biases.values() if b < 0) if direction < 0
               else 0)
    confidence = confidence_from(score, aligned)

    gates_passed = _required_gates_pass(gates)
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

    # Zone is the operator-facing decision surface (signals/zones.py). It is
    # reported alongside, not instead of, `direction`/`entry_allowed`: those
    # keep their v1.0.0 meaning for existing consumers. The two can legitimately
    # differ -- `entry_allowed` additionally requires a tradeable regime, and
    # RANGE blocks it, whereas RANGE is precisely the sell-MOVE setup.
    score_value = score.trend_score if score.trend_score is not None else 0.0
    zone = zones.zone_for_score(score_value)
    trigger_adx = trigger.get("adx")
    short_move_calm = is_calm_adx(trigger_adx)
    zone_gates = _zone_entry_gates(
        gates,
        zone=zone,
        score=score_value,
        regime=regime,
        short_move_calm=short_move_calm,
        trigger_adx=trigger_adx,
        confidence=confidence,
        config=config,
    )
    zone_gates_passed = _required_gates_pass(zone_gates)
    zone_decision = zones.decide(
        score=score_value,
        regime=regime.value,
        data_quality=data_quality,
        gates_passed=zone_gates_passed,
        stop_loss_configured=stop_loss_configured,
        short_move_calm=short_move_calm,
        directional_adx=trigger_adx,
    )
    reason_codes = build_reason_codes(
        regime=regime, direction=direction, timeframe_biases=biases,
        setup=setup, gates=gates)
    if zone == zones.SHORT_MOVE:
        # These raw directional failures are expected for a neutral MOVE setup
        # and would contradict its zone-specific gate matrix if displayed.
        reason_codes = [
            code for code in reason_codes
            if code not in {
                "GATE_REGIME_TRADEABLE_FAILED",
                "GATE_SCORE_BEYOND_ENTRY_THRESHOLD_FAILED",
                "SCORE_BELOW_ENTRY_THRESHOLD",
            }
        ]
    elif zone in {zones.CE_2_ITM, zones.PE_2_ITM}:
        # RANGE may mean only that 5m ADX is calm.  Directional entries are
        # now score-driven, so the legacy regime gate is not a failure in the
        # zone policy and must not be reported as if it blocked the trade.
        reason_codes = [
            code for code in reason_codes
            if code != "GATE_REGIME_TRADEABLE_FAILED"
        ]

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
        "zone": zone_decision.zone,
        "zone_action_allowed": zone_decision.action_allowed,
        "zone_reason": zone_decision.reason,
        "zone_option_type": zone_decision.option_type,
        "zone_itm_steps": zone_decision.itm_steps,
        "trigger_adx": trigger_adx,
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
            for tf in ("1h", "30m", "15m", "5m")
        ],
        "gates": zone_gates,
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
