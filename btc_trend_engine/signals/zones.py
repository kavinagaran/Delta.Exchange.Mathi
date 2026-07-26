"""Score -> action zone mapping (operator spec, 2026-07-26).

    +35 .. +100   BULLISH    buy 2-step ITM CE
    -35 .. -100   BEARISH    buy 2-step ITM PE
    -25 .. +25    SIDEWAYS   sell ATM MOVE
    the two gaps   HOLD      no new action

**The gaps are deliberate, not an oversight in the spec.** 25 < |score| < 35
is a hysteresis band: entering a directional trade needs |score| >= 35, but
an open one is only given up once |score| decays to <= 25. Without it a score
oscillating around a single threshold would thrash between "buy CE" and "sell
MOVE" on consecutive candles, paying both spreads each time. This mirrors the
entry/hold split the engine already uses for ``direction``.

Two deliberate differences from the legacy ``trend_score_auto.score_zone``,
both of which change real behaviour and are called out rather than absorbed
silently:

1. **Legacy PE is 3 steps ITM (`PE_3_ITM`), this is 2** (`PE_2_ITM`), per the
   spec. Legacy was asymmetric — CE at ATM-2, PE at ATM+3. This is symmetric.
2. **Legacy has no hold band.** It switches directional/MOVE hard at |25|, so
   legacy will disagree with this model for any |score| in (25, 35) — expected,
   and visible in shadow comparison rather than hidden.

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


@dataclass(frozen=True, slots=True)
class ZonePolicy:
    directional_entry_abs: float = 35.0
    sideways_max_abs: float = 25.0

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


def decide(
    *,
    score: float,
    regime: str,
    data_quality: str,
    gates_passed: bool,
    stop_loss_configured: bool = True,
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
    if not gates_passed:
        return ZoneDecision(zone, False, "one or more execution gates failed")
    if zone == SHORT_MOVE:
        if not stop_loss_configured:
            return ZoneDecision(
                zone, False,
                "refusing to sell MOVE with no stop loss configured "
                "(unbounded loss)")
        return ZoneDecision(zone, True, "sideways: sell ATM MOVE (stop required)")
    if zone == CE_2_ITM:
        return ZoneDecision(zone, True, "bullish: buy 2-step ITM CE",
                            option_type="CE", itm_steps=2)
    return ZoneDecision(zone, True, "bearish: buy 2-step ITM PE",
                        option_type="PE", itm_steps=2)


def should_exit(open_zone: str, current_zone: str) -> tuple[bool, str]:
    """Signal-driven exit rule (operator spec 2026-07-26).

    A position is closed on a **zone change only** — never part-way through a
    zone because the score drifted within it. Protective exits (SL / TSL / TP)
    are handled by ``tp_monitor.py`` and are deliberately outside this
    function: they act on price, continuously, and must not be gated on a
    candle close or on the signal engine being healthy.

    HOLD is not a zone change. The hold band exists precisely so a score
    oscillating around a boundary does not close and reopen a position; if
    HOLD forced an exit, the band would cause the churn it was added to
    prevent.
    """
    if open_zone == current_zone:
        return False, "still in the entry zone"
    if current_zone == HOLD:
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
