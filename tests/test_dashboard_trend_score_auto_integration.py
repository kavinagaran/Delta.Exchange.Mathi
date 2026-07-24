from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

import dashboard


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _safe_score_config(**updates) -> dict:
    config = {
        "DRY_RUN": "true",
        "TREND_ENGINE_SCORE_AUTO_MODE": "dry_run",
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
        "ALLOW_SHORT_MOVE": "false",
    }
    config.update(updates)
    return config


def _score_signal(mode: dict, *, score=60.0, zone=None, suffix="10:00:00Z"):
    zone = zone or dashboard.TREND_SCORE_CE_ZONE
    return {
        "mode": dict(mode),
        "snapshot": {"market": {"spot": 65_850}},
        "decision": {
            "decision_id": f"decision-{suffix}",
            "model_version": "trend-test-1",
            "schema_version": "1.0",
        },
        "score": score,
        "zone": zone,
        "signal_key": f"trend-score-auto|BTCUSD|5m|2026-07-22T{suffix}",
        "signal_bar_close_utc": f"2026-07-22T{suffix}",
        "market_regime": "TRENDING" if zone != dashboard.TREND_SCORE_MOVE_ZONE else "RANGE",
    }


def _prepared(zone):
    now = datetime.now(timezone.utc).isoformat()
    if zone == dashboard.TREND_SCORE_CE_ZONE:
        symbol, product_id, strike, side, option_type, price = (
            "C-BTC-65400-230726", 1001, 65400, "long", "CE", 220.0,
        )
    elif zone == dashboard.TREND_SCORE_PE_ZONE:
        symbol, product_id, strike, side, option_type, price = (
            "P-BTC-66400-230726", 1002, 66400, "long", "PE", 240.0,
        )
    else:
        symbol, product_id, strike, side, option_type, price = (
            "MV-BTC-65800-230726", 1003, 65800, "short", "MOVE", 700.0,
        )
    return {
        "zone": zone,
        "side": side,
        "option_type": option_type,
        "instrument_kind": (
            "BTC_MOVE" if zone == dashboard.TREND_SCORE_MOVE_ZONE
            else "BTC_OPTION"
        ),
        "lots": 1000,
        "symbol": symbol,
        "product_id": product_id,
        "strike": strike,
        "settlement": "2026-07-23T12:00:00Z",
        "contract_value": 0.001,
        "entry_price": price,
        "entry_depth": 5000,
        "quote_timestamp": now,
        "quote_snapshot": {
            "bid": price if side == "short" else price - 1,
            "ask": price + 1 if side == "short" else price,
            "bid_size": 5000,
            "ask_size": 5000,
            "quote_timestamp": now,
        },
    }


@pytest.fixture
def isolated_score_account(tmp_path, monkeypatch):
    users = tmp_path / "users"
    account = users / "alice"
    account.mkdir(parents=True)
    monkeypatch.setattr(dashboard, "USERS_DIR", users)
    monkeypatch.setattr(dashboard, "DASH_USER", "alice")
    monkeypatch.setattr(dashboard, "BOT_USER", "alice")
    monkeypatch.setattr(dashboard, "_active_user", lambda: "alice")
    dashboard._basic_cache.clear()
    dashboard._trend_score_auto_health.clear()
    dashboard._trend_score_auto_cycle_locks.clear()
    return account


