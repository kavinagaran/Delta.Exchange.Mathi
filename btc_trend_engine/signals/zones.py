"""Score -> action zone mapping (operator spec, 2026-07-29).

    +40 .. +100   BULLISH    buy 2-step ITM CE
    -40 .. -100   BEARISH    buy 2-step ITM PE
    -30 .. +30    SIDEWAYS   sell ATM MOVE when 5m ADX is below 35
    all other gaps HOLD      no new action

**The gaps are deliberate, not an oversight in the spec.** The only neutral
entry range is ``-30 <= score <= +30``. Scores between 30 and 40 (or -40 and
-30) are HOLD bands: entering a directional trade needs |score| >= 40, while
an open position is kept rather than churned through an inferred intermediate
trade. This prevents a score oscillating around a boundary from paying both
spreads on consecutive candles.

Two deliberate differences from the legacy ``trend_score_auto.score_zone``,
both of which change real behaviour and are called out rather than absorbed
silently:

1. **Legacy PE is 3 steps ITM (`PE_3_ITM`), this is 2** (`PE_2_ITM`), per the
   spec. Legacy was asymmetric — CE at ATM-2, PE at ATM+3. This is symmetric.
2. **Legacy switches hard at |25|.** It will disagree with this model for
   every non-action gap. This model uses the current closed 5m score plus a
   calm 5m ADX reading for the SHORT_MOVE policy.

A "step" is an index offset in the expiry's sorted strike list, matching
``trend_score_auto.select_policy_contract``: ITM for a call is a *lower*
strike (ATM index - n), ITM for a put is a *higher* strike (ATM index + n).
"""

from __future__ import annotations

from dataclasses import dataclass

# Zone identifiers. CE_2_ITM and SHORT_MOVE deliberately reuse the legacy
# spellings so shadow comparison can compare them without a translation
# table; PE_2_ITM is new precisely because the strike policy differs.
CE_2_ITM = "CE_2_ITM"
PE_2_ITM = "PE_2_ITM"
SHORT_MOVE = "SHORT_MOVE"
HOLD = "HOLD"

ZONES = frozenset({CE_2_ITM, PE_2_ITM, SHORT_MOVE, HOLD})

# Regimes that block every action regardless of score. RANGE is NOT here:
# under this spec a sideways market is not "no trade", it is the MOVE setup.
# HIGH_VOL_SHOCK and LOW_LIQUIDITY remain blocking for all three actions —
# selling MOVE into a volatility shock is the worst possible time to be short
# volatility, so the sideways zone is if anything *more* sensitive to it.
UNSAFE_REGIMES = frozenset({"HIGH_VOL_SHOCK", "LOW_LIQUIDITY", "DEGRADED"})

# A score can be high because a fast component is moving while the 15-minute
# structure says the opposite.  Treating that disagreement as a directional
# entry is exactly the kind of model conflict a rules engine should surface,
# not average away.  RANGE remains valid only for the SHORT_MOVE policy.
_DIRECTIONAL_REGIMES = {
    CE_2_ITM: frozenset({"TREND_UP", "BREAKOUT_UP"}),
    PE_2_ITM: frozenset({"TREND_DOWN", "BREAKOUT_DOWN"}),
}


@dataclass(frozen=True, slots=True)
class ZonePolicy:
    directional_entry_abs: float = 40.0
    sideways_max_abs: float = 30.0

    def __post_init__(self) -> None:
        if self.sideways_max_abs >= self.directional_entry_abs:
            raise ValueError(
                "sideways_max_abs must be below directional_entry_abs, or the "
                "hold band inverts and the zones overlap")


@dataclass(frozen=True, slots=True)
class ZoneDecision:
    zone: str
    action_allowed: bool
    reason: str
    option_type: str | None = None
    itm_steps: int | None = None


def zone_for_score(score: float, policy: ZonePolicy | None = None) -> str:
    """Pure score -> zone. No hysteresis state; HOLD marks the dead band."""
    policy = policy or ZonePolicy()
    if score >= policy.directional_entry_abs:
        return CE_2_ITM
    if score <= -policy.directional_entry_abs:
        return PE_2_ITM
    if abs(score) <= policy.sideways_max_abs:
        return SHORT_MOVE
    return HOLD


