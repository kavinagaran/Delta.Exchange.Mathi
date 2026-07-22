import copy
import json
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

import dashboard
from trend_engine_live import _persisted_remaining_expected_value


def _decision_and_snapshot(*, side="BUY_CE", symbol=None):
    now = datetime.now(timezone.utc)
    is_call = side == "BUY_CE"
    symbol = symbol or ("C-BTC-66000-310726" if is_call else "P-BTC-66000-310726")
    option_type = "CE" if is_call else "PE"
    direction_score = 72.0 if is_call else -72.0
    expiry = now + timedelta(days=2)
    time_exit = now + timedelta(hours=1)
    market_timestamp = now - timedelta(seconds=1)
    decision = {
        "schema_version": "1.0",
        "model_version": "test-trend-engine",
        "decision_id": f"decision-{now.timestamp()}",
        "timestamp": now.isoformat(),
        "market_data_timestamp": market_timestamp.isoformat(),
        "underlying": "BTCUSD",
        "decision": side,
        "directional_bias": "BULLISH" if is_call else "BEARISH",
        "confidence": "HIGH",
        "direction_score": direction_score,
        "direction_components": {},
        "timeframe_scores": {},
        "market_regime": "TRENDING",
        "detected_setup": {"invalidation_level": 65_000},
        "selected_contract": {
            "symbol": symbol,
            "option_type": option_type,
            "strike": 66_000,
            "expiry": expiry.isoformat(),
            "delta": 0.55 if is_call else -0.55,
            "contract_score": 84.0,
            "contract_components": {"liquidity": 18.0},
        },
        "trade_score": 82.0,
        "order_plan": {
            "order_type": "LIMIT",
            "entry_price": 100.0,
            "maximum_entry_price": 101.0,
            "quantity_lots": 25,
            "lot_size": 1,
            "stop_option_price": 80.0,
            "underlying_invalidation": 65_000.0 if is_call else 67_000.0,
            "target_option_price": 150.0,
            "underlying_target": 67_000.0 if is_call else 65_000.0,
            "time_exit": time_exit.isoformat(),
            "estimated_total_costs": 1.0,
            "maximum_estimated_loss": 12.0,
            "reward_risk": 2.5,
        },
        "risk_state": {"risk_budget": 500.0},
        "hard_gates": {
            "data_valid": True,
            "direction_pass": True,
            "price_action_pass": True,
            "contract_pass": True,
            "spread_pass": True,
            "expiry_pass": True,
            "event_pass": True,
            "expected_value_pass": True,
            "reward_risk_pass": True,
            "portfolio_risk_pass": True,
        },
        "reason_codes": ["ALL_ENTRY_GATES_PASSED"],
        "decision_summary": "All entry gates passed.",
        "audit": {
            "quote_revalidated": True,
            "config": {"max_data_latency_seconds": 30},
            "scenario": {"net_expected_value_per_lot": 0.25},
        },
    }
    candles = {}
    for timeframe, minutes in (("5m", 5), ("15m", 15), ("60m", 60)):
        candles[timeframe] = [{
            "timestamp": (now - timedelta(minutes=minutes * (3 - index))).isoformat(),
            "open": 66_000 + index,
            "high": 66_010 + index,
            "low": 65_990 + index,
            "close": 66_005 + index,
            "volume": 1_000 + index,
            "complete": True,
        } for index in range(3)]
    snapshot = {
        "timestamp": now.isoformat(),
        "market": {"spot": 66_000},
        "candles": candles,
        "option_contracts": [{
            "product_id": 101,
            "symbol": symbol,
            "option_type": option_type,
            "strike": 66_000,
            "expiry": expiry.isoformat(),
            "contract_value": 0.001,
            "ask": 100.0,
            "bid": 99.0,
        }],
    }
    return decision, snapshot


