from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from trend_score_live_execution import (
    ExactOrderLookup,
    LIVE_SCORE_LOTS,
    LiveScoreExecutionError,
    bounded_ioc_payload,
    execute_or_recover_entry,
    score_close_client_id,
    score_entry_client_id,
    switch_entry_gate,
    terminal_filled_lots,
    validate_fixed_entry,
)


NOW = datetime(2026, 7, 23, 4, 0, tzinfo=timezone.utc)
_UNSET = object()


def _signal(zone: str = "CE_2_ITM") -> dict:
    score = 60 if zone == "CE_2_ITM" else -60 if zone == "PE_3_ITM" else 0
    return {
        "snapshot": {"market": {"spot": 65_800}},
        "decision": {
            "decision_id": "decision-live-1",
            "model_version": "trend-live-test",
            "schema_version": "1.0",
        },
        "score": score,
        "zone": zone,
        "signal_key": "trend-score-auto|BTCUSD|5m|2026-07-23T03:55:00Z",
        "signal_bar_close_utc": "2026-07-23T04:00:00Z",
        "market_regime": "RANGE" if zone == "SHORT_MOVE" else "TRENDING",
    }


def _prepared(zone: str = "CE_2_ITM") -> dict:
    if zone == "CE_2_ITM":
        values = {
            "symbol": "C-BTC-65400-230726",
            "product_id": 101,
            "strike": 65_400,
            "side": "long",
            "option_type": "CE",
            "instrument_kind": "BTC_OPTION",
            "entry_price": 220.0,
        }
    elif zone == "PE_3_ITM":
        values = {
            "symbol": "P-BTC-66400-230726",
            "product_id": 102,
            "strike": 66_400,
            "side": "long",
            "option_type": "PE",
            "instrument_kind": "BTC_OPTION",
            "entry_price": 240.0,
        }
    else:
        values = {
            "symbol": "MV-BTC-65800-230726",
            "product_id": 103,
            "strike": 65_800,
            "side": "short",
            "option_type": "MOVE",
            "instrument_kind": "BTC_MOVE",
            "entry_price": 445.0,
        }
    return {
        "zone": zone,
        "lots": LIVE_SCORE_LOTS,
        "contract_value": 0.001,
        "max_order_lots": 2_000,
        "settlement": "2026-07-23T12:00:00Z",
        **values,
    }


def _quote(zone: str = "CE_2_ITM") -> dict:
    if zone == "SHORT_MOVE":
        bid, ask = 445.0, 446.0
    else:
        entry = _prepared(zone)["entry_price"]
        bid, ask = entry - 1.0, entry
    return {
        "bid": bid,
        "ask": ask,
        "bid_size": 5_000,
        "ask_size": 5_000,
        "quote_age_secs": 1.0,
        "trading_status": "operational",
        "tick_size": 0.1,
        "price_band": {"lower_limit": 1, "upper_limit": 5_000},
    }


def _policy() -> dict:
    return {
        "tp_target_pnl": 500,
        "sl_target_pnl": 250,
        "tsl_arm_pnl": 125,
        "tsl_trail_pnl": 125,
        "tsl_lock_min_pnl": 0,
        "poll_secs": 30,
    }


def _order(
    client_id: str,
    *,
    product_id: int = 101,
    side: str = "buy",
    filled: int = 1_000,
    state: str = "filled",
    price: float = 220.0,
) -> dict:
    return {
        "id": 9_001,
        "client_order_id": client_id,
        "product_id": product_id,
        "size": 1_000,
        "side": side,
        "order_type": "limit_order",
        "time_in_force": "ioc",
        "reduce_only": False,
        "state": state,
        "filled_size": filled,
        "unfilled_size": 1_000 - filled,
        "average_fill_price": price if filled else None,
        "paid_commission": 1.25 if filled else 0,
    }


