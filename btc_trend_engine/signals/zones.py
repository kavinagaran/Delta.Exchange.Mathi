"""Score -> action zone mapping (operator spec, 2026-07-26).

    +35 .. +100   BULLISH    buy 2-step ITM CE
    -35 .. -100   BEARISH    buy 2-step ITM PE
    -15 .. +15    SIDEWAYS   sell ATM MOVE after 30 minutes of confirmation
    all other gaps HOLD      no new action

**The gaps are deliberate, not an oversight in the spec.** The only neutral
entry range is ``-15 <= score <= +15``. Scores between 15 and 35 (or -35 and
-15) are HOLD bands: entering a directional trade needs |score| >= 35, while
an open position is kept rather than churned through an inferred intermediate
trade. This prevents a score oscillating around a boundary from paying both
spreads on consecutive candles.

Two deliberate differences from the legacy ``trend_score_auto.score_zone``,
both of which change real behaviour and are called out rather than absorbed
silently:

1. **Legacy PE is 3 steps ITM (`PE_3_ITM`), this is 2** (`PE_2_ITM`), per the
   spec. Legacy was asymmetric — CE at ATM-2, PE at ATM+3. This is symmetric.
2. **Legacy has no 30-minute confirmation.** It switches directional/MOVE hard
   at |25|, so legacy will disagree with this model for every non-action gap
   and until the new neutral-range confirmation completes.

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

# One spelling, because `decide` reports it from two places and a consumer
# matching on the text must not have to know which.
_GATES_FAILED = "one or more execution gates failed"

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
    directional_entry_abs: float = 35.0
    sideways_max_abs: float = 15.0
    # Ceiling on the WIDEST single component, not on their weighted sum.
    #
    # The sum is what `sideways_max_abs` bounds, and a sum near zero can mean
    # either "nothing is happening" or "large opposing readings cancelled".
    # Only the first is a sideways market; the second is a market whose
    # timeframes disagree, which is among the worst conditions to be short a
    # straddle in.
    #
    # 50 is measured, not asserted.  research/information_coefficient.
    # sideways_gate_profile over 60,422 bars of BTCUSD 5m history gives median
    # forward excursion, relative to all bars, for the gate plus each ceiling:
    #
    #     ceiling   30m     60m    share of bars
    #     (none)    0.89x   0.88x      9.6%
    #     50        0.85x   0.86x      6.0%
    #     35        0.85x   0.85x      3.4%
    #     25        0.82x   0.84x      1.8%
    #
    # 50 captures effectively all of the improvement that 35 does while
    # leaving ~78% more bars eligible, and the 50/35/25 spread is inside the
    # noise of an overlapping sample.  Two limits on how much this buys:
    # the effect is a 30-60m one (it vanishes by 120m and reverses by 240m),
    # and p90 excursion -- the tail a short straddle actually dies in -- barely
    # moves at any ceiling.  This bounds a failure mode; it is not an edge.
    sideways_max_component_abs: float = 50.0

    def __post_init__(self) -> None:
        if self.sideways_max_abs >= self.directional_entry_abs:
            raise ValueError(
                "sideways_max_abs must be below directional_entry_abs, or the "
                "hold band inverts and the zones overlap")
        if self.sideways_max_component_abs <= 0:
            raise ValueError(
                "sideways_max_component_abs must be positive, or the sideways "
                "zone can never act")


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
    short_move_confirmed: bool = False,
    max_abs_component: float | None = None,
    policy: ZonePolicy | None = None,
) -> ZoneDecision:
    """Zone plus whether its action may actually be taken.

    Selling ATM MOVE is default behaviour (operator decision 2026-07-26), so
    there is no ``ALLOW_SHORT_MOVE`` opt-in any more. The stop loss replaces
    it as the control: a short straddle's loss is unbounded in a large move,
    so ``stop_loss_configured=False`` blocks the sideways zone outright. That
    is not a policy preference — an unstopped short straddle is the one
    position in this system that can lose more than the account holds.

    ``max_abs_component`` (``ScoreResult.max_abs_component``) is required for
    the sideways zone and **omitting it blocks the action**.  A near-zero score
    is only evidence of a sideways market once the components behind it are
    known to be individually small, so treating "not measured" as "calm" would
    fail open on exactly the reading this gate exists to catch.  The
    directional zones do not use it: there, large components are the point.
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
        active = policy or ZonePolicy()
        # These two run BEFORE the generic gate check on purpose. Both are
        # also rows in the caller's zone gate matrix (snapshot.
        # _zone_entry_gates), so either failing also drives `gates_passed`
        # false -- and answering "one or more execution gates failed" would
        # discard the only description of *why* that ever leaves the engine.
        # The consumer routes on this string: dashboard.py decides
        # "awaiting confirmation" versus "blocked", and releases the previous
        # setup lock, by reading it.
        #
        # Disagreement is reported ahead of the confirmation window because
        # "waiting for confirmation" implies waiting will resolve it, when
        # what is needed is for the disagreement itself to go away.
        if max_abs_component is None:
            return ZoneDecision(
                zone, False,
                "component agreement was not measured; refusing to sell MOVE "
                "on an unverified neutral score")
        if max_abs_component > active.sideways_max_component_abs:
            return ZoneDecision(
                zone, False,
                f"components disagree: the widest reads "
                f"{max_abs_component:.1f}, beyond the "
                f"{active.sideways_max_component_abs:g} ceiling. A near-zero "
                f"score here is cancellation, not a sideways market")
        if not short_move_confirmed:
            return ZoneDecision(
                zone, False,
                "waiting for 30-minute confirmation: six consecutive "
                "completed 5-minute scores must remain inside -15 to +15")
        # Any OTHER failing gate -- a stale feed, an invalid book -- still
        # reports generically; only the two above describe themselves.
        if not gates_passed:
            return ZoneDecision(zone, False, _GATES_FAILED)
        if not stop_loss_configured:
            return ZoneDecision(
                zone, False,
                "refusing to sell MOVE with no stop loss configured "
                "(unbounded loss)")
        return ZoneDecision(
            zone, True,
            "sideways confirmed for 30 minutes: sell ATM MOVE (stop required)")
    if not gates_passed:
        return ZoneDecision(zone, False, _GATES_FAILED)
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
