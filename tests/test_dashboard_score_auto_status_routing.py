from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

import dashboard


class _LoopComplete(RuntimeError):
    pass


def _write(path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _score_config(mode: str) -> dict:
    return {
        "DRY_RUN": "true" if mode == "dry_run" else "false",
        "TREND_ENGINE_SCORE_AUTO_MODE": mode,
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


def test_score_status_reads_live_namespace_and_live_ownership(
        tmp_path, monkeypatch):
    users = tmp_path / "users"
    account = users / "alice"
    account.mkdir(parents=True)
    _write(account / "config.json", _score_config("live"))
    _write(account / "trend_state.json", {
        "status": "OPEN",
        "dry_run": False,
        "execution_mode": "live",
        "ownership": dashboard.TREND_SCORE_AUTO_LIVE_OWNERSHIP,
        "entry_trigger": dashboard.TREND_SCORE_AUTO_TRIGGER,
        "trend_score_zone": dashboard.TREND_SCORE_PE_ZONE,
        "symbol": "P-BTC-66400-230726",
        "lots": 640,
        "requested_lots": 1000,
    })
    # A conflicting paper state proves that status did not accidentally read
    # the historical DRY-RUN-only namespace.
    _write(account / "dry_run" / "trend_state.json", {
        "status": "OPEN",
        "dry_run": True,
        "execution_mode": "dry_run",
        "ownership": dashboard.TREND_SCORE_AUTO_OWNERSHIP,
        "entry_trigger": dashboard.TREND_SCORE_AUTO_TRIGGER,
        "trend_score_zone": dashboard.TREND_SCORE_CE_ZONE,
        "symbol": "C-BTC-65400-230726",
        "lots": 1000,
    })
    monkeypatch.setattr(dashboard, "USERS_DIR", users)
    monkeypatch.setattr(dashboard, "_active_user", lambda: "alice")
    dashboard._trend_score_auto_health.clear()
    dashboard._trend_score_auto_health["alice"] = {
        "mode": "dry_run",
        "engine_zone": dashboard.TREND_SCORE_CE_ZONE,
        "last_error": "stale paper-only health",
    }

    with dashboard.app.test_request_context(
            "/api/trend-engine/score-auto/status"):
        payload = dashboard.api_trend_engine_score_auto_status().get_json()

    assert payload["enabled"] is True
    assert payload["mode"] == "live"
    assert payload["execution_mode"] == "live"
    assert payload["data_namespace"] == "live"
    assert payload["dry_run_only"] is False
    assert payload["live_orders_enabled"] is True
    assert payload["ownership"] == dashboard.TREND_SCORE_AUTO_LIVE_OWNERSHIP
    assert payload["position_status"] == "OPEN"
    assert payload["current_zone"] == dashboard.TREND_SCORE_PE_ZONE
    assert payload["engine_zone"] == dashboard.TREND_SCORE_PE_ZONE
    assert payload["symbol"].startswith("P-BTC-")
    assert payload["lots"] == 640
    assert payload["requested_lots"] == 1000
    assert "last_error" not in payload


def _run_one_supervisor_iteration(
        monkeypatch, *, mode, score_effect=None, snapshot_effect=None,
        recovery_claimed=False):
    score_cycle = Mock(side_effect=score_effect)
    legacy_cycle = Mock()
    snapshot = Mock(side_effect=snapshot_effect)
    pending_recovery = Mock(return_value=recovery_claimed)
    monkeypatch.setattr(
        dashboard, "_load_accounts", lambda: [{"username": "alice"}],
    )
    monkeypatch.setattr(dashboard, "_ensure_open_monitors", Mock())
    monkeypatch.setattr(
        dashboard,
        "_maybe_recover_trend_score_live_pending",
        pending_recovery,
    )
    monkeypatch.setattr(dashboard, "_trend_score_auto_mode", lambda: mode)
    monkeypatch.setattr(dashboard, "_trend_snapshot", snapshot)
    monkeypatch.setattr(
        dashboard, "_maybe_auto_trend_score_cycle", score_cycle,
    )
    monkeypatch.setattr(dashboard, "_maybe_auto_trend_entry", legacy_cycle)
    monkeypatch.setattr(dashboard, "_trend_audit", Mock())
    monkeypatch.setattr(
        dashboard.time,
        "sleep",
        Mock(side_effect=_LoopComplete("one supervisor iteration completed")),
    )

    with pytest.raises(_LoopComplete, match="one supervisor iteration"):
        dashboard._trend_auto_loop()
    return score_cycle, legacy_cycle, snapshot, pending_recovery


@pytest.mark.parametrize("mode", ("dry_run", "live"))
def test_supervisor_routes_both_score_modes_to_score_controller(
        mode, monkeypatch):
    score_cycle, legacy_cycle, snapshot, pending_recovery = (
        _run_one_supervisor_iteration(
        monkeypatch,
        mode=mode,
        snapshot_effect=RuntimeError("legacy snapshot unavailable"),
        )
    )

    pending_recovery.assert_called_once()
    snapshot.assert_called_once()
    score_cycle.assert_called_once()
    legacy_cycle.assert_not_called()


def test_supervisor_routes_disabled_score_mode_to_legacy_controller(
        monkeypatch):
    score_cycle, legacy_cycle, _, pending_recovery = (
        _run_one_supervisor_iteration(
        monkeypatch,
        mode="disabled",
        )
    )

    pending_recovery.assert_called_once()
    score_cycle.assert_not_called()
    legacy_cycle.assert_called_once()


def test_live_score_supervisor_errors_are_reported_to_score_health(
        monkeypatch):
    dashboard._trend_score_auto_health.clear()
    dashboard._trend_auto_health.clear()

    score_cycle, legacy_cycle, _, pending_recovery = (
        _run_one_supervisor_iteration(
        monkeypatch,
        mode="live",
        score_effect=RuntimeError("live score cycle failed"),
        )
    )

    pending_recovery.assert_called_once()
    score_cycle.assert_called_once()
    legacy_cycle.assert_not_called()
    assert (
        dashboard._trend_score_auto_health["alice"]["last_error"]
        == "live score cycle failed"
    )
    assert "alice" not in dashboard._trend_auto_health


def test_supervisor_pending_recovery_preempts_disabled_legacy_entry(
        monkeypatch):
    score_cycle, legacy_cycle, snapshot, pending_recovery = (
        _run_one_supervisor_iteration(
            monkeypatch,
            mode="disabled",
            recovery_claimed=True,
        )
    )

    pending_recovery.assert_called_once()
    score_cycle.assert_not_called()
    legacy_cycle.assert_not_called()
    snapshot.assert_not_called()