@pytest.fixture
def score_cycle(isolated_score_account, monkeypatch):
    account = isolated_score_account
    _write(account / "config.json", _safe_score_config())
    mode = dashboard._trading_mode_payload()
    holder = {
        "signal": _score_signal(mode),
    }
    collector = Mock(
        side_effect=lambda: copy.deepcopy(holder["signal"]),
    )
    prepare = Mock(
        side_effect=lambda signal: copy.deepcopy(_prepared(signal["zone"])),
    )
    audit = Mock()
    notify = Mock()
    monkeypatch.setattr(
        dashboard, "_collect_trend_score_auto_signal", collector,
    )
    monkeypatch.setattr(
        dashboard, "_prepare_trend_score_auto_entry", prepare,
    )
    monkeypatch.setattr(dashboard, "_trend_audit", audit)
    monkeypatch.setattr(dashboard, "_trend_score_auto_notify", notify)
    monkeypatch.setattr(dashboard, "_import_legacy_dry_records", lambda: None)

    forbidden = {}

    def forbid(name):
        mock = Mock(side_effect=AssertionError(
            f"DRY score automation reached private/exchange action: {name}"
        ))
        forbidden[name] = mock
        return mock

    monkeypatch.setattr(dashboard, "_active_creds", forbid("active_creds"))
    monkeypatch.setattr(dashboard, "_sign", forbid("sign"))
    monkeypatch.setattr(
        dashboard, "_post_dashboard_order", forbid("post_dashboard_order"),
    )
    monkeypatch.setattr(
        dashboard, "_submit_trend_order", forbid("submit_trend_order"),
    )
    monkeypatch.setattr(
        dashboard, "_execute_trend_chunks", forbid("execute_trend_chunks"),
    )
    monkeypatch.setattr(dashboard.req, "post", forbid("http_post"))
    monkeypatch.setattr(dashboard.req, "delete", forbid("http_delete"))
    return {
        "account": account,
        "dry": account / "dry_run",
        "mode": mode,
        "holder": holder,
        "collector": collector,
        "prepare": prepare,
        "audit": audit,
        "notify": notify,
        "forbidden": forbidden,
    }


def test_score_auto_mode_is_persisted_only_and_accepts_explicit_live(
        isolated_score_account, monkeypatch):
    monkeypatch.setenv("TREND_ENGINE_SCORE_AUTO_MODE", "dry_run")

    # Automation is fail-safe until this account explicitly saves the mode.
    assert dashboard._trend_score_auto_mode() == "disabled"

    _write(
        isolated_score_account / "config.json",
        _safe_score_config(),
    )
    assert dashboard._trend_score_auto_mode() == "dry_run"

    _write(isolated_score_account / "config.json", {
        "TREND_ENGINE_SCORE_AUTO_MODE": "live",
    })
    assert dashboard._trend_score_auto_mode() == "live"

    _write(isolated_score_account / "config.json", {
        "TREND_ENGINE_SCORE_AUTO_MODE": "paper",
    })
    with pytest.raises(dashboard.AccountConfigError, match="score-auto mode"):
        dashboard._user_cfg()


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"TREND_ENGINE_SCORE_AUTO_MODE": "live"},
         "requires LIVE Trading Mode"),
        ({"DRY_RUN": "false"}, "DRY RUN only"),
        ({"TREND_AUTO_ENTRY_MODE": "shadow"}, "legacy Trend"),
        ({"MOVE_AUTO_ENTRY_MODE": "shadow"}, "legacy MOVE"),
        ({"MORNING_ENABLED": "true"}, "Morning"),
        ({"EVENING_ENABLED": "true"}, "Evening"),
        ({"MAX_ORDER_LOTS": "999"}, "1,000-lot"),
        ({"SL_TARGET_PNL_TREND": "0"}, "positive Trend TP"),
    ],
)
def test_score_auto_config_rejects_live_or_competing_automation(
        updates, message):
    current = _safe_score_config()
    payload = {**current, **updates}
    error = dashboard._validate_config_update(payload, current)
    assert error is not None
    assert message in error


def test_score_auto_config_accepts_only_the_explicit_dry_isolated_profile():
    current = _safe_score_config(
        TREND_ENGINE_SCORE_AUTO_MODE="disabled",
    )
    assert dashboard._validate_config_update(
        _safe_score_config(), current,
    ) is None


