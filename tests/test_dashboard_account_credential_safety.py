import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from flask import g

import dashboard
from risk_controls import account_entry_lock


def _write_account(users: Path, username: str) -> None:
    directory = users / username
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "account.json").write_text(json.dumps({
        "username": username,
        "display_name": username.title(),
        "pw_hash": "test-only",
        "api_key": f"key-{username}",
        "api_secret": f"secret-{username}",
    }), encoding="utf-8")


@pytest.fixture
def isolated_accounts(tmp_path, monkeypatch):
    users = tmp_path / "users"
    _write_account(users, "admin")
    _write_account(users, "alice")
    monkeypatch.setattr(dashboard, "USERS_DIR", users)
    monkeypatch.setattr(dashboard, "DASH_USER", "admin")
    monkeypatch.setattr(dashboard, "BOT_USER", "bot")
    monkeypatch.setenv("PRIMARY_ACCOUNT_USER", "admin")
    monkeypatch.setattr(dashboard, "_bot_active", lambda user: False)
    monkeypatch.setattr(dashboard, "_tp_running", lambda user, slot: False)
    dashboard._basic_cache.clear()
    return users


def _mutate_target(operation: str):
    if operation == "update":
        with dashboard.app.test_request_context(
            "/api/accounts",
            method="POST",
            json={
                "username": "alice",
                "api_key": "rotated-key",
                "api_secret": "rotated-secret",
            },
        ):
            # Prove target selection does not accidentally lock or inspect the
            # Basic-auth caller's account.
            g.basic_user = "admin"
            return dashboard.api_accounts_save()
    with dashboard.app.test_request_context(
        "/api/accounts/alice",
        method="DELETE",
    ):
        g.basic_user = "admin"
        return dashboard.api_accounts_delete("alice")


@pytest.mark.parametrize("operation", ("update", "delete"))
def test_target_account_mutation_is_rejected_while_entry_lock_is_owned(
    isolated_accounts,
    operation,
):
    account_path = isolated_accounts / "alice" / "account.json"
    original = account_path.read_bytes()
    with account_entry_lock(
        isolated_accounts / "alice",
        "live-entry-that-crosses-preflight-and-post",
    ) as acquired:
        assert acquired is True
        response, status = _mutate_target(operation)

    assert status == 409
    assert "busy processing a trading entry or recovery" in (
        response.get_json()["error"]
    )
    assert account_path.read_bytes() == original


def test_low_level_account_writer_cannot_bypass_target_entry_lock(
    isolated_accounts,
):
    account_path = isolated_accounts / "alice" / "account.json"
    updated = json.loads(account_path.read_text(encoding="utf-8"))
    updated["api_key"] = "rotated-key"
    with account_entry_lock(
        isolated_accounts / "alice",
        "live-entry",
    ) as acquired:
        assert acquired is True
        with pytest.raises(RuntimeError, match="entry lock is busy"):
            dashboard._save_account(updated)

    saved = json.loads(account_path.read_text(encoding="utf-8"))
    assert saved["api_key"] == "key-alice"


def test_low_level_account_writer_cannot_rotate_credentials_during_live_state(
    isolated_accounts,
):
    account_path = isolated_accounts / "alice" / "account.json"
    updated = json.loads(account_path.read_text(encoding="utf-8"))
    updated["api_secret"] = "rotated-secret"
    (isolated_accounts / "alice" / "trend_state.json").write_text(json.dumps({
        "status": "ENTRY_PENDING",
        "execution_mode": "live",
        "dry_run": False,
        "pending_entry_client_order_id": "score-entry-1",
    }), encoding="utf-8")

    with pytest.raises(RuntimeError, match="LIVE trading lifecycle"):
        dashboard._save_account(updated)

    saved = json.loads(account_path.read_text(encoding="utf-8"))
    assert saved["api_secret"] == "secret-alice"