def _run(
    *,
    zone: str = "CE_2_ITM",
    prepared_override=_UNSET,
    fresh_quote_override=_UNSET,
    signal_override=_UNSET,
    transition_override: str = "trend-score-live-transition-1",
    clock_value: datetime = NOW,
    existing_state: dict | None = None,
    submit_order=None,
    lookup_order=None,
    get_position=None,
    protect_position=None,
    flatten_position=None,
    persist_state=None,
    load_state=None,
    final_preflight=None,
    audit=None,
    terminal_timeout_sec: float = 0,
):
    transition = transition_override
    client_id = score_entry_client_id("alice", transition)
    baseline_prepared = _prepared(zone)
    prepared = (
        baseline_prepared
        if prepared_override is _UNSET
        else prepared_override
    )
    fresh_quote = (
        _quote(zone)
        if fresh_quote_override is _UNSET
        else fresh_quote_override
    )
    signal = (
        _signal(zone)
        if signal_override is _UNSET
        else signal_override
    )
    side = "sell" if zone == "SHORT_MOVE" else "buy"
    price = baseline_prepared["entry_price"]
    filled_order = _order(
        client_id,
        product_id=baseline_prepared["product_id"],
        side=side,
        price=price,
    )
    saved = []
    durable = {"state": copy.deepcopy(existing_state)}
    caller_persist = persist_state

    def tracked_persist(state):
        if caller_persist is not None:
            caller_persist(state)
        else:
            saved.append(copy.deepcopy(state))
        durable["state"] = copy.deepcopy(state)

    persist_state = tracked_persist
    load_state = load_state or (
        lambda: copy.deepcopy(durable["state"])
    )
    submit_order = submit_order or (
        lambda payload: (copy.deepcopy(filled_order), {"success": True})
    )
    lookup_order = lookup_order or (
        lambda order_id, cid, pid: ExactOrderLookup(
            copy.deepcopy(filled_order), True
        )
    )
    signed_size = -1_000 if side == "sell" else 1_000
    get_position = get_position or (
        lambda product_id: {
            "product_id": product_id,
            "size": signed_size,
            "entry_price": price,
        }
    )
    protect_position = protect_position or (
        lambda state, started: (
            True,
            {
                "status": "healthy",
                "exchange_protection_complete": True,
            },
        )
    )
    if flatten_position is None:

        def flatten_position(state, reason):
            closed = {
                **state,
                "status": "CLOSED",
                "flat_verified": True,
                "exit_trigger": reason,
            }
            persist_state(closed)
            return closed

    final_preflight = final_preflight or (lambda state: None)
    result = execute_or_recover_entry(
        user="alice",
        signal=signal,
        prepared=prepared,
        transition_id=transition,
        fresh_quote=fresh_quote,
        protection_config=_policy(),
        risk_snapshot={"proposed_risk_usd": 250, "allowed": True},
        existing_state=existing_state,
        persist_state=persist_state,
        load_state=load_state,
        final_preflight=final_preflight,
        submit_order=submit_order,
        lookup_order=lookup_order,
        get_position=get_position,
        protect_position=protect_position,
        flatten_position=flatten_position,
        max_slippage_pct=1,
        max_spread_pct=3,
        max_quote_age_sec=20,
        audit=audit,
        clock=lambda: clock_value,
        terminal_timeout_sec=terminal_timeout_sec,
        sleeper=lambda _: None,
    )
    return result, saved, client_id, filled_order


def test_transition_client_ids_are_stable_scoped_and_delta_sized():
    one = score_entry_client_id("Alice Smith", "transition-1")
    assert one == score_entry_client_id("Alice Smith", "transition-1")
    assert one != score_entry_client_id("Alice Smith", "transition-2")
    assert one.startswith("trend-")
    assert len(one) <= 32

    close_zero = score_close_client_id("Alice Smith", "transition-1")
    assert close_zero == score_close_client_id("Alice Smith", "transition-1")
    assert close_zero != score_close_client_id(
        "Alice Smith", "transition-1", sequence=1
    )
    assert len(close_zero) <= 32


@pytest.mark.parametrize(
    "zone",
    ("CE_2_ITM", "PE_3_ITM", "SHORT_MOVE"),
)
def test_fixed_entry_validation_accepts_only_exact_policy_contract(zone):
    normalized = validate_fixed_entry(_prepared(zone))
    assert normalized["lots"] == 1_000
    assert normalized["exchange_side"] == (
        "sell" if zone == "SHORT_MOVE" else "buy"
    )

    wrong_lots = _prepared(zone)
    wrong_lots["lots"] = 999
    with pytest.raises(LiveScoreExecutionError, match="exactly 1,000"):
        validate_fixed_entry(wrong_lots)

    wrong_contract = _prepared(zone)
    wrong_contract["symbol"] = (
        "C-BTC-65400-230726"
        if zone == "SHORT_MOVE"
        else "MV-BTC-65800-230726"
    )
    with pytest.raises(LiveScoreExecutionError, match="score zone"):
        validate_fixed_entry(wrong_contract)

    low_limit = _prepared(zone)
    low_limit["max_order_lots"] = 999
    with pytest.raises(LiveScoreExecutionError, match="1,000-lot order"):
        validate_fixed_entry(low_limit)


@pytest.mark.parametrize(
    ("zone", "expected_side", "expected_limit"),
    (
        ("CE_2_ITM", "buy", 222.2),
        ("PE_3_ITM", "buy", 242.4),
        ("SHORT_MOVE", "sell", 440.6),
    ),
)
def test_bounded_ioc_is_exactly_1000_and_rounds_inside_slippage(
    zone, expected_side, expected_limit
):
    client_id = score_entry_client_id("alice", "transition")
    payload, snapshot = bounded_ioc_payload(
        _prepared(zone),
        _quote(zone),
        client_order_id=client_id,
        max_slippage_pct=1,
        max_spread_pct=3,
        max_quote_age_sec=20,
    )

    assert payload == {
        "product_id": _prepared(zone)["product_id"],
        "size": 1_000,
        "side": expected_side,
        "order_type": "limit_order",
        "limit_price": str(expected_limit),
        "time_in_force": "ioc",
        "post_only": False,
        "client_order_id": client_id,
    }
    assert snapshot["limit_price"] == expected_limit


@pytest.mark.parametrize(
    ("change", "message"),
    (
        ({"quote_age_secs": 21}, "stale"),
        ({"bid": 200, "ask": 220}, "spread"),
        ({"ask_size": 999}, "1,000-lot IOC"),
        ({"trading_status": "halted"}, "not operational"),
        ({"ask": 223}, "bounded buy limit"),
        ({"price_band": {"upper_limit": 221}}, "price band"),
    ),
)
def test_bounded_ioc_fails_closed_on_quote_or_execution_gate(change, message):
    quote = {**_quote(), **change}
    with pytest.raises(LiveScoreExecutionError, match=message):
        bounded_ioc_payload(
            _prepared(),
            quote,
            client_order_id=score_entry_client_id("alice", "transition"),
            max_slippage_pct=1,
            max_spread_pct=3,
            max_quote_age_sec=20,
        )