@pytest.fixture
def phase1(tmp_path, monkeypatch):
    user_dir = tmp_path / "users" / "alice"
    dry_dir = user_dir / "dry_run"
    dry_dir.mkdir(parents=True)
    state_file = dry_dir / "trend_state.json"
    mode = {
        "dry_run_mode": True,
        "trading_mode": "DRY RUN",
        "execution_mode": "dry_run",
        "mode_revision": "rev-dry-1",
    }
    decision, snapshot = _decision_and_snapshot()
    holder = {"decision": decision, "snapshot": snapshot}

    @contextmanager
    def acquired(*args, **kwargs):
        yield True

    monkeypatch.setattr(dashboard, "_active_user", lambda: "alice")
    monkeypatch.setattr(dashboard, "_user_dir", lambda: user_dir)
    monkeypatch.setattr(
        dashboard, "_mode_data_dir",
        lambda dry_run=False: dry_dir if dry_run else user_dir,
    )
    monkeypatch.setattr(
        dashboard, "_slot_file",
        lambda slot, dry_run=False: (
            (dry_dir if dry_run else user_dir) / "trend_state.json"
        ),
    )
    monkeypatch.setattr(dashboard, "_trading_mode_payload", lambda: dict(mode))
    monkeypatch.setattr(dashboard, "_trend_engine_config_overrides", lambda: {})
    monkeypatch.setattr(dashboard, "_trend_engine_strategy_config", lambda: {})
    monkeypatch.setattr(dashboard, "account_entry_lock", acquired)
    monkeypatch.setattr(dashboard, "account_file_lock", acquired)
    monkeypatch.setattr(dashboard, "_tp_policy", lambda slot: {"take_profit": 0})
    monkeypatch.setattr(dashboard, "_option_fee_per_lot", lambda *args: 0.01)
    monkeypatch.setattr(dashboard, "_trend_audit", Mock())
    collector = Mock(side_effect=lambda **kwargs: (
        copy.deepcopy(holder["decision"]), copy.deepcopy(holder["snapshot"])
    ))
    monkeypatch.setattr(dashboard, "_collect_fresh_trend_engine_decision", collector)
    monkeypatch.setattr(dashboard.req, "get", Mock())
    monkeypatch.setattr(dashboard.req, "post", Mock())
    monkeypatch.setattr(dashboard.req, "delete", Mock())
    monkeypatch.setattr(dashboard, "_sign", Mock())
    monkeypatch.setattr(dashboard.app, "secret_key", "phase1-test-secret")
    return {
        "user_dir": user_dir,
        "dry_dir": dry_dir,
        "state_file": state_file,
        "mode": mode,
        "holder": holder,
        "collector": collector,
    }


def _preview():
    with dashboard.app.test_request_context(
        "/api/trend-engine/dry-run-preview"
    ):
        response = dashboard.api_trend_engine_dry_run_preview()
    return response.get_json()


def _apply(preview, **changes):
    body = {
        "confirmation_token": preview["confirmation_token"],
        "expected_mode": "dry_run",
        "mode_revision": preview["mode_revision"],
        **changes,
    }
    with dashboard.app.test_request_context(
        "/api/trend-engine/dry-run-entry", method="POST", json=body,
    ):
        raw = dashboard.api_trend_engine_dry_run_entry()
    response, status = raw if isinstance(raw, tuple) else (raw, raw.status_code)
    return status, response.get_json()


