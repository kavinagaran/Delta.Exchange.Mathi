import json
import inspect
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import dashboard
from risk_controls import account_entry_lock


@pytest.fixture
def isolated_user(tmp_path, monkeypatch):
    users = tmp_path / "users"
    users.mkdir()
    monkeypatch.setattr(dashboard, "USERS_DIR", users)
    monkeypatch.setattr(dashboard, "DASH_USER", "alice")
    monkeypatch.setattr(dashboard, "BOT_USER", "alice")
    return users / "alice"


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _response_tuple(result):
    if isinstance(result, tuple):
        return result
    return result, result.status_code


def test_exposure_routes_reject_invalid_slot_before_exchange_call():
    with dashboard.app.test_request_context(
            "/api/manual-entry?slot=typo", method="POST", json={"side": "buy"}), \
            patch.object(dashboard.req, "post") as post:
        response, status = _response_tuple(dashboard.api_manual_entry())
        assert status == 400
        assert "slot" in response.get_json()["error"]
        post.assert_not_called()

    with dashboard.app.test_request_context(
            "/api/square-off?slot=typo", method="POST"), \
            patch.object(dashboard.req, "post") as post:
        response, status = _response_tuple(dashboard.api_square_off())
        assert status == 400
        assert "slot" in response.get_json()["error"]
        post.assert_not_called()


def test_legacy_manual_sizing_fails_closed_on_unknown_or_zero_affordability():
    def cfg(key, default=""):
        return {"STRADDLE_LOTS": "800", "MAX_ORDER_LOTS": "1000"}.get(key, default)

    with patch.object(dashboard, "_cfg", side_effect=cfg), \
            patch.object(dashboard, "_affordable_option_lots", return_value=None):
        assert dashboard._manual_entry_lots("evening", 10, .001, 64000) == 0
    with patch.object(dashboard, "_cfg", side_effect=cfg), \
            patch.object(dashboard, "_affordable_option_lots", return_value=0):
        assert dashboard._manual_entry_lots("evening", 10, .001, 64000) == 0


def test_short_plan_requires_opt_in_positive_sl_and_short_cap():
    cfg = {
        "STRADDLE_LOTS": "100", "MAX_ORDER_LOTS": "100",
        "ORDER_CHUNK_LOTS": "100", "MIN_BOOK_DEPTH_MULTIPLE": "1",
        "RISK_PER_TRADE_USD_EVENING": "200", "SHORT_MAX_RISK_USD": "0",
    }
    contract = {"contract_value": ".001", "strike_price": "64000"}
    quote = {"entry_price": 10, "entry_depth": 100}
    with patch.object(dashboard, "_user_cfg", return_value=cfg), \
            patch.object(dashboard, "_cfg_bool", return_value=False), \
            patch.object(dashboard, "_affordable_option_lots", return_value=100), \
            patch.object(dashboard, "_tp_env", return_value=(100, 30, 50, 0)):
        plan = dashboard._move_lot_plan("evening", "sell", contract, quote)
    assert plan["lots"] == 0
    assert "disabled" in plan["reason"]


def test_manual_entry_honors_concurrent_move_cap(isolated_user):
    _write(isolated_user / "morning_state.json", _open_state(
        slot="morning", product_id=7, symbol="MV-BTC-OLD", lots=4,
        owned_entry_lots=4))
    with patch.object(dashboard, "_cfg",
                      side_effect=lambda key, default="": "1" if key == "MAX_CONCURRENT_MOVE_POSITIONS" else default):
        with pytest.raises(RuntimeError, match="concurrent MOVE position cap"):
            dashboard._validate_move_entry_account(
                [{"product_id": 7, "size": "4", "unrealized_pnl": "0"}], 9)


def test_manual_plan_is_explicitly_discretionary_not_value_eligible():
    cfg = {
        "STRADDLE_LOTS": "100", "MAX_ORDER_LOTS": "100",
        "ORDER_CHUNK_LOTS": "100", "MIN_BOOK_DEPTH_MULTIPLE": "1",
        "RISK_PER_TRADE_USD_EVENING": "200", "MOVE_VALUE_FILTER_ENABLED": "true",
        "MAX_ACCOUNT_PREMIUM_AT_RISK_USD": "500",
    }
    contract = {"contract_value": ".001", "strike_price": "64000"}
    quote = {"entry_price": 10, "entry_depth": 100}
    with patch.object(dashboard, "_user_cfg", return_value=cfg), \
            patch.object(dashboard, "_affordable_option_lots", return_value=100), \
            patch.object(dashboard, "_tp_env", return_value=(100, 30, 50, 0)), \
            patch.object(dashboard, "_open_long_premium_usd", return_value=0):
        plan = dashboard._move_lot_plan("evening", "buy", contract, quote)
    assert plan["move_value_filter_enabled"] is True
    assert plan["move_value_gate_evaluated"] is False
    assert plan["entry_classification"] == "discretionary_manual"