def test_full_fill_persists_pending_before_post_then_open_and_protected():
    calls = []
    saved = []

    def persist(state):
        calls.append(("persist", state["status"]))
        saved.append(copy.deepcopy(state))

    def submit(payload):
        calls.append(("submit", payload["client_order_id"]))
        order = _order(payload["client_order_id"])
        return order, {"success": True}

    result, _, client_id, _ = _run(
        persist_state=persist,
        submit_order=submit,
    )

    assert result["ok"] is True
    assert result["status"] == "OPEN"
    assert result["consume_signal"] is True
    assert result["filled_lots"] == 1_000
    assert calls.index(("persist", "ENTRY_PENDING")) < next(
        index for index, call in enumerate(calls) if call[0] == "submit"
    )
    assert saved[0]["pending_entry_client_order_id"] == client_id
    assert saved[0]["dry_run"] is False
    opened = result["state"]
    assert opened["status"] == "OPEN"
    assert opened["lots"] == opened["owned_entry_lots"] == 1_000
    assert opened["client_order_id"] == client_id
    assert opened["continuity_status"] == "awaiting_monitor_verification"
    assert opened["execution_snapshot"]["requested"] == 1_000
    assert opened["execution_snapshot"]["filled"] == 1_000
    assert opened["protection_verified_at_entry"] is True


@pytest.mark.parametrize(
    ("zone", "fill_price", "message"),
    (
        ("CE_2_ITM", 222.2001, "average buy fill exceeds"),
        ("SHORT_MOVE", 440.5999, "average sell fill is below"),
    ),
)
def test_average_fill_cannot_cross_durable_ioc_limit(
    zone, fill_price, message
):
    transition = "trend-score-live-transition-1"
    client_id = score_entry_client_id("alice", transition)
    prepared = _prepared(zone)
    side = "sell" if zone == "SHORT_MOVE" else "buy"
    order = _order(
        client_id,
        product_id=prepared["product_id"],
        side=side,
        price=fill_price,
    )
    protect = Mock(side_effect=AssertionError("invalid fill was protected"))

    result, saved, _, _ = _run(
        zone=zone,
        submit_order=lambda payload: (
            copy.deepcopy(order),
            {"success": True},
        ),
        lookup_order=lambda *args: ExactOrderLookup(
            copy.deepcopy(order), True
        ),
        get_position=lambda product_id: {
            "product_id": product_id,
            "size": -1_000 if side == "sell" else 1_000,
            "entry_price": fill_price,
        },
        protect_position=protect,
    )

    assert result["status"] == "ENTRY_PENDING"
    assert result["consume_signal"] is False
    assert message in result["error"]
    assert result["state"]["pending_entry_submission_state"] == (
        "filled_position_mismatch"
    )
    assert saved[-1]["status"] == "ENTRY_PENDING"
    protect.assert_not_called()


def test_returned_order_limit_must_equal_durable_payload_limit():
    transition = "trend-score-live-transition-1"
    client_id = score_entry_client_id("alice", transition)
    order = _order(client_id)
    order["limit_price"] = "222.3"
    lookup = Mock(
        side_effect=AssertionError("limit mismatch was hidden by lookup")
    )

    with pytest.raises(
        LiveScoreExecutionError,
        match="limit price differs from durable",
    ):
        _run(
            submit_order=lambda payload: (
                copy.deepcopy(order),
                {"success": True},
            ),
            lookup_order=lookup,
        )

    lookup.assert_not_called()


def test_move_partial_fill_is_persisted_once_and_immediately_protected():
    transition = "trend-score-live-transition-1"
    client_id = score_entry_client_id("alice", transition)
    order = _order(
        client_id,
        product_id=103,
        side="sell",
        filled=375,
        price=445,
    )
    submit = Mock(return_value=(copy.deepcopy(order), {"success": True}))
    protect = Mock(
        return_value=(True, {"status": "degraded", "local_fallback_active": True})
    )

    result, saved, _, _ = _run(
        zone="SHORT_MOVE",
        submit_order=submit,
        lookup_order=lambda *args: ExactOrderLookup(copy.deepcopy(order), True),
        get_position=lambda pid: {
            "product_id": pid,
            "size": -375,
            "entry_price": 445,
        },
        protect_position=protect,
    )

    assert result["ok"] is True
    assert result["partial_fill"] is True
    assert result["filled_lots"] == 375
    assert result["consume_signal"] is True
    assert result["state"]["lots"] == 375
    assert result["state"]["requested_lots"] == 1_000
    assert result["state"]["execution_snapshot"]["unfilled"] == 625
    submit.assert_called_once()
    protect.assert_called_once()
    assert any(row["status"] == "ENTRY_PENDING" for row in saved)


