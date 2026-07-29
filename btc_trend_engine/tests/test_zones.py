"""Score -> action zone mapping against the operator spec (2026-07-29):

    +40..+100 bullish CE 2-step ITM · -40..-100 bearish PE 2-step ITM
    -30..+30 sideways sell ATM MOVE with 5m ADX below 35 · all other gaps HOLD
"""

from __future__ import annotations

import pytest

from btc_trend_engine.signals import zones
from btc_trend_engine.signals.zones import ZonePolicy


def _decide(score, **kw):
    params = dict(score=score, regime="TREND_UP", data_quality="OK",
                  gates_passed=True, stop_loss_configured=True)
    params.update(kw)
    return zones.decide(**params)


# ── the three specified bands, including their exact boundaries ─────────
@pytest.mark.parametrize("score", [40.0, 40.1, 65.0, 99.9, 100.0])
def test_bullish_band_buys_two_step_itm_ce(score):
    decision = _decide(score)
    assert decision.zone == zones.CE_2_ITM
    assert decision.option_type == "CE" and decision.itm_steps == 2
    assert zones.strike_index_offset(decision.zone) == -2


@pytest.mark.parametrize("score", [-40.0, -40.1, -65.0, -100.0])
def test_bearish_band_buys_two_step_itm_pe(score):
    decision = _decide(score)
    assert decision.zone == zones.PE_2_ITM
    assert decision.option_type == "PE" and decision.itm_steps == 2
    # ITM for a put is a HIGHER strike -- opposite sign to the call.
    assert zones.strike_index_offset(decision.zone) == +2


@pytest.mark.parametrize("score", [-30.0, -29.9, 0.0, 12.0, 29.9, 30.0])
def test_sideways_band_sells_atm_move(score):
    decision = _decide(score)
    assert decision.zone == zones.SHORT_MOVE
    # Selling a straddle picks no vanilla strike.
    assert decision.option_type is None
    assert zones.strike_index_offset(decision.zone) is None


@pytest.mark.parametrize("score", [30.1, 35.0, 39.9, -30.1, -35.0, -39.9])
def test_the_gap_between_the_bands_is_a_hold_not_an_action(score):
    """The spec makes 30<|s|<40 HOLD; treating it as either neighbour
    would make the engine thrash across a single threshold."""
    decision = _decide(score)
    assert decision.zone == zones.HOLD
    assert decision.action_allowed is False
    assert "hold band" in decision.reason


def test_the_boundaries_are_inclusive_exactly_as_written():
    assert zones.zone_for_score(40.0) == zones.CE_2_ITM
    assert zones.zone_for_score(-40.0) == zones.PE_2_ITM
    assert zones.zone_for_score(30.0) == zones.SHORT_MOVE
    assert zones.zone_for_score(-30.0) == zones.SHORT_MOVE


def test_every_score_in_range_maps_to_exactly_one_known_zone():
    for tenth in range(-1000, 1001):
        zone = zones.zone_for_score(tenth / 10.0)
        assert zone in zones.ZONES


# ── safety gating ───────────────────────────────────────────────────────
def test_selling_move_is_now_default_behaviour():
    """Operator decision 2026-07-26: MOVE selling is always on, no opt-in."""
    assert zones.decide(score=0.0, regime="RANGE", data_quality="OK",
                        gates_passed=True).action_allowed is True


def test_selling_move_is_refused_without_a_stop_loss():
    """The stop replaces ALLOW_SHORT_MOVE as the control. An unstopped short
    straddle is the only position here that can lose more than the account."""
    decision = zones.decide(score=0.0, regime="RANGE", data_quality="OK",
                            gates_passed=True, stop_loss_configured=False)
    assert decision.action_allowed is False
    assert "stop loss" in decision.reason


def test_selling_move_is_refused_when_adx_does_not_confirm_calm():
    decision = zones.decide(score=0.0, regime="RANGE", data_quality="OK",
                            gates_passed=True, short_move_calm=False)
    assert decision.action_allowed is False
    assert "ADX is not below 35" in decision.reason