def test_confirmed_buy_opens_complete_dry_state_without_exchange_post(phase1):
    preview = _preview()
    assert preview["can_apply"] is True
    assert preview["decision"] == "BUY_CE"
    assert preview["confirmation_token"]

    status, result = _apply(preview)

    assert status == 200
    assert result["ok"] is True
    assert result["option_type"] == "CE"
    assert result["order_submitted"] is False
    state = json.loads(phase1["state_file"].read_text(encoding="utf-8"))
    assert state["status"] == "OPEN"
    assert state["entry_trigger"] == "trend_engine_phase1_confirmed"
    assert state["symbol"] == preview["selected_contract"]["symbol"]
    assert state["lots"] == preview["order_plan"]["quantity_lots"]
    assert state["last_entry_15m_candle"]
    assert state["entry_decision_snapshot"]["decision"] == "BUY_CE"
    assert state["entry_decision_audit"]["quote_revalidated"] is True
    for key in (
        "entry_decision_id", "model_version", "schema_version",
        "underlying_invalidation", "stop_option_price", "target_option_price",
        "time_exit", "remaining_expected_value",
        "remaining_expected_value_as_of_utc",
        "remaining_expected_value_valid_until_utc",
        "remaining_expected_value_source", "engine_signal_fingerprint",
        "engine_risk_plan_fingerprint",
    ):
        assert state[key] not in (None, "")
    ledger = json.loads((
        phase1["dry_dir"] / "trend_engine_consumed_signals.json"
    ).read_text(encoding="utf-8"))
    assert state["engine_signal_fingerprint"] in ledger["signals"]
    dashboard.req.post.assert_not_called()
    dashboard.req.delete.assert_not_called()
    dashboard._sign.assert_not_called()

    retry_status, retry = _apply(preview)
    assert retry_status == 200
    assert retry["idempotent"] is True
    assert retry["option_type"] == "CE"


@pytest.mark.parametrize("decision_name", ["NO_TRADE", "HOLD", "EXIT"])
def test_non_buy_decisions_are_display_only(phase1, decision_name):
    phase1["holder"]["decision"]["decision"] = decision_name
    phase1["holder"]["decision"]["reason_codes"] = ["EVENT_BLACKOUT"]

    preview = _preview()

    assert preview["can_apply"] is False
    assert preview["confirmation_token"] is None
    assert not phase1["state_file"].exists()
    dashboard.req.post.assert_not_called()


def test_live_preview_fails_before_collection_or_private_account_read(phase1):
    phase1["mode"].update({
        "dry_run_mode": False,
        "trading_mode": "LIVE",
        "execution_mode": "live",
        "mode_revision": "rev-live-1",
    })

    preview = _preview()

    assert preview["can_apply"] is False
    assert preview["dry_run"] is False
    phase1["collector"].assert_not_called()
    dashboard.req.get.assert_not_called()
    dashboard._sign.assert_not_called()


def test_browser_cannot_supply_symbol_lots_or_other_trade_controls(phase1):
    preview = _preview()

    status, result = _apply(preview, lots=999_999, symbol="P-BTC-EVIL")

    assert status == 400
    assert "server-controlled" in result["error"]
    assert not phase1["state_file"].exists()


@pytest.mark.parametrize(
    "drift",
    ["quantity", "stop", "earlier_candle", "symbol", "price_ceiling"],
)
def test_apply_rejects_any_signal_contract_quantity_or_risk_plan_drift(
    phase1, drift,
):
    preview = _preview()
    decision = phase1["holder"]["decision"]
    snapshot = phase1["holder"]["snapshot"]
    if drift == "quantity":
        decision["order_plan"]["quantity_lots"] += 1
    elif drift == "stop":
        decision["order_plan"]["stop_option_price"] -= 1
    elif drift == "earlier_candle":
        snapshot["candles"]["60m"][0]["close"] += 100
    elif drift == "symbol":
        new_symbol = "C-BTC-66100-310726"
        decision["selected_contract"]["symbol"] = new_symbol
        snapshot["option_contracts"][0]["symbol"] = new_symbol
    elif drift == "price_ceiling":
        decision["order_plan"]["maximum_entry_price"] += 1

    status, result = _apply(preview)

    assert status == 409
    assert result["ok"] is False
    assert not phase1["state_file"].exists()
    assert not (phase1["dry_dir"] / "trend_engine_consumed_signals.json").exists()


def test_open_pending_and_unreconciled_state_block_entry(phase1):
    preview = _preview()
    phase1["state_file"].write_text(json.dumps({
        "status": "ENTRY_PENDING",
        "pending_entry_order_id": "paper-pending",
    }), encoding="utf-8")

    status, result = _apply(preview)

    assert status == 409
    assert "pending" in result["error"].lower()
    assert phase1["collector"].call_count == 1


