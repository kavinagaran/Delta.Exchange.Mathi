import json
import inspect
from datetime import datetime, timedelta, timezone
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


def test_move_selector_uses_slot_horizon_and_skips_stale_nonoperational_products():
    now = datetime(2026, 7, 17, 4, 0, tzinfo=timezone.utc)
    products = [
        {"id": 1, "symbol": "MV-BTC-64000-100726", "state": "live",
         "trading_status": "operational", "settlement_time": "2026-07-10T12:00:00Z",
         "strike_price": "64000", "underlying_asset": {"symbol": "BTC"}},
        {"id": 2, "symbol": "MV-BTC-64000-170726", "state": "live",
         "trading_status": "settled", "settlement_time": "2026-07-17T12:00:00Z",
         "strike_price": "64000", "underlying_asset": {"symbol": "BTC"}},
        {"id": 3, "symbol": "MV-BTC-64500-170726", "state": "live",
         "trading_status": "operational", "settlement_time": "2026-07-17T12:00:00Z",
         "strike_price": "64500", "underlying_asset": {"symbol": "BTC"}},
        {"id": 4, "symbol": "MV-BTC-65000-180726", "state": "live",
         "trading_status": "operational", "settlement_time": "2026-07-18T12:00:00Z",
         "strike_price": "65000", "underlying_asset": {"symbol": "BTC"}},
    ]

    def cfg(key, default=""):
        return {"MOVE_MIN_TTE_MINUTES": "90", "MOVE_MAX_TTE_HOURS": "40"}.get(
            key, default)

    with patch.object(dashboard, "_cfg", side_effect=cfg):
        morning = dashboard._select_atm_mv(products, 64600, "morning", now)
        evening = dashboard._select_atm_mv(products, 64600, "evening", now)

    assert morning["id"] == 3
    assert evening["id"] == 4


def test_move_selector_fails_closed_when_target_cycle_has_no_eligible_product():
    now = datetime(2026, 7, 17, 11, 0, tzinfo=timezone.utc)
    products = [
        {"id": 1, "symbol": "MV-BTC-64000-170726", "state": "live",
         "trading_status": "operational", "settlement_time": "2026-07-17T12:00:00Z",
         "strike_price": "64000"},
        {"id": 2, "symbol": "MV-BTC-64500-170726", "state": "closed",
         "trading_status": "operational", "settlement_time": "2026-07-17T12:00:00Z",
         "strike_price": "64500"},
    ]
    with patch.object(dashboard, "_cfg", side_effect=lambda key, default="": default):
        assert dashboard._select_atm_mv(products, 64000, "morning", now) is None


def test_manual_preview_uses_requested_sell_side_and_rejects_zero_lot_plan():
    contract = {
        "id": 9, "symbol": "MV-BTC-65000-180726", "contract_value": ".001",
        "strike_price": "65000", "settlement_time": "2026-07-18T12:00:00Z",
    }
    quote = {"entry_price": 10, "entry_depth": 100}
    with dashboard.app.test_request_context(
            "/api/manual-entry/preview?slot=evening&side=sell"), \
            patch.object(dashboard, "_current_atm_mv", return_value=contract) as select, \
            patch.object(dashboard, "_move_execution_quote", return_value=quote) as pricing, \
            patch.object(dashboard, "_move_lot_plan",
                         return_value={"lots": 0, "reason": "No affordable lots"}):
        response, status = _response_tuple(dashboard.api_manual_entry_preview())

    assert status == 409
    assert response.get_json()["error"] == "No affordable lots"
    select.assert_called_once_with("evening")
    pricing.assert_called_once_with(contract["symbol"], "sell")


def test_manual_entry_refuses_contract_that_changed_after_preview(isolated_user):
    selected = {"id": 10, "symbol": "MV-BTC-65500-180726"}
    with dashboard.app.test_request_context(
            "/api/manual-entry?slot=evening", method="POST",
            json={"side": "buy", "product_id": 9,
                  "symbol": "MV-BTC-65000-180726", "lots": 2, "mark": 10}), \
            patch.object(dashboard, "_active_creds", return_value=("key", "secret")), \
            patch.object(dashboard, "_current_atm_mv", return_value=selected), \
            patch.object(dashboard, "_strict_exchange_positions") as positions, \
            patch.object(dashboard, "_post_dashboard_order") as submit:
        response, status = _response_tuple(dashboard.api_manual_entry())

    assert status == 409
    assert "changed after preview" in response.get_json()["error"]
    positions.assert_not_called()
    submit.assert_not_called()


