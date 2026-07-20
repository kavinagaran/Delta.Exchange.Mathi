import json
import re
import threading
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


def _response(result) -> tuple[dict, int]:
    if isinstance(result, tuple):
        response, status = result[0], int(result[1])
    else:
        response = result
        status = int(getattr(response, "status_code", 200))
    payload = response if isinstance(response, dict) else response.get_json()
    return payload, status


def _post_config(payload: dict) -> tuple[dict, int]:
    with dashboard.app.test_request_context(
            "/api/config", method="POST", json=payload):
        return _response(dashboard.set_config())


def _mode_availability_endpoint() -> tuple[dict, int]:
    rule = next(
        rule for rule in dashboard.app.url_map.iter_rules()
        if rule.rule == "/api/trading-mode-availability"
    )
    view = dashboard.app.view_functions[rule.endpoint]
    with dashboard.app.test_request_context(rule.rule):
        return _response(view())


def test_environment_or_legacy_flag_cannot_default_account_to_live(
        isolated_account, monkeypatch):
    monkeypatch.setenv("TREND_AUTO_ENTRY_MODE", "live")
    monkeypatch.setenv("TREND_AUTO_ENTRY_ENABLED", "true")
    monkeypatch.setenv("MOVE_AUTO_ENTRY_MODE", "live")
    assert dashboard._trend_auto_mode() == "shadow"
    assert dashboard._user_cfg()["MOVE_AUTO_ENTRY_MODE"] == "shadow"

    _write_json(isolated_account / "config.json", {
        "TREND_AUTO_ENTRY_ENABLED": "true",
    })
    assert dashboard._trend_auto_mode() == "shadow"
    assert dashboard._user_cfg()["TREND_AUTO_ENTRY_ENABLED"] == "false"
    assert dashboard._user_cfg()["MOVE_AUTO_ENTRY_MODE"] == "shadow"


def test_only_explicit_valid_per_account_mode_enables_live(isolated_account):
    _write_json(isolated_account / "config.json", {
        "TREND_AUTO_ENTRY_MODE": "live",
        "MOVE_AUTO_ENTRY_MODE": "live",
    })
    assert dashboard._trend_auto_mode() == "live"
    assert dashboard._user_cfg()["TREND_AUTO_ENTRY_ENABLED"] == "true"
    assert dashboard._user_cfg()["MOVE_AUTO_ENTRY_MODE"] == "live"