def test_terminal_zero_fill_is_verified_flat_and_consumes_signal_once():
    transition = "trend-score-live-transition-1"
    client_id = score_entry_client_id("alice", transition)
    order = _order(client_id, filled=0, state="cancelled")

    result, saved, _, _ = _run(
        submit_order=lambda payload: (copy.deepcopy(order), {"success": True}),
        lookup_order=lambda *args: ExactOrderLookup(copy.deepcopy(order), True),
        get_position=lambda pid: {
            "product_id": pid,
            "size": 0,
            "entry_price": 0,
        },
    )

    assert result["status"] == "NO_FILL"
    assert result["consume_signal"] is True
    assert result["filled_lots"] == 0
    assert saved[-1]["status"] == "IDLE"
    assert saved[-1]["last_entry_client_order_id"] == client_id


def test_explicit_rejection_consumes_only_after_exact_absence_and_flat_position():
    lookup = Mock(return_value=ExactOrderLookup(None, True))
    position = Mock(return_value={
        "product_id": 101,
        "size": 0,
        "entry_price": 0,
    })
    result, saved, client_id, _ = _run(
        submit_order=lambda payload: (
            None,
            {
                "success": False,
                "error": {"code": "insufficient_margin"},
            },
        ),
        lookup_order=lookup,
        get_position=position,
    )

    assert result["status"] == "REJECTED"
    assert result["consume_signal"] is True
    assert saved[-1]["status"] == "IDLE"
    assert saved[-1]["last_entry_client_order_id"] == client_id
    assert saved[-1]["last_entry_rejection_exact_absence"] is True
    assert saved[-1]["last_entry_position_verified_flat"] is True
    lookup.assert_called_once_with(None, client_id, 101)
    position.assert_called_once_with(101)


def test_false_negative_rejection_recovers_found_fill_and_protects_it():
    transition = "trend-score-live-transition-1"
    client_id = score_entry_client_id("alice", transition)
    recovered = _order(client_id)
    lookup = Mock(
        return_value=ExactOrderLookup(copy.deepcopy(recovered), True)
    )
    protect = Mock(return_value=(
        True,
        {"status": "healthy", "exchange_protection_complete": True},
    ))

    result, saved, _, _ = _run(
        submit_order=lambda payload: (
            None,
            {
                "success": False,
                "error": {"code": "insufficient_margin"},
            },
        ),
        lookup_order=lookup,
        protect_position=protect,
    )

    assert result["status"] == "OPEN"
    assert result["consume_signal"] is True
    assert result["filled_lots"] == 1_000
    assert saved[-1]["status"] == "OPEN"
    lookup.assert_called_once_with(None, client_id, 101)
    protect.assert_called_once()


def test_rejection_with_inconclusive_exact_lookup_remains_pending():
    lookup = Mock(
        return_value=ExactOrderLookup(None, False, "lookup timed out")
    )
    position = Mock(
        side_effect=AssertionError(
            "an inconclusive order lookup cannot prove rejection"
        )
    )

    result, saved, client_id, _ = _run(
        submit_order=lambda payload: (
            None,
            {
                "success": False,
                "error": {"code": "insufficient_margin"},
            },
        ),
        lookup_order=lookup,
        get_position=position,
    )

    assert result["status"] == "ENTRY_PENDING"
    assert result["consume_signal"] is False
    assert result["state"]["pending_entry_post_boundary"] is True
    assert (
        result["state"]["pending_entry_submission_state"]
        == "rejection_lookup_inconclusive"
    )
    lookup.assert_called_once_with(None, client_id, 101)
    position.assert_not_called()
    assert saved[-1]["status"] == "ENTRY_PENDING"


def test_rejection_with_exact_absence_but_nonzero_position_remains_pending():
    lookup = Mock(return_value=ExactOrderLookup(None, True))
    position = Mock(return_value={
        "product_id": 101,
        "size": 1_000,
        "entry_price": 220,
    })

    result, saved, client_id, _ = _run(
        submit_order=lambda payload: (
            None,
            {
                "success": False,
                "error": {"code": "insufficient_margin"},
            },
        ),
        lookup_order=lookup,
        get_position=position,
    )

    assert result["status"] == "ENTRY_PENDING"
    assert result["consume_signal"] is False
    assert (
        result["state"]["pending_entry_submission_state"]
        == "rejection_position_mismatch"
    )
    assert "position size 1000" in result["error"]
    lookup.assert_called_once_with(None, client_id, 101)
    position.assert_called_once_with(101)
    assert saved[-1]["status"] == "ENTRY_PENDING"


def test_response_loss_retains_identity_then_recovers_without_second_post():
    submit = Mock(side_effect=TimeoutError("response lost"))
    first_lookup = Mock(
        return_value=ExactOrderLookup(None, False, "exchange timeout")
    )
    first, saved, client_id, order = _run(
        submit_order=submit,
        lookup_order=first_lookup,
    )
    pending = copy.deepcopy(first["state"])

    assert first["status"] == "ENTRY_PENDING"
    assert first["consume_signal"] is False
    assert pending["pending_entry_client_order_id"] == client_id
    assert pending["pending_entry_submission_state"] == "submission_unknown"
    assert pending["pending_entry_post_boundary"] is True
    assert pending["pending_entry_attempts"] == 1
    submit.assert_called_once()

    forbidden_submit = Mock(
        side_effect=AssertionError("recovery submitted a duplicate")
    )
    second, second_saved, _, _ = _run(
        existing_state=pending,
        submit_order=forbidden_submit,
        lookup_order=lambda *args: ExactOrderLookup(copy.deepcopy(order), True),
    )

    assert second["ok"] is True
    assert second["status"] == "OPEN"
    assert second["state"]["client_order_id"] == client_id
    forbidden_submit.assert_not_called()
    assert second_saved[-1]["status"] == "OPEN"


