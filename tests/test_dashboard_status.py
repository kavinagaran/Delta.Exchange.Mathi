import json
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

import dashboard


@pytest.fixture
def isolated_status_account(tmp_path, monkeypatch):
    users = tmp_path / "users"
    users.mkdir()
    monkeypatch.setattr(dashboard, "USERS_DIR", users)
    monkeypatch.setattr(dashboard, "DASH_USER", "alice")
    monkeypatch.setattr(dashboard, "BOT_USER", "alice")
    dashboard._external_options.clear()
    return users / "alice"


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_external_option_uses_exchange_cashflows_for_live_net_pnl():
    # Long option: premium paid at entry is a realized cash outflow, while the
    # current option value is an unrealized cash inflow. Delta's standalone
    # unrealized_pnl does not include that entry premium.
    position = {
        "product_id": 142277,
        "product_symbol": "P-BTC-64400-160726",
        "size": "3",
        "entry_price": "295",
        "mark_price": "121.3",
        "realized_cashflow": "-0.885",
        "unrealized_cashflow": "0.357",
        "commission": "0.0229206858",
        "unrealized_pnl": "-0.357",
    }

    view = dashboard._external_option_view(position)

    assert view["pnl_source"] == "cashflow_net"
    assert view["live_pnl"] == pytest.approx(-0.5509206858)
    assert view["live_pnl"] != pytest.approx(-0.357)


def test_external_option_falls_back_only_when_cashflow_accounting_is_unavailable():
    view = dashboard._external_option_view({
        "product_id": 1, "product_symbol": "C-BTC-TEST", "size": "1",
        "unrealized_pnl": "12.5",
    })

    assert view["pnl_source"] == "unrealized_pnl_fallback"
    assert view["live_pnl"] == 12.5


def test_latest_closed_trade_uses_full_utc_time_and_overnight_rollover():
    # The first trade's raw exit clock is smaller, but it exited five minutes
    # later on the following UTC day.
    history = [
        {
            "slot": "evening",
            "date": "2026-07-15",
            "entry_time": "23:55:00",
            "exit_time": "00:10:00",
            "symbol": "OVERNIGHT",
            "pnl_usd": -12.5,
        },
        {
            "slot": "morning",
            "entry_date": "2026-07-16",
            "entry_time_utc": "00:01:00",
            "exit_time_utc": "00:05:00",
            "symbol": "SAME-DAY",
            "pnl_usd": 4,
        },
    ]

    latest = dashboard._latest_closed_trade(history, {})

    assert latest == {
        "slot": "evening",
        "symbol": "OVERNIGHT",
        "pnl_usd": -12.5,
        "exit_date": "2026-07-16",
        "exit_time_utc": "00:10:00",
        "closed_at_utc": "2026-07-16T00:10:00Z",
    }


def test_history_is_authoritative_for_same_closed_trade_timestamp():
    history = [{
        "slot": "trend",
        "entry_date": "2026-07-16",
        "entry_time": "08:00:00",
        "exit_time": "08:30:00",
        "symbol": "C-BTC-TEST",
        "pnl_usd": -7.25,
    }]
    states = {"trend": {
        "status": "CLOSED",
        "entry_date": "2026-07-16",
        "entry_time_utc": "08:00:00",
        "exit_time_utc": "08:30:00",
        "symbol": "C-BTC-TEST",
        "pnl_usd": 99,
    }}

    latest = dashboard._latest_closed_trade(history, states)

    assert latest["pnl_usd"] == -7.25


def test_status_latest_closed_trade_skips_dry_run_and_preserves_unknown_pnl(
        isolated_status_account, monkeypatch):
    _write(isolated_status_account / "trade_history.json", [
        {
            "slot": "morning",
            "date": "2026-07-16",
            "entry_time": "08:00:00",
            "exit_time": "09:00:00",
            "symbol": "KNOWN-OLDER",
            "pnl_usd": 10,
        },
        {
            "slot": "trend",
            "date": "2026-07-18",
            "entry_time": "08:00:00",
            "exit_time": "09:00:00",
            "symbol": "SIMULATED-NEWER",
            "pnl_usd": 1000,
            "dry_run": True,
        },
    ])
    _write(isolated_status_account / "straddle_state.json", {
        "slot": "evening",
        "status": "CLOSED",
        "entry_date": "2026-07-17",
        "entry_time_utc": "10:00:00",
        "exit_date": "2026-07-17",
        "exit_time_utc": "11:00:00",
        "symbol": "UNKNOWN-LATEST",
    })
    _write(isolated_status_account / "morning_state.json", {
        "status": "CLOSED",
        "entry_date": "2026-07-19",
        "symbol": "NO-EXIT-TIMESTAMP",
        "pnl_usd": 50,
    })

    monkeypatch.setattr(dashboard, "_sync_states_from_exchange", lambda: None)
    monkeypatch.setattr(dashboard, "_revive_tp_monitors", lambda: None)
    monkeypatch.setattr(dashboard, "_user_cfg", lambda: {})
    monkeypatch.setitem(dashboard._last_revive, "ts", time.time())

    class TickerResponse:
        @staticmethod
        def json():
            return {"result": {"mark_price": "64637"}}

    monkeypatch.setattr(dashboard.req, "get", lambda *args, **kwargs: TickerResponse())

    with dashboard.app.test_request_context("/api/status"):
        payload = dashboard.api_status().get_json()

    assert payload["latest_closed_trade"] == {
        "slot": "evening",
        "symbol": "UNKNOWN-LATEST",
        "pnl_usd": None,
        "exit_date": "2026-07-17",
        "exit_time_utc": "11:00:00",
        "closed_at_utc": "2026-07-17T11:00:00Z",
    }