def test_manual_entry_binds_preview_price_and_lots(isolated_user):
    selected = {"id": 9, "symbol": "MV-BTC-65000-180726"}
    with dashboard.app.test_request_context(
            "/api/manual-entry?slot=evening", method="POST",
            json={"side": "buy", "product_id": 9,
                  "symbol": selected["symbol"], "lots": 2, "mark": 10}), \
            patch.object(dashboard, "_active_creds", return_value=("key", "secret")), \
            patch.object(dashboard, "_current_atm_mv", return_value=selected), \
            patch.object(dashboard, "_strict_exchange_positions", return_value=[]), \
            patch.object(dashboard, "_validate_move_entry_account", return_value=0), \
            patch.object(dashboard, "_move_execution_quote",
                         return_value={"entry_price": 10, "entry_depth": 100}) as pricing, \
            patch.object(dashboard, "_move_lot_plan",
                         return_value={"lots": 3, "reason": "sizing checks passed"}), \
            patch.object(dashboard, "_post_dashboard_order") as submit:
        response, status = _response_tuple(dashboard.api_manual_entry())

    assert status == 409
    assert "sizing changed after preview" in response.get_json()["error"]
    pricing.assert_called_once_with(selected["symbol"], "buy", reference_price=10)
    submit.assert_not_called()


def test_overview_manual_entry_sends_side_and_exact_preview_identity():
    source = (Path(__file__).resolve().parents[1] / "templates" / "overview.html").read_text(
        encoding="utf-8")
    assert "'&side=' + encodeURIComponent(side)" in source
    assert "product_id: p.product_id" in source
    assert "symbol: p.symbol" in source
    assert "lots: p.lots" in source
    assert "mark: p.mark" in source


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
        "entry_fees_usd": 0.0,
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
                 "average_fill_price": "9", "paid_commission": "0"}
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
                 "average_fill_price": "10", "paid_commission": "0"}
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


@pytest.mark.parametrize(
    ("state_lots", "snapshot_lots"), ((3, 3), (3, 6), (6, 3))
)
def test_exchange_sync_never_overwrites_open_owned_same_product_trend_state(
        isolated_user, state_lots, snapshot_lots):
    """A laggy margined snapshot is observation, never Trend ownership truth."""
    state = _open_state(
        slot="trend", product_id=42, symbol="C-BTC-64000-170726",
        lots=state_lots, protection_lots=state_lots,
        owned_entry_lots=3, original_owned_entry_lots=3,
        entry_mark=1.5, entry_trigger="trend_alignment",
        ownership="trend_bot", protection_revision=7,
        continuity_revision=5, position_cycle_id="cycle-preserve-me",
    )
    _write(isolated_user / "trend_state.json", state)
    dashboard._last_sync.pop("alice", None)
    dashboard._external_options.pop("alice", None)

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    def get(url, **_kwargs):
        if url.endswith("/v2/positions/margined"):
            return Response({"success": True, "result": [{
                "product_id": 42, "product_symbol": "C-BTC-64000-170726",
                "size": str(snapshot_lots), "entry_price": "1.5",
                "mark_price": "1.4", "unrealized_pnl": "-0.1",
            }]})
        if url.endswith("/v2/orders/history"):
            # Even historical bot ownership must not invoke recovery over the
            # already-open authoritative cycle.
            return Response({"success": True, "result": [{
                "id": 999, "product_id": 42, "side": "buy",
                "reduce_only": False, "client_order_id": "trend-alice-old",
                "created_at": "2026-07-17T01:00:00Z",
            }]})
        raise AssertionError(f"sync unexpectedly fetched {url}")

    with patch.object(dashboard, "_active_creds", return_value=("key", "secret")), \
            patch.object(dashboard, "_active_user", return_value="alice"), \
            patch.object(dashboard, "_tp_health", return_value={}), \
            patch.object(dashboard, "_flush_pending_history"), \
            patch.object(dashboard, "_sign", return_value={}), \
            patch.object(dashboard.req, "get", side_effect=get):
        dashboard._sync_states_from_exchange()

    persisted = json.loads(
        (isolated_user / "trend_state.json").read_text(encoding="utf-8"))
    assert persisted == state
    assert len(dashboard._external_options["alice"]) == 1
    external = dashboard._external_options["alice"][0]
    assert external["product_id"] == 42
    assert external["lots"] == snapshot_lots
    assert external["trend_state_lots"] == state_lots
    assert external["ownership"] == "pending_trend_reconciliation"


