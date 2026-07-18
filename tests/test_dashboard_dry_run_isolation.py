import json
from pathlib import Path
from unittest.mock import Mock

import pytest

import dashboard
from risk_controls import RiskDecision


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _result(response):
    if isinstance(response, tuple):
        payload, status = response[0], response[1]
    else:
        payload, status = response, response.status_code
    return payload.get_json(), int(status)


def _allowed_risk() -> RiskDecision:
    return RiskDecision(
        allowed=True,
        reason="allowed",
        trading_date="2026-07-18",
        trades_today=0,
        daily_pnl_usd=0,
        open_risk_usd=0,
        consecutive_losses=0,
    )


def _dry_state(slot: str = "evening", **updates) -> dict:
    state = {
        "slot": slot,
        "status": "OPEN",
        "dry_run": True,
        "execution_mode": "dry_run",
        "simulation_id": f"sim-{slot}-test",
        "side": "long",
        "entry_date": "2026-07-18",
        "entry_time_utc": "12:05:00",
        "symbol": "MV-BTC-64000-190726",
        "product_id": 123,
        "strike": 64000,
        "settlement": "2099-07-19T12:00:00Z",
        "contract_value": 0.001,
        "lots": 10,
        "owned_entry_lots": 10,
        "entry_mark": 100.0,
        "total_cost_usd": 1.0,
        "entry_fees_usd": 0.0,
        "protection_config": {
            "tp_target_pnl": 100,
            "sl_target_pnl": 50,
            "tsl_arm_pnl": 50,
            "tsl_trail_pnl": 50,
            "tsl_lock_min_pnl": 0,
        },
    }
    state.update(updates)
    return state


@pytest.fixture
def isolated_dashboard(tmp_path, monkeypatch):
    users = tmp_path / "users"
    account = users / "alice"
    account.mkdir(parents=True)
    _write(account / "config.json", {
        "DRY_RUN": "true",
        "TREND_AUTO_ENTRY_MODE": "disabled",
        "MAX_TRADES_PER_DAY_GLOBAL": "10",
        "MAX_DAILY_LOSS_USD": "1000",
        "MAX_OPEN_RISK_USD": "1000",
        "MAX_ACCOUNT_PREMIUM_AT_RISK_USD": "1000",
    })
    monkeypatch.setattr(dashboard, "USERS_DIR", users)
    monkeypatch.setattr(dashboard, "DASH_USER", "alice")
    monkeypatch.setattr(dashboard, "BOT_USER", "alice")
    monkeypatch.setattr(dashboard, "_active_user", lambda: "alice")
    monkeypatch.setattr(dashboard, "_active_creds", lambda: ("", ""))
    monkeypatch.setattr(dashboard, "_sync_states_from_exchange", lambda: None)
    monkeypatch.setattr(dashboard, "_revive_tp_monitors", lambda: 0)
    monkeypatch.setattr(dashboard, "_import_legacy_dry_records", lambda: None)
    dashboard._external_options.clear()
    return account


def test_real_status_and_trades_never_publish_dry_records(
        isolated_dashboard, monkeypatch):
    account = isolated_dashboard
    live = {
        "slot": "evening",
        "status": "OPEN",
        "symbol": "MV-BTC-LIVE",
        "product_id": 1,
        "lots": 2,
        "entry_mark": 100,
        "contract_value": 0.001,
    }
    legacy_dry = _dry_state("morning", symbol="MV-BTC-LEGACY-PAPER")
    _write(account / "straddle_state.json", live)
    _write(account / "morning_state.json", legacy_dry)
    _write(account / "trade_history.json", [
        {
            "slot": "evening",
            "entry_date": "2026-07-18",
            "entry_time": "12:05:00",
            "exit_time": "13:05:00",
            "symbol": "MV-BTC-LIVE-CLOSED",
            "pnl_usd": 4.5,
        },
        {
            **legacy_dry,
            "status": "CLOSED",
            "exit_mark": 110,
            "pnl_usd": 0.1,
            "exit_time": "13:06:00",
        },
    ])
    monkeypatch.setattr(
        dashboard, "_enrich_live",
        lambda state: {**state, "live_pnl": 1.25},
    )
    monkeypatch.setattr(dashboard, "_fetch_reconstructed_trades", lambda: [])

    class Response:
        def json(self):
            return {"result": {"mark_price": "64000"}}

    monkeypatch.setattr(dashboard.req, "get", lambda *args, **kwargs: Response())

    with dashboard.app.test_request_context("/api/status"):
        status = dashboard.api_status().get_json()
    with dashboard.app.test_request_context("/api/trades"):
        trades = dashboard.api_trades().get_json()

    assert status["symbol"] == "MV-BTC-LIVE"
    assert "MV-BTC-LEGACY-PAPER" not in json.dumps(status)
    assert status["trading_mode"] == "DRY RUN"
    assert status["dry_run_mode"] is True
    assert status["execution_mode"] == "dry_run"
    assert status["mode_revision"]
    assert [row["symbol"] for row in trades] == ["MV-BTC-LIVE-CLOSED"]
    assert all(not dashboard._is_dry_record(row) for row in trades)