def test_dashboard_history_flush_keeps_unknown_external_accounting_retryable_and_upserts(
        isolated_status_account):
    state_path = isolated_status_account / "straddle_state.json"
    history_path = isolated_status_account / "trade_history.json"
    pending = {
        "slot": "evening",
        "status": "CLOSED",
        "symbol": "MV-BTC-TEST",
        "product_id": 101,
        "order_id": "entry-1",
        "client_order_id": "entry-client-1",
        "entry_date": "2026-07-15",
        "entry_time_utc": "01:02:03",
        "exit_date": "2026-07-15",
        "exit_time_utc": "01:03:00",
        "exit_trigger": "closed_externally",
        "exit_reconciliation_status": "pending_order_history",
        "exit_mark": None,
        "gross_pnl_usd": None,
        "pnl_usd": None,
        "fees_usd": None,
        "history_pending": True,
        "history_logged": False,
    }
    _write(state_path, pending)

    dashboard._flush_pending_history()

    still_pending = json.loads(state_path.read_text(encoding="utf-8"))
    first_history = json.loads(history_path.read_text(encoding="utf-8"))
    assert still_pending["history_pending"] is True
    assert still_pending["history_logged"] is False
    assert len(first_history) == 1
    assert first_history[0]["accounting_status"] == "pending"
    assert first_history[0]["pnl_usd"] is None

    complete = {
        **still_pending,
        "exit_reconciliation_status": "resolved_order_history",
        "exit_mark": 0.5,
        "gross_pnl_usd": -5.0,
        "pnl_usd": -5.3,
        "entry_fee_usd": 0.1,
        "exit_fee_usd": 0.2,
        "fees_usd": 0.3,
        "pnl_includes_fees": True,
    }
    _write(state_path, complete)

    dashboard._flush_pending_history()

    finalized = json.loads(state_path.read_text(encoding="utf-8"))
    repaired_history = json.loads(history_path.read_text(encoding="utf-8"))
    assert finalized["history_pending"] is False
    assert len(repaired_history) == 1
    assert repaired_history[0]["accounting_status"] == "complete"
    assert repaired_history[0]["exit_mark"] == 0.5
    assert repaired_history[0]["pnl_usd"] == -5.3
    assert repaired_history[0]["fees_usd"] == 0.3


def test_dashboard_defers_exchange_flat_state_to_strict_monitor(monkeypatch):
    state = {
        "slot": "trend",
        "status": "OPEN",
        "side": "long",
        "symbol": "C-BTC-TEST",
        "product_id": 101,
        "owned_entry_lots": 10,
        "entry_date": "2026-07-15",
        "entry_time_utc": "01:02:03",
        "entry_mark": 1.0,
    }

    def unexpected_exchange_lookup(*args, **kwargs):
        raise AssertionError("dashboard must not guess an external close from order history")

    monkeypatch.setattr(dashboard.req, "get", unexpected_exchange_lookup)
    original = dict(state)

    reconciled = dashboard._reconcile_stale_close("trend", state, live_pids=set())

    assert reconciled == original
    assert reconciled["status"] == "OPEN"
    assert "exit_mark" not in reconciled
    assert "pnl_usd" not in reconciled


def test_closed_external_pending_accounting_is_periodically_supervised(
        isolated_status_account, monkeypatch):
    _write(isolated_status_account / "account.json", {"username": "alice"})
    _write(isolated_status_account / "straddle_state.json", {
        "slot": "evening",
        "status": "CLOSED",
        "product_id": 101,
        "symbol": "MV-BTC-TEST",
        "exit_trigger": "closed_externally",
        "history_pending": True,
    })
    dashboard._monitor_last_restart.clear()
    spawned = Mock(return_value=Mock(pid=4321))
    monkeypatch.setattr(dashboard, "_tp_running", lambda user, slot: False)
    monkeypatch.setattr(dashboard, "_tp_health", lambda user, slot: {})
    monkeypatch.setattr(dashboard, "_spawn_tp", spawned)

    started = dashboard._ensure_open_monitors(force=True)

    assert started == 1
    spawned.assert_called_once_with("alice", "evening")


@pytest.mark.parametrize("pending_fields", [
    {
        "pending_tp_protection": {
            "client_order_id": "tp-journal-1",
            "product_id": 101,
        },
    },
    {"tsl_stop_order_id": 7001},
    {"orphan_protection_order_ids": [7002]},
    {"protection_cleanup_pending": True},
    {
        "history_pending": True,
        "accounting_status": "pending",
        "exit_trigger": "manual_squareoff",
        "exit_reconciliation_status": "pending_fill_ledger",
    },
])
def test_closed_trend_pending_cleanup_or_accounting_is_supervised(
        isolated_status_account, monkeypatch, pending_fields):
    _write(isolated_status_account / "account.json", {"username": "alice"})
    _write(isolated_status_account / "trend_state.json", {
        "slot": "trend",
        "status": "CLOSED",
        "product_id": 101,
        "symbol": "C-BTC-TEST",
        "entry_trigger": "trend_alignment",
        "ownership": "trend_bot",
        **pending_fields,
    })
    dashboard._monitor_last_restart.clear()
    spawned = Mock(return_value=Mock(pid=4322))
    monkeypatch.setattr(dashboard, "_tp_running", lambda user, slot: False)
    monkeypatch.setattr(dashboard, "_tp_health", lambda user, slot: {})
    monkeypatch.setattr(dashboard, "_spawn_tp", spawned)

    started = dashboard._ensure_open_monitors(force=True)

    assert started == 1
    spawned.assert_called_once_with("alice", "trend")
