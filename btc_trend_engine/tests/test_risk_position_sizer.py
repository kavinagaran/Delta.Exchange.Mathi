"""Sizing at cutover: the engine may only ever *reduce* size relative to the
legacy 1,000-lot fixed policy (ADR 0003). ``size_position`` wraps
``risk_controls.risk_based_lots`` — the caps it already enforces are never
reimplemented, only ever additionally clamped."""

from __future__ import annotations

import itertools

from risk_controls import risk_based_lots

from btc_trend_engine.risk.position_sizer import size_position


def test_result_never_exceeds_the_legacy_1000_lot_cap():
    lots = size_position(
        configured=5000, affordable=5000, liquidity_cap=5000, max_order_lots=5000,
        risk_budget_usd=100_000, stop_loss_usd=10, premium_per_lot=0.01,
        round_trip_fee_per_lot=0.0001, slippage_per_lot=0.0001)
    assert lots == 1000


def test_a_smaller_underlying_cap_is_never_relaxed_upward():
    lots = size_position(
        configured=50, affordable=5000, liquidity_cap=5000, max_order_lots=5000,
        risk_budget_usd=100_000, stop_loss_usd=10, premium_per_lot=0.01,
        round_trip_fee_per_lot=0.0001, slippage_per_lot=0.0001)
    assert lots == 50


def test_zero_from_any_underlying_cap_stays_zero():
    lots = size_position(
        configured=0, affordable=100, liquidity_cap=100, max_order_lots=100,
        risk_budget_usd=100, stop_loss_usd=1, premium_per_lot=0.01,
        round_trip_fee_per_lot=0.0001, slippage_per_lot=0.0001)
    assert lots == 0


def test_cutover_cap_is_configurable_and_still_a_ceiling_not_a_floor():
    lots = size_position(
        configured=5000, affordable=5000, liquidity_cap=5000, max_order_lots=5000,
        risk_budget_usd=100_000, stop_loss_usd=10, premium_per_lot=0.01,
        round_trip_fee_per_lot=0.0001, slippage_per_lot=0.0001, cutover_cap=100)
    assert lots == 100


def test_result_is_never_greater_than_risk_based_lots_across_many_inputs():
    # Hand-rolled property sweep (no hypothesis in this environment): every
    # combination in a representative grid must satisfy result <= 1000 and
    # result <= the underlying risk_based_lots value.
    configured_values = (1, 100, 2000)
    affordable_values = (0, 50, 5000)
    risk_budget_values = (0.0, 10.0, 100_000.0)
    stop_loss_values = (0.0, 5.0, 50_000.0)
    for configured, affordable, risk_budget, stop_loss in itertools.product(
        configured_values, affordable_values, risk_budget_values, stop_loss_values
    ):
        kwargs = dict(
            configured=configured, affordable=affordable, liquidity_cap=5000,
            max_order_lots=5000, risk_budget_usd=risk_budget, stop_loss_usd=stop_loss,
            premium_per_lot=0.01, round_trip_fee_per_lot=0.0001, slippage_per_lot=0.0001)
        sized = size_position(**kwargs)
        underlying = risk_based_lots(**kwargs)
        assert sized <= 1000, kwargs
        assert sized <= underlying, kwargs
        assert sized >= 0, kwargs


def test_short_without_a_positive_stop_loss_is_zero_regardless_of_other_caps():
    lots = size_position(
        configured=100, affordable=100, liquidity_cap=100, max_order_lots=100,
        risk_budget_usd=1000, stop_loss_usd=0, premium_per_lot=0.01,
        round_trip_fee_per_lot=0.0001, slippage_per_lot=0.0001, short=True)
    assert lots == 0
