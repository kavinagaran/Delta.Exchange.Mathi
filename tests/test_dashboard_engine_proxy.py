"""/api/engine/{snapshot,live,health,status} — read-only proxy onto
trend_engine_client.py for the rebuilt Trend Engine page (UI-3).

These routes carry no side effects and cannot reach any order-placement
code path; they only ever call through to the fail-closed client, which
never raises (ADR 0001).
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import patch

import dashboard
import trend_engine_client


@contextmanager
def _authenticated_client(tmp_path):
    with patch.object(dashboard, "DASH_PASS", ""), \
            patch.object(dashboard, "USERS_DIR", tmp_path / "no-accounts"):
        yield dashboard.app.test_client()


def test_snapshot_route_proxies_the_client_and_forwards_symbol(tmp_path):
    calls = []

    def fake_get_snapshot(symbol="BTCUSD", **kwargs):
        calls.append(symbol)
        return {"symbol": symbol, "regime": "RANGE"}

    with _authenticated_client(tmp_path) as client, \
            patch.object(trend_engine_client, "get_snapshot", side_effect=fake_get_snapshot):
        resp = client.get("/api/engine/snapshot?symbol=ETHUSD")
    assert resp.status_code == 200
    assert resp.get_json() == {"symbol": "ETHUSD", "regime": "RANGE"}
    assert calls == ["ETHUSD"]


def test_snapshot_route_defaults_to_btcusd(tmp_path):
    with _authenticated_client(tmp_path) as client, \
            patch.object(trend_engine_client, "get_snapshot",
                         return_value={"symbol": "BTCUSD"}) as mocked:
        resp = client.get("/api/engine/snapshot")
    assert resp.status_code == 200
    mocked.assert_called_once_with("BTCUSD")


def test_live_route_proxies_display_only_view(tmp_path):
    view = {
        "available": True,
        "provisional": True,
        "live_score": 18.4,
        "committed_score": 12.1,
    }
    with _authenticated_client(tmp_path) as client, \
            patch.object(
                trend_engine_client, "get_live_view", return_value=view
            ) as mocked:
        resp = client.get("/api/engine/live?symbol=BTCUSD")
    assert resp.status_code == 200
    assert resp.get_json() == view
    mocked.assert_called_once_with("BTCUSD")


def test_live_history_route_proxies_display_only_score_candles(tmp_path):
    history = {
        "available": True,
        "display_only": True,
        "resolution": "5m",
        "candles": [{"open": 5, "high": 10, "low": 2, "close": 7}],
    }
    with _authenticated_client(tmp_path) as client, \
            patch.object(
                trend_engine_client, "get_live_history", return_value=history
            ) as mocked:
        resp = client.get("/api/engine/live-history?symbol=BTCUSD")
    assert resp.status_code == 200
    assert resp.get_json() == {
        **history,
        "history_window_hours": 24,
        "trade_marker_mode": "dry_run",
        "trade_markers": [],
    }
    mocked.assert_called_once_with("BTCUSD", limit=288)


def test_decision_history_route_proxies_only_committed_score_points(tmp_path):
    history = {
        "available": True,
        "source": "committed_decisions",
        "resolution": "5m",
        "decisions": [{
            "time_utc": "2026-08-01T10:00:00Z",
            "committed_score": 42.5,
            "zone": "CE_2_ITM",
        }],
    }
    with _authenticated_client(tmp_path) as client, \
            patch.object(
                trend_engine_client, "get_decision_history", return_value=history
            ) as mocked:
        resp = client.get("/api/engine/decision-history?symbol=BTCUSD")
    assert resp.status_code == 200
    assert resp.get_json() == {
        **history,
        "history_window_hours": 24,
        "trade_marker_mode": "dry_run",
        "trade_markers": [],
    }
    mocked.assert_called_once_with("BTCUSD", limit=288)


def test_live_history_trade_markers_are_account_and_mode_scoped(tmp_path):
    now = datetime.now(timezone.utc)
    users = tmp_path / "users"
    for username, _code, zone, symbol in (
        ("nithi", "CE", "CE_2_ITM", "C-BTC-65000-300726"),
        ("mathi", "PE", "PE_2_ITM", "P-BTC-65000-300726"),
    ):
        data_dir = users / username / "dry_run"
        data_dir.mkdir(parents=True)
        (data_dir / "trade_history.json").write_text(json.dumps([{
            "status": "CLOSED",
            "dry_run": True,
            "execution_mode": "dry_run",
            "trend_score_zone": zone,
            "symbol": symbol,
            "entry_at_utc": (
                now - timedelta(minutes=5)
            ).isoformat().replace("+00:00", "Z"),
            "exit_at_utc": (
                now - timedelta(minutes=2)
            ).isoformat().replace("+00:00", "Z"),
        }]), encoding="utf-8")

    with dashboard.app.test_request_context(), \
            patch.object(dashboard, "DASH_USER", "nithi"), \
            patch.object(dashboard, "USERS_DIR", users), \
            patch.object(dashboard, "_user_cfg", return_value={
                "DRY_RUN": "true",
                "TREND_ENGINE_SCORE_AUTO_MODE": "dry_run",
            }):
        markers, marker_mode = dashboard._trend_chart_trade_markers()

    assert marker_mode == "dry_run"
    assert [(marker["code"], marker["action"]) for marker in markers] == [
        ("CE", "ENTRY"),
        ("CE", "EXIT"),
    ]
    assert all(
        set(marker) == {"time_utc", "code", "action"}
        for marker in markers
    )


def test_trade_marker_exit_is_kept_when_entry_is_outside_chart_window(tmp_path):
    now = datetime.now(timezone.utc)
    users = tmp_path / "users"
    data_dir = users / "nithi" / "dry_run"
    data_dir.mkdir(parents=True)
    (data_dir / "trade_history.json").write_text(json.dumps([{
        "status": "CLOSED",
        "dry_run": True,
        "execution_mode": "dry_run",
        "trend_score_zone": "PE_2_ITM",
        "symbol": "P-BTC-65000-300726",
        "entry_at_utc": (
            now - timedelta(hours=26)
        ).isoformat().replace("+00:00", "Z"),
        "exit_at_utc": (
            now - timedelta(minutes=10)
        ).isoformat().replace("+00:00", "Z"),
    }]), encoding="utf-8")

    with dashboard.app.test_request_context(), \
            patch.object(dashboard, "DASH_USER", "nithi"), \
            patch.object(dashboard, "USERS_DIR", users), \
            patch.object(dashboard, "_user_cfg", return_value={
                "DRY_RUN": "true",
                "TREND_ENGINE_SCORE_AUTO_MODE": "dry_run",
            }):
        markers, marker_mode = dashboard._trend_chart_trade_markers()

    assert marker_mode == "dry_run"
    assert len(markers) == 1
    assert markers[0]["code"] == "PE"
    assert markers[0]["action"] == "EXIT"
    assert datetime.fromisoformat(
        markers[0]["time_utc"].replace("Z", "+00:00")
    ) > now - timedelta(hours=1)


def test_bad_trade_marker_history_never_hides_score_candles(tmp_path):
    history = {
        "available": True,
        "display_only": True,
        "resolution": "5m",
        "candles": [{"open": 5, "high": 10, "low": 2, "close": 7}],
    }
    with _authenticated_client(tmp_path) as client, \
            patch.object(
                trend_engine_client, "get_live_history", return_value=history
            ), \
            patch.object(
                dashboard,
                "_trend_chart_trade_markers",
                side_effect=ValueError("bad local history"),
            ):
        response = client.get("/api/engine/live-history")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["candles"] == history["candles"]
    assert payload["trade_markers"] == []
    assert payload["trade_marker_mode"] == "unavailable"


def test_bad_trade_marker_history_never_hides_committed_decisions(tmp_path):
    history = {
        "available": True,
        "source": "committed_decisions",
        "resolution": "5m",
        "decisions": [{
            "time_utc": "2026-08-01T10:00:00Z",
            "committed_score": -45.0,
            "zone": "PE_2_ITM",
        }],
    }
    with _authenticated_client(tmp_path) as client, \
            patch.object(
                trend_engine_client, "get_decision_history", return_value=history
            ), \
            patch.object(
                dashboard,
                "_trend_chart_trade_markers",
                side_effect=ValueError("bad local history"),
            ):
        response = client.get("/api/engine/decision-history")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["decisions"] == history["decisions"]
    assert payload["trade_markers"] == []
    assert payload["trade_marker_mode"] == "unavailable"


def test_snapshot_route_never_raises_into_the_dashboard(tmp_path):
    """The whole point of trend_engine_client.get_snapshot is that it never
    raises — but if it somehow did, the route must not 500 into a broken
    page; degrading is the client's job, not this proxy's."""
    with _authenticated_client(tmp_path) as client, \
            patch.object(trend_engine_client, "get_snapshot",
                         return_value=trend_engine_client.degraded_snapshot(
                             "BTCUSD", trend_engine_client.ENGINE_UNREACHABLE)):
        resp = client.get("/api/engine/snapshot")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["data_quality"] == "ENGINE_UNREACHABLE"
    assert payload["entry_allowed"] is False


def test_health_route_proxies_the_client(tmp_path):
    with _authenticated_client(tmp_path) as client, \
            patch.object(trend_engine_client, "get_health",
                         return_value={"ok": True, "version": "0.2.0"}):
        resp = client.get("/api/engine/health")
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "version": "0.2.0"}


def test_status_route_proxies_the_client(tmp_path):
    with _authenticated_client(tmp_path) as client, \
            patch.object(trend_engine_client, "get_status",
                         return_value={"available": False, "detail": "boom"}):
        resp = client.get("/api/engine/status")
    assert resp.status_code == 200
    assert resp.get_json() == {"available": False, "detail": "boom"}


def test_risk_route_proxies_the_client(tmp_path):
    with _authenticated_client(tmp_path) as client, \
            patch.object(trend_engine_client, "get_risk_status",
                         return_value={"available": True, "any_active": True,
                                       "active": ["manual"], "switches": {}}):
        resp = client.get("/api/engine/risk")
    assert resp.status_code == 200
    assert resp.get_json()["active"] == ["manual"]


def test_risk_route_reports_active_when_the_engine_is_unreachable(tmp_path):
    """Fail closed: an unreachable engine cannot prove no switch is latched,
    so the dashboard must not render 'all clear'."""
    with _authenticated_client(tmp_path) as client, \
            patch.object(trend_engine_client, "requests") as mocked_requests:
        mocked_requests.get.side_effect = OSError("connection refused")
        resp = client.get("/api/engine/risk")
    payload = resp.get_json()
    assert payload["available"] is False
    assert payload["any_active"] is True


def test_shadow_route_proxies_the_client(tmp_path):
    with _authenticated_client(tmp_path) as client, \
            patch.object(trend_engine_client, "get_shadow_summary",
                         return_value={"available": True, "agreement_rate": 0.8,
                                       "total_candles": 40, "recent": []}):
        resp = client.get("/api/engine/shadow")
    assert resp.status_code == 200
    assert resp.get_json()["agreement_rate"] == 0.8


def test_no_route_can_post_a_shadow_decision_from_the_browser(tmp_path):
    """Only the trend-auto loop posts a shadow decision, and it does so via
    trend_engine_client directly — never through a dashboard route a browser
    could reach."""
    with _authenticated_client(tmp_path) as client:
        for path in ("/api/engine/shadow/legacy-decision",
                     "/api/engine/legacy-decision"):
            assert client.post(path).status_code == 404, path
        assert client.post("/api/engine/shadow").status_code == 405


def test_no_proxy_route_can_fire_or_resume_a_kill_switch(tmp_path):
    """Kill-switch mutation is an operator action against the engine's own
    /admin endpoints. The dashboard exposes visibility only — a page (or an
    XSS on one) must not be able to disarm the safety layer."""
    with _authenticated_client(tmp_path) as client:
        for path in ("/api/engine/kill-switch", "/api/engine/resume",
                     "/api/engine/admin/kill-switch", "/api/engine/admin/resume"):
            assert client.post(path).status_code == 404, path
        assert client.post("/api/engine/risk").status_code == 405


def test_engine_proxy_routes_require_authentication(tmp_path):
    """A trading dashboard's /api/* stays behind the login gate — these new
    routes must not be an accidental exception."""
    with patch.object(dashboard, "DASH_PASS", "secret"), \
            patch.object(dashboard, "USERS_DIR", tmp_path / "users"):
        (tmp_path / "users").mkdir()
        client = dashboard.app.test_client()
        for path in ("/api/engine/snapshot", "/api/engine/live",
                     "/api/engine/live-history",
                     "/api/engine/health",
                     "/api/engine/status", "/api/engine/risk",
                     "/api/engine/shadow"):
            resp = client.get(path)
            assert resp.status_code == 401, path