@pytest.mark.parametrize("operation", ("update", "delete"))
@pytest.mark.parametrize(
    ("state", "expected_blocker"),
    (
        (
            {
                "status": "OPEN",
                "execution_mode": "live",
                "dry_run": False,
            },
            "lifecycle is OPEN",
        ),
        (
            {
                "status": "CLOSED",
                "execution_mode": "live",
                "dry_run": False,
                "pending_close_client_order_id": "close-alice-1",
            },
            "unresolved order identity",
        ),
        (
            {
                "status": "CLOSED",
                "execution_mode": "live",
                "dry_run": False,
                "accounting_status": "pending",
            },
            "accounting is unresolved",
        ),
        (
            {
                "status": "CLOSED",
                "execution_mode": "live",
                "dry_run": False,
                "protection_cleanup_pending": True,
            },
            "protection cleanup is unresolved",
        ),
    ),
)
def test_live_lifecycle_blocks_credential_rotation_and_account_deletion(
    isolated_accounts,
    operation,
    state,
    expected_blocker,
):
    state_path = isolated_accounts / "alice" / "trend_state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    account_path = isolated_accounts / "alice" / "account.json"
    original = account_path.read_bytes()

    response, status = _mutate_target(operation)

    assert status == 409
    assert any(
        expected_blocker in blocker
        for blocker in response.get_json()["blockers"]
    )
    assert account_path.read_bytes() == original


@pytest.mark.parametrize("operation", ("update", "delete"))
def test_running_target_tp_monitor_blocks_credentials_and_deletion(
    isolated_accounts,
    monkeypatch,
    operation,
):
    monkeypatch.setattr(
        dashboard,
        "_tp_running",
        lambda user, slot: user == "alice" and slot == "trend",
    )
    account_path = isolated_accounts / "alice" / "account.json"
    original = account_path.read_bytes()

    response, status = _mutate_target(operation)

    assert status == 409
    assert "LIVE Trend TP monitor is running" in response.get_json()["blockers"]
    assert account_path.read_bytes() == original


def test_fresh_target_monitor_heartbeat_blocks_credential_rotation(
    isolated_accounts,
    monkeypatch,
):
    monkeypatch.setattr(dashboard, "_tp_running", lambda user, slot: False)
    monkeypatch.setattr(
        dashboard,
        "_tp_health",
        lambda user, slot: {"heartbeat_utc": "fresh"} if slot == "trend" else {},
    )
    monkeypatch.setattr(
        dashboard,
        "_tp_health_fresh",
        lambda health: health.get("heartbeat_utc") == "fresh",
    )

    response, status = _mutate_target("update")

    assert status == 409
    assert "LIVE Trend TP monitor is running" in response.get_json()["blockers"]


def test_noncredential_account_edit_remains_available_during_live_position(
    isolated_accounts,
):
    (isolated_accounts / "alice" / "trend_state.json").write_text(json.dumps({
        "status": "OPEN",
        "execution_mode": "live",
        "dry_run": False,
    }), encoding="utf-8")
    with dashboard.app.test_request_context(
        "/api/accounts",
        method="POST",
        json={"username": "alice", "display_name": "Alice Updated"},
    ):
        g.basic_user = "admin"
        response = dashboard.api_accounts_save()

    assert response.get_json() == {"ok": True, "created": False}
    saved = json.loads(
        (isolated_accounts / "alice" / "account.json").read_text(
            encoding="utf-8",
        )
    )
    assert saved["display_name"] == "Alice Updated"
    assert saved["api_key"] == "key-alice"
    assert saved["api_secret"] == "secret-alice"


def test_deleted_background_basic_identity_never_falls_back_to_dash_account(
    isolated_accounts,
):
    (isolated_accounts / "alice" / "account.json").unlink()

    with dashboard.app.test_request_context("/background/trend-cycle"):
        g.basic_user = "alice"
        assert dashboard._session_account() is None
        assert dashboard._active_user() == "alice"
        assert dashboard._user_dir() == isolated_accounts / "alice"
        assert dashboard._active_creds() == ("", "")