def test_score_market_evaluation_removes_positions_and_rejects_invalid_zero(
        monkeypatch):
    snapshot = {
        "positions": [{"symbol": "MV-BTC-65000-230726", "side": "short"}],
        "pending_orders": [{"id": "pending"}],
        "account": {"open_risk": 500, "current_exposure": 500},
        "risk": {
            "position_state_consistent": False,
            "orders_state_known": False,
        },
    }
    evaluated = {}

    def valid_engine(market_only, config):
        evaluated["snapshot"] = market_only
        evaluated["config"] = config
        return {
            "direction_score": -55,
            "hard_gates": {"data_valid": True},
        }

    monkeypatch.setattr(dashboard, "evaluate_trend", valid_engine)
    result = dashboard._trend_score_auto_market_decision(snapshot, {})

    assert result["direction_score"] == -55
    assert evaluated["snapshot"]["positions"] == []
    assert evaluated["snapshot"]["pending_orders"] == []
    assert evaluated["snapshot"]["account"]["open_risk"] == 0
    assert evaluated["snapshot"]["account"]["current_exposure"] == 0
    assert evaluated["snapshot"]["risk"]["position_state_consistent"] is True
    assert evaluated["snapshot"]["risk"]["orders_state_known"] is True
    assert evaluated["config"]["allow_unknown_event_risk"] is True
    # The caller's evidence is immutable.
    assert snapshot["positions"]
    assert snapshot["pending_orders"]

    monkeypatch.setattr(dashboard, "evaluate_trend", lambda *args, **kwargs: {
        # The core's invalid-input fallback score must never become permission
        # for the neutral-zone short MOVE action.
        "direction_score": 0,
        "hard_gates": {"data_valid": False},
    })
    with pytest.raises(RuntimeError, match="invalid or stale"):
        dashboard._trend_score_auto_market_decision(snapshot, {})


def test_score_signal_collector_is_dry_public_only_and_never_authenticates(
        isolated_score_account, monkeypatch):
    _write(isolated_score_account / "config.json", _safe_score_config())
    snapshot = {
        "underlying": "BTCUSD",
        "market": {"spot": 65_850},
        "candles": {"5m": [{
            "timestamp": "2026-07-22T10:00:00Z",
            "open": 65_800,
            "high": 65_900,
            "low": 65_750,
            "close": 65_850,
            "volume": 10,
            "complete": True,
        }]},
        "option_contracts": [],
    }
    collector = Mock(return_value=copy.deepcopy(snapshot))
    sign = Mock(side_effect=AssertionError("DRY collector authenticated"))
    raw_post = Mock(side_effect=AssertionError("DRY collector used POST"))
    raw_delete = Mock(side_effect=AssertionError("DRY collector used DELETE"))
    monkeypatch.setattr(dashboard, "collect_delta_trend_snapshot", collector)
    monkeypatch.setattr(dashboard, "_sign", sign)
    monkeypatch.setattr(dashboard.req, "post", raw_post)
    monkeypatch.setattr(dashboard.req, "delete", raw_delete)
    monkeypatch.setattr(dashboard, "_trend_engine_config_overrides", lambda: {})
    monkeypatch.setattr(dashboard, "_trend_engine_strategy_config", lambda: {})
    monkeypatch.setattr(
        dashboard, "_trend_score_auto_market_decision",
        lambda evidence, config: {
            "direction_score": 55,
            "market_regime": "TRENDING",
            "decision_id": "decision-public-only",
        },
    )

    signal = dashboard._collect_trend_score_auto_signal()

    assert signal["zone"] == dashboard.TREND_SCORE_CE_ZONE
    assert signal["signal_key"].endswith("2026-07-22T10:00:00Z")
    assert signal["signal_bar_close_utc"] == "2026-07-22T10:05:00Z"
    kwargs = collector.call_args.kwargs
    assert kwargs["dry_run"] is True
    assert kwargs["user_dir"] == isolated_score_account
    # The adapter accepts a signing callable for its LIVE branch, but the DRY
    # branch must not invoke it or any state-changing HTTP verb.
    assert kwargs["sign"] is sign
    sign.assert_not_called()
    raw_post.assert_not_called()
    raw_delete.assert_not_called()


def test_corrupt_score_mode_fails_closed_before_collection(
        isolated_score_account, monkeypatch):
    _write(isolated_score_account / "config.json", {
        "TREND_ENGINE_SCORE_AUTO_MODE": "invalid",
    })
    collector = Mock(side_effect=AssertionError("invalid config collected data"))
    monkeypatch.setattr(
        dashboard, "_collect_trend_score_auto_signal", collector,
    )

    assert dashboard._maybe_auto_trend_score_cycle() is False
    collector.assert_not_called()
    health = dashboard._trend_score_auto_health["alice"]
    assert health["status"] == "error"
    assert health["mode"] == "invalid"
    assert "invalid" in health["last_error"].lower()


