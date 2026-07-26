"""Rule-based regime classifier with hysteresis (§8, §10.1 option 1).

Exactly one regime at a time. Precedence is safety-first: data quality, then
volatility shock, then liquidity, then breakout, then trend-vs-range — a
market can look trendy *and* shocked, and the shocked reading must win.

Hysteresis (§8.2): TREND_* engages at |score| ≥ enter and holds until |score|
drops below exit, preventing flapping around one threshold.  Transitions are
explicit values returned to the caller for logging (§8.2: "explicit, testable,
and logged").
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from ..features.pipeline import TimeframeFeatures


class Regime(enum.StrEnum):
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"
    BREAKOUT_UP = "BREAKOUT_UP"
    BREAKOUT_DOWN = "BREAKOUT_DOWN"
    HIGH_VOL_SHOCK = "HIGH_VOL_SHOCK"
    LOW_LIQUIDITY = "LOW_LIQUIDITY"
    DEGRADED = "DEGRADED"


# No-entry regimes (§14.2: no entries in shock/illiquid/degraded; §8.3: RANGE
# denies trend entries).
NON_TRADEABLE = frozenset({Regime.RANGE, Regime.HIGH_VOL_SHOCK,
                           Regime.LOW_LIQUIDITY, Regime.DEGRADED})


@dataclass(frozen=True, slots=True)
class RegimeConfig:
    # Aligned to the 2026-07-26 zone spec (signals/zones.py): directional at
    # |35|, hold to |25|. These MUST track the zone bands. When they did not
    # (regime entered at 60 while zones entered at 35) every score in 35..60
    # produced a directional zone that the regime_tradeable gate then blocked,
    # so the whole band was silently untradeable -- visible in production only
    # as "the engine rarely trades", not as an error.
    trend_enter_score: float = 35.0   # was 60.0 (§8.2 example)
    trend_exit_score: float = 25.0    # was 30.0; matches zones' hold band
    breakout_body_atr_min: float = 1.2
    vol_shock_ratio: float = 2.5      # short vol vs long vol
    jump_shock_score: float = 4.0     # standardised last-bar move
    low_liquidity_volume_ratio: float = 0.25


@dataclass(frozen=True, slots=True)
class RegimeDecision:
    regime: Regime
    previous: Regime
    changed: bool
    reason: str


class RegimeClassifier:
    """Holds only the previous regime — all other state is per-call input."""

    def __init__(self, config: RegimeConfig | None = None) -> None:
        self.config = config or RegimeConfig()
        self._current = Regime.DEGRADED  # fail-closed until first good input

    @property
    def current(self) -> Regime:
        return self._current

    def classify(
        self,
        *,
        data_quality_ok: bool,
        trend_score: float,
        setup: TimeframeFeatures,
    ) -> RegimeDecision:
        previous = self._current
        regime, reason = self._classify(data_quality_ok, trend_score, setup)
        self._current = regime
        return RegimeDecision(regime=regime, previous=previous,
                              changed=regime is not previous, reason=reason)

    def _classify(self, data_quality_ok: bool, trend_score: float,
                  setup: TimeframeFeatures) -> tuple[Regime, str]:
        config = self.config
        if not data_quality_ok:
            return Regime.DEGRADED, "data quality is not OK"

        vol_ratio = setup.get("vol_ratio")
        jump = setup.get("jump_score")
        if ((vol_ratio is not None and vol_ratio >= config.vol_shock_ratio)
                or (jump is not None and jump >= config.jump_shock_score)):
            return Regime.HIGH_VOL_SHOCK, (
                f"vol_ratio={vol_ratio}, jump_score={jump}")

        volume_ratio = setup.get("volume_ratio")
        if (volume_ratio is not None
                and volume_ratio <= config.low_liquidity_volume_ratio):
            return Regime.LOW_LIQUIDITY, f"volume_ratio={volume_ratio:.2f}"

        breakout = setup.get("breakout_direction") or 0.0
        body = setup.get("breakout_body_atr") or 0.0
        if breakout != 0.0 and body >= config.breakout_body_atr_min:
            regime = Regime.BREAKOUT_UP if breakout > 0 else Regime.BREAKOUT_DOWN
            return regime, f"channel break, body {body:.2f} ATR"

        # Trend with hysteresis: a standing trend holds until |score| decays
        # below the exit threshold; a new trend needs the full entry threshold.
        holding_up = self._current in (Regime.TREND_UP, Regime.BREAKOUT_UP)
        holding_down = self._current in (Regime.TREND_DOWN, Regime.BREAKOUT_DOWN)
        if trend_score >= config.trend_enter_score or (
                holding_up and trend_score > config.trend_exit_score):
            return Regime.TREND_UP, f"score {trend_score:.1f}"
        if trend_score <= -config.trend_enter_score or (
                holding_down and trend_score < -config.trend_exit_score):
            return Regime.TREND_DOWN, f"score {trend_score:.1f}"
        return Regime.RANGE, f"score {trend_score:.1f} inside +/-{config.trend_enter_score}"