def test_exchange_sync_normalizes_microsecond_recovery_timestamp_to_utc_iso(
        isolated_user):
    opened_at = datetime(2026, 7, 17, 1, 2, 3, tzinfo=timezone.utc)
    opened_at_us = int(opened_at.timestamp() * 1_000_000)
    dashboard._last_sync.pop("alice", None)
    dashboard._external_options.pop("alice", None)

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    def get(url, **_kwargs):
        if url.endswith("/v2/positions/margined"):
            return Response({"success": True, "result": [{
                "product_id": 42, "product_symbol": "C-BTC-64000-170726",
                "size": "3", "entry_price": "1.5",
                "created_at": opened_at_us,
            }]})
        if url.endswith("/v2/orders/history"):
            return Response({"success": True, "result": [{
                "id": 77, "product_id": 42, "side": "buy",
                "reduce_only": False, "client_order_id": "trend-alice-recovered",
                "created_at": opened_at_us, "paid_commission": "0.01",
            }]})
        if url.endswith("/v2/products/42"):
            return Response({"result": {
                "id": 42, "symbol": "C-BTC-64000-170726",
                "strike_price": "64000", "contract_value": "0.001",
                "settlement_time": "2026-07-17T18:00:00Z",
            }})
        raise AssertionError(f"sync unexpectedly fetched {url}")

    with patch.object(dashboard, "_active_creds", return_value=("key", "secret")), \
            patch.object(dashboard, "_active_user", return_value="alice"), \
            patch.object(dashboard, "_tp_health", return_value={}), \
            patch.object(dashboard, "_flush_pending_history"), \
            patch.object(dashboard, "_sign", return_value={}), \
            patch.object(dashboard.req, "get", side_effect=get):
        dashboard._sync_states_from_exchange()

    recovered = json.loads(
        (isolated_user / "trend_state.json").read_text(encoding="utf-8"))
    assert recovered["entry_date"] == "2026-07-17"
    assert recovered["entry_time_utc"] == "01:02:03"
    assert recovered["continuity_anchor_utc"] == opened_at.isoformat()
    assert recovered["position_cycle_id"].startswith("trend-")


def _trend_squareoff_state(**updates):
    state = _open_state(
        slot="trend", product_id=42, symbol="C-BTC-64000-170726",
        lots=6, protection_lots=6, owned_entry_lots=3,
        original_owned_entry_lots=3, entry_mark=8,
        entry_fees_usd=0.0, entry_trigger="trend_alignment",
        ownership="trend_bot", protection_revision=7,
        continuity_revision=5, position_cycle_id="cycle-close",
        externally_added_lots_adopted=3,
    )
    state.update(updates)
    return state


def _trend_continuity_health(state, **updates):
    health = {
        "user": "alice", "slot": "trend",
        "product_id": state["product_id"],
        "entry_order_id": state.get("order_id")
                          or state.get("entry_order_id"),
        "entry_client_order_id": state.get("client_order_id"),
        "protection_revision": state.get("protection_revision", 0),
        "continuity_revision": state.get("continuity_revision", 0),
        "position_cycle_id": state.get("position_cycle_id"),
        "heartbeat_utc": datetime.now(timezone.utc).isoformat(),
        "next_poll_secs": 30,
        "exchange_position_size": (
            -state["lots"] if state.get("side") == "short" else state["lots"]
        ),
        "continuity_verified": True,
        "continuity_verified_size": (
            -state["lots"] if state.get("side") == "short" else state["lots"]
        ),
    }
    health.update(updates)
    return health