def test_inconclusive_existing_identity_blocks_duplicate_submission():
    first, _, _, _ = _run(
        submit_order=Mock(side_effect=TimeoutError("lost")),
        lookup_order=lambda *args: ExactOrderLookup(None, False, "offline"),
    )
    submit = Mock(side_effect=AssertionError("duplicate submission"))

    result, saved, _, _ = _run(
        existing_state=first["state"],
        submit_order=submit,
        lookup_order=lambda *args: ExactOrderLookup(None, False, "offline"),
    )

    assert result["status"] == "ENTRY_PENDING"
    assert result["consume_signal"] is False
    submit.assert_not_called()
    assert saved[-1]["pending_entry_submission_state"] == "lookup_inconclusive"


def test_post_boundary_lookup_exception_remains_pending_without_resubmit():
    first, _, _, _ = _run(
        submit_order=Mock(side_effect=TimeoutError("lost")),
        lookup_order=lambda *args: ExactOrderLookup(None, False, "offline"),
    )
    submit = Mock(side_effect=AssertionError("duplicate submission"))

    result, saved, _, _ = _run(
        existing_state=first["state"],
        submit_order=submit,
        lookup_order=Mock(side_effect=TimeoutError("lookup unavailable")),
    )

    assert result["status"] == "ENTRY_PENDING"
    assert result["consume_signal"] is False
    assert result["state"]["pending_entry_submission_state"] == (
        "lookup_inconclusive"
    )
    assert "lookup unavailable" in result["error"]
    submit.assert_not_called()
    assert saved[-1]["status"] == "ENTRY_PENDING"


def test_conclusive_absence_after_post_boundary_never_resubmits():
    first, _, client_id, _ = _run(
        submit_order=Mock(side_effect=TimeoutError("lost")),
        lookup_order=lambda *args: ExactOrderLookup(None, False, "offline"),
    )
    submit = Mock(side_effect=AssertionError("duplicate submission"))

    result, saved, _, _ = _run(
        existing_state=first["state"],
        submit_order=submit,
        lookup_order=lambda *args: ExactOrderLookup(None, True),
    )

    assert result["ok"] is False
    assert result["status"] == "ENTRY_PENDING"
    assert result["consume_signal"] is False
    assert result["state"]["pending_entry_client_order_id"] == client_id
    assert result["state"]["pending_entry_submission_state"] == (
        "post_boundary_order_absent"
    )
    submit.assert_not_called()
    assert saved[-1]["status"] == "ENTRY_PENDING"


def test_durable_pre_post_prepared_state_can_continue_submission():
    saved = []

    with pytest.raises(LiveScoreExecutionError, match="risk changed"):
        _run(
            persist_state=lambda state: saved.append(copy.deepcopy(state)),
            final_preflight=lambda state: (_ for _ in ()).throw(
                LiveScoreExecutionError("risk changed")
            ),
        )

    prepared_state = copy.deepcopy(saved[-1])
    assert prepared_state["pending_entry_submission_state"] == "prepared"
    assert prepared_state["pending_entry_post_boundary"] is False
    assert prepared_state["pending_entry_attempts"] == 0
    lookup = Mock(side_effect=AssertionError("pre-POST state was looked up"))
    submit = Mock(
        side_effect=lambda payload: (
            _order(payload["client_order_id"]),
            {"success": True},
        )
    )

    result, _, _, _ = _run(
        existing_state=prepared_state,
        submit_order=submit,
        lookup_order=lookup,
    )

    assert result["status"] == "OPEN"
    submit.assert_called_once()
    lookup.assert_not_called()


@pytest.mark.parametrize(
    "boundary_evidence",
    (
        {"pending_entry_post_boundary": True},
        {"pending_entry_last_attempt_at_utc": "2026-07-23T04:00:01Z"},
        {"pending_entry_attempts": 1},
    ),
)
def test_contradictory_prepared_journal_never_submits(boundary_evidence):
    saved = []
    with pytest.raises(LiveScoreExecutionError, match="risk changed"):
        _run(
            persist_state=lambda state: saved.append(copy.deepcopy(state)),
            final_preflight=lambda state: (_ for _ in ()).throw(
                LiveScoreExecutionError("risk changed")
            ),
        )
    ambiguous = {**copy.deepcopy(saved[-1]), **boundary_evidence}
    submit = Mock(side_effect=AssertionError("ambiguous journal was submitted"))

    result, recovered, _, _ = _run(
        existing_state=ambiguous,
        submit_order=submit,
        lookup_order=lambda *args: ExactOrderLookup(None, True),
    )

    assert result["status"] == "ENTRY_PENDING"
    assert result["consume_signal"] is False
    submit.assert_not_called()
    assert recovered[-1]["pending_entry_submission_state"] == (
        "post_boundary_order_absent"
    )


