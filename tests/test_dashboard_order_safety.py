import json
import inspect
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import dashboard
import tp_monitor
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


MANUAL_MOVE_ROUTES = ("/api/manual-entry", "/api/manual-entry/preview")


@contextmanager
def _authenticated_client(tmp_path):
    """A client past `_auth_gate`, so a 404 proves the route is gone rather
    than merely proving the request was unauthenticated."""
    with patch.object(dashboard, "DASH_PASS", ""), \
            patch.object(dashboard, "USERS_DIR", tmp_path / "no-accounts"):
        yield dashboard.app.test_client()


def test_manual_move_routes_are_unroutable():
    """Discretionary MOVE entry was retired: the routes must not exist at all.

    A 410 stub still accepts a request and depends on an early return staying
    first in the body.  An unregistered rule cannot be re-enabled by an edit
    that moves code above the guard.
    """
    registered = {str(rule) for rule in dashboard.app.url_map.iter_rules()}
    for path in MANUAL_MOVE_ROUTES:
        assert path not in registered
    assert not hasattr(dashboard, "api_manual_entry")
    assert not hasattr(dashboard, "api_manual_entry_preview")


@pytest.mark.parametrize("path", MANUAL_MOVE_ROUTES)
def test_manual_move_routes_cannot_reach_an_order_post(path, tmp_path):
    with patch.object(dashboard.req, "post") as post, \
            patch.object(dashboard, "_post_dashboard_order") as submit, \
            _authenticated_client(tmp_path) as client:
        for call in (client.get(f"{path}?slot=evening&side=buy"),
                     client.post(f"{path}?slot=evening", json={"side": "buy"})):
            assert call.status_code == 404
    post.assert_not_called()
    submit.assert_not_called()


def test_exposure_routes_reject_invalid_slot_before_exchange_call():
    with dashboard.app.test_request_context(
            "/api/square-off?slot=typo", method="POST"), \
            patch.object(dashboard.req, "post") as post:
        response, status = _response_tuple(dashboard.api_square_off())
        assert status == 400
        assert "slot" in response.get_json()["error"]
        post.assert_not_called()


def test_live_move_products_paginates_without_requiring_future_expiry():
    page_one = Mock()
    page_one.json.return_value = {
        "result": [{"id": 1}], "meta": {"after": "cursor-1"}}
    page_two = Mock()
    page_two.json.return_value = {
        "result": [{"id": 2}], "meta": {"after": None}}

    with patch.object(dashboard.req, "get",
                      side_effect=[page_one, page_two]) as get:
        products = dashboard._fetch_live_mv_products()

    assert [product["id"] for product in products] == [1, 2]
    first_params = get.call_args_list[0].kwargs["params"]
    second_params = get.call_args_list[1].kwargs["params"]
    assert "expiry" not in first_params
    assert first_params["page_size"] == 100
    assert "after" not in first_params
    assert second_params["after"] == "cursor-1"


def test_live_move_products_rejects_repeated_pagination_cursor():
    response = Mock()
    response.json.return_value = {
        "result": [], "meta": {"after": "same-cursor"}}
    with patch.object(dashboard.req, "get", return_value=response), \
            pytest.raises(RuntimeError, match="did not advance"):
        dashboard._fetch_live_mv_products()


def test_manual_move_routes_never_touch_strategy_or_exchange_helpers(tmp_path):
    """The retired routes are gone, so no request can reach contract discovery,
    exchange position reads, or order submission."""
    with patch.object(dashboard, "_active_creds", return_value=("key", "secret")), \
            patch.object(dashboard, "_fetch_live_mv_products") as products, \
            patch.object(dashboard, "_strict_exchange_positions") as positions, \
            patch.object(dashboard, "_post_dashboard_order") as submit, \
            patch.object(dashboard.req, "post") as raw_post, \
            _authenticated_client(tmp_path) as client:
        preview = client.get("/api/manual-entry/preview?slot=evening&side=sell")
        entry = client.post(
            "/api/manual-entry?slot=evening",
            json={"side": "buy", "product_id": 9,
                  "symbol": "MV-BTC-65000-180726", "lots": 2, "mark": 10})

    assert preview.status_code == 404
    assert entry.status_code == 404
    for helper in (products, positions, submit, raw_post):
        helper.assert_not_called()


def test_discretionary_move_entry_helpers_are_gone():
    """The retired routes were the only callers of these helpers.  Keeping the
    code alive behind a dead route is how a disabled path comes back."""
    for name in ("_select_atm_mv", "_current_atm_mv", "_move_execution_quote",
                 "_move_lot_plan", "_validate_move_entry_account",
                 "_manual_entry_lots", "_submit_manual_move_entry",
                 "_protect_or_flatten_move", "_force_flatten_move",
                 "_recover_pending_move_entry", "_persist_proven_move_open",
                 "_flatten_unpersisted_move_fill", "_post_entry_exchange_size"):
        assert not hasattr(dashboard, name), name