def test_consumed_signal_cannot_replay_after_position_is_closed(phase1):
    preview = _preview()
    assert _apply(preview)[0] == 200
    state = json.loads(phase1["state_file"].read_text(encoding="utf-8"))
    state["status"] = "CLOSED"
    dashboard._atomic_write_json(phase1["state_file"], state)

    status, result = _apply(preview)
    next_preview = _preview()

    assert status == 409
    assert "already simulated" in result["error"]
    assert next_preview["can_apply"] is False
    assert "already simulated" in next_preview["apply_reason"]


def test_final_mode_revision_flip_writes_neither_ledger_nor_state(
    phase1, monkeypatch,
):
    preview = _preview()
    stable = dict(phase1["mode"])
    changed = {**stable, "mode_revision": "rev-dry-changed"}
    sequence = iter((stable, stable, stable, changed))
    monkeypatch.setattr(
        dashboard, "_trading_mode_payload", lambda: dict(next(sequence)),
    )

    status, result = _apply(preview)

    assert status == 409
    assert "final" in result["error"].lower()
    assert not phase1["state_file"].exists()
    assert not (phase1["dry_dir"] / "trend_engine_consumed_signals.json").exists()


def test_tampered_confirmation_token_is_rejected_before_collection(phase1):
    preview = _preview()
    calls_before = phase1["collector"].call_count
    preview["confirmation_token"] += "tampered"

    status, result = _apply(preview)

    assert status == 400
    assert "token" in result["error"].lower()
    assert phase1["collector"].call_count == calls_before


def test_expired_confirmation_token_is_rejected_before_collection(phase1):
    preview = _preview()
    payload = dashboard._trend_engine_decode_preview_token(
        preview["confirmation_token"]
    )
    payload["issued_at"] = 1
    payload["expires_at"] = 2
    preview["confirmation_token"] = dashboard._trend_engine_encode_preview_token(
        payload
    )
    calls_before = phase1["collector"].call_count

    status, result = _apply(preview)

    assert status == 400
    assert "expired" in result["error"].lower()
    assert phase1["collector"].call_count == calls_before


def test_confirmation_is_bound_to_user_and_initial_mode_revision(
    phase1, monkeypatch,
):
    preview = _preview()
    calls_before = phase1["collector"].call_count
    monkeypatch.setattr(dashboard, "_active_user", lambda: "bob")

    wrong_user_status, wrong_user = _apply(preview)

    assert wrong_user_status == 409
    assert "account" in wrong_user["error"].lower()
    assert phase1["collector"].call_count == calls_before

    monkeypatch.setattr(dashboard, "_active_user", lambda: "alice")
    wrong_revision_status, wrong_revision = _apply(
        preview, mode_revision="rev-dry-other",
    )
    assert wrong_revision_status == 409
    assert "configuration changed" in wrong_revision["error"].lower()
    assert phase1["collector"].call_count == calls_before

    phase1["mode"].update({
        "dry_run_mode": False,
        "execution_mode": "live",
        "trading_mode": "LIVE",
    })
    wrong_mode_status, wrong_mode = _apply(preview)
    assert wrong_mode_status == 409
    assert "trading mode" in wrong_mode["error"].lower()
    assert phase1["collector"].call_count == calls_before


def test_duplicate_raw_symbol_is_ambiguous_and_fails_closed(phase1):
    duplicate = copy.deepcopy(
        phase1["holder"]["snapshot"]["option_contracts"][0]
    )
    duplicate["product_id"] = 202
    phase1["holder"]["snapshot"]["option_contracts"].append(duplicate)

    preview = _preview()

    assert preview["can_apply"] is False
    assert preview["confirmation_token"] is None
    assert not phase1["state_file"].exists()


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("decision", "trade_score", float("nan")),
        ("decision", "direction_score", float("inf")),
        ("order_plan", "entry_price", float("inf")),
        ("order_plan", "stop_option_price", float("nan")),
        ("audit_config", "max_data_latency_seconds", float("nan")),
    ],
)
def test_nan_inf_and_invalid_order_thesis_never_receive_confirmation(
    phase1, section, field, value,
):
    decision = phase1["holder"]["decision"]
    if section == "decision":
        decision[field] = value
    elif section == "order_plan":
        decision["order_plan"][field] = value
    else:
        decision["audit"]["config"][field] = value

    preview = _preview()

    assert preview["can_apply"] is False
    assert preview["confirmation_token"] is None
    assert not phase1["state_file"].exists()