def test_post_boundary_recovery_uses_persisted_contract_without_fresh_quote():
    first, _, client_id, order = _run(
        submit_order=Mock(side_effect=TimeoutError("lost")),
        lookup_order=lambda *args: ExactOrderLookup(None, False, "offline"),
    )
    pending = copy.deepcopy(first["state"])
    forbidden_submit = Mock(
        side_effect=AssertionError("recovery submitted a duplicate")
    )

    result, _, _, _ = _run(
        existing_state=pending,
        # Simulate a selector that now points at a different product.  The
        # durable post-boundary identity must win.
        prepared_override=_prepared("PE_3_ITM"),
        fresh_quote_override=None,
        submit_order=forbidden_submit,
        lookup_order=lambda *args: ExactOrderLookup(copy.deepcopy(order), True),
    )

    assert result["status"] == "OPEN"
    assert result["state"]["symbol"] == "C-BTC-65400-230726"
    assert result["state"]["client_order_id"] == client_id
    forbidden_submit.assert_not_called()


def test_recovery_returns_and_audits_original_durable_signal_identity():
    first, _, original_client_id, order = _run(
        submit_order=Mock(side_effect=TimeoutError("lost")),
        lookup_order=lambda *args: ExactOrderLookup(None, False, "offline"),
    )
    pending = copy.deepcopy(first["state"])
    original_signal_key = pending["score_auto_signal_key"]
    original_transition = pending["transition_id"]
    newer_signal = {
        **_signal(),
        "signal_key": "trend-score-auto|BTCUSD|5m|2026-07-23T04:00:00Z",
        "signal_bar_close_utc": "2026-07-23T04:05:00Z",
        "score": 75,
    }
    audits = []
    submit = Mock(side_effect=AssertionError("recovery submitted a duplicate"))

    result, _, _, _ = _run(
        existing_state=pending,
        signal_override=newer_signal,
        transition_override="newer-score-transition",
        fresh_quote_override=None,
        submit_order=submit,
        lookup_order=lambda *args: ExactOrderLookup(copy.deepcopy(order), True),
        audit=lambda event, payload: audits.append(
            (event, copy.deepcopy(payload))
        ),
    )

    assert result["status"] == "OPEN"
    assert result["handled_signal_key"] == original_signal_key
    assert result["handled_transition_id"] == original_transition
    assert result["state"]["client_order_id"] == original_client_id
    assert result["state"]["score_auto_signal_key"] == original_signal_key
    assert audits[-1][0] == "trend_score_live_entry_opened"
    assert audits[-1][1]["signal_key"] == original_signal_key
    assert audits[-1][1]["transition_id"] == original_transition
    submit.assert_not_called()


def test_zero_fill_recovery_consumes_original_not_current_signal():
    first, _, original_client_id, _ = _run(
        submit_order=Mock(side_effect=TimeoutError("lost")),
        lookup_order=lambda *args: ExactOrderLookup(None, False, "offline"),
    )
    pending = copy.deepcopy(first["state"])
    original_signal_key = pending["score_auto_signal_key"]
    original_transition = pending["transition_id"]
    zero_fill = _order(
        original_client_id,
        filled=0,
        state="cancelled",
    )
    newer_signal = {
        **_signal(),
        "signal_key": "trend-score-auto|BTCUSD|5m|2026-07-23T04:00:00Z",
        "signal_bar_close_utc": "2026-07-23T04:05:00Z",
    }

    result, _, _, _ = _run(
        existing_state=pending,
        signal_override=newer_signal,
        transition_override="newer-score-transition",
        fresh_quote_override=None,
        submit_order=Mock(
            side_effect=AssertionError("recovery submitted a duplicate")
        ),
        lookup_order=lambda *args: ExactOrderLookup(
            copy.deepcopy(zero_fill), True
        ),
        get_position=lambda product_id: {
            "product_id": product_id,
            "size": 0,
            "entry_price": 0,
        },
    )

    assert result["status"] == "NO_FILL"
    assert result["consume_signal"] is True
    assert result["handled_signal_key"] == original_signal_key
    assert result["handled_transition_id"] == original_transition
    assert result["state"]["last_entry_signal_key"] == original_signal_key
    assert result["state"]["last_entry_transition_id"] == original_transition


def test_delayed_move_recovery_routes_by_exchange_time_before_11am_ist():
    submitted_at = datetime(2026, 7, 23, 5, 29, 30, tzinfo=timezone.utc)
    filled_at = datetime(2026, 7, 23, 5, 29, 59, tzinfo=timezone.utc)
    recovered_at = datetime(2026, 7, 23, 5, 31, 0, tzinfo=timezone.utc)
    first, _, _, order = _run(
        zone="SHORT_MOVE",
        clock_value=submitted_at,
        submit_order=Mock(side_effect=TimeoutError("lost")),
        lookup_order=lambda *args: ExactOrderLookup(None, False, "offline"),
    )
    order["created_at"] = int(filled_at.timestamp() * 1_000_000)

    result, _, _, _ = _run(
        zone="SHORT_MOVE",
        existing_state=first["state"],
        fresh_quote_override=None,
        clock_value=recovered_at,
        submit_order=Mock(
            side_effect=AssertionError("recovery submitted a duplicate")
        ),
        lookup_order=lambda *args: ExactOrderLookup(copy.deepcopy(order), True),
    )

    ist = timezone(timedelta(hours=5, minutes=30))
    assert recovered_at.astimezone(ist).hour == 11
    assert filled_at.astimezone(ist).hour == 10
    assert result["state"]["entry_at_utc"] == "2026-07-23T05:29:59Z"
    assert result["state"]["entry_time_source"] == "exchange_order.created_at"
    from dashboard import _move_position_display_slot

    assert _move_position_display_slot(result["state"]) == "morning"


