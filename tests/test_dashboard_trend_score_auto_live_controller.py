from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

import dashboard


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _live_config(**updates) -> dict:
    config = {
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
    config.update(updates)
    return config


def _signal(mode: dict, score: float, *, suffix: str = "10:00:00Z") -> dict:
    zone = dashboard.score_zone(score)
    return {
        "mode": copy.deepcopy(mode),
        "snapshot": {"market": {"spot": 65_850}},
        "decision": {
            "decision_id": f"decision-{suffix}",
            "model_version": "trend-live-controller-test",
            "schema_version": "1.0",
        },
        "score": score,
        "zone": zone,
        "signal_key": f"trend-score-auto|BTCUSD|5m|2026-07-23T{suffix}",
        "signal_bar_close_utc": f"2026-07-23T{suffix}",
        "market_regime": (
            "RANGE"
            if zone == dashboard.TREND_SCORE_MOVE_ZONE
            else "TRENDING"
        ),
    }


def _prepared(zone: str) -> dict:
    if zone == dashboard.TREND_SCORE_CE_ZONE:
        symbol, product_id, strike, side, option_type, price = (
            "C-BTC-65400-240726",
            1101,
            65_400,
            "long",
            "CE",
            220.0,
        )
    elif zone == dashboard.TREND_SCORE_PE_ZONE:
        symbol, product_id, strike, side, option_type, price = (
            "P-BTC-66400-240726",
            1102,
            66_400,
            "long",
            "PE",
            240.0,
        )
    else:
        symbol, product_id, strike, side, option_type, price = (
            "MV-BTC-65800-240726",
            1103,
            65_800,
            "short",
            "MOVE",
            700.0,
        )
    return {
        "zone": zone,
        "side": side,
        "option_type": option_type,
        "instrument_kind": (
            "BTC_MOVE"
            if zone == dashboard.TREND_SCORE_MOVE_ZONE
            else "BTC_OPTION"
        ),
        "lots": 1_000,
        "symbol": symbol,
        "product_id": product_id,
        "strike": strike,
        "settlement": "2099-07-24T12:00:00Z",
        "contract_value": 0.001,
        "max_order_lots": 2_000,
        "entry_price": price,
        "entry_depth": 5_000,
        "quote_timestamp": "2099-07-23T10:00:00Z",
        "quote_snapshot": {
            "bid": price if side == "short" else price - 1,
            "ask": price + 1 if side == "short" else price,
            "bid_size": 5_000,
            "ask_size": 5_000,
            "quote_timestamp": "2099-07-23T10:00:00Z",
        },
    }


def _owned_state(
    zone: str,
    *,
    lots: int = 1_000,
    signal_key: str = "old-signal",
) -> dict:
    prepared = _prepared(zone)
    return {
        "status": "OPEN",
        "execution_mode": "live",
        "dry_run": False,
        "ownership": dashboard.TREND_SCORE_AUTO_LIVE_OWNERSHIP,
        "entry_trigger": dashboard.TREND_SCORE_AUTO_TRIGGER,
        "trend_score_zone": zone,
        "score_auto_signal_key": signal_key,
        "symbol": prepared["symbol"],
        "product_id": prepared["product_id"],
        "side": prepared["side"],
        "option_type": prepared["option_type"],
        "entry_mark": prepared["entry_price"],
        "contract_value": prepared["contract_value"],
        "lots": lots,
        "requested_lots": 1_000,
        "position_cycle_id": f"cycle-{zone.lower()}",
        "entry_at_utc": "2026-07-23T09:00:00Z",
    }


def _open_result(signal: dict, prepared: dict) -> dict:
    state = {
        **_owned_state(
            signal["zone"],
            signal_key=signal["signal_key"],
        ),
        "symbol": prepared["symbol"],
        "product_id": prepared["product_id"],
        "side": prepared["side"],
        "option_type": prepared["option_type"],
    }
    return {
        "ok": True,
        "status": "OPEN",
        "consume_signal": True,
        "order_submitted": True,
        "filled_lots": 1_000,
        "partial_fill": False,
        "state": state,
    }


@pytest.fixture
def live_account(tmp_path, monkeypatch):
    users = tmp_path / "users"
    account = users / "alice"
    account.mkdir(parents=True)
    _write(account / "config.json", _live_config())

    monkeypatch.setattr(dashboard, "USERS_DIR", users)
    monkeypatch.setattr(dashboard, "DASH_USER", "alice")
    monkeypatch.setattr(dashboard, "BOT_USER", "alice")
    monkeypatch.setattr(dashboard, "_active_user", lambda: "alice")
    monkeypatch.setattr(dashboard, "_active_creds", lambda: ("key", "secret"))
    monkeypatch.setattr(dashboard, "_trend_audit", Mock())
    monkeypatch.setattr(dashboard, "_trend_score_auto_notify", Mock())
    dashboard._basic_cache.clear()
    dashboard._trend_score_auto_health.clear()
    dashboard._trend_score_auto_cycle_locks.clear()

    def forbidden(name):
        return Mock(
            side_effect=AssertionError(
                f"LIVE controller test reached an unmocked exchange seam: {name}"
            )
        )

    monkeypatch.setattr(dashboard.req, "get", forbidden("HTTP GET"))
    monkeypatch.setattr(dashboard.req, "post", forbidden("HTTP POST"))
    monkeypatch.setattr(dashboard.req, "delete", forbidden("HTTP DELETE"))
    monkeypatch.setattr(
        dashboard,
        "_post_dashboard_order",
        forbidden("order submission"),
    )
    monkeypatch.setattr(dashboard, "_sign", forbidden("request signing"))
    return account


def _install_open_cycle(
    monkeypatch,
    signal: dict,
) -> tuple[Mock, Mock]:
    collector = Mock(return_value=copy.deepcopy(signal))
    prepare = Mock(
        side_effect=lambda value: copy.deepcopy(_prepared(value["zone"]))
    )

    def execute(**kwargs):
        return _open_result(kwargs["signal"], kwargs["prepared"])

    executor = Mock(side_effect=execute)
    monkeypatch.setattr(
        dashboard,
        "_collect_trend_score_auto_signal",
        collector,
    )
    monkeypatch.setattr(
        dashboard,
        "_prepare_trend_score_auto_entry",
        prepare,
    )
    monkeypatch.setattr(
        dashboard,
        "_trend_score_auto_live_execute",
        executor,
    )
    return prepare, executor


def test_explicit_live_mode_routes_only_to_live_controller(
    live_account,
    monkeypatch,
):
    live_cycle = Mock(return_value=True)
    collector = Mock(
        side_effect=AssertionError("the DRY controller must not start")
    )
    monkeypatch.setattr(
        dashboard,
        "_maybe_auto_trend_score_live_cycle",
        live_cycle,
    )
    monkeypatch.setattr(
        dashboard,
        "_collect_trend_score_auto_signal",
        collector,
    )

    assert dashboard._maybe_auto_trend_score_cycle() is True
    live_cycle.assert_called_once()
    assert live_cycle.call_args.args[0] == "alice"
    collector.assert_not_called()


@pytest.mark.parametrize(
    ("score", "expected_zone", "expected_type", "expected_side"),
    (
        (-100, dashboard.TREND_SCORE_PE_ZONE, "PE", "long"),
        (-40, dashboard.TREND_SCORE_PE_ZONE, "PE", "long"),
        (-39.999, dashboard.TREND_SCORE_MOVE_ZONE, "MOVE", "short"),
        (0, dashboard.TREND_SCORE_MOVE_ZONE, "MOVE", "short"),
        (39.999, dashboard.TREND_SCORE_MOVE_ZONE, "MOVE", "short"),
        (40, dashboard.TREND_SCORE_CE_ZONE, "CE", "long"),
        (100, dashboard.TREND_SCORE_CE_ZONE, "CE", "long"),
    ),
)
def test_live_cycle_maps_all_score_boundaries_to_the_approved_contract_class(
    live_account,
    monkeypatch,
    score,
    expected_zone,
    expected_type,
    expected_side,
):
    signal = _signal(dashboard._trading_mode_payload(), score)
    prepare, executor = _install_open_cycle(monkeypatch, signal)

    assert dashboard._maybe_auto_trend_score_cycle() is True
    assert prepare.call_args.args[0]["zone"] == expected_zone
    call = executor.call_args.kwargs
    assert call["signal"]["zone"] == expected_zone
    assert call["prepared"]["zone"] == expected_zone
    assert call["prepared"]["option_type"] == expected_type
    assert call["prepared"]["side"] == expected_side
    assert call["prepared"]["lots"] == 1_000


def test_matching_live_zone_holds_partial_fill_without_topping_up(
    live_account,
    monkeypatch,
):
    state = _owned_state(dashboard.TREND_SCORE_CE_ZONE, lots=400)
    _write(live_account / "trend_state.json", state)
    signal = _signal(
        dashboard._trading_mode_payload(),
        75,
        suffix="10:05:00Z",
    )
    monkeypatch.setattr(
        dashboard,
        "_collect_trend_score_auto_signal",
        Mock(return_value=signal),
    )
    prepare = Mock(
        side_effect=AssertionError("HOLD must not select another contract")
    )
    execute = Mock(
        side_effect=AssertionError("HOLD must not submit or recover an entry")
    )
    monkeypatch.setattr(
        dashboard,
        "_prepare_trend_score_auto_entry",
        prepare,
    )
    monkeypatch.setattr(
        dashboard,
        "_trend_score_auto_live_execute",
        execute,
    )

    assert dashboard._maybe_auto_trend_score_cycle() is False
    prepare.assert_not_called()
    execute.assert_not_called()
    persisted = json.loads(
        (live_account / "trend_state.json").read_text(encoding="utf-8")
    )
    assert persisted["lots"] == 400
    ledger = json.loads(
        (live_account / dashboard.TREND_SCORE_AUTO_LEDGER_FILE).read_text(
            encoding="utf-8"
        )
    )
    assert ledger["signals"][signal["signal_key"]]["action"] == "HOLD"


@pytest.mark.parametrize(
    ("updates", "message"),
    (
        ({"lots": 400.5}, "must be an integer"),
        ({"execution_mode": ""}, "explicit LIVE provenance"),
        ({"dry_run": None}, "explicit LIVE provenance"),
        ({"score_auto_signal_key": ""}, "signal identity is missing"),
    ),
)
def test_live_controller_rejects_corrupt_owned_state(updates, message):
    state = _owned_state(dashboard.TREND_SCORE_CE_ZONE, lots=400)
    state.update(updates)
    with pytest.raises(RuntimeError, match=message):
        dashboard._trend_score_auto_live_owned_position(state)


def test_same_completed_signal_is_consumed_once_even_without_state_rewrite(
    live_account,
    monkeypatch,
):
    signal = _signal(dashboard._trading_mode_payload(), -75)
    prepare, executor = _install_open_cycle(monkeypatch, signal)

    assert dashboard._maybe_auto_trend_score_cycle() is True
    assert dashboard._maybe_auto_trend_score_cycle() is False
    assert prepare.call_count == 1
    assert executor.call_count == 1
    assert (
        dashboard._trend_score_auto_health["alice"]["status"]
        == "signal_consumed"
    )


def test_live_signal_ledger_is_isolated_from_dry_run_namespace(
    live_account,
    monkeypatch,
):
    signal = _signal(dashboard._trading_mode_payload(), 75)
    dry_ledger = {
        "version": 1,
        "signals": {
            signal["signal_key"]: {
                "action": "OPEN",
                "execution_mode": "dry_run",
            }
        },
        "notifications": {},
    }
    dry_path = (
        live_account
        / "dry_run"
        / dashboard.TREND_SCORE_AUTO_LEDGER_FILE
    )
    _write(dry_path, dry_ledger)
    _write(
        live_account / "dry_run" / "trend_state.json",
        {
            **_owned_state(dashboard.TREND_SCORE_PE_ZONE),
            "execution_mode": "dry_run",
            "dry_run": True,
            "ownership": dashboard.TREND_SCORE_AUTO_OWNERSHIP,
        },
    )
    _, executor = _install_open_cycle(monkeypatch, signal)

    assert dashboard._maybe_auto_trend_score_cycle() is True
    executor.assert_called_once()
    assert json.loads(dry_path.read_text(encoding="utf-8")) == dry_ledger
    live_ledger = json.loads(
        (live_account / dashboard.TREND_SCORE_AUTO_LEDGER_FILE).read_text(
            encoding="utf-8"
        )
    )
    assert signal["signal_key"] in live_ledger["signals"]


def _pending_state() -> dict:
    return {
        "status": "ENTRY_PENDING",
        "transition_id": "transition-preflight",
        "pending_entry_client_order_id": "trend-score-test-client",
        "pending_entry_submission_state": "prepared",
    }


def _run_preflight(
    live_account: Path,
    *,
    initial_revision: str,
) -> None:
    pending = _pending_state()
    _write(live_account / "trend_state.json", pending)
    dashboard._trend_score_auto_live_final_preflight(
        pending,
        initial_revision=initial_revision,
        risk_snapshot={"proposed_risk_usd": 250.0},
        prepared=_prepared(dashboard.TREND_SCORE_CE_ZONE),
        quote={"bid": 219, "ask": 220},
    )


def _ticker_payload(
    prepared: dict,
    *,
    symbol: str | None = None,
    product_id: int | None = None,
    timestamp: float | None = None,
    success: bool = True,
) -> dict:
    price = prepared["entry_price"]
    return {
        "success": success,
        "result": {
            "symbol": symbol or prepared["symbol"],
            "product_id": (
                prepared["product_id"]
                if product_id is None
                else product_id
            ),
            "timestamp": timestamp or datetime.now(timezone.utc).timestamp(),
            "tick_size": "0.1",
            "product_trading_status": "operational",
            "quotes": {
                "best_bid": str(price - 1),
                "best_ask": str(price),
                "bid_size": "5000",
                "ask_size": "5000",
            },
        },
    }


@pytest.mark.parametrize(
    ("payload_update", "message"),
    (
        ({"symbol": "C-BTC-WRONG"}, "different contract symbol"),
        ({"product_id": 9999}, "different product identity"),
        ({"success": False}, "ticker is unavailable"),
    ),
)
def test_final_live_quote_is_bound_to_exact_selected_contract(
    live_account,
    monkeypatch,
    payload_update,
    message,
):
    prepared = _prepared(dashboard.TREND_SCORE_CE_ZONE)
    response = Mock()
    response.json.return_value = _ticker_payload(
        prepared,
        **payload_update,
    )
    monkeypatch.setattr(dashboard.req, "get", Mock(return_value=response))

    with pytest.raises(RuntimeError, match=message):
        dashboard._trend_score_auto_live_quote(prepared)


def test_final_live_quote_rejects_future_exchange_timestamp(
    live_account,
    monkeypatch,
):
    prepared = _prepared(dashboard.TREND_SCORE_CE_ZONE)
    response = Mock()
    response.json.return_value = _ticker_payload(
        prepared,
        timestamp=datetime.now(timezone.utc).timestamp() + 30,
    )
    monkeypatch.setattr(dashboard.req, "get", Mock(return_value=response))

    with pytest.raises(RuntimeError, match="timestamp is in the future"):
        dashboard._trend_score_auto_live_quote(prepared)


def test_final_preflight_blocks_any_external_live_position_before_entry(
    live_account,
    monkeypatch,
):
    revision = dashboard._trading_mode_payload()["mode_revision"]
    monkeypatch.setattr(
        dashboard,
        "_strict_exchange_positions",
        Mock(return_value=[{"product_id": 999, "size": 1}]),
    )
    open_orders = Mock(
        side_effect=AssertionError("positions must block before order scan")
    )
    monkeypatch.setattr(
        dashboard,
        "_trend_score_auto_live_open_orders",
        open_orders,
    )

    with pytest.raises(RuntimeError, match="position.*block"):
        _run_preflight(live_account, initial_revision=revision)
    open_orders.assert_not_called()


def test_final_preflight_blocks_any_open_exchange_order_before_entry(
    live_account,
    monkeypatch,
):
    revision = dashboard._trading_mode_payload()["mode_revision"]
    monkeypatch.setattr(
        dashboard,
        "_strict_exchange_positions",
        Mock(return_value=[]),
    )
    monkeypatch.setattr(
        dashboard,
        "_strict_realtime_position",
        Mock(return_value={"product_id": 1101, "size": 0}),
    )
    monkeypatch.setattr(
        dashboard,
        "_trend_score_auto_live_open_orders",
        Mock(return_value=[{"id": "open-order"}]),
    )
    risk = Mock(
        side_effect=AssertionError("open orders must block before risk refresh")
    )
    monkeypatch.setattr(
        dashboard,
        "_trend_score_auto_live_risk_snapshot",
        risk,
    )

    with pytest.raises(RuntimeError, match="open exchange order"):
        _run_preflight(live_account, initial_revision=revision)
    risk.assert_not_called()


def test_final_preflight_blocks_unresolved_morning_or_evening_state(
    live_account,
    monkeypatch,
):
    revision = dashboard._trading_mode_payload()["mode_revision"]
    _write(
        live_account / "morning_state.json",
        {"status": "OPEN", "symbol": "MV-BTC-65800-240726"},
    )
    positions = Mock(
        side_effect=AssertionError("slot state must block before exchange scan")
    )
    monkeypatch.setattr(
        dashboard,
        "_strict_exchange_positions",
        positions,
    )

    with pytest.raises(RuntimeError, match="Morning state is unresolved"):
        _run_preflight(live_account, initial_revision=revision)
    positions.assert_not_called()


def test_final_preflight_blocks_mode_revision_change_before_entry_post(
    live_account,
    monkeypatch,
):
    actual_revision = dashboard._trading_mode_payload()["mode_revision"]
    creds = Mock(
        side_effect=AssertionError("revision mismatch must block before credentials")
    )
    monkeypatch.setattr(dashboard, "_active_creds", creds)

    with pytest.raises(RuntimeError, match="mode changed|Controller mode changed"):
        _run_preflight(
            live_account,
            initial_revision=f"different-{actual_revision}",
        )
    creds.assert_not_called()


def _execution_quote(prepared: dict, *, quote_epoch: float | None = None) -> dict:
    price = prepared["entry_price"]
    return {
        "symbol": prepared["symbol"],
        "bid": price - 1,
        "ask": price,
        "bid_size": 5_000,
        "ask_size": 5_000,
        "quote_age_secs": 0.1,
        "quote_epoch": (
            datetime.now(timezone.utc).timestamp()
            if quote_epoch is None
            else quote_epoch
        ),
        "trading_status": "operational",
        "tick_size": 0.1,
        "price_band": {"lower_limit": 1, "upper_limit": 5_000},
    }


def _deep_preflight_state(
    live_account: Path,
    prepared: dict,
    quote: dict,
) -> tuple[dict, dict]:
    pending = _pending_state()
    payload, _ = dashboard.build_trend_score_live_ioc_payload(
        prepared,
        quote,
        client_order_id=pending["pending_entry_client_order_id"],
        max_slippage_pct=1,
        max_spread_pct=12,
        max_quote_age_sec=20,
    )
    pending["pending_entry_payload"] = payload
    _write(live_account / "trend_state.json", pending)
    risk = dashboard._trend_score_auto_live_risk_snapshot(
        prepared,
        quote,
        available_usd=10_000,
    )
    return pending, risk


def _mock_flat_final_boundary(monkeypatch, final_quote: dict) -> None:
    monkeypatch.setattr(
        dashboard,
        "_strict_exchange_positions",
        Mock(return_value=[]),
    )
    monkeypatch.setattr(
        dashboard,
        "_strict_realtime_position",
        Mock(return_value={"product_id": 1101, "size": 0}),
    )
    monkeypatch.setattr(
        dashboard,
        "_trend_score_auto_live_open_orders",
        Mock(return_value=[]),
    )
    monkeypatch.setattr(
        dashboard,
        "_trend_score_auto_live_available_usd",
        Mock(return_value=10_000),
    )
    monkeypatch.setattr(
        dashboard,
        "_trend_score_auto_live_quote",
        Mock(return_value=final_quote),
    )


def test_final_preflight_recomputes_quote_age_after_private_rest_checks(
    live_account,
    monkeypatch,
):
    prepared = _prepared(dashboard.TREND_SCORE_CE_ZONE)
    initial_quote = _execution_quote(prepared)
    pending, risk = _deep_preflight_state(
        live_account,
        prepared,
        initial_quote,
    )
    stale_quote = {
        **initial_quote,
        # A cached age field must not hide that wall-clock time advanced.
        "quote_age_secs": 0.1,
        "quote_epoch": datetime.now(timezone.utc).timestamp() - 30,
    }
    _mock_flat_final_boundary(monkeypatch, stale_quote)

    with pytest.raises(RuntimeError, match="aged beyond its limit"):
        dashboard._trend_score_auto_live_final_preflight(
            pending,
            initial_revision=dashboard._trading_mode_payload()[
                "mode_revision"
            ],
            risk_snapshot=risk,
            prepared=prepared,
            quote=initial_quote,
        )


def test_final_preflight_rebinds_durable_payload_to_last_order_book(
    live_account,
    monkeypatch,
):
    prepared = _prepared(dashboard.TREND_SCORE_CE_ZONE)
    initial_quote = _execution_quote(prepared)
    pending, risk = _deep_preflight_state(
        live_account,
        prepared,
        initial_quote,
    )
    changed_tick_quote = {**initial_quote, "tick_size": 1.0}
    _mock_flat_final_boundary(monkeypatch, changed_tick_quote)

    with pytest.raises(RuntimeError, match="no longer matches the durable"):
        dashboard._trend_score_auto_live_final_preflight(
            pending,
            initial_revision=dashboard._trading_mode_payload()[
                "mode_revision"
            ],
            risk_snapshot=risk,
            prepared=prepared,
            quote=initial_quote,
        )


def test_final_preflight_accepts_fresh_identity_bound_book_and_flat_account(
    live_account,
    monkeypatch,
):
    prepared = _prepared(dashboard.TREND_SCORE_CE_ZONE)
    final_quote = _execution_quote(prepared)
    pending, risk = _deep_preflight_state(
        live_account,
        prepared,
        final_quote,
    )
    _mock_flat_final_boundary(monkeypatch, final_quote)

    dashboard._trend_score_auto_live_final_preflight(
        pending,
        initial_revision=dashboard._trading_mode_payload()["mode_revision"],
        risk_snapshot=risk,
        prepared=prepared,
        quote=final_quote,
    )


def _reconciled_closed_state(open_state: dict) -> dict:
    return {
        **open_state,
        "status": "CLOSED",
        "pending_entry_client_order_id": None,
        "pending_entry_order_id": None,
        "pending_entry_submission_state": None,
        "pending_close_client_order_id": None,
        "pending_close_order_id": None,
        "pending_close_submission_state": None,
        "pending_stop_protection": None,
        "pending_tp_protection": None,
        "protection_failure_flatten_pending": None,
        "protection_cleanup_pending": False,
        "history_pending": False,
        "accounting_status": "complete",
        "partial_exit_accounting_status": "complete",
    }


@pytest.mark.parametrize(
    ("live_size", "closed_update", "message"),
    (
        (250, {}, "still has 250 lots"),
        (
            0,
            {"accounting_status": "ambiguous"},
            "accounting is incomplete",
        ),
    ),
)
def test_zone_switch_never_enters_after_partial_or_ambiguous_close(
    live_account,
    monkeypatch,
    live_size,
    closed_update,
    message,
):
    old_state = _owned_state(dashboard.TREND_SCORE_CE_ZONE)
    _write(live_account / "trend_state.json", old_state)
    signal = _signal(
        dashboard._trading_mode_payload(),
        -75,
        suffix="10:10:00Z",
    )
    monkeypatch.setattr(
        dashboard,
        "_collect_trend_score_auto_signal",
        Mock(return_value=signal),
    )
    monkeypatch.setattr(
        dashboard,
        "_prepare_trend_score_auto_entry",
        Mock(return_value=_prepared(signal["zone"])),
    )

    def close(slot, state, *, reason):
        assert slot == "trend"
        assert reason == "trend_engine_score_zone_switch"
        _write(
            live_account / "trend_state.json",
            {
                **_reconciled_closed_state(state),
                **closed_update,
            },
        )

    close_mock = Mock(side_effect=close)
    monkeypatch.setattr(
        dashboard,
        "_close_move_state_locked",
        close_mock,
    )
    monkeypatch.setattr(
        dashboard,
        "_strict_realtime_position",
        Mock(
            return_value={
                "product_id": old_state["product_id"],
                "size": live_size,
            }
        ),
    )
    execute = Mock(
        side_effect=AssertionError(
            "switch entry must wait for a conclusively reconciled close"
        )
    )
    monkeypatch.setattr(
        dashboard,
        "_trend_score_auto_live_execute",
        execute,
    )

    # The old-position close occurred, but the new entry remained blocked.
    assert dashboard._maybe_auto_trend_score_cycle() is True
    close_mock.assert_called_once()
    execute.assert_not_called()
    health = dashboard._trend_score_auto_health["alice"]
    assert health["status"] == "flat_waiting_reconciliation"
    assert message in health["last_error"]


def test_missing_config_defaults_score_automation_to_disabled(
    tmp_path,
    monkeypatch,
):
    users = tmp_path / "users"
    (users / "alice").mkdir(parents=True)
    monkeypatch.setattr(dashboard, "USERS_DIR", users)
    monkeypatch.setattr(dashboard, "DASH_USER", "alice")
    monkeypatch.setattr(dashboard, "BOT_USER", "alice")
    monkeypatch.setattr(dashboard, "_active_user", lambda: "alice")
    monkeypatch.setenv("TREND_ENGINE_SCORE_AUTO_MODE", "live")
    dashboard._basic_cache.clear()
    dashboard._trend_score_auto_health.clear()
    collector = Mock(
        side_effect=AssertionError("disabled mode must not collect a signal")
    )
    live_cycle = Mock(
        side_effect=AssertionError("disabled mode must not start LIVE")
    )
    monkeypatch.setattr(
        dashboard,
        "_collect_trend_score_auto_signal",
        collector,
    )
    monkeypatch.setattr(
        dashboard,
        "_maybe_auto_trend_score_live_cycle",
        live_cycle,
    )

    assert dashboard._trend_score_auto_mode() == "disabled"
    assert dashboard._maybe_auto_trend_score_cycle() is False
    collector.assert_not_called()
    live_cycle.assert_not_called()
    assert dashboard._trend_score_auto_health["alice"]["status"] == "disabled"


def test_live_controller_is_blocked_when_account_remains_in_dry_run(
    live_account,
    monkeypatch,
):
    _write(
        live_account / "config.json",
        _live_config(DRY_RUN="true"),
    )
    dashboard._basic_cache.clear()
    live_cycle = Mock(
        side_effect=AssertionError("mode mismatch must not start LIVE")
    )
    monkeypatch.setattr(
        dashboard,
        "_maybe_auto_trend_score_live_cycle",
        live_cycle,
    )

    assert dashboard._maybe_auto_trend_score_cycle() is False
    live_cycle.assert_not_called()
    health = dashboard._trend_score_auto_health["alice"]
    assert health["status"] == "blocked"
    assert "requires LIVE Trading Mode" in health["last_error"]


def test_zone_switch_closes_then_opens_only_after_verified_flat_gate(
    live_account,
    monkeypatch,
):
    old_state = _owned_state(dashboard.TREND_SCORE_CE_ZONE)
    _write(live_account / "trend_state.json", old_state)
    signal = _signal(
        dashboard._trading_mode_payload(),
        -75,
        suffix="10:15:00Z",
    )
    prepared = _prepared(signal["zone"])
    monkeypatch.setattr(
        dashboard,
        "_collect_trend_score_auto_signal",
        Mock(return_value=signal),
    )
    monkeypatch.setattr(
        dashboard,
        "_prepare_trend_score_auto_entry",
        Mock(return_value=prepared),
    )

    calls = []

    def close(slot, state, *, reason):
        calls.append(("close", state["symbol"]))
        _write(
            live_account / "trend_state.json",
            _reconciled_closed_state(state),
        )

    def execute(**kwargs):
        calls.append(("open", kwargs["prepared"]["symbol"]))
        return _open_result(kwargs["signal"], kwargs["prepared"])

    monkeypatch.setattr(
        dashboard,
        "_close_move_state_locked",
        Mock(side_effect=close),
    )
    monkeypatch.setattr(
        dashboard,
        "_strict_realtime_position",
        Mock(return_value={"product_id": old_state["product_id"], "size": 0}),
    )
    monkeypatch.setattr(
        dashboard,
        "_trend_score_auto_live_execute",
        Mock(side_effect=execute),
    )

    assert dashboard._maybe_auto_trend_score_cycle() is True
    assert calls == [
        ("close", old_state["symbol"]),
        ("open", prepared["symbol"]),
    ]
    ledger = json.loads(
        (live_account / dashboard.TREND_SCORE_AUTO_LEDGER_FILE).read_text(
            encoding="utf-8"
        )
    )
    assert ledger["signals"][signal["signal_key"]]["action"] == "SWITCH"


def test_zone_switch_closes_and_stays_flat_when_new_contract_is_unavailable(
    live_account,
    monkeypatch,
):
    old_state = _owned_state(dashboard.TREND_SCORE_CE_ZONE)
    _write(live_account / "trend_state.json", old_state)
    signal = _signal(
        dashboard._trading_mode_payload(),
        -75,
        suffix="10:20:00Z",
    )
    monkeypatch.setattr(
        dashboard,
        "_collect_trend_score_auto_signal",
        Mock(return_value=signal),
    )
    monkeypatch.setattr(
        dashboard,
        "_prepare_trend_score_auto_entry",
        Mock(side_effect=RuntimeError("exact PUT unavailable")),
    )

    def close(slot, state, *, reason):
        _write(
            live_account / "trend_state.json",
            _reconciled_closed_state(state),
        )

    close_mock = Mock(side_effect=close)
    execute = Mock(
        side_effect=AssertionError("missing contract must not reach entry")
    )
    monkeypatch.setattr(
        dashboard,
        "_close_move_state_locked",
        close_mock,
    )
    monkeypatch.setattr(
        dashboard,
        "_strict_realtime_position",
        Mock(return_value={"product_id": old_state["product_id"], "size": 0}),
    )
    monkeypatch.setattr(
        dashboard,
        "_trend_score_auto_live_execute",
        execute,
    )

    assert dashboard._maybe_auto_trend_score_cycle() is True
    close_mock.assert_called_once()
    execute.assert_not_called()
    state = json.loads(
        (live_account / "trend_state.json").read_text(encoding="utf-8")
    )
    assert state["status"] == "CLOSED"
    ledger = json.loads(
        (live_account / dashboard.TREND_SCORE_AUTO_LEDGER_FILE).read_text(
            encoding="utf-8"
        )
    )
    assert signal["signal_key"] not in ledger["signals"]
    assert ledger["current_transition"]["phase"] == "FLAT_WAITING_CONTRACT"


def test_live_signal_in_transition_blocks_follow_on_for_same_signal(
    live_account,
    monkeypatch,
):
    old_state = _owned_state(dashboard.TREND_SCORE_CE_ZONE)
    _write(live_account / "trend_state.json", old_state)
    signal = _signal(
        dashboard._trading_mode_payload(),
        -75,
        suffix="10:30:00Z",
    )
    prepare = Mock(
        side_effect=[
            RuntimeError("exact target quote unavailable"),
            copy.deepcopy(_prepared(signal["zone"])),
        ]
    )
    monkeypatch.setattr(
        dashboard,
        "_collect_trend_score_auto_signal",
        Mock(return_value=signal),
    )
    monkeypatch.setattr(
        dashboard,
        "_prepare_trend_score_auto_entry",
        prepare,
    )

    def close(slot, state, *, reason):
        _write(
            live_account / "trend_state.json",
            _reconciled_closed_state(state),
        )

    close_mock = Mock(side_effect=close)
    execute = Mock(
        side_effect=AssertionError(
            "new signal contract must not reach entry while in-flight"
        )
    )
    monkeypatch.setattr(
        dashboard,
        "_close_move_state_locked",
        close_mock,
    )
    monkeypatch.setattr(
        dashboard,
        "_strict_realtime_position",
        Mock(return_value={"product_id": old_state["product_id"], "size": 0}),
    )
    monkeypatch.setattr(
        dashboard,
        "_trend_score_auto_live_execute",
        execute,
    )

    assert dashboard._maybe_auto_trend_score_cycle() is True
    assert close_mock.call_count == 1
    assert execute.call_count == 0
    health = dashboard._trend_score_auto_health["alice"]
    assert health["status"] == "flat_waiting_contract"

    assert dashboard._maybe_auto_trend_score_cycle() is False
    assert prepare.call_count == 2
    assert execute.call_count == 0
    ledger = json.loads(
        (live_account / dashboard.TREND_SCORE_AUTO_LEDGER_FILE).read_text(
            encoding="utf-8"
        )
    )
    assert ledger["current_transition"]["phase"] == "FLAT_WAITING_CONTRACT"
    assert dashboard._trend_score_auto_health["alice"]["status"] == "signal_consumed"


def test_final_preflight_rechecks_daily_contract_tte_at_post_boundary(
    live_account,
):
    pending = _pending_state()
    _write(live_account / "trend_state.json", pending)
    prepared = _prepared(dashboard.TREND_SCORE_CE_ZONE)
    prepared["settlement"] = (
        datetime.now(timezone.utc) + timedelta(minutes=89)
    ).isoformat()

    with pytest.raises(RuntimeError, match="less than 1.5 hours"):
        dashboard._trend_score_auto_live_final_preflight(
            pending,
            initial_revision=dashboard._trading_mode_payload()[
                "mode_revision"
            ],
            risk_snapshot={"proposed_risk_usd": 250.0},
            prepared=prepared,
            quote={"bid": 219, "ask": 220},
        )


def test_final_preflight_blocks_realtime_target_position_if_aggregate_lags(
    live_account,
    monkeypatch,
):
    revision = dashboard._trading_mode_payload()["mode_revision"]
    monkeypatch.setattr(
        dashboard,
        "_strict_exchange_positions",
        Mock(return_value=[]),
    )
    monkeypatch.setattr(
        dashboard,
        "_strict_realtime_position",
        Mock(return_value={"product_id": 1101, "size": 1}),
    )
    open_orders = Mock(
        side_effect=AssertionError(
            "target exposure must block before the open-order scan"
        )
    )
    monkeypatch.setattr(
        dashboard,
        "_trend_score_auto_live_open_orders",
        open_orders,
    )

    with pytest.raises(RuntimeError, match="real-time position already exists"):
        _run_preflight(live_account, initial_revision=revision)
    open_orders.assert_not_called()


@pytest.mark.parametrize("raw_size", (None, "", True))
def test_realtime_position_never_treats_missing_or_boolean_size_as_flat(
    live_account,
    monkeypatch,
    raw_size,
):
    response = Mock()
    result = {"product_id": 1101}
    if raw_size is not None:
        result["size"] = raw_size
    response.json.return_value = {"success": True, "result": result}
    monkeypatch.setattr(dashboard.req, "get", Mock(return_value=response))
    monkeypatch.setattr(dashboard, "_sign", Mock(return_value={}))

    with pytest.raises(RuntimeError, match="position size is missing"):
        dashboard._strict_realtime_position(1101)


def test_protection_restart_false_is_a_failed_verification(monkeypatch):
    state = _owned_state(dashboard.TREND_SCORE_CE_ZONE)
    monkeypatch.setattr(dashboard, "_tp_health", Mock(return_value={}))
    monkeypatch.setattr(dashboard, "_tp_running", Mock(return_value=True))
    monkeypatch.setattr(
        dashboard,
        "_tp_health_matches",
        Mock(return_value=False),
    )
    monkeypatch.setattr(
        dashboard,
        "_restart_tp_monitor",
        Mock(return_value=False),
    )
    wait = Mock(
        side_effect=AssertionError("failed restart must not be called healthy")
    )
    monkeypatch.setattr(dashboard, "_wait_for_protection", wait)

    verified, health = dashboard._trend_score_auto_live_protect(
        state,
        datetime.now(timezone.utc),
    )
    assert verified is False
    assert "could not be restarted" in health["last_error"]
    wait.assert_not_called()


def test_dashboard_records_same_cycle_protection_close_as_consumed_and_flat(
    live_account,
):
    signal = _signal(dashboard._trading_mode_payload(), 75)
    transition_id = "transition-close-during-protection"
    state = {
        **_owned_state(
            dashboard.TREND_SCORE_CE_ZONE,
            signal_key=signal["signal_key"],
        ),
        "status": "CLOSED",
        "exit_trigger": "take_profit",
        "position_cycle_id": transition_id,
    }
    ledger = {
        "schema_version": 1,
        "signals": {},
        "notifications": {},
        "current_transition": None,
    }
    transition = {
        "transition_id": transition_id,
        "signal_key": signal["signal_key"],
    }
    result = {
        "ok": True,
        "status": "CLOSED_DURING_PROTECTION_SETUP",
        "consume_signal": True,
        "order_submitted": True,
        "filled_lots": 1_000,
        "state": state,
        "handled_signal_key": signal["signal_key"],
        "handled_transition_id": transition_id,
    }

    assert dashboard._trend_score_auto_live_entry_result(
        user="alice",
        signal=signal,
        result=result,
        ledger=ledger,
        data_dir=live_account,
        transition=transition,
        action="OPEN",
    ) is True
    assert (
        ledger["signals"][signal["signal_key"]]["action"]
        == "CLOSED_DURING_PROTECTION_SETUP"
    )
    assert (
        dashboard._trend_score_auto_health["alice"]["status"]
        == "flat_after_protection_exit"
    )
    assert dashboard._trend_score_auto_health["alice"]["lots"] == 0


def test_dashboard_marks_post_protection_generation_change_critical(
    live_account,
):
    signal = _signal(dashboard._trading_mode_payload(), 75)
    transition_id = "transition-generation-change"
    result = {
        "ok": False,
        "status": "POST_PROTECTION_GENERATION_CHANGED",
        "consume_signal": True,
        "order_submitted": True,
        "filled_lots": 1_000,
        "state": {"status": "OPEN", "symbol": "C-BTC-OTHER"},
        "error": "durable Trend position generation changed",
        "handled_signal_key": signal["signal_key"],
        "handled_transition_id": transition_id,
    }
    ledger = {
        "schema_version": 1,
        "signals": {},
        "notifications": {},
        "current_transition": None,
    }
    transition = {
        "transition_id": transition_id,
        "signal_key": signal["signal_key"],
    }

    assert dashboard._trend_score_auto_live_entry_result(
        user="alice",
        signal=signal,
        result=result,
        ledger=ledger,
        data_dir=live_account,
        transition=transition,
        action="OPEN",
    ) is True
    health = dashboard._trend_score_auto_health["alice"]
    assert health["status"] == "critical_state_reconciliation"
    assert "generation changed" in health["last_error"]