def test_score_live_submit_keeps_credentials_verified_before_post(monkeypatch):
    credential_source = {"value": ("verified-key", "verified-secret")}
    monkeypatch.setattr(
        dashboard,
        "_active_creds",
        lambda: credential_source["value"],
    )
    monkeypatch.setattr(
        dashboard,
        "_trend_score_auto_live_require_tte",
        Mock(),
    )
    monkeypatch.setattr(
        dashboard,
        "_trend_score_auto_live_quote",
        Mock(return_value={"bid": 10, "ask": 11}),
    )
    wallet = Mock(return_value=10_000)
    monkeypatch.setattr(
        dashboard,
        "_trend_score_auto_live_available_usd",
        wallet,
    )
    monkeypatch.setattr(
        dashboard,
        "_trend_score_auto_live_risk_snapshot",
        Mock(return_value={"proposed_risk_usd": 100}),
    )
    monkeypatch.setattr(
        dashboard,
        "_trend_score_auto_live_execution_limits",
        Mock(return_value=(1.0, 5.0, 20.0)),
    )
    monkeypatch.setattr(
        dashboard,
        "_tp_policy",
        Mock(return_value={"sl_target_pnl": 100}),
    )

    preflight = Mock()

    def complete_preflight(*args, **kwargs):
        preflight(*args, **kwargs)
        # Simulate an out-of-band account-file change after preflight returns.
        # The POST must still be signed as the identity just verified.
        credential_source["value"] = ("replacement-key", "replacement-secret")

    monkeypatch.setattr(
        dashboard,
        "_trend_score_auto_live_final_preflight",
        complete_preflight,
    )
    post = Mock(return_value=({"id": "order-1"}, {"success": True}))
    monkeypatch.setattr(dashboard, "_post_dashboard_order", post)
    exact_lookup = Mock(return_value=dashboard.TrendScoreExactOrderLookup(
        None, False, "test lookup",
    ))
    position = Mock(return_value={
        "product_id": 101,
        "size": 0,
        "entry_price": 0,
    })
    monkeypatch.setattr(
        dashboard,
        "_trend_score_auto_live_exact_order_lookup",
        exact_lookup,
    )
    monkeypatch.setattr(dashboard, "_strict_realtime_position", position)

    def executor(**kwargs):
        kwargs["final_preflight"]({"status": "ENTRY_PENDING"})
        submission = kwargs["submit_order"]({
            "client_order_id": "score-entry-1",
        })
        kwargs["lookup_order"](None, "score-entry-1", 101)
        kwargs["get_position"](101)
        return submission

    monkeypatch.setattr(
        dashboard,
        "execute_or_recover_trend_score_live_entry",
        executor,
    )

    dashboard._trend_score_auto_live_execute(
        user="alice",
        signal={"signal_key": "signal-1"},
        prepared={"product_id": 101},
        transition_id="transition-1",
        initial_revision="revision-1",
        existing_state={"status": "CLOSED"},
    )

    wallet.assert_called_once_with(
        credentials=("verified-key", "verified-secret"),
    )
    assert preflight.call_args.kwargs["expected_credentials"] == (
        "verified-key",
        "verified-secret",
    )
    post.assert_called_once_with(
        {"client_order_id": "score-entry-1"},
        credentials=("verified-key", "verified-secret"),
    )
    exact_lookup.assert_called_once_with(
        None,
        "score-entry-1",
        101,
        credentials=("verified-key", "verified-secret"),
    )
    position.assert_called_once_with(
        101,
        credentials=("verified-key", "verified-secret"),
    )


