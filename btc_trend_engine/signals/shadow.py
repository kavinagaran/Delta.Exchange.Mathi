"""Shadow comparison: legacy decision vs this engine's, per closed candle
(plan Phase 8a).

Pure functions over two decisions. The engine records what both said and why
they differed; it does not judge which was right — that requires the outcome,
which arrives later and is the cutover analysis's job, not this module's.

The classification below is the thing the 30–45 day shadow window actually
produces, so it is deliberately explicit: every disagreement gets a named
class, because "70% agreement" is worthless without knowing what the other
30% consists of. A disagreement because the engine's data was degraded is a
completely different fact from a disagreement on direction with both feeds
healthy.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

# Disagreement classes (§Phase 8 "every disagreement class explained").
AGREE = "agree"
ENGINE_MISSING = "engine_no_snapshot"
ENGINE_DEGRADED = "engine_data_degraded"
ENGINE_GATED = "engine_gated_entry"
DIRECTION_OPPOSED = "direction_opposed"
ENGINE_FLAT_LEGACY_DIRECTIONAL = "engine_flat_legacy_directional"
ENGINE_DIRECTIONAL_LEGACY_FLAT = "engine_directional_legacy_flat"

# Zone-level classes (2026-07-26 spec). Kept separate from the direction
# classes above so the summary can report both views without conflating them.
ZONE_OPPOSED = "zone_opposed"
ZONE_SAME_SIDE_DIFFERENT_STRIKE = "zone_same_side_different_strike"
ZONE_ENGINE_HOLDS = "zone_engine_holds_legacy_acts"

DISAGREEMENT_CLASSES = (
    ENGINE_MISSING, ENGINE_DEGRADED, ENGINE_GATED, DIRECTION_OPPOSED,
    ENGINE_FLAT_LEGACY_DIRECTIONAL, ENGINE_DIRECTIONAL_LEGACY_FLAT,
    ZONE_OPPOSED, ZONE_SAME_SIDE_DIFFERENT_STRIKE, ZONE_ENGINE_HOLDS,
)


def zone_agreement(legacy_zone: str, engine_zone: str | None) -> tuple[bool | None, str]:
    """Compare the *action* each engine would take, not just its direction.

    Direction agreement hides the two changes that matter most under the
    2026-07-26 zone spec: legacy buys a 3-step ITM put where this engine buys
    a 2-step one (same direction, different instrument), and legacy takes a
    directional trade in 25<|score|<35 where this engine holds flat (same
    "not sideways", opposite position).
    """
    if not engine_zone:
        return None, ENGINE_MISSING
    if engine_zone == "HOLD":
        return False, ZONE_ENGINE_HOLDS
    if legacy_zone == engine_zone:
        return True, AGREE
    # PE_3_ITM vs PE_2_ITM is the same directional call at a different strike.
    if legacy_zone.startswith("PE") and engine_zone.startswith("PE"):
        return False, ZONE_SAME_SIDE_DIFFERENT_STRIKE
    if legacy_zone.startswith("CE") and engine_zone.startswith("CE"):
        return False, ZONE_SAME_SIDE_DIFFERENT_STRIKE
    return False, ZONE_OPPOSED


def classify(legacy_direction: int,
             engine_snapshot: Mapping[str, Any] | None) -> tuple[bool | None, str]:
    """Return ``(agreed, reason)``.

    ``agreed`` is None — not False — when the engine had nothing to say. An
    absent snapshot is not a disagreement, and counting it as one would
    understate the agreement rate that gates cutover.
    """
    if engine_snapshot is None:
        return None, ENGINE_MISSING

    if str(engine_snapshot.get("data_quality")) != "OK":
        return None, ENGINE_DEGRADED

    engine_direction = int(engine_snapshot.get("direction") or 0)
    if engine_direction == legacy_direction:
        return True, AGREE

    if engine_direction != 0 and legacy_direction != 0:
        return False, DIRECTION_OPPOSED
    if engine_direction == 0 and legacy_direction != 0:
        # The engine may be flat because a gate blocked it rather than because
        # it read the market differently — a materially different reason.
        if not engine_snapshot.get("entry_allowed", False):
            return False, ENGINE_GATED
        return False, ENGINE_FLAT_LEGACY_DIRECTIONAL
    return False, ENGINE_DIRECTIONAL_LEGACY_FLAT


def summarise(rows: Sequence[Any]) -> dict[str, Any]:
    """Agreement rate, confusion matrix and disagreement histogram.

    The agreement rate denominator is *comparable* candles only — those where
    both sides actually produced a decision. Rows where the engine had no
    snapshot or was degraded are reported separately and counted toward the
    "engine had an opinion at all" coverage figure instead.
    """
    total = len(rows)
    comparable = [r for r in rows if r.agreed is not None]
    agreed = [r for r in comparable if r.agreed]
    histogram: dict[str, int] = {}
    for row in rows:
        reason = row.disagreement_reason or AGREE
        histogram[reason] = histogram.get(reason, 0) + 1

    # Confusion matrix over directions, comparable rows only.
    matrix: dict[str, int] = {}
    for row in comparable:
        key = f"legacy{_sign(row.legacy_direction)}_engine{_sign(row.engine_direction)}"
        matrix[key] = matrix.get(key, 0) + 1

    entry_eligible = sum(
        1 for r in rows if r.engine_present and r.engine_entry_allowed)

    return {
        "total_candles": total,
        "comparable_candles": len(comparable),
        "agreements": len(agreed),
        "agreement_rate": (len(agreed) / len(comparable)) if comparable else None,
        "engine_coverage": (len(comparable) / total) if total else None,
        "engine_entry_eligible_signals": entry_eligible,
        "disagreement_histogram": dict(sorted(histogram.items())),
        "confusion_matrix": dict(sorted(matrix.items())),
        "first_candle_utc": rows[0].candle_close_utc if rows else None,
        "last_candle_utc": rows[-1].candle_close_utc if rows else None,
    }


def _sign(value: int | None) -> str:
    if value is None:
        return "?"
    return "+1" if value > 0 else "-1" if value < 0 else "0"