def test_entry_identity_is_durable_before_bounded_ioc_post(isolated_user):
    contract = {"id": 9, "symbol": "MV-BTC-X", "contract_value": ".001",
                "strike_price": "64000", "settlement_time": "2026-07-16T00:00:00Z"}
    quote = {"entry_price": 10, "limit_price": 10.1}
    plan = {"lots": 5, "proposed_risk_usd": 10}
    captured = {}

    def submit(payload):
        pending = json.loads((isolated_user / "straddle_state.json").read_text())
        assert pending["status"] == "ENTRY_PENDING"
        assert pending["pending_entry_client_order_id"] == payload["client_order_id"]
        captured.update(payload)
        order = {"id": 77, "client_order_id": payload["client_order_id"],
                 "product_id": 9, "side": "buy", "reduce_only": False,
                 "state": "closed", "filled_size": 5, "unfilled_size": 0,
                 "average_fill_price": "10"}
        return order, {"success": True, "result": order}

    with patch.object(dashboard, "_tp_policy", return_value={"sl_target_pnl": 50}), \
            patch.object(dashboard, "_post_dashboard_order", side_effect=submit):
        state, order = dashboard._submit_manual_move_entry(
            "evening", "buy", contract, quote, plan, False)

    assert captured["order_type"] == "limit_order"
    assert captured["time_in_force"] == "ioc"
    assert captured["client_order_id"]
    assert state["status"] == "OPEN"
    assert state["lots"] == 5
    assert state["order_id"] == order["id"]


def test_lost_entry_response_leaves_exact_pending_identity(isolated_user):
    contract = {"id": 9, "symbol": "MV-BTC-X", "contract_value": ".001",
                "strike_price": "64000"}
    quote = {"entry_price": 10, "limit_price": 10.1}
    plan = {"lots": 5, "proposed_risk_usd": 10}
    with patch.object(dashboard, "_tp_policy", return_value={"sl_target_pnl": 50}), \
            patch.object(dashboard, "_post_dashboard_order", side_effect=TimeoutError("lost")), \
            patch.object(dashboard, "_lookup_dashboard_order", return_value=None):
        with pytest.raises(RuntimeError, match="recovery pending"):
            dashboard._submit_manual_move_entry(
                "evening", "buy", contract, quote, plan, False)
    pending = json.loads((isolated_user / "straddle_state.json").read_text())
    assert pending["status"] == "ENTRY_PENDING"
    assert pending["pending_entry_client_order_id"]
    assert pending["pending_entry_submission_state"] == "submission_unknown"


def test_proven_fill_open_write_failure_recovers_exact_order(isolated_user):
    contract = {"id": 9, "symbol": "MV-BTC-X", "contract_value": ".001",
                "strike_price": "64000"}
    quote = {"entry_price": 10, "limit_price": 10.1}
    plan = {"lots": 5, "proposed_risk_usd": 10}
    order_box = {}
    real_write = dashboard._atomic_write_json
    failed_open_once = False

    def submit(payload):
        order = {"id": 78, "client_order_id": payload["client_order_id"],
                 "product_id": 9, "side": "buy", "reduce_only": False,
                 "state": "closed", "filled_size": 5, "unfilled_size": 0,
                 "average_fill_price": "10"}
        order_box["order"] = order
        return order, {"success": True, "result": order}

    def flaky_write(path, value):
        nonlocal failed_open_once
        if value.get("status") == "OPEN" and not failed_open_once:
            failed_open_once = True
            raise OSError("one-off fsync failure")
        return real_write(path, value)

    with patch.object(dashboard, "_tp_policy", return_value={"sl_target_pnl": 50}), \
            patch.object(dashboard, "_post_dashboard_order", side_effect=submit), \
            patch.object(dashboard, "_lookup_dashboard_order",
                         side_effect=lambda *args, **kwargs: order_box.get("order")), \
            patch.object(dashboard, "_atomic_write_json", side_effect=flaky_write):
        state, _ = dashboard._submit_manual_move_entry(
            "evening", "buy", contract, quote, plan, False)

    assert failed_open_once
    assert state["status"] == "OPEN"
    assert state["entry_state_write_recovered"] is True
    assert json.loads((isolated_user / "straddle_state.json").read_text())["status"] == "OPEN"