def test_final_preflight_rejects_a_different_bound_credential_identity(
    monkeypatch,
):
    monkeypatch.setattr(
        dashboard,
        "_trend_score_auto_live_require_tte",
        Mock(),
    )
    monkeypatch.setattr(
        dashboard,
        "_trading_mode_payload",
        lambda: {"dry_run_mode": False, "mode_revision": "revision-1"},
    )
    monkeypatch.setattr(dashboard, "_user_cfg", lambda: {})
    monkeypatch.setattr(
        dashboard,
        "_trend_score_auto_config_error",
        lambda cfg: None,
    )
    monkeypatch.setattr(
        dashboard,
        "_trend_score_auto_mode",
        lambda cfg=None: "live",
    )
    monkeypatch.setattr(
        dashboard,
        "_active_creds",
        lambda: ("replacement-key", "replacement-secret"),
    )

    with pytest.raises(RuntimeError, match="credentials changed"):
        dashboard._trend_score_auto_live_final_preflight(
            {"status": "ENTRY_PENDING"},
            initial_revision="revision-1",
            risk_snapshot={},
            prepared={},
            quote={},
            expected_credentials=("verified-key", "verified-secret"),
        )


def test_order_post_signs_with_explicit_verified_credentials(monkeypatch):
    signer = Mock(return_value={"signed": "verified"})
    monkeypatch.setattr(dashboard, "_sign", signer)
    response = Mock()
    response.json.return_value = {
        "success": True,
        "result": {"id": "order-1"},
    }
    monkeypatch.setattr(dashboard.req, "post", Mock(return_value=response))

    order, payload = dashboard._post_dashboard_order(
        {"client_order_id": "score-entry-1"},
        credentials=("verified-key", "verified-secret"),
    )

    assert order == {"id": "order-1"}
    assert payload["success"] is True
    assert signer.call_args.kwargs == {
        "key": "verified-key",
        "secret": "verified-secret",
    }


def test_exact_client_order_lookup_uses_bound_credentials(monkeypatch):
    signer = Mock(return_value={"signed": "verified"})
    monkeypatch.setattr(dashboard, "_sign", signer)
    response = Mock()
    response.status_code = 200
    response.json.return_value = {
        "success": True,
        "result": {
            "id": "order-1",
            "client_order_id": "trend-entry/one",
            "product_id": 101,
        },
    }
    get = Mock(return_value=response)
    monkeypatch.setattr(dashboard.req, "get", get)

    result = dashboard._trend_score_auto_live_exact_order_lookup(
        None,
        "trend-entry/one",
        101,
        credentials=("verified-key", "verified-secret"),
    )

    assert result.conclusive is True
    assert result.order["id"] == "order-1"
    expected_path = "/v2/orders/client_order_id/trend-entry%2Fone"
    assert get.call_args.args[0] == dashboard.API_BASE + expected_path
    signer.assert_called_once_with(
        "GET",
        expected_path,
        key="verified-key",
        secret="verified-secret",
    )


def test_exact_client_order_not_found_is_conclusive_only_on_authoritative_404(
    monkeypatch,
):
    signer = Mock(return_value={"signed": "verified"})
    monkeypatch.setattr(dashboard, "_sign", signer)
    response = Mock()
    response.status_code = 404
    response.json.return_value = {
        "success": False,
        "error": {"code": "order_not_found"},
    }
    monkeypatch.setattr(dashboard.req, "get", Mock(return_value=response))

    result = dashboard._trend_score_auto_live_exact_order_lookup(
        None,
        "trend-entry-1",
        101,
        credentials=("verified-key", "verified-secret"),
    )

    assert result.order is None
    assert result.conclusive is True


def test_exact_client_order_rejection_is_inconclusive(monkeypatch):
    signer = Mock(return_value={"signed": "verified"})
    monkeypatch.setattr(dashboard, "_sign", signer)
    response = Mock()
    response.status_code = 503
    response.json.return_value = {
        "success": False,
        "error": {"code": "service_unavailable"},
    }
    monkeypatch.setattr(dashboard.req, "get", Mock(return_value=response))

    result = dashboard._trend_score_auto_live_exact_order_lookup(
        None,
        "trend-entry-1",
        101,
        credentials=("verified-key", "verified-secret"),
    )

    assert result.order is None
    assert result.conclusive is False
    assert "service_unavailable" in result.error
