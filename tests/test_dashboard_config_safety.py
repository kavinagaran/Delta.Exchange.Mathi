import json
import re
from pathlib import Path
from unittest.mock import patch

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
    assert "let configReady = false" in html
    assert "if (!configReady)" in html
    assert "saveButton.disabled = false" in html
    assert "Configuration could not be verified — Save remains locked" in html
    assert "function shortMoveUi()" in html
    assert "Short MOVE is enabled. Enter a positive Maximum short risk $" in html


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