def _run_trend_dashboard_squareoff(isolated_user, state, order, proven_fill):
    _write(isolated_user / "trend_state.json", state)
    with patch.object(dashboard, "_move_client_id", return_value="close-trend-test"), \
            patch.object(dashboard, "_active_user", return_value="alice"), \
            patch.object(
                dashboard, "_tp_health",
                return_value=_trend_continuity_health(state)), \
            patch.object(dashboard, "_strict_realtime_position",
                         side_effect=[
                             {"product_id": 42, "size": "6"},
                             {"product_id": 42, "size": "6"},
                             None,
                         ]), \
            patch.object(dashboard, "_lookup_dashboard_order", return_value=None), \
            patch.object(dashboard, "_post_dashboard_order",
                         return_value=(order, {"success": True, "result": order})), \
            patch.object(dashboard, "_wait_dashboard_terminal",
                         return_value=(order, proven_fill)), \
            patch.object(dashboard, "_cancel_flat_position_protection",
                         return_value=(True, [])), \
            patch.object(dashboard, "_append_trade_history", return_value=True), \
            patch.object(dashboard, "audit_event"):
        result = dashboard._close_move_state_locked("trend", state)
    persisted = json.loads(
        (isolated_user / "trend_state.json").read_text(encoding="utf-8"))
    return result, persisted


@pytest.mark.parametrize(
    "health_change",
    [
        {
            "heartbeat_utc": (
                datetime.now(timezone.utc) - timedelta(minutes=5)
            ).isoformat(),
        },
        {"position_cycle_id": "replacement-cycle"},
        {"continuity_verified_size": 5},
    ],
)
def test_new_trend_squareoff_requires_fresh_exact_fill_ledger_continuity(
        isolated_user, health_change):
    state = _trend_squareoff_state()
    _write(isolated_user / "trend_state.json", state)
    health = _trend_continuity_health(state, **health_change)
    submit = Mock()

    with patch.object(dashboard, "_active_user", return_value="alice"), \
            patch.object(dashboard, "_tp_health", return_value=health), \
            patch.object(
                dashboard, "_strict_realtime_position",
                return_value={"product_id": 42, "size": "6"}), \
            patch.object(dashboard, "_post_dashboard_order", submit):
        with pytest.raises(RuntimeError, match="fill-ledger continuity"):
            dashboard._close_move_state_locked("trend", state)

    submit.assert_not_called()
    persisted = json.loads(
        (isolated_user / "trend_state.json").read_text(encoding="utf-8"))
    assert not persisted.get("pending_close_client_order_id")


def test_existing_trend_close_intent_recovers_without_new_continuity_gate(
        isolated_user):
    state = _trend_squareoff_state(
        pending_close_client_order_id="close-trend-existing",
        pending_close_order_id=88,
        pending_close_requested_lots=6,
        pending_close_start_size=6,
        pending_close_side="sell",
        pending_close_submission_state="acknowledged",
    )
    _write(isolated_user / "trend_state.json", state)
    order = {
        "id": 88, "client_order_id": "close-trend-existing",
        "product_id": 42, "side": "sell", "reduce_only": True,
        "state": "closed", "filled_size": 6, "unfilled_size": 0,
        "average_fill_price": "9", "paid_commission": "0",
    }
    submit = Mock()
    health = Mock(return_value={})

    with patch.object(dashboard, "_active_user", return_value="alice"), \
            patch.object(dashboard, "_tp_health", health), \
            patch.object(
                dashboard, "_strict_realtime_position",
                side_effect=[{"product_id": 42, "size": "6"}, None]), \
            patch.object(dashboard, "_lookup_dashboard_order", return_value=order), \
            patch.object(
                dashboard, "_wait_dashboard_terminal",
                return_value=(order, 6)), \
            patch.object(dashboard, "_post_dashboard_order", submit), \
            patch.object(
                dashboard, "_cancel_flat_position_protection",
                return_value=(True, [])), \
            patch.object(dashboard, "_append_trade_history", return_value=True), \
            patch.object(dashboard, "audit_event"):
        result = dashboard._close_move_state_locked("trend", state)

    assert result["order_id"] == 88
    submit.assert_not_called()
    health.assert_not_called()
    closed = json.loads(
        (isolated_user / "trend_state.json").read_text(encoding="utf-8"))
    assert closed["status"] == "CLOSED"