def test_concurrent_double_submit_allows_only_one_mutation(phase1, monkeypatch):
    preview = _preview()
    started = threading.Event()
    release = threading.Event()
    original_decision = copy.deepcopy(phase1["holder"]["decision"])
    original_snapshot = copy.deepcopy(phase1["holder"]["snapshot"])

    def paused_collection(**kwargs):
        started.set()
        assert release.wait(timeout=5)
        return copy.deepcopy(original_decision), copy.deepcopy(original_snapshot)

    monkeypatch.setattr(
        dashboard, "_collect_fresh_trend_engine_decision", paused_collection,
    )
    first_result = []
    first = threading.Thread(
        target=lambda: first_result.append(_apply(preview)), daemon=True,
    )
    first.start()
    assert started.wait(timeout=5)

    second_status, second = _apply(preview)
    release.set()
    first.join(timeout=10)

    assert second_status == 409
    assert "in progress" in second["error"].lower()
    assert first_result and first_result[0][0] == 200
    state = json.loads(phase1["state_file"].read_text(encoding="utf-8"))
    assert state["status"] == "OPEN"
    assert len(json.loads((
        phase1["dry_dir"] / "trend_engine_consumed_signals.json"
    ).read_text(encoding="utf-8"))["signals"]) == 1


def test_state_write_failure_consumes_signal_before_failing_closed(
    phase1, monkeypatch,
):
    preview = _preview()
    real_atomic_write = dashboard._atomic_write_json

    def ledger_then_fail(path, value):
        if Path(path) == phase1["state_file"]:
            raise OSError("simulated state write failure")
        return real_atomic_write(path, value)

    monkeypatch.setattr(dashboard, "_atomic_write_json", ledger_then_fail)

    first_status, first = _apply(preview)

    assert first_status == 409
    assert "state write failure" in first["error"]
    assert not phase1["state_file"].exists()
    ledger_path = phase1["dry_dir"] / "trend_engine_consumed_signals.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert len(ledger["signals"]) == 1

    calls_before = phase1["collector"].call_count
    retry_status, retry = _apply(preview)
    assert retry_status == 409
    assert "already simulated" in retry["error"]
    assert phase1["collector"].call_count == calls_before


def test_remaining_expected_value_expires_instead_of_being_reused_forever():
    now = datetime.now(timezone.utc)
    state = {
        "remaining_expected_value": 0.25,
        "remaining_expected_value_as_of_utc": (
            now - timedelta(seconds=2)
        ).isoformat(),
        "remaining_expected_value_valid_until_utc": (
            now + timedelta(seconds=2)
        ).isoformat(),
        "remaining_expected_value_source": "entry decision scenario",
    }

    assert _persisted_remaining_expected_value(state, now) == (
        0.25, "valid_persisted_value",
    )
    assert _persisted_remaining_expected_value(
        state, now + timedelta(seconds=2)
    ) == (0.0, "stale_persisted_value")
    assert _persisted_remaining_expected_value(
        {"remaining_expected_value": 0.25}, now
    ) == (0.0, "unverified_persisted_value")


def test_only_explicit_dry_run_phase1_routes_are_registered():
    rules = {rule.rule: set(rule.methods) for rule in dashboard.app.url_map.iter_rules()}
    assert rules["/api/trend-engine/dry-run-preview"] == {"GET", "HEAD", "OPTIONS"}
    assert "POST" in rules["/api/trend-engine/dry-run-entry"]
    assert "/api/trend-engine/entry" not in rules