def test_dry_status_trades_and_summary_publish_only_simulations(
        isolated_dashboard, monkeypatch):
    account = isolated_dashboard
    _write(account / "straddle_state.json", {
        "slot": "evening", "status": "OPEN", "symbol": "MV-BTC-LIVE",
    })
    _write(
        account / "dry_run" / "straddle_state.json",
        _dry_state(symbol="MV-BTC-PAPER-OPEN"),
    )
    _write(account / "trade_history.json", [{
        "symbol": "MV-BTC-LIVE-CLOSED", "pnl_usd": 99,
    }])
    _write(account / "dry_run" / "trade_history.json", [
        {
            **_dry_state(symbol="MV-BTC-PAPER-WIN"),
            "status": "CLOSED",
            "exit_mark": 120,
            "pnl_usd": 8,
            "exit_time_utc": "13:00:00",
        },
        {
            **_dry_state("trend", symbol="P-BTC-PAPER-LOSS"),
            "status": "CLOSED",
            "exit_mark": 80,
            "pnl_usd": -3,
            "exit_time_utc": "14:00:00",
        },
        {
            "execution_mode": "live",
            "symbol": "CONTAMINATING-LIVE-ROW",
            "pnl_usd": 500,
        },
    ])
    monkeypatch.setattr(dashboard, "_enrich_live", lambda state: dict(state))

    with dashboard.app.test_request_context("/api/dry-run/status"):
        status = dashboard.api_dry_run_status().get_json()
    with dashboard.app.test_request_context("/api/dry-run/trades"):
        trades = dashboard.api_dry_run_trades().get_json()
    with dashboard.app.test_request_context("/api/dry-run/summary"):
        summary = dashboard.api_dry_run_summary().get_json()

    assert status["evening"]["symbol"] == "MV-BTC-PAPER-OPEN"
    assert "MV-BTC-LIVE" not in json.dumps(status)
    assert status["trading_mode"] == "DRY RUN"
    assert status["mode_active"] is True
    assert {row["symbol"] for row in trades} == {
        "MV-BTC-PAPER-WIN", "P-BTC-PAPER-LOSS",
    }
    assert all(dashboard._is_dry_record(row) for row in trades)
    assert summary["total_days"] == 2
    assert summary["total_pnl"] == 5
    assert summary["wins"] == 1
    assert summary["losses"] == 1


def test_open_dry_pnl_refreshes_from_mark_price_while_close_uses_book(
        isolated_dashboard, monkeypatch):
    state = _dry_state(
        entry_mark=145.0,
        lots=1000,
        contract_value=0.001,
        entry_fees_usd=0.0,
    )
    marks = iter(("143.50", "144.25", "145.00"))

    class Response:
        def __init__(self, mark):
            self.mark = mark

        def json(self):
            return {
                "result": {
                    "mark_price": self.mark,
                    "quotes": {"best_bid": "142.00", "best_ask": "147.00"},
                },
            }

    monkeypatch.setattr(
        dashboard.req, "get",
        lambda *args, **kwargs: Response(next(marks)),
    )
    monkeypatch.setattr(
        dashboard, "_option_fee_per_lot",
        lambda mark, cv, notional_reference=0: 0.0,
    )

    first = dashboard._enrich_dry_state(state)
    second = dashboard._enrich_dry_state(state)
    close_mark, close_pnl, _, _ = dashboard._dry_run_mark_and_pnl(state)

    assert first["current_mark"] == 143.5
    assert first["live_pnl"] == -1.5
    assert first["live_pnl_price_source"] == "mark_price"
    assert second["current_mark"] == 144.25
    assert second["live_pnl"] == -0.75
    assert close_mark == 142.0
    assert close_pnl == -3.0