def test_prepared_trend_close_rechecks_current_generation_before_post(
        isolated_user):
    state = _trend_squareoff_state(
        pending_close_client_order_id="close-trend-prepared",
        pending_close_requested_lots=6,
        pending_close_start_size=6,
        pending_close_side="sell",
        pending_close_submission_state="prepared",
    )
    _write(isolated_user / "trend_state.json", state)
    stale_health = _trend_continuity_health(
        state, position_cycle_id="replacement-cycle",
    )
    submit = Mock()

    with patch.object(dashboard, "_active_user", return_value="alice"), \
            patch.object(dashboard, "_tp_health", return_value=stale_health), \
            patch.object(
                dashboard, "_strict_realtime_position",
                return_value={"product_id": 42, "size": "6"}), \
            patch.object(dashboard, "_lookup_dashboard_order", return_value=None), \
            patch.object(dashboard, "_post_dashboard_order", submit):
        with pytest.raises(RuntimeError, match="fill-ledger continuity"):
            dashboard._close_move_state_locked("trend", state)

    submit.assert_not_called()
    persisted = json.loads(
        (isolated_user / "trend_state.json").read_text(encoding="utf-8"))
    assert persisted["pending_close_client_order_id"] == "close-trend-prepared"
    assert persisted["pending_close_submission_state"] == "prepared"


def test_prepared_trend_close_with_fresh_generation_reuses_client_id(
        isolated_user):
    state = _trend_squareoff_state(
        pending_close_client_order_id="close-trend-prepared",
        pending_close_requested_lots=6,
        pending_close_start_size=6,
        pending_close_side="sell",
        pending_close_submission_state="prepared",
    )
    _write(isolated_user / "trend_state.json", state)
    order = {
        "id": 89, "client_order_id": "close-trend-prepared",
        "product_id": 42, "side": "sell", "reduce_only": True,
        "state": "closed", "filled_size": 6, "unfilled_size": 0,
        "average_fill_price": "9", "paid_commission": "0",
    }
    submit = Mock(return_value=(order, {"success": True, "result": order}))

    with patch.object(dashboard, "_active_user", return_value="alice"), \
            patch.object(
                dashboard, "_tp_health",
                return_value=_trend_continuity_health(state)), \
            patch.object(
                dashboard, "_strict_realtime_position",
                side_effect=[
                    {"product_id": 42, "size": "6"},
                    {"product_id": 42, "size": "6"},
                    None,
                ]), \
            patch.object(dashboard, "_lookup_dashboard_order", return_value=None), \
            patch.object(dashboard, "_post_dashboard_order", submit), \
            patch.object(
                dashboard, "_wait_dashboard_terminal",
                return_value=(order, 6)), \
            patch.object(
                dashboard, "_cancel_flat_position_protection",
                return_value=(True, [])), \
            patch.object(dashboard, "_append_trade_history", return_value=True), \
            patch.object(dashboard, "audit_event"):
        result = dashboard._close_move_state_locked("trend", state)

    assert result["order_id"] == 89
    submit.assert_called_once()
    assert (
        submit.call_args.args[0]["client_order_id"]
        == "close-trend-prepared"
    )


def test_concurrent_fill_during_trend_squareoff_leaves_accounting_pending(
        isolated_user):
    # The dashboard order proves only three fills, while the aggregate position
    # fell by six. The other three may be a protection/manual fill and must not
    # be priced using this order's average fill.
    order = {
        "id": 88, "client_order_id": "close-trend-test",
        "product_id": 42, "side": "sell", "reduce_only": True,
        "state": "closed", "filled_size": 3, "unfilled_size": 3,
        "average_fill_price": "9", "paid_commission": "0.03",
    }

    result, closed = _run_trend_dashboard_squareoff(
        isolated_user, _trend_squareoff_state(), order, proven_fill=3)

    assert result["pnl"] is None
    assert closed["status"] == "CLOSED"
    assert closed["accounting_status"] == "pending"
    assert closed["partial_exit_accounting_status"] == "fill_ledger_pending"
    assert closed["unreconciled_partial_exit_lots"] == 6
    assert closed["pnl_usd"] is None
    assert closed["gross_pnl_usd"] is None
    assert closed["exit_mark"] is None


