"""Tests for the Cockpit manual-trade panel (POST /api/cockpit/enter).

Reuses the identical LIVE execution seam as the automated score-auto
controller (see test_dashboard_trend_score_auto_live_controller.py), so
these tests focus on what is genuinely new: exclusivity (FCFS with both the
bot and other manual trades), ownership tagging, setup locking without
signal consumption, and the manual Sell MOVE ADX/premium bypass -- not the
execution seam's own fill/risk
mechanics, which have their own dedicated coverage.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

import dashboard
from risk_controls import account_entry_lock


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _live_config(**updates) -> dict:
    config = {
        "DRY_RUN": "false",
        "TREND_ENGINE_SCORE_AUTO_MODE": "live",
        "TREND_AUTO_ENTRY_MODE": "disabled",
        "MOVE_AUTO_ENTRY_MODE": "disabled",
        "MORNING_ENABLED": "false",
        "EVENING_ENABLED": "false",
        "MAX_ORDER_LOTS": "1000",
        "TP_TARGET_PNL_TREND": "500",
        "SL_TARGET_PNL_TREND": "250",
        "TSL_TARGET_PNL_TREND": "100",
        "TSL_ARM_PNL_TREND": "100",
        "TSL_TRAIL_PNL_TREND": "50",
        "SAFE_EXECUTION_ENABLED": "true",
        "ALLOW_SHORT_MOVE": "true",
        "SHORT_MAX_RISK_USD": "500",
        "TREND_RISK_BUDGET_USD": "500",
    }
    config.update(updates)
    return config


def _response_tuple(result):
    if isinstance(result, tuple):
        return result
    return result, result.status_code


@pytest.fixture
def live_account(tmp_path, monkeypatch):
    users = tmp_path / "users"
    account = users / "alice"
    account.mkdir(parents=True)
    _write(account / "config.json", _live_config())

    monkeypatch.setattr(dashboard, "USERS_DIR", users)
    monkeypatch.setattr(dashboard, "DASH_USER", "alice")
    monkeypatch.setattr(dashboard, "BOT_USER", "alice")
    monkeypatch.setattr(dashboard, "_active_user", lambda: "alice")
    monkeypatch.setattr(dashboard, "_active_creds", lambda: ("key", "secret"))
    monkeypatch.setattr(dashboard, "_trend_audit", Mock())
    monkeypatch.setattr(dashboard, "_trend_score_auto_notify", Mock())
    dashboard._basic_cache.clear()
    dashboard._trend_score_auto_health.clear()
    dashboard._trend_score_auto_cycle_locks.clear()
    dashboard._last_sync.clear()

    def forbidden(name):
        return Mock(
            side_effect=AssertionError(
                f"Cockpit test reached an unmocked exchange seam: {name}"
            )
        )

    monkeypatch.setattr(dashboard.req, "get", forbidden("HTTP GET"))
    monkeypatch.setattr(dashboard.req, "post", forbidden("HTTP POST"))
    monkeypatch.setattr(dashboard.req, "delete", forbidden("HTTP DELETE"))
    monkeypatch.setattr(
        dashboard, "_post_dashboard_order", forbidden("order submission"),
    )
    monkeypatch.setattr(dashboard, "_sign", forbidden("request signing"))
    return account


def _bot_owned_open_state() -> dict:
    return {
        "status": "OPEN", "execution_mode": "live", "dry_run": False,
        "ownership": dashboard.TREND_SCORE_AUTO_LIVE_OWNERSHIP,
        "entry_trigger": dashboard.TREND_SCORE_AUTO_TRIGGER,
        "product_id": 101, "symbol": "C-BTC-65400-240726",
        "trend_score_zone": dashboard.TREND_SCORE_CE_ZONE,
        "score_auto_signal_key": "trend-score-auto|BTCUSD|5m|2026-08-05T10:00:00Z",
        "lots": 500, "requested_lots": 1000,
        "entry_mark": 220.0, "contract_value": 0.001,
        "position_cycle_id": "cycle-bot",
    }


def _manual_owned_open_state() -> dict:
    return {
        "status": "OPEN", "execution_mode": "live", "dry_run": False,
        "ownership": dashboard.TREND_SCORE_MANUAL_LIVE_OWNERSHIP,
        "entry_trigger": "manual_cockpit_buy_pe",
        "product_id": 202, "symbol": "P-BTC-66400-240726",
    }


def _mock_execution_result(*, symbol="C-BTC-65400-240726", lots=1_000) -> dict:
    return {
        "ok": True, "status": "OPEN", "consume_signal": True,
        "order_submitted": True, "filled_lots": lots, "partial_fill": False,
        "error": None,
        "state": {
            "status": "OPEN", "symbol": symbol, "lots": lots,
            "ownership": dashboard.TREND_SCORE_MANUAL_LIVE_OWNERSHIP,
        },
    }


def _post_cockpit_enter(action: str):
    with dashboard.app.test_request_context(
        "/api/cockpit/enter", method="POST", json={"action": action},
    ):
        return _response_tuple(dashboard.api_cockpit_enter())


def _post_cockpit_preview(action: str):
    with dashboard.app.test_request_context(
        "/api/cockpit/preview", method="POST", json={"action": action},
    ):
        return _response_tuple(dashboard.api_cockpit_preview())


# ---------------------------------------------------------------------------
# Input validation / boundary checks
# ---------------------------------------------------------------------------

def test_cockpit_enter_rejects_an_unknown_action(live_account):
    response, status = _post_cockpit_enter("buy_the_dip")
    assert status == 400
    assert "action" in response.get_json()["error"]


def test_cockpit_enter_refuses_outside_live_trading_mode(live_account, monkeypatch):
    monkeypatch.setattr(
        dashboard, "_trading_mode_payload",
        lambda: {"dry_run_mode": True, "mode_revision": "rev-1"},
    )
    response, status = _post_cockpit_enter("buy_ce")
    assert status == 409
    assert "LIVE" in response.get_json()["error"]


# ---------------------------------------------------------------------------
# FCFS exclusivity -- the core safety property of this feature
# ---------------------------------------------------------------------------

def test_cockpit_enter_refused_while_the_account_entry_lock_is_held(
    live_account,
):
    """Whichever side -- the automated 15s cycle or a Cockpit click --
    acquires this lock first wins; the other is refused immediately.  This
    is the exact mutex _maybe_auto_trend_score_live_cycle takes."""
    with account_entry_lock(live_account, "holder") as held:
        assert held
        response, status = _post_cockpit_enter("buy_ce")
    assert status == 409
    assert "progress" in response.get_json()["error"]


def test_cockpit_enter_refused_when_a_bot_owned_position_is_open(
    live_account, monkeypatch,
):
    _write(live_account / "trend_state.json", _bot_owned_open_state())
    execute = Mock()
    monkeypatch.setattr(dashboard, "_trend_score_auto_live_execute", execute)

    response, status = _post_cockpit_enter("buy_pe")

    assert status == 409
    assert "already open" in response.get_json()["error"].lower()
    execute.assert_not_called()


def test_cockpit_enter_refused_when_another_manual_position_is_open(
    live_account, monkeypatch,
):
    _write(live_account / "trend_state.json", _manual_owned_open_state())
    execute = Mock()
    monkeypatch.setattr(dashboard, "_trend_score_auto_live_execute", execute)

    response, status = _post_cockpit_enter("sell_move")

    assert status == 409
    error = response.get_json()["error"].lower()
    assert "manually opened" in error
    assert "external" not in error
    execute.assert_not_called()


def test_cockpit_enter_refused_when_an_adopted_external_position_is_open(
    live_account, monkeypatch,
):
    _write(live_account / "trend_state.json", {
        "status": "OPEN", "entry_trigger": "exchange_sync",
        "ownership": "external_protection_only",
        "operator_authorized_protection_only": True,
        "product_id": 303,
    })
    execute = Mock()
    monkeypatch.setattr(dashboard, "_trend_score_auto_live_execute", execute)

    response, status = _post_cockpit_enter("buy_move")

    assert status == 409
    assert "protected" in response.get_json()["error"].lower()
    execute.assert_not_called()


# ---------------------------------------------------------------------------
# Successful entries -- ownership tagging and execution-seam wiring
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("action", "zone", "side"),
    (
        ("buy_ce", dashboard.TREND_SCORE_CE_ZONE, "long"),
        ("buy_pe", dashboard.TREND_SCORE_PE_ZONE, "long"),
        ("buy_move", dashboard.TREND_SCORE_LONG_MOVE_ZONE, "long"),
        ("sell_move", dashboard.TREND_SCORE_MOVE_ZONE, "short"),
    ),
)
def test_cockpit_enter_tags_manual_ownership_for_every_trade_type(
    live_account, monkeypatch, action, zone, side,
):
    prepared = {
        "zone": zone, "side": side, "symbol": "X", "product_id": 1,
        "lots": 1_000,
    }
    monkeypatch.setattr(
        dashboard, "_cockpit_market_snapshot", lambda: {"market": {"spot": 65_000}},
    )
    monkeypatch.setattr(
        dashboard, "_cockpit_prepare_manual_entry",
        lambda action_arg, snapshot: copy.deepcopy(prepared),
    )
    execute = Mock(return_value=_mock_execution_result())
    monkeypatch.setattr(dashboard, "_trend_score_auto_live_execute", execute)

    response, status = _post_cockpit_enter(action)

    assert status == 200
    assert response.get_json()["ok"] is True
    execute.assert_called_once()
    call_kwargs = execute.call_args.kwargs
    assert call_kwargs["ownership"] == dashboard.TREND_SCORE_MANUAL_LIVE_OWNERSHIP
    assert call_kwargs["prepared"]["zone"] == zone
    assert call_kwargs["signal"]["signal_key"].startswith(
        f"manual-cockpit|{action}|"
    )
    saved = json.loads(
        (live_account / "config.json").read_text(encoding="utf-8")
    )
    assert saved["TREND_ENGINE_SCORE_AUTO_MODE"] == "disabled"
    assert response.get_json()["bot_automation_mode"] == "disabled"
    dashboard._trend_score_auto_notify.assert_called_once()


def test_cockpit_enter_records_setup_lock_without_consuming_engine_signal(
    live_account, monkeypatch,
):
    """A confirmed manual fill activates Reset Zone Lock without adding its
    synthetic key to the completed engine-signal ledger."""
    monkeypatch.setattr(
        dashboard, "_cockpit_market_snapshot", lambda: {"market": {"spot": 65_000}},
    )
    monkeypatch.setattr(
        dashboard, "_cockpit_prepare_manual_entry",
        lambda action, snapshot: {
            "zone": dashboard.TREND_SCORE_CE_ZONE, "side": "long",
            "symbol": "C-BTC-1", "product_id": 1, "lots": 1_000,
        },
    )
    monkeypatch.setattr(
        dashboard, "_trend_score_auto_live_execute",
        Mock(return_value=_mock_execution_result()),
    )
    response, status = _post_cockpit_enter("buy_ce")

    assert status == 200
    ledger_path = live_account / dashboard.TREND_SCORE_AUTO_LEDGER_FILE
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert ledger["signals"] == {}
    assert ledger["setup_lock"]["target_zone"] == dashboard.TREND_SCORE_CE_ZONE
    assert ledger["setup_lock"]["source_action"] == "COCKPIT_BUY_CE"
    assert ledger["setup_lock"]["source_signal_key"].startswith(
        "manual-cockpit|buy_ce|"
    )


def test_cockpit_bot_stays_off_until_manually_reenabled_after_close(
    live_account, monkeypatch,
):
    monkeypatch.setattr(
        dashboard, "_cockpit_market_snapshot",
        lambda: {"market": {"spot": 65_000}},
    )
    monkeypatch.setattr(
        dashboard, "_cockpit_prepare_manual_entry",
        lambda action, snapshot: {
            "zone": dashboard.TREND_SCORE_PE_ZONE,
            "side": "long",
            "symbol": "P-BTC-1",
            "product_id": 1,
            "lots": 1_000,
        },
    )
    monkeypatch.setattr(
        dashboard,
        "_trend_score_auto_live_execute",
        Mock(return_value=_mock_execution_result(symbol="P-BTC-1")),
    )

    response, status = _post_cockpit_enter("buy_pe")

    assert status == 200
    assert response.get_json()["bot_automation_mode"] == "disabled"
    assert dashboard._trend_score_auto_mode() == "disabled"

    # Closing the manually owned trade must not silently turn automation on.
    _write(live_account / "trend_state.json", {"status": "CLOSED"})
    assert dashboard._trend_score_auto_mode() == "disabled"

    # The only re-enable path is a later explicit operator action.
    with dashboard.app.test_request_context(
        "/api/config",
        method="POST",
        json={"TREND_ENGINE_SCORE_AUTO_MODE": "live"},
    ):
        enabled = dashboard.set_config()
    enabled_body, enabled_status = (
        enabled if isinstance(enabled, tuple)
        else (enabled, enabled.status_code)
    )
    assert enabled_status == 200
    assert enabled_body.get_json()["ok"] is True
    assert dashboard._trend_score_auto_mode() == "live"


def test_cockpit_sell_move_never_evaluates_the_automated_adx_gate(
    live_account, monkeypatch,
):
    """Manual Sell MOVE deliberately bypasses the automated calm-market
    (5m ADX <= 25) gate -- the operator substitutes their own judgement.
    That gate is applied only inside plan_score_transition, which the
    manual path must never call."""
    monkeypatch.setattr(
        dashboard, "_cockpit_market_snapshot", lambda: {"market": {"spot": 65_000}},
    )
    monkeypatch.setattr(
        dashboard, "_cockpit_prepare_manual_entry",
        lambda action, snapshot: {
            "zone": dashboard.TREND_SCORE_MOVE_ZONE, "side": "short",
            "symbol": "MV-BTC-1", "product_id": 1, "lots": 1_000,
        },
    )
    monkeypatch.setattr(
        dashboard, "_trend_score_auto_live_execute",
        Mock(return_value=_mock_execution_result(symbol="MV-BTC-1")),
    )
    plan = Mock()
    monkeypatch.setattr(dashboard, "plan_score_transition", plan)

    response, status = _post_cockpit_enter("sell_move")

    assert status == 200
    plan.assert_not_called()


def test_cockpit_enter_surfaces_a_failed_execution_without_a_500(
    live_account, monkeypatch,
):
    monkeypatch.setattr(
        dashboard, "_cockpit_market_snapshot", lambda: {"market": {"spot": 65_000}},
    )
    monkeypatch.setattr(
        dashboard, "_cockpit_prepare_manual_entry",
        lambda action, snapshot: {
            "zone": dashboard.TREND_SCORE_CE_ZONE, "side": "long",
            "symbol": "C-BTC-1", "product_id": 1, "lots": 1_000,
        },
    )
    monkeypatch.setattr(
        dashboard, "_trend_score_auto_live_execute",
        Mock(return_value={
            "ok": False, "status": "NO_FILL", "consume_signal": True,
            "order_submitted": True, "filled_lots": 0, "partial_fill": False,
            "error": "no fill within the IOC window", "state": {},
        }),
    )

    response, status = _post_cockpit_enter("buy_ce")

    assert status == 409
    payload = response.get_json()
    assert payload["ok"] is False
    assert payload["status"] == "NO_FILL"
    dashboard._trend_score_auto_notify.assert_not_called()
    saved = json.loads(
        (live_account / "config.json").read_text(encoding="utf-8")
    )
    assert saved["TREND_ENGINE_SCORE_AUTO_MODE"] == "live"
    ledger_path = live_account / dashboard.TREND_SCORE_AUTO_LEDGER_FILE
    assert not ledger_path.exists()


def test_cockpit_enter_refuses_when_a_contract_cannot_be_selected(
    live_account, monkeypatch,
):
    """A preparation failure (e.g. no exact executable contract) must fail
    closed before any order is attempted."""
    monkeypatch.setattr(
        dashboard, "_cockpit_market_snapshot", lambda: {"market": {"spot": 65_000}},
    )

    def raise_no_contract(action, snapshot):
        raise RuntimeError("No exact executable 2-step ITM CALL contract is available")

    monkeypatch.setattr(
        dashboard, "_cockpit_prepare_manual_entry", raise_no_contract,
    )
    execute = Mock()
    monkeypatch.setattr(dashboard, "_trend_score_auto_live_execute", execute)

    response, status = _post_cockpit_enter("buy_ce")

    assert status == 400
    assert "executable" in response.get_json()["error"].lower()
    execute.assert_not_called()


# ---------------------------------------------------------------------------
# Preview -- read-only contract/price resolution for the confirm prompt
# ---------------------------------------------------------------------------

def test_cockpit_preview_rejects_an_unknown_action(live_account):
    response, status = _post_cockpit_preview("buy_the_dip")
    assert status == 400
    assert "action" in response.get_json()["error"]


def test_cockpit_preview_refuses_outside_live_trading_mode(
    live_account, monkeypatch,
):
    monkeypatch.setattr(
        dashboard, "_trading_mode_payload",
        lambda: {"dry_run_mode": True, "mode_revision": "rev-1"},
    )
    response, status = _post_cockpit_preview("buy_ce")
    assert status == 409
    assert "LIVE" in response.get_json()["error"]


def test_cockpit_preview_refuses_without_credentials(live_account, monkeypatch):
    monkeypatch.setattr(dashboard, "_active_creds", lambda: (None, None))
    response, status = _post_cockpit_preview("buy_ce")
    assert status == 409
    assert "credentials" in response.get_json()["error"].lower()


def test_cockpit_preview_returns_the_resolved_contract_and_price(
    live_account, monkeypatch,
):
    prepared = {
        "zone": dashboard.TREND_SCORE_CE_ZONE, "side": "long",
        "option_type": "CE", "instrument_kind": "BTC_OPTION",
        "symbol": "C-BTC-65400-060826", "strike": 65_400.0,
        "lots": 708, "entry_price": 220.5, "contract_value": 0.001,
    }
    snapshot_fn = Mock(return_value={"market": {"spot": 65_850}})
    monkeypatch.setattr(dashboard, "_cockpit_market_snapshot", snapshot_fn)
    prepare = Mock(return_value=prepared)
    monkeypatch.setattr(dashboard, "_cockpit_prepare_manual_entry", prepare)
    execute = Mock(
        side_effect=AssertionError("preview must never reach the execution seam")
    )
    monkeypatch.setattr(dashboard, "_trend_score_auto_live_execute", execute)

    response, status = _post_cockpit_preview("buy_ce")

    assert status == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["symbol"] == "C-BTC-65400-060826"
    assert body["strike"] == 65_400.0
    assert body["lots"] == 708
    assert body["entry_price"] == 220.5
    assert body["side"] == "long"
    assert body["instrument_kind"] == "BTC_OPTION"
    prepare.assert_called_once_with("buy_ce", snapshot_fn.return_value)
    execute.assert_not_called()


def test_cockpit_preview_surfaces_a_contract_resolution_failure_without_a_500(
    live_account, monkeypatch,
):
    monkeypatch.setattr(
        dashboard, "_cockpit_market_snapshot", lambda: {"market": {"spot": 65_000}},
    )

    def raise_no_contract(action, snapshot):
        raise RuntimeError("No exact executable 2-step ITM CALL contract is available")

    monkeypatch.setattr(
        dashboard, "_cockpit_prepare_manual_entry", raise_no_contract,
    )

    response, status = _post_cockpit_preview("buy_ce")

    assert status == 400
    assert "executable" in response.get_json()["error"].lower()


def test_cockpit_preview_ignores_an_open_position_and_the_entry_lock(
    live_account, monkeypatch,
):
    """Preview is purely a read/compute step -- it must not take the entry
    lock or refuse just because a position is already open (the real
    /api/cockpit/enter route is the sole authority on whether the trade is
    actually allowed; this only resolves what it would look like)."""
    _write(live_account / "trend_state.json", _bot_owned_open_state())
    monkeypatch.setattr(
        dashboard, "_cockpit_market_snapshot", lambda: {"market": {"spot": 65_000}},
    )
    monkeypatch.setattr(
        dashboard, "_cockpit_prepare_manual_entry",
        lambda action, snapshot: {
            "zone": dashboard.TREND_SCORE_CE_ZONE, "side": "long",
            "option_type": "CE", "instrument_kind": "BTC_OPTION",
            "symbol": "C-BTC-1", "strike": 65_000.0,
            "lots": 1_000, "entry_price": 200.0, "contract_value": 0.001,
        },
    )

    with account_entry_lock(live_account, "someone-else") as held:
        assert held
        response, status = _post_cockpit_preview("buy_ce")

    assert status == 200
    assert response.get_json()["ok"] is True


# ---------------------------------------------------------------------------
# Current Position display parity (requirement: shown exactly like a bot trade)
# ---------------------------------------------------------------------------

def test_manual_trade_appears_in_today_trades_like_a_bot_trade(
    live_account, monkeypatch,
):
    now = dashboard.datetime.now(dashboard.timezone.utc)
    manual_state = {
        **_manual_owned_open_state(),
        "lots": 700, "entry_mark": 240.0, "contract_value": 0.001,
        "entry_date": now.strftime("%Y-%m-%d"),
        "entry_time_utc": now.strftime("%H:%M:%S"),
    }
    _write(live_account / "trend_state.json", manual_state)
    monkeypatch.setattr(dashboard, "_enrich_live", lambda state: state)

    with dashboard.app.test_request_context("/api/today-trades"):
        response, status = _response_tuple(dashboard.api_today_trades())

    assert status == 200
    trades = response.get_json()
    trend_trades = [t for t in trades if t.get("symbol") == manual_state["symbol"]]
    assert len(trend_trades) == 1
    assert trend_trades[0]["status"] == "OPEN"
    assert trend_trades[0]["ownership"] == dashboard.TREND_SCORE_MANUAL_LIVE_OWNERSHIP


# ---------------------------------------------------------------------------
# _cockpit_prepare_manual_entry -- per-trade-type contract/zone selection
# ---------------------------------------------------------------------------

def _snapshot(spot=65_000.0):
    return {"market": {"spot": spot}, "option_contracts": []}


def test_prepare_buy_ce_selects_the_2_step_itm_call_zone(live_account, monkeypatch):
    selection = {
        "zone": dashboard.TREND_SCORE_CE_ZONE, "symbol": "C-BTC-1",
        "product_id": 1, "strike": 65_000, "expiry": "2026-08-06T12:00:00Z",
        "executable_contract": {
            "contract_value": "0.001", "ask_size": 5000,
            "quote_timestamp": dashboard.datetime.now(dashboard.timezone.utc).isoformat(),
        },
    }
    select = Mock(return_value=selection)
    monkeypatch.setattr(dashboard, "select_directional_option", select)
    monkeypatch.setattr(dashboard, "_fetch_live_vanilla_products", lambda: [])
    monkeypatch.setattr(
        dashboard, "_trend_score_auto_live_affordable_entry", lambda p: p,
    )

    prepared = dashboard._cockpit_prepare_manual_entry("buy_ce", _snapshot())

    assert prepared["zone"] == dashboard.TREND_SCORE_CE_ZONE
    assert prepared["side"] == "long"
    assert prepared["instrument_kind"] == "BTC_OPTION"
    assert select.call_args.kwargs["zone"] == dashboard.TREND_SCORE_CE_ZONE
    # Cockpit always restricts to today's IST expiry (never rolls to
    # tomorrow) but, unlike the automated controller, does not enforce the
    # standard 90-minute floor -- the operator's own judgement substitutes.
    assert select.call_args.kwargs["today_only"] is True
    assert select.call_args.kwargs["min_time_to_expiry_seconds"] == 0


def test_prepare_sell_move_keeps_short_zone_and_checks_short_move_eligibility(
    live_account, monkeypatch,
):
    selection = {
        "zone": dashboard.TREND_SCORE_MOVE_ZONE, "symbol": "MV-BTC-1",
        "product_id": 2, "expiry": "2026-08-06T12:00:00Z",
    }
    select = Mock(return_value=selection)
    monkeypatch.setattr(dashboard, "select_move_contract", select)
    monkeypatch.setattr(dashboard, "_fetch_live_mv_products", lambda: [])
    quote_fn = Mock(return_value={
        "entry_price": 350.0, "entry_depth": 10, "side": "sell",
    })
    monkeypatch.setattr(dashboard, "_trend_score_auto_move_quote", quote_fn)
    eligibility = Mock(return_value={"quoted_premium_usd": 350.0})
    monkeypatch.setattr(
        dashboard, "_trend_score_auto_short_move_eligibility", eligibility,
    )
    monkeypatch.setattr(
        dashboard, "_trend_score_auto_live_affordable_entry", lambda p: p,
    )

    prepared = dashboard._cockpit_prepare_manual_entry("sell_move", _snapshot())

    assert prepared["zone"] == dashboard.TREND_SCORE_MOVE_ZONE
    assert prepared["side"] == "short"
    assert quote_fn.call_args.kwargs.get("side", "sell") == "sell"
    assert select.call_args.kwargs["today_only"] is True
    assert select.call_args.kwargs["min_time_to_expiry_seconds"] == 0
    eligibility.assert_called_once()
    # The 90-minute liquidity floor is bypassed for a Cockpit entry --
    # the automated ADX, 90-minute, and $300-premium checks are bypassed.
    # The strategy-wide weekday blackout remains in effect.
    assert eligibility.call_args.kwargs["enforce_min_tte"] is False
    assert eligibility.call_args.kwargs["enforce_min_premium"] is False


def test_cockpit_sell_move_allows_premium_below_300(live_account):
    selection = {"expiry": "2026-08-08T18:00:00+00:00"}
    quote = {"bid": 250.0}
    now = dashboard.datetime.fromisoformat("2026-08-08T12:00:00+00:00")

    result = dashboard._trend_score_auto_short_move_eligibility(
        selection,
        quote,
        now=now,
        enforce_min_tte=False,
        enforce_min_premium=False,
    )

    assert result["quoted_premium_usd"] == 250.0
    assert result["minimum_premium_usd"] == 0


def test_automated_sell_move_still_enforces_premium_above_300(live_account):
    selection = {"expiry": "2026-08-08T18:00:00+00:00"}
    quote = {"bid": 250.0}
    now = dashboard.datetime.fromisoformat("2026-08-08T12:00:00+00:00")

    with pytest.raises(RuntimeError, match="premium must be above"):
        dashboard._trend_score_auto_short_move_eligibility(
            selection,
            quote,
            now=now,
        )


def test_prepare_buy_move_overrides_zone_and_side_and_skips_short_eligibility(
    live_account, monkeypatch,
):
    """Buy MOVE has no automated precedent -- it must not be sized or
    validated as a short straddle, and the SHORT-specific weekday-blackout/
    premium-floor messaging must not be attached to a long trade."""
    selection = {
        "zone": dashboard.TREND_SCORE_MOVE_ZONE, "symbol": "MV-BTC-1",
        "product_id": 2, "expiry": "2026-08-06T12:00:00Z",
    }
    select = Mock(return_value=selection)
    monkeypatch.setattr(dashboard, "select_move_contract", select)
    monkeypatch.setattr(dashboard, "_fetch_live_mv_products", lambda: [])
    quote_fn = Mock(return_value={
        "entry_price": 355.0, "entry_depth": 10, "side": "buy",
    })
    monkeypatch.setattr(dashboard, "_trend_score_auto_move_quote", quote_fn)
    eligibility = Mock()
    monkeypatch.setattr(
        dashboard, "_trend_score_auto_short_move_eligibility", eligibility,
    )
    monkeypatch.setattr(
        dashboard, "_trend_score_auto_live_affordable_entry", lambda p: p,
    )

    prepared = dashboard._cockpit_prepare_manual_entry("buy_move", _snapshot())

    assert prepared["zone"] == dashboard.TREND_SCORE_LONG_MOVE_ZONE
    assert prepared["side"] == "long"
    assert quote_fn.call_args.kwargs["side"] == "buy"
    assert select.call_args.kwargs["today_only"] is True
    assert select.call_args.kwargs["min_time_to_expiry_seconds"] == 0
    eligibility.assert_not_called()
    assert "move_eligibility" not in prepared
