"""Transparent weighted trend score (§11) with explainable ADX/RSI inputs.

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
from .regime import CALM_ADX_MAX

# ADR 0004: order_flow held at 0 until recorded history can validate it.
# ADX is strength only; its signed contribution is set by the independently
# calculated RSI/structure direction.
#
# Reweighted 2026-07-29 from the measured information coefficients and
# correlation matrix in docs/signal-study.md (115,188 candles, 13 months).
# Read that document before changing these: every component except
# derivatives_context has a NEGATIVE IC at every horizon this strategy
# trades, so these weights reduce harm, they do not create edge.  The
# composite improves from -0.040 to -0.032; it does not become positive, and
# no weighting of these inputs can make it positive.
#
#   higher_timeframe_trend  0.30 -> 0.24  trimmed: significant at only 1 of 4
#                                         horizons, but 4H/1H features are
#                                         forecasting a 0.6-3.3h holding period
#   market_structure        0.18 -> 0.12  cut: significantly negative at 3 of
#                                         4 horizons (|t| = 2.5 at 120m)
#   lower_timeframe_momentum 0.13 -> 0.14 held: the most negative single
#                                         component; must NOT absorb rsi's
#                                         freed weight
#   rsi_momentum            0.14 -> 0.00  removed: Spearman +0.778 against
#                                         lower_timeframe_momentum -- the same
#                                         input measured twice.  This is the
#                                         one change justified structurally
#                                         (redundancy) rather than by fitting
#                                         an IC estimate, which is what makes
#                                         it the safest of them.
#   adx_trend_strength      0.10 -> 0.20  raised: its IC is indistinguishable
#                                         from zero (|t| = 1.4).  Weights are
#                                         normalised, so weight on a zero-IC
#                                         component is weight NOT on a
#                                         negative one.  Dilution, not a claim
#                                         that ADX predicts anything.
#   breakout_quality        0.08 -> 0.15  raised: least negative of the
#                                         significant price components at 120m
#   derivatives_context     0.07 -> 0.25  see below; raised twice, on better
#                                         measurement each time
#
# 2026-07-30, second pass on derivatives_context (0.15 -> 0.25).  The first
# pass measured it with funding_percentile included, which is NOT what
# production computes (see the comment at signals/producer.py where the
# omission is deliberate).  Re-measured in the production variant over the
# same 114k bars, it is a different component:
#
#     IC @120m          +0.036   (vs +0.017 diluted)
#     IC @240m          +0.054   |t| = 2.5 -- the only significant positive
#                                cell anywhere in this model
#     correlation vs every price component:  |rho| <= 0.04
#
# That last line is the reason for the raise, more than the IC.  Diluted, this
# component correlated +0.267 with higher_timeframe_trend; undiluted it is
# effectively orthogonal to the entire price block (+0.038 at most).  It is
# the only genuinely independent evidence in the score.
#
# Composite IC at 120m, production variant:
#     current (0.15)                       -0.028
#     this vector (0.25)                   -0.017
#     unconstrained optimiser at 0.25      -0.009
#
# The optimiser reaches -0.009 by zeroing market_structure and
# lower_timeframe_momentum outright and pushing adx_trend_strength and
# breakout_quality to 0.29 each.  Deliberately not taken: that is a corner
# solution on one 13-month sample, it leaves 58% of the score resting on two
# components whose near-zero IC is itself an estimate, and it would blind the
# score to structure and momentum entirely if their negative IC turns out to
# be regime-specific.  The freed 0.10 comes instead from the two
# best-measured negatives, in the direction the optimiser points, without
# following it off the edge.
#
# Note that weights are normalised to 1.0, so they can only change the mix,
# never the amount of conviction.  To act less on a negative signal the levers
# are the tanh gain below, ZonePolicy's entry threshold, and position size.
V1_WEIGHTS: dict[str, float] = {
    "higher_timeframe_trend": 0.24,
    "market_structure": 0.07,
    "lower_timeframe_momentum": 0.09,
    "rsi_momentum": 0.00,
    "adx_trend_strength": 0.20,
    "order_flow": 0.00,
    "breakout_quality": 0.15,
    "derivatives_context": 0.25,
}

# Derivatives data is context, not a stand-alone direction signal.  Even with
# its research-backed weight, one OI/basis observation must not contribute
# more than 12.5 points to the pre-tanh weighted score (0.25 * 0.50).  More
# importantly, its OI term is scaled by the continuous higher-timeframe trend
# reading below rather than its sign, preventing a tiny cross through zero
# from flipping the full component from roughly +50 to -50 in one candle.
DERIVATIVES_CONTEXT_CAP = 0.50


@dataclass(frozen=True, slots=True)
class ComponentScore:
    name: str
    weight: float
    score: float | None   # [-100, 100] for display; None when unavailable
    available: bool


@dataclass(frozen=True, slots=True)
class ScoreResult:
    trend_score: float | None      # (-100, 100); None when insufficient inputs
    # The decision score is intentionally rounded to one decimal before it is
    # committed.  The live preview chart, however, needs the unrounded value
    # so that genuine movement inside a five-minute candle does not collapse
    # into a row of artificial dojis.
    raw_trend_score: float | None
    components: list[ComponentScore]
    available_weight: float

    @property
    def max_abs_component(self) -> float | None:
        """Widest single-component reading, on the same scale as the score.

        A weighted sum near zero has two very different causes and cannot tell
        them apart: every component is quiet, or large components cancel.
        ``0.40 x (+50) + 0.20 x (-100)`` is also zero, and that is a market in
        violent disagreement across timeframes, not a sideways one.  A consumer
        that needs genuine neutrality must require *every* component to be
        small, which is what this reports.

        Unavailable components and weight-0 components are excluded: neither
        contributes to the sum, so neither can be the disagreement.  Returns
        None when nothing is available, which callers must not read as calm.
        """
        magnitudes = [abs(c.score) for c in self.components
                      if c.available and c.score is not None and c.weight > 0]
        return max(magnitudes) if magnitudes else None


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _higher_timeframe_trend(structural: TimeframeFeatures,
                            primary: TimeframeFeatures) -> float | None:
    """1H structural environment + 30M primary trend (§7 table)."""
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
    slope = trigger.get("ema_slope_atr")
    vwap_distance = trigger.get("vwap_distance_atr")
    if slope is None and vwap_distance is None:
        return None
    score = 0.0
    weight = 0.0
    if slope is not None:
        score += 0.65 * _clamp(slope / 1.0)
        weight += 0.65
    if vwap_distance is not None:
        score += 0.35 * _clamp(vwap_distance / 2.0)
        weight += 0.35
    return _clamp(score / weight) if weight else None


def _rsi_momentum(structural: TimeframeFeatures, primary: TimeframeFeatures,
                  setup: TimeframeFeatures, trigger: TimeframeFeatures
                  ) -> float | None:
    """Multi-timeframe RSI direction, centred at neutral 50.

    RSI above 70 and below 30 saturate at +/-1.  The setup and trigger
    readings carry most of the weight because they make the immediate
    entry-direction call, while 1h/30m prevent an isolated five-minute RSI
    spike from dominating the score.
    """
    weighted = 0.0
    available = 0.0
    for timeframe, weight in ((structural, 0.15), (primary, 0.25),
                              (setup, 0.35), (trigger, 0.25)):
        rsi = timeframe.get("rsi")
        if rsi is None:
            continue
        weighted += weight * _clamp((rsi - 50.0) / 20.0)
        available += weight
    return _clamp(weighted / available) if available else None


def _adx_trend_strength(trigger: TimeframeFeatures,
                        rsi_score: float | None,
                        direction_hint: float | None) -> float | None:
    """Signed ADX evidence from the 5m trigger timeframe.

    ADX at or below 25 intentionally contributes a neutral score: that is the
    calm-zone threshold shared with the regime classifier.  Above 30, strength
    is signed only when RSI or higher-timeframe structure has an opinion;
    ADX itself never invents a direction.
    """
    adx = trigger.get("adx")
    if adx is None:
        return None
    strength = _clamp((adx - CALM_ADX_MAX) / 20.0, 0.0, 1.0)
    sign_source = rsi_score if rsi_score is not None else direction_hint
    if sign_source is None or abs(sign_source) < 0.05:
        return 0.0
    return strength if sign_source > 0 else -strength


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
                         underlying_direction: float) -> float | None:
    """§9.5: probabilistic context only. Crowded funding *against* the current
    direction is a mild contradiction; OI expanding with price confirms.

    ``underlying_direction`` is deliberately continuous in [-1, +1].  Treating
    every non-zero reading as a full +/-1 made this component discontinuous:
    an almost-flat higher-timeframe input crossing zero inverted the entire OI
    contribution even though the market evidence had barely changed.
    """
    if not derivatives:
        return None
    direction = _clamp(underlying_direction)
    score = 0.0
    used = False
    funding_rank = derivatives.get("funding_percentile")
    if funding_rank is not None and direction != 0.0:
        crowding = (funding_rank - 0.5) * 2.0   # -1 .. +1
        score += -0.5 * crowding * direction
        used = True
    oi_change = derivatives.get("oi_change_6h_pct")
    if oi_change is not None and direction != 0.0:
        score += 0.5 * _clamp(oi_change / 5.0) * direction
        used = True
    basis = derivatives.get("mark_spot_basis_pct")
    if basis is not None:
        score += 0.2 * _clamp(basis / 0.5)
        used = True
    return _clamp(
        score, -DERIVATIVES_CONTEXT_CAP, DERIVATIVES_CONTEXT_CAP,
    ) if used else None


def compute_score(
    *,
    structural: TimeframeFeatures,   # 1h
    primary: TimeframeFeatures,      # 30m
    setup: TimeframeFeatures,        # 15m
    trigger: TimeframeFeatures,      # 5m
    derivatives: Mapping[str, float],
    weights: Mapping[str, float] | None = None,
) -> ScoreResult:
    active_weights = dict(weights or V1_WEIGHTS)
    if abs(sum(active_weights.values()) - 1.0) > 1e-9:
        raise ValueError("score weights must sum to 1.0")

    direction_hint = _higher_timeframe_trend(structural, primary)
    underlying_direction = (
        0.0 if direction_hint is None else _clamp(direction_hint)
    )
    rsi_score = _rsi_momentum(structural, primary, setup, trigger)
    raw: dict[str, float | None] = {
        "higher_timeframe_trend": direction_hint,
        "market_structure": _market_structure(primary, setup),
        "lower_timeframe_momentum": _lower_timeframe_momentum(trigger),
        "rsi_momentum": rsi_score,
        "adx_trend_strength": _adx_trend_strength(
            trigger, rsi_score, direction_hint),
        "order_flow": None,  # ADR 0004: not computed in v1
        "breakout_quality": _breakout_quality(setup),
        "derivatives_context": _derivatives_context(
            derivatives, underlying_direction,
        ),
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
        return ScoreResult(trend_score=None, raw_trend_score=None, components=components,
                           available_weight=available_weight)
    # Renormalise over the available weight so missing optional context does
    # not systematically shrink the score toward zero.
    normalised = weighted_sum / available_weight
    raw_trend_score = 100.0 * math.tanh(1.5 * normalised)
    return ScoreResult(trend_score=round(raw_trend_score, 1),
                       raw_trend_score=raw_trend_score,
                       components=components,
                       available_weight=round(available_weight, 4))
