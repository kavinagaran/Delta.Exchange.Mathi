"""Data-only order intent (ADR 0003 §5.4/§5.5) and the pure state-machine
validator: illegal transitions must raise, never clamp or ignore."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from btc_trend_engine.execution.intents import (
    IntentRiskDecision,
    InvalidOrderIntent,
    OrderIntent,
    build_order_intent,
)
from btc_trend_engine.execution.order_state_machine import (
    IllegalTransitionError,
    OrderState,
    is_terminal,
    transition,
)

T0 = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)


def _allowed_decision(**overrides) -> IntentRiskDecision:
    fields = dict(allowed=True, reason="risk checks passed",
                 signal_id="sig-1", signal_key="up|c1|c2|c3", lots=5,
                 proposed_risk_usd=42.0)
    fields.update(overrides)
    return IntentRiskDecision(**fields)


def _refused_decision(**overrides) -> IntentRiskDecision:
    fields = dict(allowed=False, reason="no", signal_id="sig-1", signal_key="up")
    fields.update(overrides)
    return IntentRiskDecision(**fields)


# ── IntentRiskDecision invariants ───────────────────────────────────────
def test_allowed_decision_with_zero_lots_is_rejected():
    with pytest.raises(InvalidOrderIntent):
        IntentRiskDecision(allowed=True, reason="x", signal_id="s", signal_key="k", lots=0)


def test_allowed_decision_with_active_kill_switch_is_rejected():
    with pytest.raises(InvalidOrderIntent):
        IntentRiskDecision(allowed=True, reason="x", signal_id="s", signal_key="k",
                          lots=1, kill_switches_active=("max_daily_loss",))


def test_allowed_decision_with_expired_signal_is_rejected():
    with pytest.raises(InvalidOrderIntent):
        IntentRiskDecision(allowed=True, reason="x", signal_id="s", signal_key="k",
                          lots=1, signal_expired=True)


# ── OrderIntent invariants ───────────────────────────────────────────────
def test_order_intent_rejects_a_refused_risk_decision():
    with pytest.raises(InvalidOrderIntent):
        OrderIntent(
            symbol="C-BTC-64000-170726", side="long", lots=1, max_price=Decimal("100"),
            signal_id="s", signal_key="k", reason="r", risk=_refused_decision(),
            created_at_utc="2026-07-25T12:00:00Z", expires_at_utc="2026-07-25T12:05:00Z")


@pytest.mark.parametrize("field,value", [
    ("side", "sideways"), ("lots", 0), ("max_price", Decimal("0")),
])
def test_order_intent_rejects_invalid_fields(field, value):
    kwargs = dict(
        symbol="C-BTC-64000-170726", side="long", lots=1, max_price=Decimal("100"),
        signal_id="s", signal_key="k", reason="r", risk=_allowed_decision(),
        created_at_utc="2026-07-25T12:00:00Z", expires_at_utc="2026-07-25T12:05:00Z")
    kwargs[field] = value
    with pytest.raises(InvalidOrderIntent):
        OrderIntent(**kwargs)


def test_order_intent_rejects_expiry_not_after_creation():
    with pytest.raises(InvalidOrderIntent):
        OrderIntent(
            symbol="C-BTC-64000-170726", side="long", lots=1, max_price=Decimal("100"),
            signal_id="s", signal_key="k", reason="r", risk=_allowed_decision(),
            created_at_utc="2026-07-25T12:05:00Z", expires_at_utc="2026-07-25T12:00:00Z")


def test_build_order_intent_returns_none_for_a_refused_decision():
    assert build_order_intent(
        symbol="C-BTC-64000-170726", side="long", max_price=Decimal("100"),
        reason="r", risk=_refused_decision(), now=T0, ttl_seconds=300) is None


def test_build_order_intent_carries_signal_id_through_untouched():
    intent = build_order_intent(
        symbol="C-BTC-64000-170726", side="long", max_price=Decimal("100"),
        reason="r", risk=_allowed_decision(signal_id="trace-me"), now=T0, ttl_seconds=300)
    assert intent is not None
    assert intent.signal_id == "trace-me"
    assert intent.lots == 5
    assert intent.expires_at_utc == "2026-07-25T12:05:00Z"


# ── order state machine ─────────────────────────────────────────────────
def test_legal_chain_to_filled():
    state = OrderState.PROPOSED
    for target in (OrderState.RISK_APPROVED, OrderState.SUBMITTED, OrderState.FILLED):
        state = transition(state, target)
    assert state == OrderState.FILLED
    assert is_terminal(state)


def test_partial_fill_can_still_complete():
    state = transition(OrderState.SUBMITTED, OrderState.PARTIALLY_FILLED)
    state = transition(state, OrderState.FILLED)
    assert state == OrderState.FILLED


@pytest.mark.parametrize("current,target", [
    (OrderState.FILLED, OrderState.SUBMITTED),
    (OrderState.PROPOSED, OrderState.FILLED),
    (OrderState.REJECTED, OrderState.RISK_APPROVED),
    (OrderState.CANCELLED, OrderState.PROPOSED),
])
def test_illegal_transitions_raise(current, target):
    with pytest.raises(IllegalTransitionError):
        transition(current, target)


def test_every_terminal_state_rejects_any_further_transition():
    for terminal in (OrderState.FILLED, OrderState.REJECTED,
                     OrderState.CANCELLED, OrderState.EXPIRED):
        assert is_terminal(terminal)
        with pytest.raises(IllegalTransitionError):
            transition(terminal, OrderState.PROPOSED)
