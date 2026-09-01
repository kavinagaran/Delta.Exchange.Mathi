"""Tests for the Cockpit manual-trade panel (POST /api/cockpit/enter).

LIVE reuses the automated score controller's exchange seam; DRY RUN writes
only an isolated paper position. These tests focus on Cockpit-specific mode
routing, exclusivity, ownership, setup locking without signal consumption,
and the manual Sell MOVE ADX/premium bypass. The underlying fill and risk
mechanics retain their dedicated coverage.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

import dashboard
from risk_controls import account_entry_lock

# Captured before ``live_account`` stubs it, so the override tests can
# exercise the real authorization path while keeping the fixture's
# unmocked-exchange-seam guards in place.
_REAL_REQUIRE_ELIGIBLE = dashboard._cockpit_require_eligible_setup


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _live_config(**updates) -> dict:
    config = {
        "DRY_RUN": "false",
        "TREND_ENGINE_SCORE_AUTO_MODE": "disabled",
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
    monkeypatch.setattr(
        dashboard,
        "_cockpit_require_eligible_setup",
        lambda setup, action: {"data_quality": "OK"},
    )
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


def test_cockpit_setup_matrix_maps_bullish_bearish_and_calm_sells():
    common = [
        {"name": "higher_timeframe_trend", "score": 60, "available": True},
        {"name": "lower_timeframe_momentum", "score": 55, "available": True},
        {"name": "rsi_momentum", "score": 40, "available": True},
        {"name": "market_structure", "score": 35, "available": True},
        {"name": "breakout_quality", "score": 0, "available": True},
        {"name": "order_flow", "score": None, "available": False},
    ]
    bullish = dashboard._cockpit_setup_eligibility({
        "data_quality": "OK", "trend_score": 52, "trigger_adx": 31,
        "components": common, "reason_codes": [],
    })
    assert bullish["trend_bullish"]["eligible"] is True
    assert set(bullish["trend_bullish"]["actions"]) == {"buy_ce", "sell_pe"}
    assert bullish["trend_bearish"]["eligible"] is False

    calm = dashboard._cockpit_setup_eligibility({
        "data_quality": "OK", "trend_score": 12, "trigger_adx": 18,
        "components": common, "reason_codes": [],
    })
    assert calm["calm_range"]["eligible"] is True
    assert set(calm["calm_range"]["actions"]) == {
        "sell_ce", "sell_pe", "sell_move",
    }


def _setup_for_action(action: str) -> str:
    return {
        "buy_ce": "trend_bullish",
        "sell_pe": "trend_bullish",
        "buy_pe": "trend_bearish",
        "sell_ce": "trend_bearish",
        "buy_move": "volatility_expansion",
        "sell_move": "calm_range",
    }.get(action, "trend_bullish")


def _post_cockpit_enter(action: str, setup: str | None = None):
    with dashboard.app.test_request_context(
        "/api/cockpit/enter", method="POST",
        json={"action": action, "setup": setup or _setup_for_action(action)},
    ):
        return _response_tuple(dashboard.api_cockpit_enter())


def _post_cockpit_preview(action: str, setup: str | None = None):
    with dashboard.app.test_request_context(
        "/api/cockpit/preview", method="POST",
        json={"action": action, "setup": setup or _setup_for_action(action)},
    ):
        return _response_tuple(dashboard.api_cockpit_preview())


# ---------------------------------------------------------------------------
# Input validation / boundary checks
# ---------------------------------------------------------------------------

def test_cockpit_enter_rejects_an_unknown_action(live_account):
    response, status = _post_cockpit_enter("buy_the_dip")
    assert status == 400
    assert "action" in response.get_json()["error"]


def test_cockpit_enter_in_dry_run_opens_only_an_isolated_simulation(
    live_account, monkeypatch,
):
    _write(live_account / "config.json", _live_config(
        DRY_RUN="true", TREND_ENGINE_SCORE_AUTO_MODE="dry_run",
    ))
    now = dashboard.datetime.now(dashboard.timezone.utc)
    prepared = {
        "zone": dashboard.TREND_SCORE_CE_ZONE,
        "side": "long",
        "option_type": "CE",
        "instrument_kind": "BTC_OPTION",
        "symbol": "C-BTC-65400-140826",
        "product_id": 101,
        "strike": 65_400.0,
        "settlement": "2026-08-14T12:00:00Z",
        "contract_value": 0.001,
        "lots": 1_000,
        "entry_price": 220.5,
        "entry_depth": 2_000,
        "quote_timestamp": now.isoformat(),
        "quote_snapshot": {"ask": 220.5, "ask_size": 2_000},
    }
    snapshot = {"market": {"spot": 65_850}}
    market = Mock(return_value=snapshot)
    prepare = Mock(return_value=copy.deepcopy(prepared))
    execute = Mock(side_effect=AssertionError("DRY RUN reached LIVE execution"))
    monkeypatch.setattr(dashboard, "_cockpit_market_snapshot", market)
    monkeypatch.setattr(dashboard, "_cockpit_prepare_manual_entry", prepare)
    monkeypatch.setattr(
        dashboard, "_trend_score_auto_dry_risk_snapshot", lambda *_: {},
    )
    monkeypatch.setattr(dashboard, "_trend_score_auto_live_execute", execute)

    response, status = _post_cockpit_enter("buy_ce")

    assert status == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["dry_run"] is True
    assert body["destination"] == "dry_run_dashboard"
    assert body["order_submitted"] is False
    assert body["bot_automation_mode"] == "dry_run"
    execute.assert_not_called()
    market.assert_called_once_with(dry_run=True)
    prepare.assert_called_once_with("buy_ce", snapshot, dry_run=True)
    state_path = live_account / "dry_run" / "trend_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["status"] == "OPEN"
    assert state["dry_run"] is True
    assert state["ownership"] == dashboard.TREND_SCORE_MANUAL_DRY_OWNERSHIP
    assert state["entry_trigger"] == "manual_cockpit_buy_ce"
    assert state["cockpit_destination"] == "dry_run_dashboard"
    monkeypatch.setattr(dashboard, "_enrich_dry_state", lambda value: value)
    with dashboard.app.test_request_context("/api/today-trades"):
        dashboard_response, dashboard_status = _response_tuple(
            dashboard.api_today_trades()
        )
    assert dashboard_status == 200
    dashboard_rows = dashboard_response.get_json()
    assert len(dashboard_rows) == 1
    assert dashboard_rows[0]["simulation_id"] == state["simulation_id"]
    assert dashboard_rows[0]["symbol"] == state["symbol"]


def test_cockpit_enter_does_not_require_or_change_bot_mode(
    live_account, monkeypatch,
):
    config = _live_config(TREND_ENGINE_SCORE_AUTO_MODE="live")
    _write(live_account / "config.json", config)
    monkeypatch.setattr(
        dashboard, "_cockpit_market_snapshot",
        lambda: {"market": {"spot": 65_000}},
    )
    monkeypatch.setattr(
        dashboard, "_cockpit_prepare_manual_entry",
        lambda action, snapshot: {
            "zone": dashboard.TREND_SCORE_CE_ZONE,
            "side": "long",
            "symbol": "C-BTC-1",
            "product_id": 1,
            "lots": 1_000,
        },
    )
    monkeypatch.setattr(
        dashboard, "_trend_score_auto_live_execute",
        Mock(return_value=_mock_execution_result(symbol="C-BTC-1")),
    )

    response, status = _post_cockpit_enter("buy_ce")

    assert status == 200
    assert response.get_json()["bot_automation_mode"] == "live"
    saved = json.loads(
        (live_account / "config.json").read_text(encoding="utf-8")
    )
    assert saved["TREND_ENGINE_SCORE_AUTO_MODE"] == "live"


def test_cockpit_preview_does_not_require_bot_off(live_account, monkeypatch):
    config = _live_config(TREND_ENGINE_SCORE_AUTO_MODE="live")
    _write(live_account / "config.json", config)
    monkeypatch.setattr(
        dashboard, "_cockpit_market_snapshot",
        lambda: {"market": {"spot": 65_000}},
    )
    monkeypatch.setattr(
        dashboard, "_cockpit_prepare_manual_entry",
        lambda action, snapshot: {
            "zone": dashboard.TREND_SCORE_CE_ZONE,
            "side": "long",
            "option_type": "CE",
            "instrument_kind": "BTC_OPTION",
            "symbol": "C-BTC-1",
            "strike": 65_000,
            "product_id": 1,
            "lots": 1_000,
            "entry_price": 200,
            "contract_value": 0.001,
        },
    )

    response, status = _post_cockpit_preview("buy_ce")

    assert status == 200
    assert response.get_json()["ok"] is True
    assert dashboard._trend_score_auto_mode() == "live"


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
        ("sell_ce", dashboard.TREND_SCORE_SHORT_CE_ZONE, "short"),
        ("sell_pe", dashboard.TREND_SCORE_SHORT_PE_ZONE, "short"),
        ("sell_move", dashboard.TREND_SCORE_MOVE_ZONE, "short"),
    ),
)
def test_cockpit_enter_tags_manual_ownership_for_every_trade_type(
    live_account, monkeypatch, action, zone, side,
):
    _write(live_account / "config.json", _live_config(
        TREND_ENGINE_SCORE_AUTO_MODE="live",
    ))
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
    assert saved["TREND_ENGINE_SCORE_AUTO_MODE"] == "live"
    assert response.get_json()["bot_automation_mode"] == "live"
    dashboard._trend_score_auto_notify.assert_called_once()


def test_cockpit_enter_does_not_record_setup_lock_or_consume_engine_signal(
    live_account, monkeypatch,
):
    """A confirmed manual fill does not arm the bot's zone lock or consume
    its synthetic key in the completed engine-signal ledger."""
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
    assert ledger["setup_lock"] is None


def test_cockpit_entry_preserves_bot_setting_after_open_and_close(
    live_account, monkeypatch,
):
    _write(live_account / "config.json", _live_config(
        TREND_ENGINE_SCORE_AUTO_MODE="live",
    ))
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
    assert response.get_json()["bot_automation_mode"] == "live"
    assert dashboard._trend_score_auto_mode() == "live"

    # Closing the manually owned trade must not mutate automation either.
    _write(live_account / "trend_state.json", {"status": "CLOSED"})
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
    assert saved["TREND_ENGINE_SCORE_AUTO_MODE"] == "disabled"
    ledger_path = live_account / dashboard.TREND_SCORE_AUTO_LEDGER_FILE
    assert not ledger_path.exists()


def test_cockpit_retries_once_with_delta_affordable_lots_after_margin_rejection(
    live_account, monkeypatch,
):
    monkeypatch.setattr(
        dashboard, "_cockpit_market_snapshot",
        lambda: {"market": {"spot": 65_000}},
    )
    monkeypatch.setattr(
        dashboard, "_cockpit_prepare_manual_entry",
        lambda action, snapshot: {
            "zone": dashboard.TREND_SCORE_SHORT_CE_ZONE,
            "side": "short",
            "symbol": "C-BTC-65000-140826",
            "product_id": 101,
            "lots": 1_000,
            "live_affordability": {
                "configured_lots": 1_000,
                "selected_lots": 1_000,
                "affordable_lots": 1_000,
            },
        },
    )
    rejection = {
        "code": "insufficient_margin",
        "context": {
            "available_balance": "2.83347541",
            "required_additional_balance": "2.0594530211466667",
        },
    }
    calls = []

    def execute(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        if len(calls) == 1:
            return {
                "ok": False,
                "status": "REJECTED",
                "order_submitted": True,
                "state": {
                    "status": "IDLE",
                    "last_entry_rejection": rejection,
                    "last_entry_rejection_exact_absence": True,
                    "last_entry_position_verified_flat": True,
                },
                "error": str(rejection),
            }
        return _mock_execution_result(
            symbol=kwargs["prepared"]["symbol"],
            lots=kwargs["prepared"]["lots"],
        )

    monkeypatch.setattr(
        dashboard, "_trend_score_auto_live_execute", Mock(side_effect=execute),
    )

    response, status = _post_cockpit_enter("sell_ce")

    assert status == 200
    body = response.get_json()
    assert body["ok"] is True
    assert len(calls) == 2
    expected = dashboard._downsized_lots(1_000, rejection["context"])
    assert calls[0]["prepared"]["lots"] == 1_000
    assert calls[1]["prepared"]["lots"] == expected
    assert body["state"]["lots"] == expected
    assert body["margin_retry"] == {
        "attempted_lots": 1_000,
        "retried_lots": expected,
    }


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


def test_cockpit_preview_in_dry_run_needs_no_credentials(
    live_account, monkeypatch,
):
    _write(live_account / "config.json", _live_config(DRY_RUN="true"))
    monkeypatch.setattr(dashboard, "_active_creds", lambda: (None, None))
    snapshot = {"market": {"spot": 65_000}}
    monkeypatch.setattr(
        dashboard, "_cockpit_market_snapshot", Mock(return_value=snapshot),
    )
    monkeypatch.setattr(
        dashboard,
        "_cockpit_prepare_manual_entry",
        Mock(return_value={
            "zone": dashboard.TREND_SCORE_CE_ZONE,
            "side": "long",
            "option_type": "CE",
            "instrument_kind": "BTC_OPTION",
            "symbol": "C-BTC-65000-140826",
            "strike": 65_000.0,
            "lots": 1_000,
            "entry_price": 200.0,
            "contract_value": 0.001,
        }),
    )
    response, status = _post_cockpit_preview("buy_ce")
    assert status == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["dry_run"] is True
    assert body["execution_mode"] == "dry_run"
    assert body["destination"] == "dry_run_dashboard"


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
                "contract_value": "0.001", "ask": 250.0,
                "ask_size": 5000,
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


@pytest.mark.parametrize(
    ("action", "long_zone", "short_zone", "option_type"),
    (
        ("sell_ce", dashboard.TREND_SCORE_CE_ZONE,
         dashboard.TREND_SCORE_SHORT_CE_ZONE, "CE"),
        ("sell_pe", dashboard.TREND_SCORE_PE_ZONE,
         dashboard.TREND_SCORE_SHORT_PE_ZONE, "PE"),
    ),
)
def test_prepare_individual_option_sell_selects_atm_and_short_side(
    live_account, monkeypatch, action, long_zone, short_zone, option_type,
):
    now = dashboard.datetime.now(dashboard.timezone.utc).isoformat()
    selection = {
        "zone": long_zone, "symbol": f"{option_type[0]}-BTC-65000-060826",
        "product_id": 7, "strike": 65_000,
        "option_type": option_type,
        "expiry": "2026-08-06T12:00:00Z",
        "entry_price": 205.0, "max_order_lots": 5000,
        "executable_contract": {
            "contract_value": "0.001", "bid": 200.0, "ask": 205.0,
            "bid_size": 2500, "ask_size": 2500,
            "quote_timestamp": now,
        },
    }
    select = Mock(return_value=selection)
    monkeypatch.setattr(dashboard, "select_directional_option", select)
    monkeypatch.setattr(dashboard, "_fetch_live_vanilla_products", lambda: [])
    monkeypatch.setattr(
        dashboard, "_trend_score_auto_live_affordable_entry", lambda p: p,
    )

    prepared = dashboard._cockpit_prepare_manual_entry(action, _snapshot())

    assert prepared["zone"] == short_zone
    assert prepared["side"] == "short"
    assert prepared["option_type"] == option_type
    assert prepared["entry_price"] == 200.0
    assert prepared["entry_depth"] == 2500
    assert select.call_args.kwargs["zone"] == long_zone
    assert select.call_args.kwargs["manual_itm_steps"] == 0
    assert select.call_args.kwargs["today_only"] is True


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


# ---------------------------------------------------------------------------
# Top-of-book depth is observational, never a gate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("action", ("buy_pe", "sell_ce"))
def test_dry_run_simulates_when_touch_depth_is_below_the_configured_size(
    live_account, monkeypatch, action,
):
    """A thin touch must not block a simulation that consumes no liquidity.

    Delta's daily BTC option book routinely rests tens-to-hundreds of
    contracts at the touch against a four-digit configured size, and the
    LIVE path treats that quantity as observational because the bounded IOC
    sweeps deeper levels. DRY RUN must not be the stricter of the two.
    """
    now = dashboard.datetime.now(dashboard.timezone.utc).isoformat()
    configured = dashboard._trend_score_auto_configured_lots()
    thin = 12
    assert thin < configured
    selection = {
        "zone": dashboard.TREND_SCORE_PE_ZONE, "symbol": "P-BTC-76800-230826",
        "product_id": 9, "strike": 76_800, "option_type": "PE",
        "expiry": "2026-08-23T12:00:00Z", "lots": configured,
        "max_order_lots": 5000,
        "executable_contract": {
            "contract_value": "0.001", "bid": 200.0, "ask": 205.0,
            "bid_size": thin, "ask_size": thin, "quote_timestamp": now,
        },
    }
    monkeypatch.setattr(
        dashboard, "select_directional_option", Mock(return_value=selection),
    )
    monkeypatch.setattr(dashboard, "_fetch_live_vanilla_products", lambda: [])
    affordable = Mock(
        side_effect=AssertionError("DRY RUN must not wallet-size a simulation")
    )
    monkeypatch.setattr(
        dashboard, "_trend_score_auto_live_affordable_entry", affordable,
    )

    prepared = dashboard._cockpit_prepare_manual_entry(
        action, _snapshot(), dry_run=True,
    )

    # The configured simulation size is kept, and the thin touch quantity is
    # recorded for the operator rather than used to refuse the trade.
    assert prepared["lots"] == configured
    assert prepared["entry_depth"] == thin


def test_dry_run_still_requires_a_positive_quoted_depth(
    live_account, monkeypatch,
):
    """Observational is not optional -- a quote-less book still fails closed."""
    now = dashboard.datetime.now(dashboard.timezone.utc).isoformat()
    selection = {
        "zone": dashboard.TREND_SCORE_CE_ZONE, "symbol": "C-BTC-76000-230826",
        "product_id": 9, "strike": 76_000, "option_type": "CE",
        "expiry": "2026-08-23T12:00:00Z", "lots": 1000,
        "executable_contract": {
            "contract_value": "0.001", "bid": 200.0, "ask": 205.0,
            "bid_size": 0, "ask_size": 0, "quote_timestamp": now,
        },
    }
    monkeypatch.setattr(
        dashboard, "select_directional_option", Mock(return_value=selection),
    )
    monkeypatch.setattr(dashboard, "_fetch_live_vanilla_products", lambda: [])

    with pytest.raises(RuntimeError, match="depth"):
        dashboard._cockpit_prepare_manual_entry(
            "buy_ce", _snapshot(), dry_run=True,
        )


@pytest.mark.parametrize(
    ("action", "side"), (("sell_move", "sell"), ("buy_move", "buy")),
)
def test_dry_run_move_quote_requires_one_lot_of_depth_like_live(
    live_account, monkeypatch, action, side,
):
    """Both Cockpit modes ask the MOVE quote helper for the same one lot."""
    selection = {
        "zone": dashboard.TREND_SCORE_MOVE_ZONE, "symbol": "MV-BTC-1",
        "product_id": 2, "expiry": "2026-08-23T12:00:00Z",
    }
    monkeypatch.setattr(
        dashboard, "select_move_contract", Mock(return_value=selection),
    )
    monkeypatch.setattr(dashboard, "_fetch_live_mv_products", lambda: [])
    quote_fn = Mock(return_value={
        "entry_price": 350.0, "entry_depth": 3, "side": side,
    })
    monkeypatch.setattr(dashboard, "_trend_score_auto_move_quote", quote_fn)
    monkeypatch.setattr(
        dashboard,
        "_trend_score_auto_short_move_eligibility",
        Mock(return_value={"quoted_premium_usd": 350.0}),
    )

    prepared = dashboard._cockpit_prepare_manual_entry(
        action, _snapshot(), dry_run=True,
    )

    assert quote_fn.call_args.args[1] == 1
    assert prepared["entry_depth"] == 3


def test_dry_run_buy_move_accepts_a_real_book_thinner_than_the_order_size(
    live_account, monkeypatch,
):
    """End-to-end against a real ATM MOVE book, through the real quote helper.

    Reproduces the reported "MOVE ask depth cannot fill the configured order
    size" toast: Delta's ATM MOVE book rests a few hundred contracts at the
    touch (484-723 observed on the 2026-08-23 expiry) while the configured
    simulation size is four digits. Only ``_trend_score_auto_move_quote`` is
    left unmocked here, so the depth rule itself is what is under test.
    """
    configured = dashboard._trend_score_auto_configured_lots()
    thin = 484
    assert thin < configured
    ticker = {
        "symbol": "MV-BTC-76400-230826",
        "mark_price": "427.0",
        "timestamp": int(dashboard.time.time() * 1_000_000),
        "product_trading_status": "operational",
        "quotes": {
            "best_bid": "424.0", "best_ask": "430.0",
            "bid_size": 716, "ask_size": thin,
        },
    }
    monkeypatch.setattr(
        dashboard.req, "get", Mock(return_value=Mock(
            json=Mock(return_value={"result": ticker}),
        )),
    )
    monkeypatch.setattr(dashboard, "_fetch_live_mv_products", lambda: [])
    monkeypatch.setattr(
        dashboard,
        "select_move_contract",
        Mock(return_value={
            "zone": dashboard.TREND_SCORE_MOVE_ZONE,
            "symbol": "MV-BTC-76400-230826",
            "product_id": 2, "expiry": "2026-08-23T12:00:00Z",
            "lots": configured,
        }),
    )

    prepared = dashboard._cockpit_prepare_manual_entry(
        "buy_move", _snapshot(spot=76_309.3), dry_run=True,
    )

    assert prepared["side"] == "long"
    assert prepared["zone"] == dashboard.TREND_SCORE_LONG_MOVE_ZONE
    assert prepared["entry_price"] == 430.0
    assert prepared["lots"] == configured
    assert prepared["entry_depth"] == thin


# ---------------------------------------------------------------------------
# "Lemme Risk" -- the ungated operator-override setup
# ---------------------------------------------------------------------------

def test_lemme_risk_is_eligible_without_any_engine_evidence():
    """The override setup must stay green when every gated setup is red,
    including on degraded data quality -- that is the state an operator is
    most likely to want it in."""
    matrix = dashboard._cockpit_setup_eligibility({
        "data_quality": "DEGRADED", "trend_score": None, "trigger_adx": None,
        "components": [], "reason_codes": [],
    })
    eligible = {key for key, value in matrix.items() if value["eligible"]}
    assert eligible == {dashboard.COCKPIT_OVERRIDE_SETUP}
    override = matrix[dashboard.COCKPIT_OVERRIDE_SETUP]
    assert override["override"] is True
    assert set(override["actions"]) == set(dashboard.TREND_SCORE_MANUAL_TRIGGERS)
    assert all(
        matrix[key]["override"] is False
        for key in matrix
        if key != dashboard.COCKPIT_OVERRIDE_SETUP
    )


def test_lemme_risk_authorizes_every_trade_type_without_the_trend_engine(
    monkeypatch,
):
    """Authorization short-circuits before the engine snapshot: an
    unreachable Trend Engine may not block the override, but must still
    block a gated setup."""
    snapshot = Mock(side_effect=RuntimeError("engine unreachable"))
    monkeypatch.setattr(dashboard.trend_engine_client, "get_snapshot", snapshot)

    for action in dashboard.TREND_SCORE_MANUAL_TRIGGERS:
        dashboard._cockpit_require_eligible_setup(
            dashboard.COCKPIT_OVERRIDE_SETUP, action,
        )
    snapshot.assert_not_called()

    with pytest.raises(RuntimeError, match="engine unreachable"):
        dashboard._cockpit_require_eligible_setup("trend_bullish", "buy_ce")


def test_lemme_risk_still_rejects_an_action_outside_the_cockpit_set():
    """Ungated means "any listed strategy", not "any string": the override
    still may not authorize a trade type the Cockpit does not offer."""
    with pytest.raises(ValueError, match="does not match this market setup"):
        dashboard._cockpit_require_eligible_setup(
            dashboard.COCKPIT_OVERRIDE_SETUP, "buy_the_dip",
        )


def test_lemme_risk_live_entry_uses_the_standard_protected_seam(
    live_account, monkeypatch,
):
    """An override entry is ordinary downstream: it takes the real
    authorization path (not the fixture stub), and still reaches the same
    execution seam with the same manual-LIVE ownership that spawns
    TP / SL / TSL protection."""
    monkeypatch.setattr(
        dashboard, "_cockpit_require_eligible_setup", _REAL_REQUIRE_ELIGIBLE,
    )
    monkeypatch.setattr(
        dashboard.trend_engine_client, "get_snapshot",
        Mock(side_effect=AssertionError("override consulted the Trend Engine")),
    )
    monkeypatch.setattr(
        dashboard, "_cockpit_market_snapshot", lambda: {"market": {"spot": 65_000}},
    )
    monkeypatch.setattr(
        dashboard, "_cockpit_prepare_manual_entry",
        lambda action_arg, snapshot: {
            "zone": dashboard.TREND_SCORE_SHORT_PE_ZONE, "side": "short",
            "symbol": "MV-BTC-1", "product_id": 2, "lots": 1_000,
        },
    )
    execute = Mock(return_value=_mock_execution_result())
    monkeypatch.setattr(dashboard, "_trend_score_auto_live_execute", execute)

    response, status = _post_cockpit_enter(
        "sell_move", setup=dashboard.COCKPIT_OVERRIDE_SETUP,
    )

    assert status == 200
    assert response.get_json()["ok"] is True
    execute.assert_called_once()
    call_kwargs = execute.call_args.kwargs
    assert (
        call_kwargs["ownership"] == dashboard.TREND_SCORE_MANUAL_LIVE_OWNERSHIP
    )
    assert call_kwargs["signal"]["signal_key"].startswith(
        "manual-cockpit|sell_move|"
    )