def test_manual_move_dry_entry_is_disabled_and_never_writes_or_posts(
        isolated_dashboard, monkeypatch):
    account = isolated_dashboard
    contract = {
        "id": 321,
        "symbol": "MV-BTC-64200-190726",
        "strike_price": 64200,
        "settlement_time": "2026-07-19T12:00:00Z",
        "contract_value": 0.001,
    }
    quote = {"entry_price": 100.0, "limit_price": 101.0}
    plan = {"lots": 7, "proposed_risk_usd": 10.0}
    monkeypatch.setattr(dashboard, "_current_atm_mv", lambda slot: contract)
    monkeypatch.setattr(dashboard, "_move_execution_quote", lambda *a, **k: quote)
    lot_plan = Mock(return_value=plan)
    monkeypatch.setattr(dashboard, "_move_lot_plan", lot_plan)
    monkeypatch.setattr(dashboard, "evaluate_entry", lambda *a, **k: _allowed_risk())
    monkeypatch.setattr(dashboard, "_tp_policy", lambda slot: {
        "tp_target_pnl": 10, "sl_target_pnl": 5,
        "tsl_arm_pnl": 5, "tsl_trail_pnl": 2,
        "tsl_lock_min_pnl": 0,
    })
    monkeypatch.setattr(dashboard, "_send_telegram", lambda message: None)
    order_post = Mock(side_effect=AssertionError("DRY RUN reached order submission"))
    raw_post = Mock(side_effect=AssertionError("DRY RUN reached HTTP POST"))
    monkeypatch.setattr(dashboard, "_post_dashboard_order", order_post)
    monkeypatch.setattr(dashboard.req, "post", raw_post)
    mode = dashboard._trading_mode_payload()
    body = {
        "side": "buy",
        "product_id": contract["id"],
        "symbol": contract["symbol"],
        "lots": plan["lots"],
        "mark": quote["entry_price"],
        "expected_mode": mode["execution_mode"],
        "dry_run": True,
        "mode_revision": mode["mode_revision"],
    }

    with dashboard.app.test_request_context(
            "/api/manual-entry?slot=evening", method="POST", json=body):
        payload, status = _result(dashboard.api_manual_entry())

    assert status == 410
    assert payload["ok"] is False
    assert payload["code"] == "MANUAL_MOVE_DISABLED"
    assert not (account / "dry_run" / "straddle_state.json").exists()
    assert not (account / "straddle_state.json").exists()
    lot_plan.assert_not_called()
    order_post.assert_not_called()
    raw_post.assert_not_called()


@pytest.mark.parametrize(
    ("slot", "state_name"),
    (
        ("morning", "morning_state.json"),
        ("evening", "straddle_state.json"),
        ("trend", "trend_state.json"),
    ),
)
def test_dry_manual_exit_supports_every_strategy_and_appends_exactly_once(
        isolated_dashboard, monkeypatch, slot, state_name):
    account = isolated_dashboard
    state_path = account / "dry_run" / state_name
    _write(state_path, _dry_state(slot))
    monkeypatch.setattr(
        dashboard, "_dry_run_mark_and_pnl",
        lambda state: (125.0, 0.25, 0.25, 0.0),
    )
    raw_post = Mock(side_effect=AssertionError("simulation used HTTP POST"))
    monkeypatch.setattr(dashboard.req, "post", raw_post)
    mode = dashboard._trading_mode_payload()
    body = {
        "expected_mode": "dry_run",
        "dry_run": True,
        "mode_revision": mode["mode_revision"],
    }

    with dashboard.app.test_request_context(
            f"/api/square-off?slot={slot}", method="POST", json=body):
        first, first_status = _result(dashboard.api_square_off())
    with dashboard.app.test_request_context(
            f"/api/square-off?slot={slot}", method="POST", json=body):
        second, second_status = _result(dashboard.api_square_off())

    history = json.loads(
        (account / "dry_run" / "trade_history.json").read_text(
            encoding="utf-8"))
    closed = json.loads(state_path.read_text(encoding="utf-8"))
    assert first_status == 200
    assert first["dry_run"] is True
    assert first["history_pending"] is False
    assert second_status == 400
    assert "No open" in second["error"]
    assert closed["status"] == "CLOSED"
    assert closed["exit_trigger"] == "manual_squareoff_simulated"
    assert len(history) == 1
    assert history[0]["simulation_id"] == f"sim-{slot}-test"
    assert not (account / "trade_history.json").exists()
    raw_post.assert_not_called()