def test_delayed_recovery_without_exchange_time_uses_durable_pending_time():
    submitted_at = datetime(2026, 7, 23, 5, 29, 30, tzinfo=timezone.utc)
    recovered_at = datetime(2026, 7, 23, 5, 31, 0, tzinfo=timezone.utc)
    first, _, _, order = _run(
        zone="SHORT_MOVE",
        clock_value=submitted_at,
        submit_order=Mock(side_effect=TimeoutError("lost")),
        lookup_order=lambda *args: ExactOrderLookup(None, False, "offline"),
    )

    result, _, _, _ = _run(
        zone="SHORT_MOVE",
        existing_state=first["state"],
        fresh_quote_override=None,
        clock_value=recovered_at,
        submit_order=Mock(
            side_effect=AssertionError("recovery submitted a duplicate")
        ),
        lookup_order=lambda *args: ExactOrderLookup(copy.deepcopy(order), True),
    )

    assert result["state"]["entry_at_utc"] == "2026-07-23T05:29:30Z"
    assert result["state"]["entry_time_source"] == (
        "durable_pending_entry_started_at_utc"
    )


def test_persisted_payload_corruption_blocks_recovery_before_exchange_calls():
    first, _, _, _ = _run(
        submit_order=Mock(side_effect=TimeoutError("lost")),
        lookup_order=lambda *args: ExactOrderLookup(None, False, "offline"),
    )
    pending = copy.deepcopy(first["state"])
    pending["pending_entry_payload"]["size"] = 999
    submit = Mock(side_effect=AssertionError("corrupt intent submitted"))
    lookup = Mock(side_effect=AssertionError("corrupt intent looked up"))

    with pytest.raises(LiveScoreExecutionError, match="exactly 1,000"):
        _run(
            existing_state=pending,
            fresh_quote_override=None,
            submit_order=submit,
            lookup_order=lookup,
        )

    submit.assert_not_called()
    lookup.assert_not_called()


def test_proven_fill_without_matching_realtime_position_stays_pending():
    protect = Mock(side_effect=AssertionError("unverified exposure protected"))
    result, saved, _, _ = _run(
        get_position=lambda pid: {
            "product_id": pid,
            "size": 999,
            "entry_price": 220,
        },
        protect_position=protect,
    )

    assert result["status"] == "ENTRY_PENDING"
    assert result["consume_signal"] is False
    assert result["state"]["pending_entry_submission_state"] == (
        "filled_position_mismatch"
    )
    assert result["state"]["pending_entry_proven_filled_lots"] == 1_000
    protect.assert_not_called()
    assert saved[-1]["status"] == "ENTRY_PENDING"


def test_protection_failure_invokes_verified_emergency_flatten():
    durable = {"state": None}

    def persist(state):
        durable["state"] = copy.deepcopy(state)

    def close_and_persist(state, reason):
        closed = {
            **state,
            "status": "CLOSED",
            "flat_verified": True,
            "exit_trigger": reason,
        }
        persist(closed)
        return closed

    flatten = Mock(
        side_effect=close_and_persist
    )
    result, _, _, _ = _run(
        protect_position=lambda state, started: (
            False,
            {"status": "degraded", "last_error": "no protection"},
        ),
        flatten_position=flatten,
        persist_state=persist,
        load_state=lambda: copy.deepcopy(durable["state"]),
    )

    assert result["status"] == "FLATTENED_UNPROTECTED"
    assert result["consume_signal"] is True
    assert result["flat_verified"] is True
    flatten.assert_called_once()
    assert flatten.call_args.args[1] == "protection_failure_flatten"
    assert durable["state"]["protection_verified_at_entry"] is False


@pytest.mark.parametrize("protection_succeeded", (True, False))
def test_same_cycle_close_during_protection_setup_is_never_resurrected(
    protection_succeeded,
):
    durable = {"state": None}
    writes = []

    def persist(state):
        durable["state"] = copy.deepcopy(state)
        writes.append(copy.deepcopy(state))

    def protect_and_close(state, started):
        closed = {
            **state,
            "status": "CLOSED",
            "flat_verified": True,
            "exit_trigger": "tp",
        }
        persist(closed)
        return protection_succeeded, {"status": "monitor_returned"}

    flatten = Mock(side_effect=AssertionError("already-closed cycle flattened"))
    result, _, _, _ = _run(
        persist_state=persist,
        load_state=lambda: copy.deepcopy(durable["state"]),
        protect_position=protect_and_close,
        flatten_position=flatten,
    )

    assert result["status"] == "CLOSED_DURING_PROTECTION_SETUP"
    assert result["consume_signal"] is True
    assert result["state"]["status"] == "CLOSED"
    assert result["state"]["exit_trigger"] == "tp"
    assert durable["state"]["status"] == "CLOSED"
    assert writes[-1]["status"] == "CLOSED"
    flatten.assert_not_called()


def test_changed_position_generation_after_protection_fails_closed():
    durable = {"state": None}

    def persist(state):
        durable["state"] = copy.deepcopy(state)

    def protect_and_replace_generation(state, started):
        persist(
            {
                **state,
                "status": "OPEN",
                "position_cycle_id": "different-live-cycle",
                "transition_id": "different-transition",
            }
        )
        return False, {"status": "late"}

    flatten = Mock(side_effect=AssertionError("new generation flattened"))
    result, _, _, _ = _run(
        persist_state=persist,
        load_state=lambda: copy.deepcopy(durable["state"]),
        protect_position=protect_and_replace_generation,
        flatten_position=flatten,
    )

    assert result["status"] == "POST_PROTECTION_GENERATION_CHANGED"
    assert result["consume_signal"] is True
    assert "generation changed" in result["error"]
    assert durable["state"]["position_cycle_id"] == "different-live-cycle"
    flatten.assert_not_called()