def test_a_missing_stop_does_not_block_the_directional_zones():
    """Long options have bounded loss (the premium), so the stop requirement
    is specific to the short-vol leg."""
    for score in (85.0, -85.0):
        assert zones.decide(score=score, regime="TREND_UP", data_quality="OK",
                            gates_passed=True,
                            stop_loss_configured=False).action_allowed is True


def test_range_does_not_block_the_move_action():
    """RANGE is non-tradeable for directional entries, but under this spec a
    sideways market IS the sell-MOVE setup -- blocking it would make the
    sideways band unreachable."""
    assert _decide(0.0, regime="RANGE").action_allowed is True


@pytest.mark.parametrize("regime", ["HIGH_VOL_SHOCK", "LOW_LIQUIDITY", "DEGRADED"])
@pytest.mark.parametrize("score", [85.0, 0.0, -85.0])
def test_unsafe_regimes_block_every_zone_including_sideways(regime, score):
    """Selling MOVE into a volatility shock is the worst possible moment to
    be short volatility, so the sideways band gets no exemption."""
    assert _decide(score, regime=regime).action_allowed is False


@pytest.mark.parametrize("score", [85.0, 0.0, -85.0])
def test_degraded_data_blocks_every_zone(score):
    assert _decide(score, data_quality="STALE_L1").action_allowed is False


@pytest.mark.parametrize("score", [85.0, 0.0, -85.0])
def test_a_failed_gate_blocks_every_zone(score):
    assert _decide(score, gates_passed=False).action_allowed is False


def test_the_zone_is_still_reported_when_the_action_is_blocked():
    """Blocking must not erase what the engine thought -- 'bullish but
    blocked' and 'not bullish' are different facts."""
    decision = _decide(85.0, data_quality="STALE_L1")
    assert decision.zone == zones.CE_2_ITM
    assert decision.action_allowed is False


# ── policy validation ───────────────────────────────────────────────────
def test_overlapping_thresholds_are_rejected_rather_than_silently_inverted():
    with pytest.raises(ValueError):
        ZonePolicy(directional_entry_abs=25.0, sideways_max_abs=35.0)


def test_a_custom_policy_moves_both_boundaries():
    policy = ZonePolicy(directional_entry_abs=60.0, sideways_max_abs=20.0)
    assert zones.zone_for_score(50.0, policy) == zones.HOLD
    assert zones.zone_for_score(60.0, policy) == zones.CE_2_ITM
    assert zones.zone_for_score(20.0, policy) == zones.SHORT_MOVE


# ── divergence from legacy, stated explicitly ───────────────────────────
def test_this_model_differs_from_legacy_pe_strike_depth():
    """Legacy trend_score_auto uses PE_3_ITM (ATM+3); this spec says 2 steps.
    Pinned so the change is deliberate rather than drifted into."""
    from trend_score_auto import PE_3_ITM

    assert zones.PE_2_ITM != PE_3_ITM
    assert zones.strike_index_offset(zones.PE_2_ITM) == 2


def test_trend_score_auto_now_delegates_here_so_there_is_one_source_of_truth():
    """``trend_score_auto.score_zone`` used to own its own thresholds — a
    hard switch at |25| with no hold band, and PE_3_ITM for puts. It now
    delegates to this module, so the old divergence is gone by construction
    rather than by both copies happening to be edited together.
    """
    from trend_score_auto import score_zone as legacy_entry_point

    for score in (-100, -50, -40, -35, -30, 0, 30, 35, 40, 50, 100):
        assert legacy_entry_point(score) == zones.zone_for_score(float(score))

    # Specifically: all scores from 30 to 40 are HOLD (the neutral entry
    # band is +/-30), and puts are 2-step rather than 3-step ITM.
    assert legacy_entry_point(35) == zones.HOLD
    assert legacy_entry_point(-35) == zones.HOLD
    assert legacy_entry_point(-50) == zones.PE_2_ITM


