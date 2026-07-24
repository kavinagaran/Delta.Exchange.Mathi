from __future__ import annotations

import copy
import json
import threading
from unittest.mock import Mock

import pytest

import dashboard


def _write(path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _live_config() -> dict:
    return {
        "DRY_RUN": "false",
        "TREND_ENGINE_SCORE_AUTO_MODE": "live",
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


def _signal(mode, *, key, score, zone):
    return {
        "mode": dict(mode),
        "snapshot": {"market": {"spot": 65_800}},
        "decision": {
            "decision_id": f"decision-{key}",
            "model_version": "trend-live-test",
            "schema_version": "1.0",
        },
        "score": score,
        "zone": zone,
        "signal_key": key,
        "signal_bar_close_utc": "2026-07-23T10:05:00Z",
        "market_regime": "TRENDING",
    }


def _pending_state(*, submission_state="prepared"):
    post_boundary = submission_state != "prepared"
    return {
        "slot": "trend",
        "status": "ENTRY_PENDING",
        "dry_run": False,
        "execution_mode": "live",
        "ownership": dashboard.TREND_SCORE_AUTO_LIVE_OWNERSHIP,
        "entry_trigger": dashboard.TREND_SCORE_AUTO_TRIGGER,
        "transition_id": "trend-score-old-transition",
        "position_cycle_id": "trend-score-old-transition",
        "trend_score_zone": dashboard.TREND_SCORE_CE_ZONE,
        "engine_zone": dashboard.TREND_SCORE_CE_ZONE,
        "direction_score_at_entry": 60.0,
        "score_auto_signal_key": "score-bar-old",
        "signal_bar_close_utc": "2026-07-23T10:00:00Z",
        "market_regime_at_entry": "TRENDING",
        "btc_at_entry": 65_800,
        "entry_decision_snapshot": {
            "decision_id": "decision-old",
            "model_version": "trend-live-test",
            "schema_version": "1.0",
        },
        "symbol": "C-BTC-65400-230726",
        "product_id": 1001,
        "pending_entry_client_order_id": "trend-alice-old-entry",
        "pending_entry_order_id": 91 if submission_state == "acknowledged" else None,
        "pending_entry_requested_lots": 1000,
        "pending_entry_side": "buy",
        "pending_entry_submission_state": submission_state,
        "pending_entry_post_boundary": post_boundary,
        "pending_entry_attempts": 1 if post_boundary else 0,
        "pending_entry_last_attempt_at_utc": (
            "2026-07-23T10:00:01Z" if post_boundary else None
        ),
        "pending_entry_payload": {
            "product_id": 1001,
            "size": 1000,
            "side": "buy",
            "order_type": "limit_order",
            "time_in_force": "ioc",
            "client_order_id": "trend-alice-old-entry",
        },
        "selected_contract_snapshot": {
            "zone": dashboard.TREND_SCORE_CE_ZONE,
            "symbol": "C-BTC-65400-230726",
        },
        "execution_snapshot": {
            "order_submitted": post_boundary,
            "exchange_api_called": post_boundary,
        },
    }


@pytest.fixture
def live_recovery_account(tmp_path, monkeypatch):
    users = tmp_path / "users"
    account = users / "alice"
    account.mkdir(parents=True)
    _write(account / "config.json", _live_config())
    monkeypatch.setattr(dashboard, "USERS_DIR", users)
    monkeypatch.setattr(dashboard, "_active_user", lambda: "alice")
    monkeypatch.setattr(
        dashboard, "_active_creds", lambda: ("api-key", "api-secret"),
    )
    dashboard._trend_score_auto_cycle_locks.clear()
    dashboard._trend_score_auto_health.clear()
    return account


def test_new_bar_cancels_only_proven_pre_post_pending_intent(
        live_recovery_account, monkeypatch):
    pending = _pending_state()
    _write(live_recovery_account / "trend_state.json", pending)
    mode = dashboard._trading_mode_payload()
    current = _signal(
        mode,
        key="score-bar-new",
        score=-60,
        zone=dashboard.TREND_SCORE_PE_ZONE,
    )
    execute = Mock(side_effect=AssertionError(
        "stale pre-POST intent reached exchange execution"
    ))
    audit = Mock()
    monkeypatch.setattr(
        dashboard,
        "_collect_trend_score_auto_signal",
        lambda: copy.deepcopy(current),
    )
    monkeypatch.setattr(dashboard, "_trend_score_auto_live_execute", execute)
    monkeypatch.setattr(dashboard, "_trend_audit", audit)

    changed = dashboard._maybe_auto_trend_score_live_cycle(
        "alice", "2026-07-23T10:05:01Z",
    )

    assert changed is True
    execute.assert_not_called()
    state = json.loads(
        (live_recovery_account / "trend_state.json").read_text(
            encoding="utf-8"
        )
    )
    assert state["status"] == "IDLE"
    assert state["last_entry_outcome"] == "STALE_PRE_POST_CANCELLED"
    assert state["last_entry_signal_key"] == "score-bar-old"
    assert state["last_entry_order_submitted"] is False
    assert not state.get("pending_entry_client_order_id")
    ledger = json.loads(
        (live_recovery_account / dashboard.TREND_SCORE_AUTO_LEDGER_FILE)
        .read_text(encoding="utf-8")
    )
    assert ledger["signals"]["score-bar-old"]["action"] \
        == "STALE_PRE_POST_CANCELLED"
    assert "score-bar-new" not in ledger["signals"]
    assert ledger["current_transition"]["phase"] == "COMPLETE"
    assert (
        ledger["current_transition"]["superseded_by_signal_key"]
        == "score-bar-new"
    )
    assert audit.call_args.args[0] \
        == "trend_score_auto_live_stale_pre_post_cancelled"
    assert audit.call_args.args[1]["exchange_api_called"] is False


def test_same_bar_discards_pre_post_intent_without_consuming_fresh_signal(
        live_recovery_account, monkeypatch):
    pending = _pending_state()
    _write(live_recovery_account / "trend_state.json", pending)
    mode = dashboard._trading_mode_payload()
    current = _signal(
        mode,
        key="score-bar-old",
        score=-60,
        zone=dashboard.TREND_SCORE_PE_ZONE,
    )
    execute = Mock(side_effect=AssertionError(
        "stale same-bar pre-POST intent reached exchange execution"
    ))
    audit = Mock()
    monkeypatch.setattr(
        dashboard,
        "_collect_trend_score_auto_signal",
        lambda: copy.deepcopy(current),
    )
    monkeypatch.setattr(dashboard, "_trend_score_auto_live_execute", execute)
    monkeypatch.setattr(dashboard, "_trend_audit", audit)

    changed = dashboard._maybe_auto_trend_score_live_cycle(
        "alice", "2026-07-23T10:05:01Z",
    )

    assert changed is True
    execute.assert_not_called()
    state = json.loads(
        (live_recovery_account / "trend_state.json").read_text(
            encoding="utf-8"
        )
    )
    assert state["status"] == "IDLE"
    assert state["last_entry_outcome"] == "STALE_PRE_POST_REBUILD"
    assert "last_entry_signal_key" not in state
    assert state["last_entry_order_submitted"] is False
    assert not state.get("pending_entry_client_order_id")
    ledger = json.loads(
        (live_recovery_account / dashboard.TREND_SCORE_AUTO_LEDGER_FILE)
        .read_text(encoding="utf-8")
    )
    assert "score-bar-old" not in ledger["signals"]
    assert ledger["current_transition"]["phase"] == "REBUILD_REQUIRED"
    assert ledger["current_transition"]["action"] \
        == "STALE_PRE_POST_REBUILD"
    assert ledger["current_transition"]["rebuild_signal_key"] \
        == "score-bar-old"
    assert ledger["current_transition"]["previous_target_zone"] \
        == dashboard.TREND_SCORE_CE_ZONE
    assert ledger["current_transition"]["fresh_target_zone"] \
        == dashboard.TREND_SCORE_PE_ZONE
    assert audit.call_args.args[0] \
        == "trend_score_auto_live_stale_pre_post_rebuild"
    assert audit.call_args.args[1]["exchange_api_called"] is False
    assert dashboard._trend_score_auto_health["alice"]["status"] \
        == "stale_pre_post_rebuild"

    fresh_prepared = {
        "zone": dashboard.TREND_SCORE_PE_ZONE,
        "symbol": "P-BTC-66000-230726",
    }
    prepare = Mock(return_value=copy.deepcopy(fresh_prepared))
    monkeypatch.setattr(
        dashboard, "_prepare_trend_score_auto_entry", prepare,
    )

    def fresh_execute(**kwargs):
        return {
            "ok": False,
            "status": "NO_FILL",
            "state": {
                "slot": "trend",
                "status": "IDLE",
                "dry_run": False,
                "execution_mode": "live",
                "last_entry_signal_key": kwargs["signal"]["signal_key"],
                "last_entry_transition_id": kwargs["transition_id"],
            },
            "order_submitted": True,
            "consume_signal": True,
            "handled_signal_key": kwargs["signal"]["signal_key"],
            "handled_transition_id": kwargs["transition_id"],
            "filled_lots": 0,
        }

    execute.side_effect = fresh_execute
    changed = dashboard._maybe_auto_trend_score_live_cycle(
        "alice", "2026-07-23T10:05:02Z",
    )

    assert changed is True
    prepare.assert_called_once()
    assert prepare.call_args.args[0]["zone"] == dashboard.TREND_SCORE_PE_ZONE
    execute.assert_called_once()
    assert execute.call_args.kwargs["signal"]["score"] == -60
    assert execute.call_args.kwargs["prepared"] == fresh_prepared
    ledger = json.loads(
        (live_recovery_account / dashboard.TREND_SCORE_AUTO_LEDGER_FILE)
        .read_text(encoding="utf-8")
    )
    assert ledger["signals"]["score-bar-old"]["action"] == "NO_FILL"


@pytest.mark.parametrize("submission_state", ("submitting", "acknowledged"))
def test_new_bar_never_cancels_post_boundary_pending_identity(
        submission_state, live_recovery_account, monkeypatch):
    pending = _pending_state(submission_state=submission_state)
    _write(live_recovery_account / "trend_state.json", pending)
    mode = dashboard._trading_mode_payload()
    current = _signal(
        mode,
        key="score-bar-new",
        score=-60,
        zone=dashboard.TREND_SCORE_PE_ZONE,
    )
    execute = Mock(return_value={
        "ok": False,
        "status": "ENTRY_PENDING",
        "state": copy.deepcopy(pending),
        "order_submitted": False,
        "consume_signal": False,
        "error": "exact recovery remains pending",
    })
    monkeypatch.setattr(
        dashboard,
        "_collect_trend_score_auto_signal",
        lambda: copy.deepcopy(current),
    )
    monkeypatch.setattr(dashboard, "_trend_score_auto_live_execute", execute)
    monkeypatch.setattr(dashboard, "_trend_audit", Mock())

    changed = dashboard._maybe_auto_trend_score_live_cycle(
        "alice", "2026-07-23T10:05:01Z",
    )

    assert changed is True
    execute.assert_called_once()
    assert execute.call_args.kwargs["signal"]["signal_key"] == "score-bar-old"
    assert execute.call_args.kwargs["existing_state"][
        "pending_entry_submission_state"
    ] == submission_state
    state = json.loads(
        (live_recovery_account / "trend_state.json").read_text(
            encoding="utf-8"
        )
    )
    assert state["status"] == "ENTRY_PENDING"
    assert state["pending_entry_submission_state"] == submission_state
    ledger = json.loads(
        (live_recovery_account / dashboard.TREND_SCORE_AUTO_LEDGER_FILE)
        .read_text(encoding="utf-8")
    )
    assert "score-bar-old" not in ledger["signals"]
    assert "score-bar-new" not in ledger["signals"]


@pytest.mark.parametrize("controller_mode", ("disabled", "invalid"))
@pytest.mark.parametrize("submission_state", ("submitting", "acknowledged"))
def test_recovery_only_lane_reconciles_post_boundary_when_mode_is_not_active(
        controller_mode, submission_state, live_recovery_account, monkeypatch):
    config = _live_config()
    config["TREND_ENGINE_SCORE_AUTO_MODE"] = controller_mode
    _write(live_recovery_account / "config.json", config)
    pending = _pending_state(submission_state=submission_state)
    _write(live_recovery_account / "trend_state.json", pending)
    execute = Mock(return_value={
        "ok": False,
        "status": "ENTRY_PENDING",
        "state": copy.deepcopy(pending),
        "order_submitted": False,
        "consume_signal": False,
        "error": "exact recovery remains pending",
    })
    # Exercise the dashboard adapter as well as the recovery supervisor.  In
    # particular, an invalid score-mode field must not make post-boundary
    # recovery read the normal score-controller configuration.
    monkeypatch.setattr(
        dashboard,
        "execute_or_recover_trend_score_live_entry",
        execute,
    )
    monkeypatch.setattr(
        dashboard,
        "_collect_trend_score_auto_signal",
        Mock(side_effect=AssertionError(
            "recovery-only lane collected a new market signal"
        )),
    )
    monkeypatch.setattr(dashboard, "_trend_audit", Mock())

    claimed = dashboard._maybe_recover_trend_score_live_pending(
        "alice", "2026-07-23T10:05:01Z",
    )

    assert claimed is True
    execute.assert_called_once()
    assert execute.call_args.kwargs["prepared"] is None
    assert execute.call_args.kwargs["signal"]["signal_key"] == "score-bar-old"
    assert execute.call_args.kwargs["signal"]["zone"] \
        == dashboard.TREND_SCORE_CE_ZONE
    assert execute.call_args.kwargs["transition_id"] \
        == "trend-score-old-transition"
    assert execute.call_args.kwargs["existing_state"][
        "pending_entry_submission_state"
    ] == submission_state
    state = json.loads(
        (live_recovery_account / "trend_state.json").read_text(
            encoding="utf-8"
        )
    )
    assert state["status"] == "ENTRY_PENDING"
    assert state["pending_entry_submission_state"] == submission_state


@pytest.mark.parametrize(
    "boundary_evidence",
    (
        {"pending_entry_attempts": 1},
        {"pending_entry_post_boundary": True},
        {
            "pending_entry_post_boundary": None,
            "pending_entry_attempts": 1,
        },
    ),
)
def test_contradictory_prepared_entry_routes_to_lookup_only_recovery(
        boundary_evidence, live_recovery_account, monkeypatch):
    config = _live_config()
    config["TREND_ENGINE_SCORE_AUTO_MODE"] = "disabled"
    _write(live_recovery_account / "config.json", config)
    pending = {**_pending_state(submission_state="prepared"), **boundary_evidence}
    _write(live_recovery_account / "trend_state.json", pending)
    execute = Mock(return_value={
        "ok": False,
        "status": "ENTRY_PENDING",
        "state": copy.deepcopy(pending),
        "order_submitted": False,
        "consume_signal": False,
        "error": "exact recovery remains pending",
    })
    monkeypatch.setattr(
        dashboard,
        "execute_or_recover_trend_score_live_entry",
        execute,
    )
    monkeypatch.setattr(
        dashboard,
        "_collect_trend_score_auto_signal",
        Mock(side_effect=AssertionError(
            "recovery-only lane collected a new market signal"
        )),
    )
    monkeypatch.setattr(dashboard, "_trend_audit", Mock())

    claimed = dashboard._maybe_recover_trend_score_live_pending(
        "alice", "2026-07-23T10:05:01Z",
    )

    assert claimed is True
    execute.assert_called_once()
    execution_args = execute.call_args.kwargs
    assert execution_args["prepared"] is None
    assert execution_args["fresh_quote"] is None
    assert execution_args["max_slippage_pct"] == 0
    assert execution_args["max_spread_pct"] == 0
    state = json.loads(
        (live_recovery_account / "trend_state.json").read_text(
            encoding="utf-8"
        )
    )
    assert state["status"] == "ENTRY_PENDING"
    assert state["pending_entry_submission_state"] == "prepared"
    assert state.get("last_entry_outcome") != "STALE_PRE_POST_REBUILD"


def test_recovery_only_lane_discards_pre_post_without_submitting(
        live_recovery_account, monkeypatch):
    config = _live_config()
    config["TREND_ENGINE_SCORE_AUTO_MODE"] = "disabled"
    _write(live_recovery_account / "config.json", config)
    pending = _pending_state(submission_state="prepared")
    _write(live_recovery_account / "trend_state.json", pending)
    execute = Mock(side_effect=AssertionError(
        "recovery-only lane submitted a prepared intent"
    ))
    monkeypatch.setattr(dashboard, "_trend_score_auto_live_execute", execute)
    monkeypatch.setattr(dashboard, "_trend_audit", Mock())

    claimed = dashboard._maybe_recover_trend_score_live_pending(
        "alice", "2026-07-23T10:05:01Z",
    )

    assert claimed is True
    execute.assert_not_called()
    state = json.loads(
        (live_recovery_account / "trend_state.json").read_text(
            encoding="utf-8"
        )
    )
    assert state["status"] == "IDLE"
    assert state["last_entry_outcome"] == "STALE_PRE_POST_REBUILD"
    assert state["last_entry_order_submitted"] is False
    assert "last_entry_signal_key" not in state
    ledger = json.loads(
        (live_recovery_account / dashboard.TREND_SCORE_AUTO_LEDGER_FILE)
        .read_text(encoding="utf-8")
    )
    assert "score-bar-old" not in ledger["signals"]


@pytest.mark.parametrize(
    ("invalid_key", "invalid_value", "expected_error"),
    (
        (
            "DRY_RUN",
            "unknown",
            "Account Trading Mode is invalid",
        ),
        (
            "TREND_AUTO_ENTRY_MODE",
            "unknown",
            "Account Trend auto-entry mode is invalid",
        ),
    ),
)
def test_recovery_only_lane_tolerates_only_invalid_score_mode(
        invalid_key, invalid_value, expected_error,
        live_recovery_account, monkeypatch):
    config = _live_config()
    config["TREND_ENGINE_SCORE_AUTO_MODE"] = "invalid"
    config[invalid_key] = invalid_value
    _write(live_recovery_account / "config.json", config)
    pending = _pending_state(submission_state="submitting")
    _write(live_recovery_account / "trend_state.json", pending)
    execute = Mock()
    monkeypatch.setattr(dashboard, "_trend_score_auto_live_execute", execute)
    monkeypatch.setattr(dashboard, "_trend_audit", Mock())

    claimed = dashboard._maybe_recover_trend_score_live_pending(
        "alice", "2026-07-23T10:05:01Z",
    )

    assert claimed is True
    execute.assert_not_called()
    assert expected_error in dashboard._trend_score_auto_health[
        "alice"
    ]["last_error"]
    state = json.loads(
        (live_recovery_account / "trend_state.json").read_text(
            encoding="utf-8"
        )
    )
    assert state["status"] == "ENTRY_PENDING"
    assert state["pending_entry_submission_state"] == "submitting"


@pytest.mark.parametrize("submission_state", ("submitting", "acknowledged"))
def test_config_api_blocks_score_mode_change_while_live_entry_is_pending(
        submission_state, live_recovery_account):
    pending = _pending_state(submission_state=submission_state)
    _write(live_recovery_account / "trend_state.json", pending)

    with dashboard.app.test_request_context(
        "/api/config",
        method="POST",
        json={"TREND_ENGINE_SCORE_AUTO_MODE": "disabled"},
    ):
        response = dashboard.set_config()
    body, status = response if isinstance(response, tuple) else (
        response, response.status_code,
    )

    assert status == 400
    payload = body.get_json()
    assert payload["ok"] is False
    assert "pending exact exchange recovery" in payload["error"]
    saved = json.loads(
        (live_recovery_account / "config.json").read_text(encoding="utf-8")
    )
    assert saved["TREND_ENGINE_SCORE_AUTO_MODE"] == "live"


def test_legacy_trend_enabled_alias_is_normalized_before_score_validation(
        live_recovery_account):
    config = _live_config()
    config["TREND_ENGINE_SCORE_AUTO_MODE"] = "disabled"
    _write(live_recovery_account / "config.json", config)

    with dashboard.app.test_request_context(
        "/api/config",
        method="POST",
        json={
            "TREND_ENGINE_SCORE_AUTO_MODE": "live",
            "TREND_AUTO_ENTRY_ENABLED": True,
        },
    ):
        response = dashboard.set_config()
    body, status = response if isinstance(response, tuple) else (
        response, response.status_code,
    )

    assert status == 400
    payload = body.get_json()
    assert payload["ok"] is False
    assert "legacy Trend auto-entry mode" in payload["error"]
    saved = json.loads(
        (live_recovery_account / "config.json").read_text(
            encoding="utf-8"
        )
    )
    assert saved["TREND_ENGINE_SCORE_AUTO_MODE"] == "disabled"
    assert saved["TREND_AUTO_ENTRY_MODE"] == "disabled"


def test_legacy_trend_enabled_alias_change_uses_account_entry_lock(
        live_recovery_account):
    with dashboard.account_entry_lock(
        live_recovery_account, "test-entry-holder",
    ) as acquired:
        assert acquired is True
        with dashboard.app.test_request_context(
            "/api/config",
            method="POST",
            json={"TREND_AUTO_ENTRY_ENABLED": True},
        ):
            response = dashboard.set_config()
    body, status = response if isinstance(response, tuple) else (
        response, response.status_code,
    )

    assert status == 409
    payload = body.get_json()
    assert payload["ok"] is False
    assert payload["config_saved"] is False
    assert "Strategy entry mode cannot change" in payload["error"]
    saved = json.loads(
        (live_recovery_account / "config.json").read_text(
            encoding="utf-8"
        )
    )
    assert saved["TREND_AUTO_ENTRY_MODE"] == "disabled"
    assert saved["TREND_ENGINE_SCORE_AUTO_MODE"] == "live"


def test_score_mode_change_cannot_race_live_pending_recovery(
        live_recovery_account, monkeypatch):
    pending = _pending_state(submission_state="submitting")
    _write(live_recovery_account / "trend_state.json", pending)
    recovery_entered = threading.Event()
    release_recovery = threading.Event()
    recovery_result = {}

    def execute(**_kwargs):
        recovery_entered.set()
        assert release_recovery.wait(3), "test did not release recovery"
        return {
            "ok": False,
            "status": "ENTRY_PENDING",
            "state": copy.deepcopy(pending),
            "order_submitted": False,
            "consume_signal": False,
            "error": "exact recovery remains pending",
        }

    def recover():
        recovery_result["claimed"] = (
            dashboard._maybe_recover_trend_score_live_pending(
                "alice", "2026-07-23T10:05:01Z",
            )
        )

    monkeypatch.setattr(
        dashboard, "_trend_score_auto_live_execute", execute,
    )
    monkeypatch.setattr(dashboard, "_trend_audit", Mock())
    worker = threading.Thread(target=recover)
    worker.start()
    assert recovery_entered.wait(2), "recovery did not acquire the entry lock"
    try:
        with dashboard.app.test_request_context(
            "/api/config",
            method="POST",
            json={"TREND_ENGINE_SCORE_AUTO_MODE": "disabled"},
        ):
            response = dashboard.set_config()
        body, status = response if isinstance(response, tuple) else (
            response, response.status_code,
        )
        assert status == 409
        payload = body.get_json()
        assert payload["ok"] is False
        assert payload["config_saved"] is False
        assert "score mode cannot change" in payload["error"]
        saved = json.loads(
            (live_recovery_account / "config.json").read_text(
                encoding="utf-8"
            )
        )
        assert saved["TREND_ENGINE_SCORE_AUTO_MODE"] == "live"
    finally:
        release_recovery.set()
        worker.join(3)

    assert not worker.is_alive()
    assert recovery_result["claimed"] is True
