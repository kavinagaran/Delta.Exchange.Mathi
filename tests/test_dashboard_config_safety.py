import json
import re
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import dashboard


@pytest.fixture
def isolated_account(tmp_path, monkeypatch):
    users = tmp_path / "users"
    users.mkdir()
    monkeypatch.setattr(dashboard, "USERS_DIR", users)
    monkeypatch.setattr(dashboard, "DASH_USER", "alice")
    monkeypatch.setattr(dashboard, "BOT_USER", "alice")
    dashboard._basic_cache.clear()
    dashboard._trend_auto_health.clear()
    return users / "alice"


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_environment_or_legacy_flag_cannot_default_account_to_live(
        isolated_account, monkeypatch):
    monkeypatch.setenv("TREND_AUTO_ENTRY_MODE", "live")
    monkeypatch.setenv("TREND_AUTO_ENTRY_ENABLED", "true")
    assert dashboard._trend_auto_mode() == "shadow"

    _write_json(isolated_account / "config.json", {
        "TREND_AUTO_ENTRY_ENABLED": "true",
    })
    assert dashboard._trend_auto_mode() == "shadow"
    assert dashboard._user_cfg()["TREND_AUTO_ENTRY_ENABLED"] == "false"


def test_only_explicit_valid_per_account_mode_enables_live(isolated_account):
    _write_json(isolated_account / "config.json", {
        "TREND_AUTO_ENTRY_MODE": "live",
    })
    assert dashboard._trend_auto_mode() == "live"
    assert dashboard._user_cfg()["TREND_AUTO_ENTRY_ENABLED"] == "true"


def test_corrupt_account_config_blocks_auto_entry_and_config_api(isolated_account):
    isolated_account.mkdir(parents=True, exist_ok=True)
    (isolated_account / "config.json").write_text("{not-json", encoding="utf-8")

    with patch.object(dashboard, "_execute_trend_entry") as execute:
        with pytest.raises(dashboard.AccountConfigError):
            dashboard._maybe_auto_trend_entry()
        execute.assert_not_called()

    with dashboard.app.test_request_context("/api/config"):
        response, status = dashboard.get_config()
    assert status == 409
    assert response.get_json()["config_valid"] is False


def test_account_file_username_must_match_its_directory(isolated_account):
    _write_json(isolated_account / "account.json", {
        "username": "bob",
        "pw_hash": "irrelevant",
    })
    assert dashboard._find_account("alice") is None
    assert dashboard._load_accounts() == []


def _select_markup(html: str, element_id: str) -> str:
    match = re.search(
        rf'<select id="{re.escape(element_id)}"[^>]*>(.*?)</select>',
        html,
        flags=re.DOTALL,
    )
    assert match, f"missing select {element_id}"
    return match.group(1)


def test_config_page_is_fail_safe_until_verified_load():
    html = (Path(dashboard.BASE) / "templates" / "config.html").read_text(
        encoding="utf-8")
    assert _select_markup(html, "c-DRY_RUN").lstrip().startswith(
        '<option value="true">')
    assert _select_markup(html, "c-MORNING_ENABLED").lstrip().startswith(
        '<option value="false">')
    assert _select_markup(html, "c-EVENING_ENABLED").lstrip().startswith(
        '<option value="false">')
    assert re.search(r'<button[^>]+id="config-save"[^>]+disabled', html)
    assert re.search(r'<button[^>]+id="config-reset"[^>]+disabled', html)
    assert "let configReady = false" in html
    assert "if (!configReady)" in html
    assert "saveButton.disabled = false" in html
    assert "resetButton.disabled = false" in html
    assert "Configuration could not be verified — Save remains locked" in html
    assert "function shortMoveUi()" in html
    assert "Short MOVE is enabled. Enter a positive Maximum short risk $" in html


