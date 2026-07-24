from __future__ import annotations

import json

import pytest

import dashboard


def _score_config(mode: str, *, dry_run: str) -> dict:
    return {
        "DRY_RUN": dry_run,
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


@pytest.mark.parametrize(
    ("mode", "dry_run"),
    (("dry_run", "true"), ("live", "false")),
)
def test_score_auto_accepts_explicit_mode_matching_trading_mode(
        mode, dry_run):
    config = _score_config(mode, dry_run=dry_run)

    assert dashboard._trend_score_auto_config_error(config) is None
    assert dashboard._validate_config_update(config, config) is None


@pytest.mark.parametrize(
    ("mode", "dry_run", "message"),
    (
        ("dry_run", "false", "DRY RUN only"),
        ("live", "true", "requires LIVE Trading Mode"),
    ),
)
def test_score_auto_rejects_controller_and_trading_mode_mismatch(
        mode, dry_run, message):
    config = _score_config(mode, dry_run=dry_run)

    assert message in dashboard._trend_score_auto_config_error(config)
    assert message in dashboard._validate_config_update(config, config)


def test_saved_account_config_accepts_explicit_live_score_mode(
        tmp_path, monkeypatch):
    account = tmp_path / "users" / "alice"
    account.mkdir(parents=True)
    (account / "config.json").write_text(
        json.dumps(_score_config("live", dry_run="false")),
        encoding="utf-8",
    )
    monkeypatch.setattr(dashboard, "USERS_DIR", tmp_path / "users")
    monkeypatch.setattr(dashboard, "_active_user", lambda: "alice")

    saved, exists = dashboard._saved_user_cfg()

    assert exists is True
    assert saved["TREND_ENGINE_SCORE_AUTO_MODE"] == "live"
    assert dashboard._user_cfg()["TREND_ENGINE_SCORE_AUTO_MODE"] == "live"


def test_score_auto_ownership_is_mode_specific_and_live_state_is_trend_owned():
    assert (
        dashboard._trend_score_auto_ownership("dry_run")
        == dashboard.TREND_SCORE_AUTO_OWNERSHIP
    )
    assert (
        dashboard._trend_score_auto_ownership("live")
        == dashboard.TREND_SCORE_AUTO_LIVE_OWNERSHIP
    )
    with pytest.raises(RuntimeError, match="no ownership identity"):
        dashboard._trend_score_auto_ownership("disabled")

    assert dashboard._is_owned_trend_state({
        "status": "OPEN",
        "ownership": dashboard.TREND_SCORE_AUTO_LIVE_OWNERSHIP,
        "entry_trigger": dashboard.TREND_SCORE_AUTO_TRIGGER,
    })
    assert not dashboard._is_owned_trend_state({
        "status": "OPEN",
        "entry_trigger": dashboard.TREND_SCORE_AUTO_TRIGGER,
    })
    assert not dashboard._is_owned_trend_state({
        "status": "OPEN",
        "ownership": dashboard.TREND_SCORE_AUTO_LIVE_OWNERSHIP,
    })
