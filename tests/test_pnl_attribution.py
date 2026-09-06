"""Tests for the option P&L attribution module.

The load-bearing property is the accounting identity: the named greek
contributions plus the residual must equal the realised total, exactly, for
every input including the degenerate ones. If that ever breaks the table
silently lies about where money went, so it is asserted everywhere.
"""

import math

import pytest

from pnl_attribution import (
    CALL,
    PUT,
    STRADDLE,
    Observation,
    attribute_trade,
    bs_greeks,
    bs_price,
    implied_vol,
    summarise,
)

# A representative BTC option: spot 60k, 30 days out, 55% vol.
SPOT = 60_000.0
STRIKE = 60_000.0
T = 30.0 / 365.0
VOL = 0.55
CONTRACT_VALUE = 0.001      # Delta India BTC options: 1 lot = 0.001 BTC


# ── Black-Scholes correctness ───────────────────────────────────────────

def test_put_call_parity_holds():
    call = bs_price(CALL, SPOT, STRIKE, T, VOL)
    put = bs_price(PUT, SPOT, STRIKE, T, VOL)
    # r = 0, so parity reduces to C - P = S - K
    assert call - put == pytest.approx(SPOT - STRIKE, abs=1e-6)


def test_straddle_is_call_plus_put():
    straddle = bs_price(STRADDLE, SPOT, STRIKE, T, VOL)
    parts = (bs_price(CALL, SPOT, STRIKE, T, VOL)
             + bs_price(PUT, SPOT, STRIKE, T, VOL))
    assert straddle == pytest.approx(parts, rel=1e-12)


def test_call_price_is_monotonic_in_vol_and_bounded():
    cheap = bs_price(CALL, SPOT, STRIKE, T, 0.2)
    dear = bs_price(CALL, SPOT, STRIKE, T, 0.9)
    assert 0 < cheap < dear < SPOT


@pytest.mark.parametrize("kind", [CALL, PUT, STRADDLE])
def test_delta_matches_finite_difference(kind):
    h = 1.0
    analytic = bs_greeks(kind, SPOT, STRIKE, T, VOL).delta
    numeric = ((bs_price(kind, SPOT + h, STRIKE, T, VOL)
                - bs_price(kind, SPOT - h, STRIKE, T, VOL)) / (2 * h))
    assert analytic == pytest.approx(numeric, abs=1e-6)


@pytest.mark.parametrize("kind", [CALL, PUT, STRADDLE])
def test_gamma_and_vega_match_finite_difference(kind):
    greeks = bs_greeks(kind, SPOT, STRIKE, T, VOL)

    h = 1.0
    gamma_numeric = ((bs_price(kind, SPOT + h, STRIKE, T, VOL)
                      - 2 * bs_price(kind, SPOT, STRIKE, T, VOL)
                      + bs_price(kind, SPOT - h, STRIKE, T, VOL)) / (h * h))
    assert greeks.gamma == pytest.approx(gamma_numeric, rel=1e-4)

    dv = 1e-5
    vega_numeric = ((bs_price(kind, SPOT, STRIKE, T, VOL + dv)
                     - bs_price(kind, SPOT, STRIKE, T, VOL - dv)) / (2 * dv))
    assert greeks.vega == pytest.approx(vega_numeric, rel=1e-5)


@pytest.mark.parametrize("kind", [CALL, PUT, STRADDLE])
def test_theta_is_negative_for_long_premium_and_matches_decay(kind):
    greeks = bs_greeks(kind, SPOT, STRIKE, T, VOL)
    assert greeks.theta < 0

    dt = 1e-6
    # theta = dV/dt; advancing time shrinks time-to-expiry
    numeric = ((bs_price(kind, SPOT, STRIKE, T - dt, VOL)
                - bs_price(kind, SPOT, STRIKE, T + dt, VOL)) / (2 * dt))
    assert greeks.theta == pytest.approx(numeric, rel=1e-4)
    assert greeks.theta_per_day == pytest.approx(greeks.theta / 365.0)


def test_invalid_inputs_raise_rather_than_returning_nonsense():
    for bad in (
        dict(spot=-1.0, strike=STRIKE, t_years=T, sigma=VOL),
        dict(spot=SPOT, strike=0.0, t_years=T, sigma=VOL),
        dict(spot=SPOT, strike=STRIKE, t_years=0.0, sigma=VOL),
        dict(spot=SPOT, strike=STRIKE, t_years=T, sigma=0.0),
    ):
        with pytest.raises(ValueError):
            bs_price(CALL, **bad)
    with pytest.raises(ValueError):
        bs_price("swaption", SPOT, STRIKE, T, VOL)