def test_score_open_state_is_ui_ready_protected_and_exactly_1000_lots(
        isolated_score_account, monkeypatch):
    policy = {
        "tp_target_pnl": 500.0,
        "sl_target_pnl": 250.0,
        "tsl_arm_pnl": 100.0,
        "tsl_trail_pnl": 50.0,
        "tsl_lock_min_pnl": 10.0,
        "tsl_target_pnl": 50.0,
        "poll_secs": 15,
    }
    monkeypatch.setattr(dashboard, "_tp_policy", lambda slot: dict(policy))
    monkeypatch.setattr(dashboard, "_option_fee_per_lot", lambda *args: 0.01)
    signal = {
        "zone": dashboard.TREND_SCORE_CE_ZONE,
        "signal_key": "trend-score-auto|BTCUSD|5m|2026-07-22T10:00:00Z",
        "signal_bar_close_utc": "2026-07-22T10:05:00Z",
        "score": 63.5,
        "market_regime": "TRENDING",
        "snapshot": {"market": {"spot": 65_850}},
        "decision": {
            "decision_id": "decision-test",
            "model_version": "trend-test-1",
            "schema_version": "1.0",
        },
    }
    prepared = {
        "zone": dashboard.TREND_SCORE_CE_ZONE,
        "side": "long",
        "option_type": "CE",
        "instrument_kind": "BTC_OPTION",
        "lots": 1000,
        "symbol": "C-BTC-65400-230726",
        "product_id": 12345,
        "strike": 65400,
        "settlement": "2026-07-23T12:00:00Z",
        "contract_value": 0.001,
        "entry_price": 220.0,
        "entry_depth": 5000,
        "quote_snapshot": {"ask": 220.0, "ask_size": 5000},
    }

    state = dashboard._trend_score_auto_open_state(
        signal, prepared, "trend-score-transition-test",
    )

    assert dashboard._is_dry_record(state)
    assert state["slot"] == "trend"
    assert state["status"] == "OPEN"
    assert state["side"] == "long"
    assert state["lots"] == state["owned_entry_lots"] == 1000
    assert state["ownership"] == dashboard.TREND_SCORE_AUTO_OWNERSHIP
    assert state["entry_trigger"] == dashboard.TREND_SCORE_AUTO_TRIGGER
    assert state["trend_score_zone"] == dashboard.TREND_SCORE_CE_ZONE
    assert state["direction_score_at_entry"] == 63.5
    assert state["simulation_id"].startswith("sim-trend-score-")
    assert state["protection_config"] == policy
    assert state["risk_at_entry_usd"] == policy["sl_target_pnl"]
    assert state["entry_fees_usd"] == 10.0
    assert state["execution_snapshot"]["order_submitted"] is False
    assert state["execution_snapshot"]["exchange_api_called"] is False

    # These are the exact economic and lifecycle fields consumed by the DRY
    # dashboard and its always-on TP/SL/TSL monitor.
    for key in (
        "symbol", "product_id", "strike", "settlement", "contract_value",
        "entry_mark", "entry_date", "entry_time_utc", "total_cost_usd",
        "entry_fees_usd", "protection_config", "simulation_id",
    ):
        assert state[key] not in (None, "")

    monkeypatch.setattr(
        dashboard, "_dry_run_live_mark_and_pnl",
        lambda record: (225.0, 4.98, 5.0, 0.01),
    )
    view = dashboard._enrich_dry_state(state)
    assert view["current_mark"] == 225.0
    assert view["live_pnl"] == 4.98
    assert view["dry_protection"]["running"] is True
    assert view["dry_protection"]["status"] == "starting"


def test_score_open_state_refuses_any_downsized_order(monkeypatch):
    monkeypatch.setattr(dashboard, "_tp_policy", lambda slot: {
        "tp_target_pnl": 500,
        "sl_target_pnl": 250,
        "tsl_arm_pnl": 100,
        "tsl_trail_pnl": 50,
    })
    signal = {
        "zone": dashboard.TREND_SCORE_MOVE_ZONE,
        "signal_key": "signal",
        "signal_bar_close_utc": "2026-07-22T10:05:00Z",
        "score": 0,
        "market_regime": "RANGE",
        "snapshot": {"market": {"spot": 65_000}},
        "decision": {},
    }
    prepared = {
        "side": "short",
        "option_type": "MOVE",
        "instrument_kind": "BTC_MOVE",
        "lots": 999,
        "symbol": "MV-BTC-65000-230726",
        "product_id": 123,
        "strike": 65000,
        "settlement": "2026-07-23T12:00:00Z",
        "contract_value": 0.001,
        "entry_price": 500,
    }
    with pytest.raises(RuntimeError, match="exactly 1,000 lots"):
        dashboard._trend_score_auto_open_state(
            signal, prepared, "transition-downsized",
        )