def test_config_reset_profile_covers_every_page_field_and_is_fail_safe():
    html = (Path(dashboard.BASE) / "templates" / "config.html").read_text(
        encoding="utf-8")
    page_keys = set(re.findall(r'id="c-([A-Z0-9_]+)"', html))
    preserved = set(dashboard.CONFIG_PAGE_PRESERVED_KEYS)
    time_keys = {
        "MORNING_H_UTC", "MORNING_M_UTC",
        "MORNING_EXIT_H_UTC", "MORNING_EXIT_M_UTC",
        "ENTRY_H_UTC", "ENTRY_M_UTC", "EXIT_H_UTC", "EXIT_M_UTC",
    }

    assert len(page_keys) == 62
    assert preserved == {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"}
    assert page_keys - preserved == set(dashboard.CONFIG_PAGE_DEFAULTS) - time_keys
    assert time_keys <= set(dashboard.CONFIG_PAGE_DEFAULTS)
    assert set(dashboard.CONFIG_PAGE_DEFAULTS) <= set(dashboard.CONFIG_KEYS)

    defaults = dashboard.CONFIG_PAGE_DEFAULTS
    assert defaults["DRY_RUN"] == "true"
    assert defaults["MORNING_ENABLED"] == "false"
    assert defaults["EVENING_ENABLED"] == "false"
    assert defaults["MORNING_EXIT_ENABLED"] == "false"
    assert defaults["EVENING_EXIT_ENABLED"] == "false"
    assert defaults["MORNING_SIDE"] == defaults["EVENING_SIDE"] == "buy"
    assert defaults["TREND_AUTO_ENTRY_MODE"] == "shadow"
    assert defaults["ALLOW_SHORT_MOVE"] == "false"
    assert defaults["SHORT_MAX_RISK_USD"] == "0"
    assert defaults["RISK_FAIL_CLOSED"] == "true"
    assert defaults["SAFE_EXECUTION_ENABLED"] == "true"
    assert defaults["ALLOW_EXTERNAL_POSITIONS_WITH_BOT"] == "false"
    assert defaults["TREND_ALLOW_MISSING_BOOK"] == "false"
    assert defaults["TREND_MARKET_FALLBACK_ENABLED"] == "false"

    hazardous_current = {
        "DRY_RUN": "false", "MORNING_ENABLED": "true",
        "EVENING_ENABLED": "true", "TREND_AUTO_ENTRY_MODE": "live",
        "ALLOW_SHORT_MOVE": "true", "SHORT_MAX_RISK_USD": "0",
    }
    assert dashboard._validate_config_update(
        dict(defaults), hazardous_current) is None

    assert "function resetToDefaults()" in html
    assert "RESET_PRESERVED_KEYS.has(k)" in html
    assert "function rememberSavedPreservedValues(savedBody = null)" in html
    assert "rememberSavedPreservedValues();" in html
    assert "rememberSavedPreservedValues(body);" in html
    assert "hasOwnProperty.call(savedBody, k)" in html
    assert "el.value = window._loadedPreservedValues[k]" in html
    assert "Reset only fills this form; it does not close open" in html
    assert "will no longer have a scheduled" in html
    assert "including the displayed schedules" in html
    assert "Recommended defaults loaded — review them, then Save configuration" in html


def test_saving_reset_profile_preserves_credentials_and_off_page_protection(
        isolated_account):
    current = {
        "DRY_RUN": "false",
        "ALLOW_SHORT_MOVE": "true",
        "SHORT_MAX_RISK_USD": "200",
        "TELEGRAM_BOT_TOKEN": "account-secret-token",
        "TELEGRAM_CHAT_ID": "123456",
        "TP_TARGET_PNL": "250",
        "SL_TARGET_PNL": "200",
        "TSL_TARGET_PNL": "100",
        "TP_TARGET_PNL_TREND": "125",
        "DYNAMIC_LOTS": "true",
        "MAX_TRADES_PER_DAY": "1",
    }
    _write_json(isolated_account / "config.json", current)

    with dashboard.app.test_request_context(
            "/api/config", method="POST",
            json=dict(dashboard.CONFIG_PAGE_DEFAULTS)):
        response = dashboard.set_config()
    assert response.get_json()["ok"] is True

    saved = json.loads((isolated_account / "config.json").read_text(
        encoding="utf-8"))
    for key, value in dashboard.CONFIG_PAGE_DEFAULTS.items():
        assert saved[key] == value
    assert saved["TELEGRAM_BOT_TOKEN"] == "account-secret-token"
    assert saved["TELEGRAM_CHAT_ID"] == "123456"
    assert saved["TP_TARGET_PNL"] == "250"
    assert saved["SL_TARGET_PNL"] == "200"
    assert saved["TSL_TARGET_PNL"] == "100"
    assert saved["TP_TARGET_PNL_TREND"] == "125"
    assert saved["DYNAMIC_LOTS"] == "true"
    assert saved["MAX_TRADES_PER_DAY"] == "3"
    assert saved["TREND_AUTO_ENTRY_ENABLED"] == "False"


def test_tp_save_snapshots_open_state_under_close_lock_and_restarts_after_release(
        isolated_account):
    state_path = isolated_account / "trend_state.json"
    pending_intent = {
        "client_order_id": "pending-tp-1",
        "product_id": 42,
        "lots": 6,
    }
    _write_json(state_path, {
        "slot": "trend",
        "status": "OPEN",
        "product_id": 42,
        "lots": 6,
        "pending_tp_protection": pending_intent,
        "pending_close_client_order_id": "close-journal-1",
        "orphan_protection_order_ids": [77],
        "protection_config": {"tp_target_pnl": 25},
    })
    lock_active = {"value": False}

    @contextmanager
    def close_lock(user_dir, name, owner, **kwargs):
        assert user_dir == isolated_account
        assert name == "close-trend"
        assert "dashboard-config" in owner
        lock_active["value"] = True
        try:
            yield True
        finally:
            lock_active["value"] = False

    real_load = dashboard._load_json

    def guarded_load(path, default):
        if Path(path) == state_path:
            assert lock_active["value"], "OPEN state must be reloaded inside its close lock"
        return real_load(path, default)

    restart = Mock()

    def restart_after_release(user, slot):
        assert lock_active["value"] is False
        restart(user, slot)
        return True

    policy = {
        "tp_target_pnl": 125.0,
        "sl_target_pnl": 50.0,
        "tsl_arm_pnl": 50.0,
        "tsl_trail_pnl": 25.0,
        "tsl_lock_min_pnl": 5.0,
        "poll_secs": 15,
    }
    with patch.object(dashboard, "account_file_lock", side_effect=close_lock), \
            patch.object(dashboard, "_load_json", side_effect=guarded_load), \
            patch.object(dashboard, "_tp_policy", return_value=policy), \
            patch.object(dashboard, "_tp_running", return_value=True), \
            patch.object(dashboard, "_restart_tp_monitor",
                         side_effect=restart_after_release):
        with dashboard.app.test_request_context(
                "/api/config", method="POST",
                json={"TP_TARGET_PNL_TREND": "125"}):
            response = dashboard.set_config()

    assert response.get_json() == {"ok": True, "tp_restarted": ["trend"]}
    restart.assert_called_once_with("alice", "trend")
    saved_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved_state["protection_config"] == policy
    assert saved_state["pending_tp_protection"] == pending_intent
    assert saved_state["pending_close_client_order_id"] == "close-journal-1"
    assert saved_state["orphan_protection_order_ids"] == [77]


def test_short_move_requires_explicit_positive_risk_cap():
    current = {
        "ALLOW_SHORT_MOVE": "false",
        "SHORT_MAX_RISK_USD": "0",
    }
    assert dashboard._validate_config_update({}, current) is None
    assert dashboard._validate_config_update({
        "ALLOW_SHORT_MOVE": "false", "SHORT_MAX_RISK_USD": "0",
    }, current) is None
    error = dashboard._validate_config_update({
        "ALLOW_SHORT_MOVE": "true", "SHORT_MAX_RISK_USD": "0",
    }, current)
    assert "Maximum short risk" in error
    assert dashboard._validate_config_update({
        "ALLOW_SHORT_MOVE": "true", "SHORT_MAX_RISK_USD": "100",
    }, current) is None

    # A full-form save can explicitly turn an invalid/stale current flag off.
    stale_enabled = {
        "ALLOW_SHORT_MOVE": "true",
        "SHORT_MAX_RISK_USD": "0",
    }
    assert dashboard._validate_config_update({
        "ALLOW_SHORT_MOVE": "false", "SHORT_MAX_RISK_USD": "0",
    }, stale_enabled) is None

    # An unrelated partial client may not bypass an already-invalid short setup.
    error = dashboard._validate_config_update({"DRY_RUN": "true"}, stale_enabled)
    assert "Short MOVE is enabled" in error
