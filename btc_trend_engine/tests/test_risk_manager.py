"""risk_manager composition order and its fail-closed guarantees:
kill switch > signal freshness > entry_allowed > sizing > account limits.

The account-limit layer is ``risk_controls.evaluate_entry`` itself, unmodified
(ADR 0003 single-source-of-truth); these tests assert the wrapper defers to it
rather than re-deciding.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from btc_trend_engine.execution.intents import build_order_intent
from btc_trend_engine.execution.reconciliation import compare
from btc_trend_engine.risk import risk_manager
from btc_trend_engine.risk.risk_manager import SizingInputs

T0 = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)


def _sizing(**overrides) -> SizingInputs:
    fields = dict(
        configured=100, affordable=100, liquidity_cap=100, max_order_lots=100,
        risk_budget_usd=1000.0, stop_loss_usd=50.0, premium_per_lot=0.5,
        round_trip_fee_per_lot=0.01, slippage_per_lot=0.01)
    fields.update(overrides)
    return SizingInputs(**fields)


def _snapshot(**overrides) -> dict:
    snap = {
        "timestamp": "2026-07-25T12:00:00Z",
        "signal_ttl_seconds": 300,
        "entry_allowed": True,
        "signal_id": "sig-1",
    }
    snap.update(overrides)
    return snap


def _evaluate(tmp_path: Path, **overrides):
    kwargs = dict(
        data_dir=tmp_path, signal_id="sig-1", signal_key="up|a|b|c",
        config={}, sizing=_sizing(), snapshot=_snapshot(), now=T0)
    kwargs.update(overrides)
    return risk_manager.evaluate_entry(**kwargs)


# ── kill switches dominate everything ───────────────────────────────────
def test_an_active_kill_switch_refuses_before_any_other_check(tmp_path):
    result = _evaluate(tmp_path, kill_switches_active=["max_daily_loss"])
    assert not result.decision.allowed
    assert result.decision.kill_switches_active == ("max_daily_loss",)
    assert "kill switch" in result.decision.reason
    # It short-circuits: the account layer was never consulted.
    assert result.account is None


def test_multiple_active_switches_are_all_named(tmp_path):
    result = _evaluate(tmp_path, kill_switches_active=["manual", "data_quality"])
    assert result.decision.kill_switches_active == ("data_quality", "manual")


# ── signal freshness ─────────────────────────────────────────────────────
def test_an_expired_signal_produces_no_entry(tmp_path):
    result = _evaluate(tmp_path, now=T0 + timedelta(seconds=301))
    assert not result.decision.allowed
    assert result.decision.signal_expired
    assert "expired" in result.decision.reason


def test_a_signal_exactly_at_its_ttl_is_still_valid(tmp_path):
    result = _evaluate(tmp_path, now=T0 + timedelta(seconds=300))
    assert result.decision.allowed


def test_a_future_dated_signal_is_refused_as_clock_skew(tmp_path):
    result = _evaluate(tmp_path, now=T0 - timedelta(seconds=60))
    assert not result.decision.allowed
    assert result.decision.signal_expired
    assert "clock skew" in result.decision.reason


def test_a_snapshot_without_a_usable_timestamp_is_refused(tmp_path):
    result = _evaluate(tmp_path, snapshot=_snapshot(timestamp="not-a-time"))
    assert not result.decision.allowed
    assert result.decision.signal_expired


def test_entry_allowed_false_is_honoured_verbatim(tmp_path):
    result = _evaluate(tmp_path, snapshot=_snapshot(entry_allowed=False))
    assert not result.decision.allowed
    assert "entry_allowed=false" in result.decision.reason


# ── sizing ───────────────────────────────────────────────────────────────
def test_zero_lots_produces_no_entry(tmp_path):
    result = _evaluate(tmp_path, sizing=_sizing(affordable=0))
    assert not result.decision.allowed
    assert "zero lots" in result.decision.reason


def test_lots_never_exceed_the_cutover_cap(tmp_path):
    result = _evaluate(tmp_path, sizing=_sizing(
        configured=5000, affordable=5000, liquidity_cap=5000, max_order_lots=5000,
        risk_budget_usd=1_000_000, premium_per_lot=0.001,
        round_trip_fee_per_lot=0.0, slippage_per_lot=0.0))
    assert result.decision.allowed
    assert result.decision.lots <= 1000


# ── account limits come from risk_controls, unmodified ──────────────────
def test_the_account_daily_trade_cap_is_deferred_to_and_reported(tmp_path):
    (tmp_path / "trade_history.json").write_text(json.dumps([
        {"slot": "trend", "trading_date": "2026-07-25", "order_id": 1, "pnl_usd": 5},
        {"slot": "trend", "trading_date": "2026-07-25", "order_id": 2, "pnl_usd": 5},
    ]), encoding="utf-8")
    result = _evaluate(tmp_path, config={"MAX_TRADES_PER_DAY_GLOBAL": 2})
    assert not result.decision.allowed
    assert "daily trade cap" in result.decision.reason
    assert result.decision.account_risk_reason == result.decision.reason
    assert result.account is not None and not result.account.allowed


def test_a_clean_account_permits_entry_and_reports_sizing(tmp_path):
    result = _evaluate(tmp_path)
    assert result.decision.allowed
    assert result.decision.lots > 0
    assert result.decision.proposed_risk_usd > 0
    assert result.account is not None and result.account.allowed


def test_a_daily_loss_breach_both_refuses_and_recommends_a_kill_switch(tmp_path):
    result = _evaluate(tmp_path, config={"MAX_DAILY_LOSS_USD": 100},
                       unrealized_pnl_usd=-150)
    assert not result.decision.allowed
    assert "max_daily_loss" in result.kill_switch_triggers


# ── reconciliation -> kill-switch recommendation ────────────────────────
def test_a_reconciliation_mismatch_recommends_the_reconciliation_switch():
    mismatch = compare({"status": "OPEN", "lots": 5}, None)
    assert risk_manager.reconciliation_trigger(mismatch) == ["reconciliation_mismatch"]


def test_a_clean_reconciliation_recommends_nothing():
    assert risk_manager.reconciliation_trigger(compare(None, None)) == []


# ── the seam that matters: decision -> OrderIntent ──────────────────────
def _intent_from(result):
    return build_order_intent(
        symbol="C-BTC-64000-170726", side="long", max_price=Decimal("1.50"),
        reason=result.decision.reason, risk=result.decision, now=T0, ttl_seconds=300)


def test_an_expired_signal_yields_no_order_intent(tmp_path):
    result = _evaluate(tmp_path, now=T0 + timedelta(seconds=301))
    assert _intent_from(result) is None


def test_an_active_kill_switch_yields_no_order_intent(tmp_path):
    result = _evaluate(tmp_path, kill_switches_active=["manual"])
    assert _intent_from(result) is None


def test_an_account_limit_breach_yields_no_order_intent(tmp_path):
    result = _evaluate(tmp_path, config={"MAX_DAILY_LOSS_USD": 100},
                       unrealized_pnl_usd=-150)
    assert _intent_from(result) is None


def test_a_clean_pass_yields_an_intent_tracing_back_to_its_signal(tmp_path):
    result = _evaluate(tmp_path)
    intent = _intent_from(result)
    assert intent is not None
    # Criterion 19: every order traces to the signal that produced it.
    assert intent.signal_id == "sig-1"
    assert intent.signal_key == "up|a|b|c"
    assert intent.lots == result.decision.lots