def test_score_cycle_opens_once_and_never_reuses_same_bar_after_protection_exit(
        score_cycle, monkeypatch):
    state_path = score_cycle["dry"] / "trend_state.json"

    assert dashboard._maybe_auto_trend_score_cycle() is True
    first = json.loads(state_path.read_text(encoding="utf-8"))
    assert first["status"] == "OPEN"
    assert first["lots"] == 1000
    assert first["trend_score_zone"] == dashboard.TREND_SCORE_CE_ZONE
    assert not (score_cycle["account"] / "trend_state.json").exists()

    # A second supervisor poll during the same completed bar is a pure no-op.
    assert dashboard._maybe_auto_trend_score_cycle() is False
    repeated = json.loads(state_path.read_text(encoding="utf-8"))
    assert repeated["simulation_id"] == first["simulation_id"]
    assert score_cycle["prepare"].call_count == 1
    assert score_cycle["notify"].call_count == 1

    # A TP/SL/TSL exit consumes this bar too. It cannot immediately reopen
    # just because the 15-second supervisor polls again before the next close.
    monkeypatch.setattr(
        dashboard, "_dry_run_mark_and_pnl",
        lambda state: (230.0, 9.98, 10.0, 0.01),
    )
    with dashboard.account_file_lock(
        score_cycle["dry"], "close-trend", "test-protection-close",
    ) as acquired:
        assert acquired
        closed = dashboard._close_dry_simulation_locked(
            "trend", repeated, trigger="take_profit_simulated",
        )
    assert closed["status"] == "CLOSED"

    assert dashboard._maybe_auto_trend_score_cycle() is False
    final = json.loads(state_path.read_text(encoding="utf-8"))
    history = json.loads((
        score_cycle["dry"] / "trade_history.json"
    ).read_text(encoding="utf-8"))
    ledger = json.loads((
        score_cycle["dry"] / dashboard.TREND_SCORE_AUTO_LEDGER_FILE
    ).read_text(encoding="utf-8"))
    assert final["status"] == "CLOSED"
    assert final["simulation_id"] == first["simulation_id"]
    assert len(history) == 1
    assert history[0]["exit_trigger"] == "take_profit_simulated"
    assert first["score_auto_signal_key"] in ledger["signals"]
    assert score_cycle["prepare"].call_count == 1
    assert score_cycle["notify"].call_count == 1
    for mock in score_cycle["forbidden"].values():
        mock.assert_not_called()


def test_score_cycle_closes_and_switches_on_the_same_new_signal_exactly_once(
        score_cycle, monkeypatch):
    state_path = score_cycle["dry"] / "trend_state.json"
    assert dashboard._maybe_auto_trend_score_cycle() is True
    call_state = json.loads(state_path.read_text(encoding="utf-8"))

    score_cycle["holder"]["signal"] = _score_signal(
        score_cycle["mode"],
        score=-70,
        zone=dashboard.TREND_SCORE_PE_ZONE,
        suffix="10:05:00Z",
    )
    monkeypatch.setattr(
        dashboard, "_dry_run_mark_and_pnl",
        lambda state: (210.0, -10.02, -10.0, 0.01),
    )

    assert dashboard._maybe_auto_trend_score_cycle() is True
    put_state = json.loads(state_path.read_text(encoding="utf-8"))
    history = json.loads((
        score_cycle["dry"] / "trade_history.json"
    ).read_text(encoding="utf-8"))
    ledger = json.loads((
        score_cycle["dry"] / dashboard.TREND_SCORE_AUTO_LEDGER_FILE
    ).read_text(encoding="utf-8"))

    assert put_state["status"] == "OPEN"
    assert put_state["symbol"].startswith("P-BTC-")
    assert put_state["trend_score_zone"] == dashboard.TREND_SCORE_PE_ZONE
    assert put_state["lots"] == 1000
    assert put_state["simulation_id"] != call_state["simulation_id"]
    assert len(history) == 1
    assert history[0]["symbol"] == call_state["symbol"]
    assert history[0]["exit_trigger"] == "trend_engine_score_zone_switch"
    assert len(ledger["signals"]) == 2
    assert ledger["current_transition"]["phase"] == "COMPLETE"
    assert ledger["current_transition"]["action"] == "SWITCH"

    # The same new signal cannot duplicate either side of the switch.
    assert dashboard._maybe_auto_trend_score_cycle() is False
    assert json.loads(state_path.read_text(
        encoding="utf-8"))["simulation_id"] == put_state["simulation_id"]
    assert len(json.loads((
        score_cycle["dry"] / "trade_history.json"
    ).read_text(encoding="utf-8"))) == 1
    assert score_cycle["prepare"].call_count == 2
    assert score_cycle["notify"].call_count == 2
    for mock in score_cycle["forbidden"].values():
        mock.assert_not_called()