# ── Implied vol inversion ───────────────────────────────────────────────

@pytest.mark.parametrize("kind", [CALL, PUT, STRADDLE])
@pytest.mark.parametrize("true_vol", [0.15, 0.55, 1.4])
def test_implied_vol_round_trips(kind, true_vol):
    price = bs_price(kind, SPOT, STRIKE, T, true_vol)
    assert implied_vol(kind, price, SPOT, STRIKE, T) == pytest.approx(
        true_vol, abs=1e-5)


def test_implied_vol_returns_none_below_intrinsic():
    # A call priced under intrinsic has no Black-Scholes solution.
    deep_itm_intrinsic = SPOT - 40_000.0
    assert implied_vol(CALL, deep_itm_intrinsic - 500, SPOT, 40_000.0, T) is None


def test_implied_vol_returns_none_for_degenerate_inputs():
    assert implied_vol(CALL, 0.0, SPOT, STRIKE, T) is None
    assert implied_vol(CALL, 100.0, SPOT, STRIKE, 0.0) is None
    assert implied_vol(CALL, 100.0, -1.0, STRIKE, T) is None


# ── Attribution: the accounting identity ────────────────────────────────

def _round_trip(kind=CALL, *, spot_out=SPOT, vol_out=VOL, days_held=1.0,
                direction="LONG", lots=1000.0, vol_in=VOL, fees=0.0):
    """Build a synthetic round trip priced consistently by Black-Scholes."""
    t_out = T - days_held / 365.0
    entry = Observation(underlying=SPOT, t_years=T,
                        option_price=bs_price(kind, SPOT, STRIKE, T, vol_in))
    exit_ = Observation(underlying=spot_out, t_years=t_out,
                        option_price=bs_price(kind, spot_out, STRIKE,
                                              t_out, vol_out))
    return attribute_trade(kind=kind, strike=STRIKE, entry=entry, exit=exit_,
                           lots=lots, contract_value=CONTRACT_VALUE,
                           direction=direction, fees_usd=fees)


@pytest.mark.parametrize("kind", [CALL, PUT, STRADDLE])
@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
@pytest.mark.parametrize("spot_out,vol_out", [
    (SPOT, VOL),               # nothing moved: pure theta
    (SPOT * 1.03, VOL),        # spot up
    (SPOT * 0.95, VOL),        # spot down
    (SPOT, VOL * 1.4),         # vol up
    (SPOT, VOL * 0.6),         # vol crush
    (SPOT * 1.06, VOL * 0.7),  # move with IV crush: the classic loser
])
def test_components_always_sum_to_total(kind, direction, spot_out, vol_out):
    attribution = _round_trip(kind, spot_out=spot_out, vol_out=vol_out,
                              direction=direction)
    assert attribution.explained + attribution.residual == pytest.approx(
        attribution.total, abs=1e-9)


def test_identity_holds_with_fees():
    attribution = _round_trip(fees=42.5)
    assert attribution.explained + attribution.residual == pytest.approx(
        attribution.total, abs=1e-9)


def test_total_reconciles_with_dashboard_pnl_formula():
    """Must match _reconstruct_trades_from_orders in dashboard.py exactly,
    or the attribution table and the Performance table disagree."""
    lots = 1000.0
    attribution = _round_trip(spot_out=SPOT * 1.02, lots=lots)
    entry_price = bs_price(CALL, SPOT, STRIKE, T, VOL)
    t_out = T - 1.0 / 365.0
    exit_price = bs_price(CALL, SPOT * 1.02, STRIKE, t_out, VOL)
    expected = (exit_price - entry_price) * CONTRACT_VALUE * lots * 1
    assert attribution.total == pytest.approx(expected, abs=1e-9)


# ── Attribution: does it identify the right cause? ──────────────────────

def test_pure_time_decay_is_attributed_to_theta():
    attribution = _round_trip(spot_out=SPOT, vol_out=VOL, days_held=3.0)
    assert attribution.total < 0                      # long premium bleeds
    assert attribution.theta < 0
    # theta is the dominant named term when nothing else changed
    assert abs(attribution.theta) > abs(attribution.delta)
    assert abs(attribution.theta) > abs(attribution.vega)