# ── zone-level shadow comparison ────────────────────────────────────────
def test_zone_agreement_flags_the_pe_strike_difference_as_a_disagreement():
    """Same direction, different instrument. Direction-only comparison would
    score this as agreement and hide a real execution difference."""
    from btc_trend_engine.signals import shadow

    agreed, reason = shadow.zone_agreement("PE_3_ITM", "PE_2_ITM")
    assert agreed is False
    assert reason == shadow.ZONE_SAME_SIDE_DIFFERENT_STRIKE


def test_zone_agreement_flags_the_hold_band_divergence():
    from btc_trend_engine.signals import shadow

    agreed, reason = shadow.zone_agreement("CE_2_ITM", "HOLD")
    assert agreed is False
    assert reason == shadow.ZONE_ENGINE_HOLDS


def test_zone_agreement_matches_on_identical_zones():
    from btc_trend_engine.signals import shadow

    assert shadow.zone_agreement("CE_2_ITM", "CE_2_ITM") == (True, shadow.AGREE)
    assert shadow.zone_agreement("SHORT_MOVE", "SHORT_MOVE") == (True, shadow.AGREE)


def test_zone_agreement_reports_a_missing_engine_zone_as_incomparable():
    from btc_trend_engine.signals import shadow

    agreed, reason = shadow.zone_agreement("CE_2_ITM", None)
    assert agreed is None and reason == shadow.ENGINE_MISSING


def test_directional_versus_sideways_is_a_full_opposition():
    from btc_trend_engine.signals import shadow

    agreed, reason = shadow.zone_agreement("CE_2_ITM", "SHORT_MOVE")
    assert agreed is False and reason == shadow.ZONE_OPPOSED


# ── exit rule: zone change only, never mid-zone ─────────────────────────
def test_no_exit_while_still_in_the_entry_zone():
    assert zones.should_exit("CE_2_ITM", "CE_2_ITM")[0] is False


def test_a_real_zone_change_exits():
    assert zones.should_exit("CE_2_ITM", "PE_2_ITM")[0] is True
    assert zones.should_exit("CE_2_ITM", "SHORT_MOVE")[0] is True
    assert zones.should_exit("SHORT_MOVE", "CE_2_ITM")[0] is True


def test_drifting_into_the_hold_band_does_not_exit():
    """If HOLD forced an exit, the hysteresis band would cause exactly the
    churn it exists to prevent."""
    for held in ("CE_2_ITM", "PE_2_ITM", "SHORT_MOVE"):
        exits, reason = zones.should_exit(held, "HOLD")
        assert exits is False
        assert "hold band" in reason


def test_opposite_hold_band_invalidates_an_existing_directional_position():
    """The dead band prevents churn, but cannot preserve a contradicted CE/PE.

    A call at -35 (or put at +35) is in HOLD rather than the opposite entry
    zone.  That must be an exit-only decision, not a stale position or a
    premature reversal.
    """
    assert zones.should_exit("CE_2_ITM", "HOLD", score=-35.0)[0] is True
    assert zones.should_exit("PE_2_ITM", "HOLD", score=35.0)[0] is True
    assert zones.should_exit("CE_2_ITM", "HOLD", score=35.0)[0] is False
    assert zones.should_exit("PE_2_ITM", "HOLD", score=-35.0)[0] is False


@pytest.mark.parametrize(
    ("zone", "regime", "expected"),
    [
        ("CE_2_ITM", "TREND_UP", True),
        ("CE_2_ITM", "BREAKOUT_UP", True),
        ("CE_2_ITM", "BREAKOUT_DOWN", False),
        ("PE_2_ITM", "TREND_DOWN", True),
        ("PE_2_ITM", "BREAKOUT_DOWN", True),
        ("PE_2_ITM", "TREND_UP", False),
        ("SHORT_MOVE", "RANGE", True),
    ],
)
def test_directional_regime_alignment_is_explicit(zone, regime, expected):
    assert zones.directional_regime_matches(zone, regime) is expected


def test_score_drift_within_a_zone_never_exits():
    """+40 -> +90 -> +41 is all one CE zone: no exit, no re-entry, no churn."""
    open_zone = zones.zone_for_score(40.0)
    for score in (90.0, 41.0, 100.0, 40.0):
        assert zones.should_exit(open_zone, zones.zone_for_score(score))[0] is False