def test_trend_dry_entry_writes_only_isolated_state_and_never_submits_order(
        isolated_dashboard, monkeypatch):
    account = isolated_dashboard
    preview = {
        "ok": True,
        "can_enter": True,
        "dry_run": True,
        "direction": "up",
        "option_type": "CE",
        "symbol": "C-BTC-63000-190726",
        "product_id": 456,
        "strike": 63000,
        "settlement": "2026-07-19T12:00:00Z",
        "contract_value": 0.001,
        "lots": 6,
        "spot": 64000,
        "mark": 200,
        "ask": 205,
        "timeframes": {"15m": {"candle_time": "2026-07-18T10:00:00Z"}},
        "signal_snapshot": {},
        "quote": {},
        "sizing": {"proposed_risk_usd": 20},
        "risk": {"trading_date": "2026-07-18"},
        "signal_key": "up|5|15|1h",
    }
    monkeypatch.setattr(
        dashboard, "_trend_entry_preview_data",
        lambda **kwargs: (preview, 200),
    )
    monkeypatch.setattr(dashboard, "_tp_policy", lambda slot: {
        "tp_target_pnl": 10, "sl_target_pnl": 5,
        "tsl_arm_pnl": 5, "tsl_trail_pnl": 2,
        "tsl_lock_min_pnl": 0,
    })
    monkeypatch.setattr(dashboard, "_trend_audit", lambda *a, **k: None)
    monkeypatch.setattr(dashboard, "_send_telegram", lambda message: None)
    submit = Mock(side_effect=AssertionError("DRY RUN submitted a Trend order"))
    chunks = Mock(side_effect=AssertionError("DRY RUN executed Trend chunks"))
    raw_post = Mock(side_effect=AssertionError("DRY RUN reached HTTP POST"))
    monkeypatch.setattr(dashboard, "_submit_trend_order", submit)
    monkeypatch.setattr(dashboard, "_execute_trend_chunks", chunks)
    monkeypatch.setattr(dashboard.req, "post", raw_post)
    mode = dashboard._trading_mode_payload()
    body = {
        "expected_mode": "dry_run",
        "dry_run": True,
        "mode_revision": mode["mode_revision"],
    }

    with dashboard.app.test_request_context(
            "/api/trend-entry", method="POST", json=body):
        payload, status = _result(dashboard.api_trend_entry())

    assert status == 200
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    state = json.loads(
        (account / "dry_run" / "trend_state.json").read_text(
            encoding="utf-8"))
    assert state["status"] == "OPEN"
    assert state["execution_mode"] == "dry_run"
    assert state["simulation_id"]
    assert state["symbol"] == preview["symbol"]
    assert not (account / "trend_state.json").exists()
    submit.assert_not_called()
    chunks.assert_not_called()
    raw_post.assert_not_called()


def test_mode_and_revision_mismatch_fail_before_move_or_trend_strategy_work(
        isolated_dashboard, monkeypatch):
    move_work = Mock(side_effect=AssertionError("MOVE work ran after mode mismatch"))
    trend_work = Mock(side_effect=AssertionError("Trend work ran after revision mismatch"))
    order_post = Mock(side_effect=AssertionError("mismatch reached order POST"))
    monkeypatch.setattr(dashboard, "_current_atm_mv", move_work)
    monkeypatch.setattr(dashboard, "_trend_entry_preview_data", trend_work)
    monkeypatch.setattr(dashboard, "_post_dashboard_order", order_post)

    with dashboard.app.test_request_context(
            "/api/manual-entry?slot=evening", method="POST",
            json={"side": "buy", "expected_mode": "live"}):
        move_payload, move_status = _result(dashboard.api_manual_entry())
    with dashboard.app.test_request_context(
            "/api/trend-entry", method="POST",
            json={"expected_mode": "dry_run", "mode_revision": "stale"}):
        trend_payload, trend_status = _result(dashboard.api_trend_entry())

    assert move_status == 410
    assert move_payload["code"] == "MANUAL_MOVE_DISABLED"
    assert trend_status == 409
    assert "Configuration changed" in trend_payload["error"]
    move_work.assert_not_called()
    trend_work.assert_not_called()
    order_post.assert_not_called()