def test_missing_close_commission_keeps_trend_squareoff_accounting_pending(
        isolated_user):
    order = {
        "id": 89, "client_order_id": "close-trend-test",
        "product_id": 42, "side": "sell", "reduce_only": True,
        "state": "closed", "filled_size": 6, "unfilled_size": 0,
        "average_fill_price": "9",
    }

    result, closed = _run_trend_dashboard_squareoff(
        isolated_user, _trend_squareoff_state(), order, proven_fill=6)

    assert result["pnl"] is None
    assert closed["accounting_status"] == "pending"
    assert closed["fees_complete"] is False
    assert closed["fees_usd"] is None
    assert closed["pnl_usd"] is None
    assert closed["gross_pnl_usd"] is None
    assert closed["unreconciled_partial_exit_lots"] == 6


@pytest.mark.parametrize("status,pending_fields", [
    ("CLOSED", {
        "pending_stop_protection": {
            "client_order_id": "stop-journal-1",
            "product_id": 42,
        },
    }),
    ("IDLE", {"pending_close_client_order_id": "close-journal-1"}),
    ("CLOSED", {
        "history_pending": True,
        "accounting_status": "pending",
        "exit_reconciliation_status": "pending_fill_ledger",
    }),
])
def test_trend_preview_blocks_previous_unresolved_state(
        isolated_user, status, pending_fields):
    _write(isolated_user / "trend_state.json", {
        "slot": "trend",
        "status": status,
        "product_id": 42,
        "entry_trigger": "trend_alignment",
        **pending_fields,
    })
    snapshot = {
        "combined": "up",
        "timeframes": {
            "5m": {"candle_time": "5"},
            "15m": {"candle_time": "15"},
            "1h": {"candle_time": "60"},
        },
    }
    with patch.object(dashboard, "_sync_states_from_exchange"), \
            patch.object(dashboard, "_trend_snapshot", return_value=snapshot), \
            patch.object(dashboard, "_current_trend_option_details") as select:
        result, status_code = dashboard._trend_entry_preview_data()

    assert status_code == 200
    assert result["can_enter"] is False
    assert "reconcil" in result["reason"].lower() \
        or "cleanup" in result["reason"].lower()
    select.assert_not_called()


def test_trend_execute_rechecks_previous_state_before_exchange_submission(
        isolated_user):
    clean_preview = {
        "ok": True,
        "can_enter": True,
        "product_id": 42,
        "lots": 3,
        "contract_value": 0.001,
        "dry_run": False,
    }

    def preview_then_publish_pending_cleanup():
        _write(isolated_user / "trend_state.json", {
            "slot": "trend",
            "status": "CLOSED",
            "product_id": 42,
            "entry_trigger": "trend_alignment",
            "pending_tp_protection": {
                "client_order_id": "tp-journal-race",
                "product_id": 42,
            },
        })
        return clean_preview, 200

    submit = Mock()
    credentials = Mock(return_value=("key", "secret"))
    with dashboard.app.test_request_context(
            "/api/trend-entry", method="POST"), \
            patch.object(dashboard, "_sync_states_from_exchange"), \
            patch.object(dashboard, "_trend_entry_preview_data",
                         side_effect=preview_then_publish_pending_cleanup), \
            patch.object(dashboard, "_active_creds", credentials), \
            patch.object(dashboard, "_execute_trend_chunks", submit), \
            patch.object(dashboard, "_trend_audit"):
        response, status_code = dashboard._execute_trend_entry(auto=False)

    assert status_code == 409
    assert "cleanup" in response.get_json()["error"].lower()
    credentials.assert_not_called()
    submit.assert_not_called()
    persisted = json.loads(
        (isolated_user / "trend_state.json").read_text(encoding="utf-8"))
    assert persisted["pending_tp_protection"]["client_order_id"] \
        == "tp-journal-race"


@pytest.mark.parametrize(
    "fee_source_updates",
    [
        {"entry_fee_source": "configured_estimate"},
        {
            "entry_fee_source": "exchange_fill_ledger",
            "original_bot_entry_fee_source": "fee_pending",
        },
    ],
)
def test_estimated_or_pending_entry_fee_keeps_trend_flat_accounting_pending(
        isolated_user, fee_source_updates):
    order = {
        "id": 90, "client_order_id": "close-trend-test",
        "product_id": 42, "side": "sell", "reduce_only": True,
        "state": "closed", "filled_size": 6, "unfilled_size": 0,
        "average_fill_price": "9", "paid_commission": "0.05",
    }
    state = _trend_squareoff_state(
        entry_fees_usd=0.25,
        original_bot_entry_fee_usd=0.10,
        **fee_source_updates,
    )

    result, closed = _run_trend_dashboard_squareoff(
        isolated_user, state, order, proven_fill=6)

    assert result["pnl"] is None
    assert closed["accounting_status"] == "pending"
    assert closed["exit_reconciliation_status"] == "pending_fill_ledger"
    assert closed["history_pending"] is True
    assert closed["fees_usd"] is None
    assert closed["pnl_usd"] is None