def directional_regime_matches(zone: str, regime: str) -> bool:
    """Whether a directional zone agrees with the classified structure.

    Non-directional zones deliberately return ``True``: their own gates carry
    the relevant safety policy and this helper must never turn a HOLD/MOVE
    condition into a fabricated directional opinion.
    """
    permitted = _DIRECTIONAL_REGIMES.get(zone)
    return True if permitted is None else regime in permitted


def decide(
    *,
    score: float,
    regime: str,
    data_quality: str,
    gates_passed: bool,
    stop_loss_configured: bool = True,
    short_move_calm: bool = True,
    policy: ZonePolicy | None = None,
) -> ZoneDecision:
    """Zone plus whether its action may actually be taken.

    Selling ATM MOVE is default behaviour (operator decision 2026-07-26), so
    there is no ``ALLOW_SHORT_MOVE`` opt-in any more. The stop loss replaces
    it as the control: a short straddle's loss is unbounded in a large move,
    so ``stop_loss_configured=False`` blocks the sideways zone outright. That
    is not a policy preference — an unstopped short straddle is the one
    position in this system that can lose more than the account holds.
    """
    zone = zone_for_score(score, policy)

    if data_quality != "OK":
        return ZoneDecision(zone, False, f"data quality is {data_quality}")
    if regime in UNSAFE_REGIMES:
        return ZoneDecision(zone, False, f"regime {regime} blocks all entries")
    if zone == HOLD:
        policy = policy or ZonePolicy()
        return ZoneDecision(
            zone, False,
            f"|score| {abs(score):.1f} is inside the "
            f"{policy.sideways_max_abs:g}-{policy.directional_entry_abs:g} hold band")
    if zone == SHORT_MOVE:
        if not short_move_calm:
            return ZoneDecision(
                zone, False,
                "ADX is not below 35; calm-market confirmation is required "
                "before selling MOVE")
        if not stop_loss_configured:
            return ZoneDecision(
                zone, False,
                "refusing to sell MOVE with no stop loss configured "
                "(unbounded loss)")
        if not gates_passed:
            return ZoneDecision(zone, False, "one or more execution gates failed")
        return ZoneDecision(
            zone, True,
            "calm 5-minute ADX confirms SHORT_MOVE: sell ATM MOVE (stop required)")
    if not gates_passed:
        return ZoneDecision(zone, False, "one or more execution gates failed")
    if zone == CE_2_ITM:
        return ZoneDecision(zone, True, "bullish: buy 2-step ITM CE",
                            option_type="CE", itm_steps=2)
    return ZoneDecision(zone, True, "bearish: buy 2-step ITM PE",
                        option_type="PE", itm_steps=2)


def should_exit(
    open_zone: str,
    current_zone: str,
    *,
    score: float | None = None,
    policy: ZonePolicy | None = None,
) -> tuple[bool, str]:
    """Signal-driven exit rule (operator spec 2026-07-26).

    A position is normally closed on a **zone change only** — never part-way
    through a zone because the score drifted within it.  The exception is a
    directional invalidation: a CE is no longer defensible once the score has
    crossed below the *opposite* neutral boundary, and a PE is no longer
    defensible once it has crossed above it.  In that case the HOLD band closes
    the old directional position but does not open a replacement.  This keeps
    the anti-churn band while avoiding a long CE being held at (say) -30.

    Protective exits (SL / TSL / TP) are handled by ``tp_monitor.py`` and are
    deliberately outside this function: they act on price, continuously, and
    must not be gated on a candle close or on the signal engine being healthy.
    """
    if open_zone == current_zone:
        return False, "still in the entry zone"
    if current_zone == HOLD:
        active_policy = policy or ZonePolicy()
        if score is not None:
            if open_zone == CE_2_ITM and score < -active_policy.sideways_max_abs:
                return True, (
                    "bullish position invalidated: score crossed below the "
                    "opposite neutral boundary")
            if open_zone == PE_2_ITM and score > active_policy.sideways_max_abs:
                return True, (
                    "bearish position invalidated: score crossed above the "
                    "opposite neutral boundary")
        return False, "hold band is not a zone change; position is kept"
    return True, f"zone changed {open_zone} -> {current_zone}"


def strike_index_offset(zone: str) -> int | None:
    """Offset from the ATM index in the expiry's sorted strike list.

    Negative for calls (ITM call = lower strike), positive for puts. Returns
    None for zones that do not select a vanilla option.
    """
    if zone == CE_2_ITM:
        return -2
    if zone == PE_2_ITM:
        return +2
    return None