def test_pure_vol_crush_is_attributed_to_vega():
    attribution = _round_trip(spot_out=SPOT, vol_out=VOL * 0.6, days_held=0.5)
    assert attribution.vega < 0
    assert abs(attribution.vega) > abs(attribution.delta)


def test_directional_move_is_attributed_to_delta():
    attribution = _round_trip(spot_out=SPOT * 1.04, vol_out=VOL, days_held=0.5)
    assert attribution.delta > 0
    assert abs(attribution.delta) > abs(attribution.theta)


def test_right_direction_but_iv_crush_shows_the_conflict():
    """The failure mode this module exists to expose: the call was correct on
    direction and still lost, because vol collapsed."""
    attribution = _round_trip(spot_out=SPOT * 1.02, vol_out=VOL * 0.5,
                              days_held=2.0)
    assert attribution.delta > 0        # direction was right
    assert attribution.vega < 0         # but vol paid for it
    assert attribution.theta < 0


def test_short_position_flips_every_sign():
    long_leg = _round_trip(STRADDLE, spot_out=SPOT, days_held=2.0,
                           direction="LONG")
    short_leg = _round_trip(STRADDLE, spot_out=SPOT, days_held=2.0,
                            direction="SHORT")
    assert short_leg.total == pytest.approx(-long_leg.total, abs=1e-9)
    assert short_leg.theta == pytest.approx(-long_leg.theta, abs=1e-9)
    # A short straddle collects theta -- the reason the strategy sells MOVE.
    assert short_leg.theta > 0


def test_short_straddle_large_move_shows_negative_gamma():
    attribution = _round_trip(STRADDLE, spot_out=SPOT * 1.05,
                              days_held=1.0, direction="SHORT")
    assert attribution.gamma < 0


def test_gamma_term_is_second_order_and_signed_for_long_premium():
    small = _round_trip(spot_out=SPOT * 1.01, days_held=0.1)
    large = _round_trip(spot_out=SPOT * 1.04, days_held=0.1)
    assert small.gamma > 0 and large.gamma > 0
    # quadratic in dS: a 4x move produces far more than 4x the gamma term
    assert large.gamma > 8 * small.gamma


# ── Honest degradation ──────────────────────────────────────────────────

def test_uninvertible_price_reports_unreliable_and_attributes_nothing():
    """Rather than invent greeks, put the whole move in residual and say so."""
    entry = Observation(underlying=SPOT, t_years=T, option_price=1.0)
    # Exit price below intrinsic for this deep-ITM call: not invertible.
    exit_ = Observation(underlying=90_000.0, t_years=T / 2, option_price=1.0)
    attribution = attribute_trade(kind=CALL, strike=40_000.0, entry=entry,
                                  exit=exit_, lots=1000.0,
                                  contract_value=CONTRACT_VALUE,
                                  direction="LONG")
    assert attribution.reliable is False
    assert attribution.exit_iv is None
    assert attribution.delta == 0.0 and attribution.vega == 0.0
    assert attribution.residual == pytest.approx(attribution.total)
    assert any("implied vol" in n for n in attribution.notes)


@pytest.mark.parametrize("spot_out", [SPOT * 1.15, SPOT * 1.5, SPOT * 0.6])
def test_violent_moves_are_still_fully_explained(spot_out):
    """Sequential revaluation has no truncation error, so even an absurd
    move leaves nothing unexplained -- unlike the Taylor expansion this
    replaced, which degraded badly with move size."""
    attribution = _round_trip(spot_out=spot_out, days_held=1.0)
    assert attribution.reliable is True
    # Sub-cent on a multi-thousand-dollar P&L: this is the implied-vol
    # bisection tolerance, not a modelling gap.
    assert attribution.residual == pytest.approx(0.0, abs=1e-3)
    assert attribution.residual_fraction < 1e-6
    assert attribution.explained + attribution.residual == pytest.approx(
        attribution.total, abs=1e-9)


def test_residual_is_zero_when_marks_are_consistent():
    """The property that makes `residual` meaningful: with no fees it is
    zero, so any non-zero value points at the marks, not the model."""
    attribution = _round_trip(spot_out=SPOT * 1.03, vol_out=VOL * 0.7,
                              days_held=2.0)
    assert attribution.residual == pytest.approx(0.0, abs=1e-3)