def test_overview_has_no_manual_or_scheduled_move_controls():
    source = (Path(__file__).resolve().parents[1] / "templates" / "overview.html").read_text(
        encoding="utf-8")
    mobile = (
        Path(__file__).resolve().parents[1]
        / "mv_btc_bot" / "lib" / "main.dart"
    ).read_text(encoding="utf-8")
    assert "manualEntry(" not in source
    assert "manualBtns(" not in source
    assert "Scheduled forecast-driven entries only" not in source
    assert "function moveDecisionHtml(view)" not in source
    assert "Automatic MOVE Forecast" not in source
    assert "SIDEWAYS SELL immediate" not in source
    # UI-4: the three separate morning/evening/trend cards (one of which was
    # literally titled "Trend-based position (CE / PE)") were replaced by one
    # unified open-positions list; the Trend slot is still bot-owned and
    # still iterated, just rendered as a compact row instead of its own card.
    assert "for (const slot of ['morning', 'evening', 'trend'])" in source
    assert "st.display_slots ||" in source
    assert "/api/manual-entry" not in mobile
    assert "MORNING_SIDE" not in mobile
    assert "EVENING_SIDE" not in mobile
    assert (
        "AUTO forecast" in mobile
        or (
            "class DashboardWebPage" in mobile
            and "label: 'Nithi Bot'" in mobile
            and "path: '/'" in mobile
        )
    )


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


@pytest.mark.parametrize(
    "order",
    (
        {"state": "closed", "filled_size": "9.5"},
        {"state": "closed", "filled_size": -1},
        {"state": "closed", "filled_size": 11},
        {"state": "closed", "unfilled_size": "0.5"},
        {"state": "closed", "unfilled_size": -1},
        {"state": "closed", "unfilled_size": 11},
    ),
)
def test_dashboard_terminal_fill_rejects_malformed_or_out_of_range_lots(order):
    assert dashboard._terminal_fill(order, 10) is None


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


def test_square_off_honors_shared_account_exposure_lock(isolated_user):
    """The retired manual-entry route used to prove this; the surviving
    exposure-changing route must honour the same cross-process lock."""
    _write(isolated_user / "straddle_state.json", _open_state())
    with account_entry_lock(isolated_user, "holder") as held:
        assert held
        with dashboard.app.test_request_context(
                "/api/square-off?slot=evening", method="POST"), \
                patch.object(dashboard, "_active_creds", return_value=("key", "secret")), \
                patch.object(dashboard.req, "post") as post:
            response, status = _response_tuple(dashboard.api_square_off())
    assert status == 409
    assert "in progress" in response.get_json()["error"]
    post.assert_not_called()


def test_no_order_path_gates_on_an_always_true_expression():
    """The original guard covered the retired manual-entry route.  Apply it to
    every surviving dashboard order path instead of dropping the check."""
    for func in (dashboard.api_square_off, dashboard.api_trend_entry,
                 dashboard._execute_trend_entry,
                 dashboard._close_move_state_locked):
        assert "dry_run or True" not in inspect.getsource(func)


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


def test_trend_close_post_boundary_is_durable_and_restart_never_resubmits(
        isolated_user):
    state = _trend_squareoff_state()
    _write(isolated_user / "trend_state.json", state)

    def crash_after_post_boundary(_payload):
        durable = json.loads(
            (isolated_user / "trend_state.json").read_text(encoding="utf-8"))
        assert durable["pending_close_submission_state"] == "submitting"
        assert durable["pending_close_post_boundary"] is True
        raise SystemExit("simulated process death after request boundary")

    with patch.object(dashboard, "_active_user", return_value="alice"), \
            patch.object(
                dashboard, "_tp_health",
                return_value=_trend_continuity_health(state)), \
            patch.object(
                dashboard, "_strict_realtime_position",
                return_value={"product_id": 42, "size": "6"}), \
            patch.object(dashboard, "_lookup_dashboard_order", return_value=None), \
            patch.object(
                dashboard, "_post_dashboard_order",
                side_effect=crash_after_post_boundary) as first_submit, \
            patch.object(dashboard, "audit_event"):
        with pytest.raises(SystemExit, match="simulated process death"):
            dashboard._close_move_state_locked("trend", state)

    first_submit.assert_called_once()
    pending = json.loads(
        (isolated_user / "trend_state.json").read_text(encoding="utf-8"))
    duplicate = Mock(side_effect=AssertionError("duplicate close submitted"))

    with patch.object(dashboard, "_active_user", return_value="alice"), \
            patch.object(
                dashboard, "_strict_realtime_position",
                return_value={"product_id": 42, "size": "6"}), \
            patch.object(dashboard, "_lookup_dashboard_order", return_value=None), \
            patch.object(dashboard, "_post_dashboard_order", duplicate):
        with pytest.raises(RuntimeError, match="prior close submission remains unresolved"):
            dashboard._close_move_state_locked("trend", pending)

    duplicate.assert_not_called()