def _open_state(**updates):
    state = {
        "slot": "evening", "status": "OPEN", "side": "long",
        "product_id": 9, "symbol": "MV-BTC-X", "lots": 10,
        "owned_entry_lots": 10, "entry_mark": 8, "contract_value": .001,
        "entry_date": "2026-07-15", "entry_time_utc": "01:00:00",
        "order_id": 11, "client_order_id": "mv-e-alice-e-old",
        "tsl_stop_order_id": 501, "tp_stop_order_id": 502,
    }
    state.update(updates)
    return state


def test_squareoff_persists_reduce_only_identity_and_cancels_only_after_flat(
        isolated_user):
    state = _open_state()
    _write(isolated_user / "straddle_state.json", state)
    events = []
    calls = 0

    def positions():
        nonlocal calls
        calls += 1
        events.append("position_initial" if calls == 1 else "position_flat")
        return ([{"product_id": 9, "size": "10"}] if calls == 1 else [])

    def submit(payload):
        persisted = json.loads((isolated_user / "straddle_state.json").read_text())
        assert persisted["pending_close_client_order_id"] == payload["client_order_id"]
        events.append("post")
        order = {"id": 88, "client_order_id": payload["client_order_id"],
                 "product_id": 9, "side": "sell", "reduce_only": True,
                 "state": "closed", "filled_size": 10, "unfilled_size": 0,
                 "average_fill_price": "9"}
        return order, {"success": True, "result": order}

    def cancel(flat_state):
        events.append("cancel")
        return True, []

    with patch.object(dashboard, "_strict_exchange_positions", side_effect=positions), \
            patch.object(dashboard, "_lookup_dashboard_order", return_value=None), \
            patch.object(dashboard, "_post_dashboard_order", side_effect=submit), \
            patch.object(dashboard, "_cancel_flat_position_protection", side_effect=cancel), \
            patch.object(dashboard, "_append_trade_history", return_value=True):
        result = dashboard._close_move_state_locked("evening", state)

    assert events.index("post") < events.index("position_flat") < events.index("cancel")
    assert result["order_id"] == 88
    closed = json.loads((isolated_user / "straddle_state.json").read_text())
    assert closed["status"] == "CLOSED"
    assert closed["exit_client_order_id"]


def test_partial_squareoff_keeps_state_open_and_retains_protection(isolated_user):
    state = _open_state()
    _write(isolated_user / "straddle_state.json", state)
    calls = 0

    def positions():
        nonlocal calls
        calls += 1
        return ([{"product_id": 9, "size": "10"}] if calls == 1
                else [{"product_id": 9, "size": "3"}])

    def submit(payload):
        order = {"id": 89, "client_order_id": payload["client_order_id"],
                 "product_id": 9, "side": "sell", "reduce_only": True,
                 "state": "closed", "filled_size": 7, "unfilled_size": 3,
                 "average_fill_price": "9"}
        return order, {"success": True, "result": order}

    cancel = Mock()
    with patch.object(dashboard, "_strict_exchange_positions", side_effect=positions), \
            patch.object(dashboard, "_lookup_dashboard_order", return_value=None), \
            patch.object(dashboard, "_post_dashboard_order", side_effect=submit), \
            patch.object(dashboard, "_cancel_flat_position_protection", cancel), \
            patch.object(dashboard.time, "sleep"):
        with pytest.raises(RuntimeError, match="3 lots remain OPEN"):
            dashboard._close_move_state_locked("evening", state)
    remaining = json.loads((isolated_user / "straddle_state.json").read_text())
    assert remaining["status"] == "OPEN"
    assert remaining["lots"] == 3
    assert remaining["owned_entry_lots"] == 10
    assert remaining["original_owned_entry_lots"] == 10
    assert remaining["partial_exit_gross_pnl_usd"] == pytest.approx(.007)
    assert remaining["tsl_stop_order_id"] == 501
    cancel.assert_not_called()

    final_calls = 0

    def final_positions():
        nonlocal final_calls
        final_calls += 1
        return ([{"product_id": 9, "size": "3"}] if final_calls == 1 else [])

    def final_submit(payload):
        order = {"id": 90, "client_order_id": payload["client_order_id"],
                 "product_id": 9, "side": "sell", "reduce_only": True,
                 "state": "closed", "filled_size": 3, "unfilled_size": 0,
                 "average_fill_price": "10"}
        return order, {"success": True, "result": order}

    with patch.object(dashboard, "_strict_exchange_positions", side_effect=final_positions), \
            patch.object(dashboard, "_lookup_dashboard_order", return_value=None), \
            patch.object(dashboard, "_post_dashboard_order", side_effect=final_submit), \
            patch.object(dashboard, "_cancel_flat_position_protection", return_value=(True, [])), \
            patch.object(dashboard, "_append_trade_history", return_value=True):
        dashboard._close_move_state_locked("evening", remaining)
    closed = json.loads((isolated_user / "straddle_state.json").read_text())
    assert closed["status"] == "CLOSED"
    assert closed["owned_entry_lots"] == 10
    assert closed["gross_pnl_usd"] == pytest.approx(.013)
    assert closed["pnl_usd"] == pytest.approx(.01)