@pytest.mark.parametrize(
    ("slot", "pnl", "state_updates", "policy", "expected_trigger"),
    [
        (
            "morning",
            12.0,
            {},
            {"tp_target_pnl": 10},
            "take_profit_simulated",
        ),
        (
            "evening",
            -12.0,
            {},
            {"sl_target_pnl": 10},
            "stop_loss_simulated",
        ),
        (
            "trend",
            14.0,
            {"dry_peak_pnl_usd": 20},
            {
                "tsl_arm_pnl": 10,
                "tsl_trail_pnl": 5,
                "tsl_lock_min_pnl": 0,
            },
            "trailing_stop_simulated",
        ),
    ],
)
def test_dry_protection_tp_sl_tsl_close_locally_and_append_once(
        isolated_dashboard, monkeypatch, slot, pnl, state_updates, policy,
        expected_trigger):
    account = isolated_dashboard
    state = _dry_state(slot, **state_updates)
    state["protection_config"] = policy
    state_path = (
        account / "dry_run" / dashboard.SLOT_STATE_FILES[slot])
    _write(state_path, state)
    monkeypatch.setattr(
        dashboard, "_dry_run_mark_and_pnl",
        lambda record: (125.0, pnl, pnl, 0.0),
    )
    raw_post = Mock(side_effect=AssertionError("protection used HTTP POST"))
    monkeypatch.setattr(dashboard.req, "post", raw_post)

    with dashboard.app.test_request_context("/api/dry-run/status"):
        assert dashboard._dry_run_protection_cycle() == 1
        assert dashboard._dry_run_protection_cycle() == 0

    closed = json.loads(state_path.read_text(encoding="utf-8"))
    history = json.loads(
        (account / "dry_run" / "trade_history.json").read_text(
            encoding="utf-8"))
    assert closed["status"] == "CLOSED"
    assert closed["exit_trigger"] == expected_trigger
    assert closed["history_pending"] is False
    assert len(history) == 1
    assert history[0]["exit_trigger"] == expected_trigger
    assert not (account / "trade_history.json").exists()
    raw_post.assert_not_called()


def test_topbar_contains_server_driven_trading_mode_next_to_theme():
    root = Path(dashboard.__file__).resolve().parent
    template = (root / "templates" / "base.html").read_text(encoding="utf-8")
    script = (root / "static" / "js" / "app.js").read_text(encoding="utf-8")

    theme_index = template.index('id="theme-toggle"')
    mode_index = template.index('id="tb-mode"')
    spacer_index = template.index('class="topbar-spacer"')
    assert theme_index < mode_index < spacer_index
    assert "Trading Mode" in template
    assert "setTradingModeIndicator(st.trading_mode, st.dry_run_mode)" in script


def test_dry_run_live_status_refresh_is_fast_uncached_and_non_overlapping():
    root = Path(dashboard.__file__).resolve().parent
    template = (root / "templates" / "dry_run.html").read_text(
        encoding="utf-8")
    script = (root / "static" / "js" / "app.js").read_text(
        encoding="utf-8")

    assert "fetch(url, { cache: 'no-store' })" in script
    assert "let dryStatusRefreshPending = false;" in template
    assert "if (dryStatusRefreshPending)" in template
    assert "setInterval(() => loadDryStatus(false), 4_000);" in template
    assert "setInterval(() => loadDryStatus(true), 20_000);" in template


def test_dry_run_cards_are_equal_sized_and_every_open_slot_has_manual_exit():
    root = Path(dashboard.__file__).resolve().parent
    template = (root / "templates" / "dry_run.html").read_text(
        encoding="utf-8")
    overview = (root / "templates" / "overview.html").read_text(
        encoding="utf-8")
    styles = (root / "static" / "css" / "app.css").read_text(
        encoding="utf-8")

    assert "grid-auto-rows: 1fr" in styles
    assert ".dry-slot-grid > .card {" in styles
    assert (
        ".grid > .card, .dry-slot-grid > .card { margin-top: 0; }"
        in styles
    )
    assert ".dry-slot-card {" in styles
    assert "min-height: 420px; flex: 1 1 auto" in styles
    assert "dry-slot-footer-panel" in template
    assert "min-height: 86px" in styles
    assert "\n          Exit\n" in template
    assert ">Exit</button>" in overview
    assert "endDrySimulation('${slot}')" in template
    for slot in ("morning", "evening", "trend"):
        assert f"dryPositionDetails(dryStatus.{slot} || {{}}, '{slot}'" in template

    assert "squareOff('${slot}', 'dry_run')" in overview
    assert "squareOff('${slot}', 'live')" in overview
    assert "target_mode: targetMode" in overview