def test_failed_emergency_flatten_remains_visible_open_and_consumes_signal():
    result, saved, _, _ = _run(
        protect_position=lambda state, started: (False, {"status": "degraded"}),
        flatten_position=Mock(side_effect=RuntimeError("close unavailable")),
    )

    assert result["status"] == "UNPROTECTED_OPEN"
    assert result["consume_signal"] is True
    assert result["state"]["status"] == "OPEN"
    assert result["state"]["protection_failure_flatten_pending"] is True
    assert saved[-1]["status"] == "OPEN"


def test_final_preflight_runs_after_durable_intent_and_before_post():
    calls = []

    def persist(state):
        calls.append(("persist", state["pending_entry_submission_state"]))

    def preflight(state):
        calls.append(("preflight", state["pending_entry_client_order_id"]))
        raise LiveScoreExecutionError("external position appeared")

    submit = Mock(side_effect=AssertionError("unsafe order submitted"))
    with pytest.raises(
        LiveScoreExecutionError, match="external position appeared"
    ):
        _run(
            persist_state=persist,
            final_preflight=preflight,
            submit_order=submit,
        )
    submit.assert_not_called()
    assert calls[0] == ("persist", "prepared")
    assert calls[1][0] == "preflight"


def test_terminal_positive_fill_without_average_price_stays_pending():
    transition = "trend-score-live-transition-1"
    client_id = score_entry_client_id("alice", transition)
    order = _order(client_id)
    order["average_fill_price"] = None

    result, saved, _, _ = _run(
        submit_order=lambda payload: (copy.deepcopy(order), {"success": True}),
        lookup_order=lambda *args: ExactOrderLookup(copy.deepcopy(order), True),
    )

    assert result["status"] == "ENTRY_PENDING"
    assert result["consume_signal"] is False
    assert result["state"]["pending_entry_submission_state"] == (
        "terminal_or_fill_ambiguous"
    )
    assert saved[-1]["status"] == "ENTRY_PENDING"


@pytest.mark.parametrize(
    ("order", "expected"),
    (
        ({"state": "open"}, None),
        ({"state": "filled", "filled_size": 600}, 600),
        ({"state": "filled", "filled_size": 999.5}, None),
        ({"state": "filled", "filled_size": -1}, None),
        ({"state": "filled", "filled_size": 1_001}, None),
        ({"state": "cancelled", "unfilled_size": 750}, 250),
        ({"state": "cancelled", "unfilled_size": "not-a-number"}, None),
        ({"state": "cancelled", "unfilled_size": 1_000.5}, None),
        ({"state": "cancelled", "unfilled_size": -1}, None),
        ({"state": "cancelled", "unfilled_size": 1_001}, None),
        ({"state": "rejected"}, 0),
        ({"state": "cancelled"}, None),
    ),
)
def test_terminal_fill_requires_explicit_quantity(order, expected):
    assert terminal_filled_lots(order) == expected


def test_switch_gate_requires_flat_clean_accounted_close():
    state = {
        "status": "CLOSED",
        "product_id": 101,
        "history_pending": False,
        "accounting_status": "complete",
        "partial_exit_accounting_status": "complete",
        "protection_cleanup_pending": False,
    }
    flat = {"product_id": 101, "size": 0}
    assert switch_entry_gate(state, flat) == (True, "")

    variants = (
        ({**state, "status": "OPEN"}, flat, "not CLOSED"),
        (state, {"product_id": 101, "size": 1}, "still has"),
        ({**state, "history_pending": True}, flat, "history"),
        (
            {**state, "pending_close_client_order_id": "close-1"},
            flat,
            "identity",
        ),
        (
            {**state, "protection_cleanup_pending": True},
            flat,
            "cleanup",
        ),
        (
            {**state, "accounting_status": "pending"},
            flat,
            "accounting",
        ),
        (
            {**state, "partial_exit_accounting_status": "fee_pending"},
            flat,
            "partial-exit",
        ),
    )
    for candidate, position, message in variants:
        allowed, reason = switch_entry_gate(candidate, position)
        assert allowed is False
        assert message.lower() in reason.lower()


def test_missing_entry_fee_marks_accounting_pending_and_blocks_switch():
    transition = "trend-score-live-transition-1"
    client_id = score_entry_client_id("alice", transition)
    order = _order(client_id)
    order.pop("paid_commission")

    result, _, _, _ = _run(
        submit_order=lambda payload: (copy.deepcopy(order), {"success": True}),
    )

    assert result["status"] == "OPEN"
    assert result["state"]["accounting_status"] == "fee_pending"
    closed = {
        **result["state"],
        "status": "CLOSED",
        "pending_entry_client_order_id": None,
        "pending_entry_order_id": None,
        "pending_entry_submission_state": None,
    }
    allowed, reason = switch_entry_gate(
        closed,
        {"product_id": closed["product_id"], "size": 0},
    )
    assert allowed is False
    assert "accounting" in reason.lower()