def test_move_decision_dashboard_view_is_mode_isolated_and_compact(
        isolated_account):
    def decision(action, dry_run):
        return {
            "schema_version": 1,
            "slot": "morning",
            "decision_id": f"{action}-1",
            "recorded_at_utc": "2026-07-18T00:15:00Z",
            "auto_mode": "shadow",
            "dry_run": dry_run,
            "normalized_input": {
                "contract": {"symbol": "MV-BTC-64000-180726"},
                "market": {"ask": 100},
            },
            "forecast": {
                "expected_payoff_low": 120,
                "expected_payoff_mid": 140,
                "expected_payoff_high": 160,
                "payoff_p99": 400,
                "jump_event_score": 1,
                "event_score_available": False,
                "event_risk_source": "unknown_high_risk",
                "model_features": {"completed_bars": 8640},
            },
            "decision": {
                "action": action,
                "side": "buy" if action == "LONG_MOVE" else None,
                "conflict": False,
                "metrics": {
                    "long_edge_per_contract": .01,
                    "short_edge_per_contract": -.1,
                },
                "failed_gates": {
                    "common": [],
                    "long": [],
                    "short": ["jump_event_risk"],
                },
            },
        }

    _write_json(
        isolated_account / "move_decision_morning.json",
        decision("LONG_MOVE", False),
    )
    _write_json(
        isolated_account / "dry_run" / "move_decision_morning.json",
        decision("NO_TRADE", True),
    )

    live = dashboard._move_decision_dashboard_view("morning")
    paper = dashboard._move_decision_dashboard_view(
        "morning", dry_run=True)

    assert live["action"] == "LONG_MOVE"
    assert paper["action"] == "NO_TRADE"
    assert paper["dry_run"] is True
    assert paper["forecast"]["event_score_available"] is False
    assert paper["failed_gates"]["short"] == ["jump_event_risk"]
    assert "normalized_input" not in paper
    assert "model_features" not in paper["forecast"]


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
    assert re.search(r'<select id="c-DRY_RUN"[^>]*\bdisabled\b', html)
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

    assert len(page_keys) == 65
    assert preserved == {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"}
    assert page_keys - preserved == set(dashboard.CONFIG_PAGE_DEFAULTS) - time_keys
    assert time_keys <= set(dashboard.CONFIG_PAGE_DEFAULTS)
    assert set(dashboard.CONFIG_PAGE_DEFAULTS) <= set(dashboard.CONFIG_KEYS)
    internal_move_keys = {
        "MOVE_FORECAST_LOOKBACK_DAYS",
        "MOVE_FORECAST_OUTER_SCENARIOS",
        "MOVE_FORECAST_PATHS_PER_SCENARIO",
        "MOVE_MAX_MODEL_AGE_SEC",
        "MOVE_MIN_TTE_MINUTES",
        "MOVE_MAX_TTE_HOURS",
        "MOVE_NO_ENTRY_BEFORE_SETTLEMENT_SEC",
        "MOVE_MIN_LONG_EDGE_ABS_USD",
        "MOVE_MIN_LONG_EDGE_PCT",
        "MOVE_MIN_SHORT_EDGE_ABS_USD",
        "MOVE_MIN_SHORT_EDGE_PCT",
        "MOVE_MAX_JUMP_SCORE_SHORT",
    }
    assert internal_move_keys.isdisjoint(page_keys)
    assert internal_move_keys <= set(dashboard.CONFIG_KEYS)

    defaults = dashboard.CONFIG_PAGE_DEFAULTS
    assert defaults["DRY_RUN"] == "true"
    assert defaults["MORNING_ENABLED"] == "false"
    assert defaults["EVENING_ENABLED"] == "false"
    assert defaults["MORNING_EXIT_ENABLED"] == "false"
    assert defaults["EVENING_EXIT_ENABLED"] == "false"
    assert "MORNING_SIDE" not in page_keys
    assert "EVENING_SIDE" not in page_keys
    assert defaults["MOVE_AUTO_ENTRY_MODE"] == "shadow"
    assert defaults["MOVE_DRY_RUN_CAPITAL_USD"] == "1000"
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

    with patch.object(dashboard, "_active_creds", return_value=("key", "secret")), \
            patch.object(dashboard, "_strict_exchange_positions", return_value=[]):
        payload, status = _post_config(dict(dashboard.CONFIG_PAGE_DEFAULTS))
    assert status == 200
    assert payload["ok"] is True

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


@pytest.mark.parametrize("slot", dashboard.SLOTS)
@pytest.mark.parametrize(
    "dry_namespace,current_mode,target_mode",
    [
        (False, "false", "true"),
        (True, "true", "false"),
    ],
)
def test_mode_transition_is_blocked_by_open_position_in_either_namespace(
        isolated_account, slot, dry_namespace, current_mode, target_mode):
    _write_json(isolated_account / "config.json", {
        "DRY_RUN": current_mode,
        "TREND_AUTO_ENTRY_MODE": "disabled",
    })
    state_dir = isolated_account / "dry_run" if dry_namespace else isolated_account
    _write_json(state_dir / dashboard.SLOT_STATE_FILES[slot], {
        "slot": slot,
        "status": "OPEN",
        "product_id": 101,
        "symbol": f"{slot.upper()}-OPEN",
        "lots": 2,
        "dry_run": dry_namespace,
        "execution_mode": "dry_run" if dry_namespace else "live",
    })

    with patch.object(dashboard, "_active_creds", return_value=("key", "secret")), \
            patch.object(dashboard, "_strict_exchange_positions", return_value=[]):
        with dashboard.app.test_request_context("/api/trading-mode-availability"):
            availability = dashboard._trading_mode_change_status()
        payload, response_status = _post_config({"DRY_RUN": target_mode})

    assert availability["mode_change_allowed"] is False
    assert availability["mode_selection_enabled"] is False
    assert availability["open_position_count"] >= 1
    assert availability["mode_lock_reason"]
    assert response_status == 409
    assert payload["ok"] is False
    assert "Trading Mode" in payload["error"]
    saved = json.loads(
        (isolated_account / "config.json").read_text(encoding="utf-8"))
    assert saved["DRY_RUN"] == current_mode


@pytest.mark.parametrize(
    "dry_namespace,pending_state",
    [
        (False, "ENTRY_PENDING"),
        (True, "CLOSE_PENDING"),
    ],
)
def test_mode_transition_is_blocked_by_unresolved_state(
        isolated_account, dry_namespace, pending_state):
    current_mode = "true" if dry_namespace else "false"
    target_mode = "false" if dry_namespace else "true"
    _write_json(isolated_account / "config.json", {
        "DRY_RUN": current_mode,
        "TREND_AUTO_ENTRY_MODE": "disabled",
    })
    state_dir = isolated_account / "dry_run" if dry_namespace else isolated_account
    _write_json(state_dir / dashboard.SLOT_STATE_FILES["evening"], {
        "slot": "evening",
        "status": pending_state,
        "pending_entry_client_order_id": "entry-pending-1",
        "pending_close_client_order_id": "close-pending-1",
        "dry_run": dry_namespace,
    })

    with patch.object(dashboard, "_strict_exchange_positions", return_value=[]):
        payload, response_status = _post_config({"DRY_RUN": target_mode})

    assert response_status == 409
    assert payload["ok"] is False
    assert "Trading Mode" in payload["error"]
    saved = json.loads(
        (isolated_account / "config.json").read_text(encoding="utf-8"))
    assert saved["DRY_RUN"] == current_mode


def test_mode_transition_is_blocked_by_scheduled_entry_journal(
        isolated_account):
    _write_json(isolated_account / "config.json", {
        "DRY_RUN": "false",
        "TREND_AUTO_ENTRY_MODE": "disabled",
    })
    _write_json(isolated_account / "pending_evening_entry.json", {
        "slot": "evening",
        "product_id": 123,
        "requested_lots": 4,
        "client_order_id": "scheduled-entry-pending",
        "submission_state": "submission_unknown",
    })

    with patch.object(dashboard, "_strict_exchange_positions", return_value=[]):
        with dashboard.app.test_request_context("/api/trading-mode-availability"):
            availability = dashboard._trading_mode_change_status()
        payload, response_status = _post_config({"DRY_RUN": "true"})

    assert availability["mode_change_allowed"] is False
    assert "pending" in availability["mode_lock_reason"].lower()
    assert response_status == 409
    assert payload["ok"] is False
    assert json.loads(
        (isolated_account / "config.json").read_text(
            encoding="utf-8"))["DRY_RUN"] == "false"


def test_mode_transition_is_blocked_by_unresolved_trend_order_intent(
        isolated_account):
    _write_json(isolated_account / "config.json", {
        "DRY_RUN": "false",
        "TREND_AUTO_ENTRY_MODE": "disabled",
    })
    _write_json(isolated_account / "pending_trend_order_trend-abc-1l.json", {
        "status": "PENDING",
        "client_order_id": "trend-abc-1l",
        "product_id": 123,
        "size": 4,
    })

    with patch.object(dashboard, "_strict_exchange_positions", return_value=[]):
        with dashboard.app.test_request_context("/api/trading-mode-availability"):
            availability = dashboard._trading_mode_change_status()
        payload, response_status = _post_config({"DRY_RUN": "true"})

    assert availability["mode_change_allowed"] is False
    assert any(item["source"] == "trend_order_intent"
               for item in availability["blockers"])
    assert response_status == 409
    assert payload["ok"] is False


def test_mode_transition_is_blocked_by_external_position_even_when_allowed_for_entries(
        isolated_account):
    _write_json(isolated_account / "config.json", {
        "DRY_RUN": "false",
        "TREND_AUTO_ENTRY_MODE": "disabled",
        "ALLOW_EXTERNAL_POSITIONS_WITH_BOT": "true",
    })
    exchange_position = {
        "product_id": 888,
        "product_symbol": "BTCUSD",
        "size": "1",
    }

    with patch.object(dashboard, "_active_creds", return_value=("key", "secret")), \
            patch.object(
            dashboard, "_strict_exchange_positions",
            return_value=[exchange_position]):
        with dashboard.app.test_request_context("/api/trading-mode-availability"):
            availability = dashboard._trading_mode_change_status()
        payload, response_status = _post_config({"DRY_RUN": "true"})

    assert availability["verification_ok"] is True
    assert availability["mode_change_allowed"] is False
    assert availability["open_position_count"] >= 1
    assert response_status == 409
    assert payload["ok"] is False
    assert json.loads(
        (isolated_account / "config.json").read_text(
            encoding="utf-8"))["DRY_RUN"] == "false"


@pytest.mark.parametrize("size", ["0.5", "-0.25"])
def test_fractional_exchange_position_also_locks_mode(
        isolated_account, size):
    _write_json(isolated_account / "config.json", {
        "DRY_RUN": "false",
        "TREND_AUTO_ENTRY_MODE": "disabled",
    })
    exchange_response = Mock()
    exchange_response.json.return_value = {
        "success": True,
        "result": [{
            "product_id": 999,
            "product_symbol": "BTCUSD",
            "size": size,
        }],
    }

    with patch.object(dashboard, "_active_creds", return_value=("key", "secret")), \
            patch.object(dashboard, "_sign", return_value={}), \
            patch.object(dashboard.req, "get", return_value=exchange_response):
        with dashboard.app.test_request_context("/api/trading-mode-availability"):
            availability = dashboard._trading_mode_change_status()

    assert availability["verification_ok"] is True
    assert availability["mode_change_allowed"] is False
    assert availability["mode_selection_enabled"] is False
    assert availability["open_position_count"] >= 1


def test_exchange_verification_failure_locks_mode_and_rejects_transition(
        isolated_account):
    _write_json(isolated_account / "config.json", {
        "DRY_RUN": "false",
        "TREND_AUTO_ENTRY_MODE": "disabled",
    })

    with patch.object(dashboard, "_active_creds", return_value=("key", "secret")), \
            patch.object(
            dashboard, "_strict_exchange_positions",
            side_effect=RuntimeError("exchange positions unavailable")):
        with dashboard.app.test_request_context("/api/trading-mode-availability"):
            availability = dashboard._trading_mode_change_status()
        payload, response_status = _post_config({"DRY_RUN": "true"})

    assert availability["verification_ok"] is False
    assert availability["mode_change_allowed"] is False
    assert availability["mode_selection_enabled"] is False
    assert availability["mode_lock_reason"]
    assert response_status == 409
    assert payload["ok"] is False
    assert json.loads(
        (isolated_account / "config.json").read_text(
            encoding="utf-8"))["DRY_RUN"] == "false"


def test_missing_credentials_fail_closed_before_exchange_mode_verification(
        isolated_account):
    _write_json(isolated_account / "config.json", {
        "DRY_RUN": "true",
        "TREND_AUTO_ENTRY_MODE": "disabled",
    })
    positions = Mock(side_effect=AssertionError(
        "exchange verification must not run without account credentials"))

    with patch.object(dashboard, "_active_creds", return_value=("", "")), \
            patch.object(dashboard, "_strict_exchange_positions", positions):
        with dashboard.app.test_request_context("/api/trading-mode-availability"):
            availability = dashboard._trading_mode_change_status()
        payload, response_status = _post_config({"DRY_RUN": "false"})

    assert availability["verification_ok"] is False
    assert availability["mode_change_allowed"] is False
    assert availability["mode_selection_enabled"] is False
    assert availability["mode_lock_reason"]
    assert response_status == 409
    assert payload["ok"] is False
    positions.assert_not_called()
    assert json.loads(
        (isolated_account / "config.json").read_text(
            encoding="utf-8"))["DRY_RUN"] == "true"


def test_corrupt_position_state_fails_closed_for_mode_transition(
        isolated_account):
    _write_json(isolated_account / "config.json", {
        "DRY_RUN": "false",
        "TREND_AUTO_ENTRY_MODE": "disabled",
    })
    state_path = isolated_account / dashboard.SLOT_STATE_FILES["trend"]
    state_path.write_text("{not-json", encoding="utf-8")
    state_path.with_suffix(state_path.suffix + ".bak").write_text(
        "{also-not-json", encoding="utf-8")

    with patch.object(dashboard, "_active_creds", return_value=("key", "secret")), \
            patch.object(dashboard, "_strict_exchange_positions", return_value=[]):
        with dashboard.app.test_request_context("/api/trading-mode-availability"):
            availability = dashboard._trading_mode_change_status()
        payload, response_status = _post_config({"DRY_RUN": "true"})

    assert availability["verification_ok"] is False
    assert availability["mode_change_allowed"] is False
    assert availability["mode_selection_enabled"] is False
    assert response_status == 409
    assert payload["ok"] is False
    assert json.loads(
        (isolated_account / "config.json").read_text(
            encoding="utf-8"))["DRY_RUN"] == "false"


@pytest.mark.parametrize(
    "current_mode,dry_namespace",
    [
        ("false", False),
        ("true", True),
    ],
)
def test_unchanged_mode_full_form_save_remains_allowed_with_open_positions(
        isolated_account, current_mode, dry_namespace):
    _write_json(isolated_account / "config.json", {
        "DRY_RUN": current_mode,
        "TREND_AUTO_ENTRY_MODE": "disabled",
        "STRIKE_STEP": "100",
    })
    state_dir = isolated_account / "dry_run" if dry_namespace else isolated_account
    _write_json(state_dir / dashboard.SLOT_STATE_FILES["morning"], {
        "slot": "morning",
        "status": "OPEN",
        "product_id": 101,
        "lots": 1,
        "dry_run": dry_namespace,
    })

    with patch.object(dashboard, "_strict_exchange_positions", return_value=[{
        "product_id": 777,
        "product_symbol": "EXTERNAL",
        "size": "1",
    }]):
        payload, response_status = _post_config({
            "DRY_RUN": current_mode,
            "STRIKE_STEP": "250",
        })

    assert response_status == 200
    assert payload["ok"] is True
    saved = json.loads(
        (isolated_account / "config.json").read_text(encoding="utf-8"))
    assert saved["DRY_RUN"] == current_mode
    assert saved["STRIKE_STEP"] == "250"


@pytest.mark.parametrize(
    "current_mode,target_mode",
    [
        ("false", "true"),
        ("true", "false"),
    ],
)
def test_mode_transition_is_allowed_only_after_verified_flat(
        isolated_account, current_mode, target_mode):
    _write_json(isolated_account / "config.json", {
        "DRY_RUN": current_mode,
        "TREND_AUTO_ENTRY_MODE": "disabled",
    })
    for dry_namespace in (False, True):
        state_dir = (
            isolated_account / "dry_run"
            if dry_namespace else isolated_account
        )
        _write_json(state_dir / dashboard.SLOT_STATE_FILES["evening"], {
            "slot": "evening",
            "status": "CLOSED",
            "dry_run": dry_namespace,
        })

    with patch.object(dashboard, "_active_creds", return_value=("key", "secret")), \
            patch.object(dashboard, "_strict_exchange_positions", return_value=[]):
        with dashboard.app.test_request_context("/api/trading-mode-availability"):
            availability = dashboard._trading_mode_change_status()
        payload, response_status = _post_config({"DRY_RUN": target_mode})

    assert availability["verification_ok"] is True
    assert availability["mode_change_allowed"] is True
    assert availability["mode_selection_enabled"] is True
    assert availability["open_position_count"] == 0
    assert response_status == 200
    assert payload["ok"] is True
    assert json.loads(
        (isolated_account / "config.json").read_text(
            encoding="utf-8"))["DRY_RUN"] == target_mode


def test_mode_availability_endpoint_publishes_stable_ui_contract(
        isolated_account):
    _write_json(isolated_account / "config.json", {
        "DRY_RUN": "false",
        "TREND_AUTO_ENTRY_MODE": "disabled",
    })

    with patch.object(dashboard, "_active_creds", return_value=("key", "secret")), \
            patch.object(dashboard, "_strict_exchange_positions", return_value=[]):
        payload, response_status = _mode_availability_endpoint()

    assert response_status == 200
    assert {
        "mode_change_allowed",
        "mode_selection_enabled",
        "mode_lock_reason",
        "verification_ok",
        "open_position_count",
    } <= set(payload)
    assert payload["mode_change_allowed"] is True
    assert payload["mode_selection_enabled"] is True
    assert payload["verification_ok"] is True
    assert payload["open_position_count"] == 0


def test_concurrent_partial_save_cannot_revert_verified_mode_change(
        isolated_account):
    _write_json(isolated_account / "config.json", {
        "DRY_RUN": "false",
        "TREND_AUTO_ENTRY_MODE": "disabled",
        "STRIKE_STEP": "100",
    })
    first_write_ready = threading.Event()
    release_first_write = threading.Event()
    mode_save_done = threading.Event()
    results = {}
    real_atomic_write = dashboard._atomic_write_json
    first_config_write = {"pending": True}

    def paused_first_config_write(path, value):
        if Path(path) == isolated_account / "config.json" \
                and first_config_write["pending"]:
            first_config_write["pending"] = False
            first_write_ready.set()
            assert release_first_write.wait(2), "test did not release config writer"
        return real_atomic_write(path, value)

    def save_partial():
        results["partial"] = _post_config({"STRIKE_STEP": "250"})

    def save_mode():
        try:
            results["mode"] = _post_config({"DRY_RUN": "true"})
        finally:
            mode_save_done.set()

    with patch.object(
            dashboard, "_atomic_write_json",
            side_effect=paused_first_config_write), \
            patch.object(
            dashboard, "_active_creds",
            return_value=("key", "secret")), \
            patch.object(
            dashboard, "_strict_exchange_positions",
            return_value=[]):
        partial_thread = threading.Thread(target=save_partial)
        partial_thread.start()
        assert first_write_ready.wait(2), "partial save did not reach its write"

        mode_thread = threading.Thread(target=save_mode)
        mode_thread.start()
        assert not mode_save_done.wait(0.2), (
            "mode save bypassed the in-progress config read/modify/write lock")
        release_first_write.set()
        partial_thread.join(3)
        mode_thread.join(3)

    assert not partial_thread.is_alive()
    assert not mode_thread.is_alive()
    assert results["partial"][1] == 200
    assert results["mode"][1] == 200
    saved = json.loads(
        (isolated_account / "config.json").read_text(encoding="utf-8"))
    assert saved["STRIKE_STEP"] == "250"
    assert saved["DRY_RUN"] == "true"


def test_config_template_refreshes_mode_lock_and_preserves_locked_mode_on_save_reset():
    html = (Path(dashboard.BASE) / "templates" / "config.html").read_text(
        encoding="utf-8")

    assert 'id="mode-lock-hint"' in html
    assert "/api/trading-mode-availability" in html
    assert "function applyModeSelectionStatus(payload)" in html
    assert "function refreshModeAvailability(force = false)" in html
    assert "let loadedDryRun" in html
    assert "let modeChangeAllowed" in html
    assert "let lastModeAvailability" in html
    assert html.count("refreshModeAvailability(") >= 2
    assert re.search(
        r"k\s*===\s*['\"]DRY_RUN['\"]\s*&&\s*!modeChangeAllowed",
        html,
    )
    assert "el.value = loadedDryRun" in html
    assert "modeChangeAllowed && el.value !== loadedDryRun" in html


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
        if name == "config":
            yield True
            return
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