def test_failed_switch_contract_leaves_flat_then_retries_same_signal_once(
        score_cycle, monkeypatch):
    state_path = score_cycle["dry"] / "trend_state.json"
    assert dashboard._maybe_auto_trend_score_cycle() is True
    first = json.loads(state_path.read_text(encoding="utf-8"))
    score_cycle["holder"]["signal"] = _score_signal(
        score_cycle["mode"],
        score=-70,
        zone=dashboard.TREND_SCORE_PE_ZONE,
        suffix="10:05:00Z",
    )
    score_cycle["prepare"].side_effect = [
        RuntimeError("exact target quote unavailable"),
        copy.deepcopy(_prepared(dashboard.TREND_SCORE_PE_ZONE)),
    ]
    monkeypatch.setattr(
        dashboard, "_dry_run_mark_and_pnl",
        lambda state: (210.0, -10.02, -10.0, 0.01),
    )

    # The old-zone exposure exits; the failed entry is not falsely consumed.
    assert dashboard._maybe_auto_trend_score_cycle() is True
    flat = json.loads(state_path.read_text(encoding="utf-8"))
    ledger = json.loads((
        score_cycle["dry"] / dashboard.TREND_SCORE_AUTO_LEDGER_FILE
    ).read_text(encoding="utf-8"))
    new_key = score_cycle["holder"]["signal"]["signal_key"]
    assert flat["status"] == "CLOSED"
    assert flat["simulation_id"] == first["simulation_id"]
    assert new_key not in ledger["signals"]
    assert ledger["current_transition"]["phase"] == "EXIT_COMMITTED"
    assert dashboard._trend_score_auto_health["alice"]["status"] == \
        "flat_waiting_contract"

    # A later fresh quote for this same completed bar is blocked for this signal
    # because the first switch entered its in-flight transition and is waiting
    # for completion before any follow-on action can happen.
    assert dashboard._maybe_auto_trend_score_cycle() is False
    opened = json.loads(state_path.read_text(encoding="utf-8"))
    history = json.loads((
        score_cycle["dry"] / "trade_history.json"
    ).read_text(encoding="utf-8"))
    ledger = json.loads((
        score_cycle["dry"] / dashboard.TREND_SCORE_AUTO_LEDGER_FILE
    ).read_text(encoding="utf-8"))
    assert opened["status"] == "CLOSED"
    assert opened["trend_score_zone"] == dashboard.TREND_SCORE_CE_ZONE
    assert new_key not in ledger["signals"]
    assert ledger["current_transition"]["phase"] == "EXIT_COMMITTED"
    assert dashboard._trend_score_auto_health["alice"]["status"] == "signal_consumed"
    assert len(history) == 1
    assert history[0]["exit_trigger"] == "trend_engine_score_zone_switch"
    for mock in score_cycle["forbidden"].values():
        mock.assert_not_called()


