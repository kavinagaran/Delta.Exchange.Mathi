"""Pure pre-trade slippage/spread guard and the local-state-vs-exchange
comparator (ADR 0003) — both take plain values/dicts and do no I/O."""

from __future__ import annotations

from decimal import Decimal

from btc_trend_engine.execution.reconciliation import ReconciliationResult, compare
from btc_trend_engine.execution.slippage_guard import check_quote_age, check_slippage


# ── slippage_guard ──────────────────────────────────────────────────────
def test_long_fill_worse_than_reference_within_cap_is_ok():
    result = check_slippage(reference_price=Decimal("100"), fill_price=Decimal("100.5"),
                            max_slippage_pct=Decimal("1"), side="long")
    assert result.ok


def test_long_fill_worse_than_reference_beyond_cap_is_rejected():
    result = check_slippage(reference_price=Decimal("100"), fill_price=Decimal("102"),
                            max_slippage_pct=Decimal("1"), side="long")
    assert not result.ok
    assert "slipped" in result.reason


def test_short_adverse_direction_is_fill_below_reference():
    result = check_slippage(reference_price=Decimal("100"), fill_price=Decimal("97"),
                            max_slippage_pct=Decimal("1"), side="short")
    assert not result.ok


def test_short_favorable_direction_never_trips_the_cap():
    result = check_slippage(reference_price=Decimal("100"), fill_price=Decimal("120"),
                            max_slippage_pct=Decimal("1"), side="short")
    assert result.ok


def test_unknown_side_is_rejected_not_guessed():
    result = check_slippage(reference_price=Decimal("100"), fill_price=Decimal("100"),
                            max_slippage_pct=Decimal("1"), side="sideways")
    assert not result.ok


def test_quote_age_within_cap_is_ok():
    assert check_quote_age(quote_age_seconds=5, max_quote_age_seconds=20).ok


def test_quote_age_beyond_cap_is_rejected():
    result = check_quote_age(quote_age_seconds=25, max_quote_age_seconds=20)
    assert not result.ok


# ── reconciliation ───────────────────────────────────────────────────────
def test_no_local_state_and_no_exchange_position_matches():
    assert compare(None, None) == ReconciliationResult(True)


def test_exchange_position_with_no_local_state_is_a_mismatch():
    result = compare(None, {"symbol": "C-BTC-64000-170726", "size": "5"})
    assert not result.matched
    assert "no matching local state" in result.mismatches[0]


def test_open_local_state_with_no_exchange_position_is_a_mismatch():
    result = compare({"status": "OPEN", "lots": 5}, None)
    assert not result.matched
    assert "no position" in result.mismatches[0]


def test_entry_pending_with_no_exchange_position_yet_is_not_a_mismatch():
    result = compare({"status": "ENTRY_PENDING", "lots": 5}, None)
    assert result.matched


def test_matching_open_position_reconciles():
    result = compare(
        {"status": "OPEN", "side": "long", "lots": 5, "symbol": "C-BTC-64000-170726"},
        {"side": "long", "size": "5", "symbol": "C-BTC-64000-170726"})
    assert result.matched


def test_lot_size_mismatch_is_reported():
    result = compare(
        {"status": "OPEN", "side": "long", "lots": 5},
        {"side": "long", "size": "3"})
    assert not result.matched
    assert any("lot size" in m for m in result.mismatches)


def test_side_mismatch_is_reported():
    result = compare(
        {"status": "OPEN", "side": "long", "lots": 5},
        {"size": "-5"})  # negative size implies short on the exchange side
    assert not result.matched
    assert any("side mismatch" in m for m in result.mismatches)