def test_legacy_numeric_entry_fee_without_source_remains_authoritative(
        isolated_user):
    order = {
        "id": 91, "client_order_id": "close-trend-test",
        "product_id": 42, "side": "sell", "reduce_only": True,
        "state": "closed", "filled_size": 6, "unfilled_size": 0,
        "average_fill_price": "9", "paid_commission": "0.05",
    }
    state = _trend_squareoff_state(entry_fees_usd=0.25)

    result, closed = _run_trend_dashboard_squareoff(
        isolated_user, state, order, proven_fill=6)

    assert result["pnl"] is not None
    assert closed["accounting_status"] == "complete"
    assert closed["exit_reconciliation_status"] == "complete"
    assert closed["history_pending"] is False
    assert closed["fees_usd"] == pytest.approx(0.30)


def test_tp_sl_tsl_exchange_flags_require_fresh_matching_strict_proofs(
        isolated_user):
    state = _trend_squareoff_state(
        status="OPEN", stop_kind="tsl", tsl_stop_order_id=501,
        stop_client_order_id="trend-stop-501",
        tp_stop_order_id=502, tp_client_order_id="trend-tp-502", order_id=11,
        client_order_id="trend-alice-entry",
    )
    _write(isolated_user / "trend_state.json", state)
    fresh = {
        "user": "alice", "slot": "trend", "product_id": 42,
        "entry_order_id": 11,
        "entry_client_order_id": "trend-alice-entry",
        "protection_revision": 7, "continuity_revision": 5,
        "position_cycle_id": "cycle-close",
        "heartbeat_utc": datetime.now(timezone.utc).isoformat(),
        "next_poll_secs": 30, "status": "healthy",
        "protected_lots": 6, "exchange_position_size": 6,
        "exchange_protected_lots": 6,
        "continuity_verified": True, "continuity_verified_size": 6,
        "protection_established": True,
        "exchange_protection_complete": True,
        "stop_order_id": 501, "tp_order_id": 502,
        "stop_order_proof": {
            "ok": True, "covered_lots": 6,
            "order": {
                "id": 501, "client_order_id": "trend-stop-501",
                "product_id": 42,
            },
        },
        "tp_order_proof": {
            "ok": True, "covered_lots": 6,
            "order": {
                "id": 502, "client_order_id": "trend-tp-502",
                "product_id": 42,
            },
        },
    }

    def payload_for(health):
        with patch.object(
                dashboard, "_tp_health",
                side_effect=lambda _user, slot: health if slot == "trend" else {}), \
                patch.object(
                    dashboard, "_tp_running",
                    side_effect=lambda _user, slot: slot == "trend"), \
                dashboard.app.test_request_context("/api/tp-monitor"):
            return dashboard.tp_monitor_status().get_json()["trend"]

    proven = payload_for(fresh)
    assert proven["tp_on_exchange"] is True
    assert proven["tsl_on_exchange"] is True
    assert proven["sl_on_exchange"] is False

    state["stop_kind"] = "sl"
    _write(isolated_user / "trend_state.json", state)
    assert payload_for(fresh)["sl_on_exchange"] is True

    stale = {
        **fresh,
        "heartbeat_utc": (
            datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
    }
    stale_payload = payload_for(stale)
    assert stale_payload["tp_on_exchange"] is False
    assert stale_payload["sl_on_exchange"] is False
    assert stale_payload["tsl_on_exchange"] is False

    legacy_claim_only = dict(fresh)
    legacy_claim_only.pop("stop_order_proof")
    legacy_claim_only.pop("tp_order_proof")
    legacy_claim_only["tp_on_exchange"] = True
    legacy_claim_only["sl_on_exchange"] = True
    legacy_payload = payload_for(legacy_claim_only)
    assert legacy_payload["tp_on_exchange"] is False
    assert legacy_payload["sl_on_exchange"] is False