def test_squareoff_blocks_aggregate_size_mismatch_before_post(isolated_user):
    state = _open_state()
    _write(isolated_user / "straddle_state.json", state)
    submit = Mock()
    with patch.object(dashboard, "_strict_exchange_positions",
                      return_value=[{"product_id": "9", "size": "12"}]), \
            patch.object(dashboard, "_post_dashboard_order", submit):
        with pytest.raises(RuntimeError, match="owned position mismatch"):
            dashboard._close_move_state_locked("evening", state)
    submit.assert_not_called()


def test_manual_entry_honors_shared_account_exposure_lock(isolated_user):
    with account_entry_lock(isolated_user, "holder") as held:
        assert held
        with dashboard.app.test_request_context(
                "/api/manual-entry?slot=evening", method="POST", json={"side": "buy"}), \
                patch.object(dashboard, "_active_creds", return_value=("key", "secret")), \
                patch.object(dashboard.req, "post") as post:
            response, status = _response_tuple(dashboard.api_manual_entry())
    assert status == 409
    assert "exposure change" in response.get_json()["error"]
    post.assert_not_called()


def test_protection_failure_forces_verified_flatten(isolated_user):
    state = _open_state()
    _write(isolated_user / "straddle_state.json", state)
    with patch.object(dashboard, "_tp_health", return_value={}), \
            patch.object(dashboard, "_tp_running", return_value=False), \
            patch.object(dashboard, "_spawn_tp", return_value=object()), \
            patch.object(dashboard, "_wait_for_protection", return_value=(False, {})), \
            patch.object(dashboard, "_send_telegram"), \
            patch.object(dashboard, "_force_flatten_move",
                         return_value={"pnl": 1, "order_id": 90}) as flatten:
        protected, detail = dashboard._protect_or_flatten_move(
            "evening", state, datetime.now(timezone.utc))
    assert not protected
    assert detail["flattened"] is True
    flatten.assert_called_once()


def test_monitor_start_exception_also_forces_flatten(isolated_user):
    state = _open_state()
    _write(isolated_user / "straddle_state.json", state)
    with patch.object(dashboard, "_tp_health", return_value={}), \
            patch.object(dashboard, "_tp_running", return_value=False), \
            patch.object(dashboard, "_spawn_tp", side_effect=OSError("spawn failed")), \
            patch.object(dashboard, "_send_telegram"), \
            patch.object(dashboard, "_force_flatten_move",
                         return_value={"pnl": 1, "order_id": 91}) as flatten:
        protected, detail = dashboard._protect_or_flatten_move(
            "evening", state, datetime.now(timezone.utc))
    assert not protected
    assert detail["flattened"] is True
    assert detail["protection_health"]["last_error"] == "spawn failed"
    flatten.assert_called_once()


def test_success_response_does_not_use_an_always_true_expression():
    source = inspect.getsource(dashboard.api_manual_entry)
    assert "dry_run or True" not in source