def test_score_cycle_neutral_zone_opens_short_move_in_trend_slot_without_window(
        score_cycle):
    # No clock/window seam is patched: an arbitrary completed-bar event opens
    # immediately and is deliberately stored in the single controller slot.
    score_cycle["holder"]["signal"] = _score_signal(
        score_cycle["mode"],
        score=0,
        zone=dashboard.TREND_SCORE_MOVE_ZONE,
        suffix="10:02:17Z",
    )
    assert dashboard._maybe_auto_trend_score_cycle() is True

    state = json.loads((
        score_cycle["dry"] / "trend_state.json"
    ).read_text(encoding="utf-8"))
    assert state["slot"] == "trend"
    assert state["side"] == "short"
    assert state["option_type"] == "MOVE"
    assert state["symbol"].startswith("MV-BTC-")
    assert state["lots"] == 1000
    assert not (score_cycle["dry"] / "morning_state.json").exists()
    assert not (score_cycle["dry"] / "straddle_state.json").exists()
    for mock in score_cycle["forbidden"].values():
        mock.assert_not_called()


def test_score_cycle_blocks_foreign_paper_position_without_modifying_it(
        score_cycle):
    foreign = {
        "slot": "trend",
        "status": "OPEN",
        "dry_run": True,
        "execution_mode": "dry_run",
        "simulation_id": "sim-manual-foreign",
        "ownership": "trend_engine_dry_run",
        "entry_trigger": "trend_engine_phase1_confirmed",
        "side": "long",
        "symbol": "C-BTC-65000-230726",
        "lots": 1000,
    }
    state_path = score_cycle["dry"] / "trend_state.json"
    _write(state_path, foreign)

    assert dashboard._maybe_auto_trend_score_cycle() is False
    assert json.loads(state_path.read_text(encoding="utf-8")) == foreign
    assert "non-controller" in (
        dashboard._trend_score_auto_health["alice"]["last_error"]
    )
    score_cycle["prepare"].assert_not_called()
    score_cycle["notify"].assert_not_called()
    for mock in score_cycle["forbidden"].values():
        mock.assert_not_called()


@pytest.mark.parametrize(
    ("slot", "filename"),
    (("morning", "morning_state.json"),
     ("evening", "straddle_state.json")),
)
def test_score_cycle_blocks_any_other_open_paper_slot(
        score_cycle, slot, filename):
    other = {
        "slot": slot,
        "status": "OPEN",
        "dry_run": True,
        "execution_mode": "dry_run",
        "simulation_id": f"sim-{slot}-existing",
        "symbol": "MV-BTC-65000-230726",
        "side": "short",
        "lots": 1000,
    }
    other_path = score_cycle["dry"] / filename
    _write(other_path, other)

    assert dashboard._maybe_auto_trend_score_cycle() is False
    assert json.loads(other_path.read_text(encoding="utf-8")) == other
    assert not (score_cycle["dry"] / "trend_state.json").exists()
    assert slot.title() in dashboard._trend_score_auto_health[
        "alice"
    ]["last_error"]
    score_cycle["notify"].assert_not_called()
    for mock in score_cycle["forbidden"].values():
        mock.assert_not_called()


def test_score_cycle_mode_revision_change_fails_before_any_state_write(
        score_cycle):
    score_cycle["holder"]["signal"]["mode"]["mode_revision"] = "stale-mode"

    assert dashboard._maybe_auto_trend_score_cycle() is False
    assert not (score_cycle["dry"] / "trend_state.json").exists()
    assert "Configuration changed" in (
        dashboard._trend_score_auto_health["alice"]["last_error"]
    )
    score_cycle["notify"].assert_not_called()
    for mock in score_cycle["forbidden"].values():
        mock.assert_not_called()


def test_score_auto_status_reports_server_mode_and_controller_position(
        score_cycle):
    assert dashboard._maybe_auto_trend_score_cycle() is True

    with dashboard.app.test_request_context(
            "/api/trend-engine/score-auto/status"):
        payload = dashboard.api_trend_engine_score_auto_status().get_json()

    assert payload["enabled"] is True
    assert payload["mode"] == "dry_run"
    assert payload["dry_run_only"] is True
    assert payload["fixed_lots"] == 1000
    assert payload["position_status"] == "OPEN"
    assert payload["current_zone"] == dashboard.TREND_SCORE_CE_ZONE
    assert payload["symbol"].startswith("C-BTC-")
    assert payload["lots"] == 1000