def test_residual_equals_fees_when_fees_are_charged():
    attribution = _round_trip(spot_out=SPOT * 1.02, fees=37.5)
    assert attribution.residual == pytest.approx(-37.5, abs=1e-3)


def test_inconsistent_marks_are_flagged_as_a_data_quality_problem():
    """An off-market exit print cannot be explained by any risk factor."""
    entry = Observation(underlying=SPOT, t_years=T,
                        option_price=bs_price(CALL, SPOT, STRIKE, T, VOL))
    t_out = T - 1.0 / 365.0
    fair = bs_price(CALL, SPOT, STRIKE, t_out, VOL)
    exit_ = Observation(underlying=SPOT, t_years=t_out,
                        option_price=fair * 1.5)   # crossed / stale print
    attribution = attribute_trade(kind=CALL, strike=STRIKE, entry=entry,
                                  exit=exit_, lots=1000.0,
                                  contract_value=CONTRACT_VALUE,
                                  direction="LONG")
    # It still reconciles -- the vol step absorbs a repriceable difference --
    # but the implied vol jump it implies is the tell.
    assert attribution.explained + attribution.residual == pytest.approx(
        attribution.total, abs=1e-9)
    assert attribution.exit_iv > attribution.entry_iv * 1.2


def test_expiry_close_is_reported_not_decomposed():
    entry = Observation(underlying=SPOT, t_years=T,
                        option_price=bs_price(CALL, SPOT, STRIKE, T, VOL))
    exit_ = Observation(underlying=SPOT * 1.05, t_years=0.0,
                        option_price=3000.0)
    attribution = attribute_trade(kind=CALL, strike=STRIKE, entry=entry,
                                  exit=exit_, lots=1000.0,
                                  contract_value=CONTRACT_VALUE,
                                  direction="LONG")
    assert attribution.reliable is False
    assert attribution.residual == pytest.approx(attribution.total)
    assert any("expiry" in n for n in attribution.notes)


def test_bad_direction_and_sizes_raise():
    entry = Observation(underlying=SPOT, t_years=T, option_price=100.0)
    exit_ = Observation(underlying=SPOT, t_years=T / 2, option_price=90.0)
    common = dict(kind=CALL, strike=STRIKE, entry=entry, exit=exit_,
                  contract_value=CONTRACT_VALUE)
    with pytest.raises(ValueError):
        attribute_trade(**common, lots=1000.0, direction="FLAT")
    with pytest.raises(ValueError):
        attribute_trade(**common, lots=0.0, direction="LONG")


def test_residual_fraction_is_negligible_for_a_gentle_move():
    attribution = _round_trip(spot_out=SPOT * 1.005, days_held=0.25)
    assert attribution.reliable is True
    assert attribution.residual_fraction < 1e-6


# ── Aggregation ─────────────────────────────────────────────────────────

def test_summarise_totals_each_leg():
    trades = [
        _round_trip(spot_out=SPOT * 1.02, days_held=1.0),
        _round_trip(spot_out=SPOT * 0.98, days_held=2.0),
        _round_trip(STRADDLE, spot_out=SPOT, days_held=1.0, direction="SHORT"),
    ]
    summary = summarise(trades)
    assert summary["trades"] == 3
    assert summary["total"] == pytest.approx(sum(t.total for t in trades))
    assert summary["theta"] == pytest.approx(sum(t.theta for t in trades))
    assert (summary["reliable_trades"] + summary["unreliable_trades"]) == 3


def test_summarise_handles_no_trades():
    summary = summarise([])
    assert summary["trades"] == 0
    assert summary["total"] == 0.0


def test_as_dict_is_json_friendly():
    payload = _round_trip().as_dict()
    assert payload["notes"] == [] or isinstance(payload["notes"], list)
    assert isinstance(payload["total"], float)
    assert math.isfinite(payload["total"])


def test_residual_reduces_to_exactly_minus_fees():
    """Verified against a live account: summed residual matched summed USD
    commission to the cent. It follows from the revaluation being exact, and
    it is why `residual` must not be read as an execution-cost measure --
    the spread is absorbed into the vol step instead.
    """
    for fees in (0.0, 12.5, 640.25):
        attribution = _round_trip(spot_out=SPOT * 1.03, vol_out=VOL * 0.8,
                                  days_held=2.0, fees=fees)
        assert attribution.residual == pytest.approx(-fees, abs=1e-3)
