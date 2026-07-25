"""Transparent weighted trend score (§11) with the v1 weighting of ADR 0004.

Each component maps features → a raw score in [-1, +1]; the weighted sum goes
through ``100·tanh`` so the final score lives in (-100, +100) and saturates
gracefully.  A component whose inputs are missing contributes *nothing* and is
reported ``available=False`` — silence, never a guessed zero that quietly
drags the score toward neutral while claiming knowledge (§3.2).  When less
than half the total weight is available the whole score is unavailable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping

from ..features.pipeline import TimeframeFeatures

# ADR 0004: order_flow held at 0 until recorded history can validate it.
V1_WEIGHTS: dict[str, float] = {
    "higher_timeframe_trend": 0.40,
    "market_structure": 0.20,
    "lower_timeframe_momentum": 0.20,
    "order_flow": 0.00,
    "breakout_quality": 0.10,
    "derivatives_context": 0.10,
}


@dataclass(frozen=True, slots=True)
class ComponentScore:
    name: str
    weight: float
    score: float | None   # [-100, 100] for display; None when unavailable
    available: bool


@dataclass(frozen=True, slots=True)
class ScoreResult:
    trend_score: float | None      # (-100, 100); None when insufficient inputs
    components: list[ComponentScore]
    available_weight: float


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _higher_timeframe_trend(structural: TimeframeFeatures,
                            primary: TimeframeFeatures) -> float | None:
    """4H structural environment + 1H primary trend (§7 table)."""
    parts: list[float] = []
    for tf, weight in ((structural, 0.4), (primary, 0.6)):
        distance = tf.get("ema_distance_atr")
        slope = tf.get("ema_slope_atr")
        efficiency = tf.get("directional_efficiency")
        if distance is None or slope is None or efficiency is None:
            continue
        parts.append(weight * _clamp(
            0.45 * _clamp(distance / 2.0)
            + 0.30 * _clamp(slope / 1.0)
            + 0.25 * _clamp(efficiency / 0.5)))
    return sum(parts) if parts else None


def _market_structure(primary: TimeframeFeatures,
                      setup: TimeframeFeatures) -> float | None:
    parts: list[float] = []
    for tf in (primary, setup):
        bias = tf.get("structure_bias")
        donchian = tf.get("donchian_position")
        if bias is None or donchian is None:
            continue
        parts.append(0.5 * (0.6 * bias + 0.4 * (2.0 * donchian - 1.0)))
    return sum(parts) if parts else None


def _lower_timeframe_momentum(trigger: TimeframeFeatures) -> float | None:
    rsi = trigger.get("rsi")
    slope = trigger.get("ema_slope_atr")
    vwap_distance = trigger.get("vwap_distance_atr")
    if rsi is None or slope is None:
        return None
    score = 0.5 * _clamp((rsi - 50.0) / 25.0) + 0.35 * _clamp(slope / 1.0)
    if vwap_distance is not None:
        score += 0.15 * _clamp(vwap_distance / 2.0)
    return _clamp(score)


def _breakout_quality(setup: TimeframeFeatures) -> float | None:
    direction = setup.get("breakout_direction")
    if direction is None:
        return None
    if direction == 0.0:
        return 0.0
    body = setup.get("breakout_body_atr") or 0.0
    location = setup.get("close_location")
    volume_ratio = setup.get("volume_ratio")
    quality = 0.5 * _clamp(body / 1.5, 0.0, 1.0)
    if location is not None:
        # Close near the breakout extreme confirms; direction-dependent.
        confirmation = location if direction > 0 else 1.0 - location
        quality += 0.3 * confirmation
    if volume_ratio is not None:
        quality += 0.2 * _clamp((volume_ratio - 1.0) / 2.0, 0.0, 1.0)
    return direction * _clamp(quality, 0.0, 1.0)


def _derivatives_context(derivatives: Mapping[str, float],
                         underlying_sign: float) -> float | None:
    """§9.5: probabilistic context only. Crowded funding *against* the current
    direction is a mild contradiction; OI expanding with price confirms."""
    if not derivatives:
        return None
    score = 0.0
    used = False
    funding_rank = derivatives.get("funding_percentile")
    if funding_rank is not None and underlying_sign != 0.0:
        crowding = (funding_rank - 0.5) * 2.0   # -1 .. +1
        score += -0.5 * crowding * underlying_sign
        used = True
    oi_change = derivatives.get("oi_change_6h_pct")
    if oi_change is not None and underlying_sign != 0.0:
        score += 0.5 * _clamp(oi_change / 5.0) * underlying_sign
        used = True
    basis = derivatives.get("mark_spot_basis_pct")
    if basis is not None:
        score += 0.2 * _clamp(basis / 0.5)
        used = True
    return _clamp(score) if used else None


def compute_score(
    *,
    structural: TimeframeFeatures,   # 4h
    primary: TimeframeFeatures,      # 1h
    setup: TimeframeFeatures,        # 15m
    trigger: TimeframeFeatures,      # 5m
    derivatives: Mapping[str, float],
    weights: Mapping[str, float] | None = None,
) -> ScoreResult:
    active_weights = dict(weights or V1_WEIGHTS)
    if abs(sum(active_weights.values()) - 1.0) > 1e-9:
        raise ValueError("score weights must sum to 1.0")

    direction_hint = _higher_timeframe_trend(structural, primary)
    underlying_sign = (0.0 if direction_hint is None
                      else (1.0 if direction_hint > 0 else
                            -1.0 if direction_hint < 0 else 0.0))
    raw: dict[str, float | None] = {
        "higher_timeframe_trend": direction_hint,
        "market_structure": _market_structure(primary, setup),
        "lower_timeframe_momentum": _lower_timeframe_momentum(trigger),
        "order_flow": None,  # ADR 0004: not computed in v1
        "breakout_quality": _breakout_quality(setup),
        "derivatives_context": _derivatives_context(derivatives, underlying_sign),
    }

    components: list[ComponentScore] = []
    weighted_sum = 0.0
    available_weight = 0.0
    for name, weight in active_weights.items():
        value = raw.get(name)
        available = value is not None
        if available and weight > 0:
            weighted_sum += weight * value          # type: ignore[operator]
            available_weight += weight
        components.append(ComponentScore(
            name=name, weight=weight,
            score=round(value * 100.0, 1) if available else None,  # type: ignore[operator]
            available=available))

    if available_weight < 0.5:
        return ScoreResult(trend_score=None, components=components,
                           available_weight=available_weight)
    # Renormalise over the available weight so missing optional context does
    # not systematically shrink the score toward zero.
    normalised = weighted_sum / available_weight
    trend_score = 100.0 * math.tanh(1.5 * normalised)
    return ScoreResult(trend_score=round(trend_score, 1),
                       components=components,
                       available_weight=round(available_weight, 4))