def test_dashboard_prepared_close_is_a_clean_tp_recoverable_pre_post_journal(
        isolated_user):
    """Exercise the actual dashboard fsync shape through TP recovery."""
    state = _trend_squareoff_state(
        # Simulate residue from a prior partial-close generation. Creating a
        # new identity must replace every field that could imply an old POST.
        pending_close_submission_state="submission_unknown",
        pending_close_post_boundary=True,
        pending_close_last_attempt_at_utc="2026-07-15T01:00:01+00:00",
        pending_close_last_attempt_utc="2026-07-15T01:00:01+00:00",
        pending_close_attempts=2,
        pending_close_order_state="closed",
        pending_close_exchange_state="closed",
        pending_close_state="post_boundary_unresolved",
        pending_close_lookup_conclusive=True,
        pending_close_last_reconciled_utc="2026-07-15T01:00:02+00:00",
    )
    state_file = isolated_user / "trend_state.json"
    _write(state_file, state)

    def crash_after_dashboard_prepared(_order_id, _client_id, _product_id):
        prepared = json.loads(state_file.read_text(encoding="utf-8"))
        assert prepared["pending_close_submission_state"] == "prepared"
        assert prepared["pending_close_post_boundary"] is False
        assert prepared["pending_close_last_attempt_at_utc"] is None
        assert prepared["pending_close_last_attempt_utc"] is None
        assert prepared["pending_close_attempts"] == 0
        assert prepared["pending_close_order_state"] is None
        assert prepared["pending_close_exchange_state"] == ""
        assert prepared["pending_close_state"] == "prepared"
        assert prepared["pending_close_lookup_conclusive"] is False
        raise SystemExit("crash after prepared fsync")

    with patch.object(dashboard, "_move_client_id",
                      return_value="dashboard-prepared-close"), \
            patch.object(dashboard, "_active_user", return_value="alice"), \
            patch.object(
                dashboard, "_tp_health",
                return_value=_trend_continuity_health(state)), \
            patch.object(
                dashboard, "_strict_realtime_position",
                return_value={"product_id": 42, "size": "6"}), \
            patch.object(
                dashboard, "_lookup_dashboard_order",
                side_effect=crash_after_dashboard_prepared), \
            patch.object(dashboard, "audit_event"):
        with pytest.raises(SystemExit, match="crash after prepared fsync"):
            dashboard._close_move_state_locked("trend", state)

    prepared = json.loads(state_file.read_text(encoding="utf-8"))
    recovered_order = {
        "id": "tp-recovered-close",
        "client_order_id": "dashboard-prepared-close",
        "product_id": 42,
        "side": "sell",
        "reduce_only": True,
        "state": "closed",
        "size": 6,
        "unfilled_size": 0,
        "average_fill_price": "9",
        "commission": "0",
    }
    response = {"success": True, "result": recovered_order}
    with patch.object(tp_monitor, "STATE_FILE", state_file), \
            patch.object(
                tp_monitor, "HISTORY_FILE",
                isolated_user / "trend_history.json"), \
            patch.object(tp_monitor, "USER_DIR", isolated_user), \
            patch.object(tp_monitor, "SLOT", "trend"), \
            patch.object(
                tp_monitor, "_trend_fill_ledger_required",
                return_value=False), \
            patch.object(
                tp_monitor, "_verify_trend_close_cycle",
                return_value=(True, "")), \
            patch.object(
                tp_monitor, "get_exchange_size",
                side_effect=[6, 0]), \
            patch.object(
                tp_monitor, "_lookup_pending_close",
                side_effect=[({}, True), (recovered_order, True)]), \
            patch.object(
                tp_monitor, "place_order",
                return_value=response) as submit, \
            patch.object(tp_monitor, "send_telegram"):
        assert tp_monitor.close_position(prepared, 9.0, 6.0)

    submit.assert_called_once()
    assert (
        submit.call_args.kwargs["client_order_id"]
        == "dashboard-prepared-close"
    )
    closed = json.loads(state_file.read_text(encoding="utf-8"))
    assert closed["status"] == "CLOSED"
    assert closed["pending_close_submission_state"] is None
    assert closed["pending_close_client_order_id"] is None
    assert closed["pending_close_order_id"] is None
    assert closed["pending_close_post_boundary"] is False
    assert not dashboard._trend_score_auto_pending_identity(closed)


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
