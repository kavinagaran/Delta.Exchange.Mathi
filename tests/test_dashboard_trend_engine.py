from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import pytest

import dashboard


REQUIRED_DECISION_KEYS = {
    "schema_version",
    "model_version",
    "decision_id",
    "timestamp",
    "market_data_timestamp",
    "underlying",
    "decision",
    "directional_bias",
    "confidence",
    "direction_score",
    "direction_components",
    "timeframe_scores",
    "market_regime",
    "detected_setup",
    "selected_contract",
    "trade_score",
    "order_plan",
    "risk_state",
    "hard_gates",
    "reason_codes",
    "decision_summary",
    "audit",
}

REASON_CODES_WITH_PLAIN_ENGLISH_COPY = {
    "ALL_ENTRY_GATES_PASSED",
    "EXISTING_POSITION_THESIS_VALID",
    "INVALID_OR_STALE_DATA",
    "QUOTE_REVALIDATION_FAILED",
    "EVENT_DATA_UNAVAILABLE",
    "EVENT_BLACKOUT",
    "KILL_SWITCH_ACTIVE",
    "DAILY_LOSS_LIMIT",
    "CONSECUTIVE_LOSS_LIMIT",
    "BROKER_CONNECTION_UNRELIABLE",
    "BROKER_OR_EXCHANGE_ERROR",
    "POSITION_STATE_MISMATCH",
    "ORDER_STATE_UNKNOWN",
    "ACCOUNT_RISK_STATE_UNKNOWN",
    "ABNORMAL_SPREAD_OR_VOLATILITY",
    "EXPOSURE_LIMIT",
    "PENDING_ORDER_EXISTS",
    "TIMEFRAME_CONFLICT",
    "DIRECTION_SCORE_TOO_LOW",
    "PRICE_ACTION_NOT_CONFIRMED",
    "DIRECTION_REVERSAL",
    "WRONG_OPTION_TYPE",
    "INSUFFICIENT_LIQUIDITY",
    "SPREAD_TOO_WIDE",
    "EXPIRY_RESTRICTION",
    "ENTRY_PRICE_DEVIATION",
    "NO_ELIGIBLE_CONTRACT",
    "TRADE_SCORE_TOO_LOW",
    "RISK_CALCULATION_FAILED",
    "BREAKEVEN_UNREALISTIC",
    "EXPECTED_VALUE_UNAVAILABLE",
    "NEGATIVE_EXPECTED_VALUE",
    "REWARD_RISK_TOO_LOW",
    "MINIMUM_LOT_EXCEEDS_RISK_LIMIT",
    "NAKED_OPTION_SELLING_PROHIBITED",
    "UNDERLYING_INVALIDATION_REACHED",
    "EMERGENCY_OPTION_STOP_REACHED",
    "TARGET_REACHED",
    "TIME_STOP_REACHED",
}


@pytest.fixture
def isolated_trend_endpoint(tmp_path, monkeypatch):
    """Keep route tests away from account state and the process-wide cache."""
    state = {
        "user": "alice",
        "mode": {
            "dry_run_mode": False,
            "trading_mode": "LIVE",
            "execution_mode": "live",
            "mode_revision": "rev-live-a",
        },
    }
    dashboard._trend_engine_cache.clear()
    monkeypatch.setattr(dashboard, "_active_user", lambda: state["user"])
    monkeypatch.setattr(
        dashboard, "_trading_mode_payload", lambda: dict(state["mode"])
    )
    monkeypatch.setattr(
        dashboard,
        "_trend_engine_config_overrides",
        lambda: {"underlying": "BTCUSD"},
    )
    monkeypatch.setattr(dashboard, "_trend_engine_strategy_config", lambda: {})
    monkeypatch.setattr(
        dashboard,
        "_user_dir",
        lambda: tmp_path / "users" / state["user"],
    )
    monkeypatch.setattr(
        dashboard,
        "_mode_data_dir",
        lambda dry_run=False: tmp_path / state["user"] / (
            "dry_run" if dry_run else "live"
        ),
    )

    @contextmanager
    def acquired_lock(*args, **kwargs):
        yield True

    writes = []
    monkeypatch.setattr(dashboard, "account_file_lock", acquired_lock)
    monkeypatch.setattr(
        dashboard,
        "_atomic_write_json",
        lambda path, value: writes.append((Path(path), value)),
    )
    yield state, writes
    dashboard._trend_engine_cache.clear()


def _get_trend_engine(path="/api/trend-engine"):
    with dashboard.app.test_request_context(path):
        response = dashboard.api_trend_engine()
    return response, response.get_json()


def _actionable_decision(snapshot, config):
    """An actionable signal proves the dashboard route still cannot execute it."""
    return {
        "schema_version": "1.0",
        "model_version": "test-engine",
        "decision_id": f"decision-{snapshot['token']}",
        "timestamp": "2026-07-22T10:00:00Z",
        "market_data_timestamp": "2026-07-22T09:59:55Z",
        "underlying": "BTCUSD",
        "decision": "BUY_CE",
        "directional_bias": "BULLISH",
        "confidence": "HIGH",
        "direction_score": 72,
        "direction_components": {},
        "timeframe_scores": {},
        "market_regime": "TRENDING",
        "detected_setup": {},
        "selected_contract": {"symbol": "C-BTC-TEST"},
        "trade_score": 81,
        "order_plan": {
            "quantity_lots": 25,
            "order_type": "LIMIT",
            "entry_price": 100,
            "maximum_entry_price": 101,
        },
        "risk_state": {},
        "hard_gates": {"data_valid": True},
        "reason_codes": ["ALL_ENTRY_GATES_PASSED"],
        "decision_summary": "Bullish test setup passed every gate.",
        "audit": {"source": "route-test"},
    }


def _cacheable_decision(snapshot, config):
    decision = _actionable_decision(snapshot, config)
    decision["decision"] = "NO_TRADE"
    decision["order_plan"] = {"quantity_lots": 0, "order_type": None}
    decision["reason_codes"] = ["EVENT_DATA_UNAVAILABLE"]
    return decision


def test_collection_failure_is_http_200_schema_shaped_no_trade(
    isolated_trend_endpoint, monkeypatch
):
    _, writes = isolated_trend_endpoint

    def unavailable(**kwargs):
        raise RuntimeError("option chain unavailable")

    monkeypatch.setattr(dashboard, "collect_delta_trend_snapshot", unavailable)

    response, payload = _get_trend_engine("/api/trend-engine?refresh=1")

    assert response.status_code == 200
    assert REQUIRED_DECISION_KEYS <= payload.keys()
    assert payload["decision"] == "NO_TRADE"
    assert payload["reason_codes"] == ["INVALID_OR_STALE_DATA"]
    assert payload["hard_gates"]["data_valid"] is False
    assert payload["selected_contract"]["symbol"] is None
    assert payload["order_plan"]["quantity_lots"] == 0
    assert payload["audit"]["collection_error"] == "option chain unavailable"
    assert payload["audit"]["execution_mode"] == "live"
    assert payload["audit"]["mode_revision"] == "rev-live-a"
    assert payload["audit"]["order_submitted"] is False
    assert writes and writes[-1][1] == payload
    assert writes[-1][0].name == "trend_engine_decision.json"


def test_actionable_response_remains_read_only(
    isolated_trend_endpoint, monkeypatch
):
    get = Mock(name="http_get")
    post = Mock(name="http_post")
    delete = Mock(name="http_delete")
    execute = Mock(name="execute_trend_entry")
    sync = Mock(name="sync_states_from_exchange")
    collector = Mock(return_value={"token": "bullish"})

    monkeypatch.setattr(dashboard.req, "get", get)
    monkeypatch.setattr(dashboard.req, "post", post)
    monkeypatch.setattr(dashboard.req, "delete", delete)
    monkeypatch.setattr(dashboard, "_execute_trend_entry", execute)
    monkeypatch.setattr(dashboard, "_sync_states_from_exchange", sync)
    monkeypatch.setattr(dashboard, "collect_delta_trend_snapshot", collector)
    monkeypatch.setattr(dashboard, "evaluate_trend", _actionable_decision)

    response, payload = _get_trend_engine("/api/trend-engine?refresh=true")

    assert response.status_code == 200
    assert payload["decision"] == "BUY_CE"
    assert payload["selected_contract"]["symbol"] == "C-BTC-TEST"
    assert payload["order_plan"]["quantity_lots"] == 25
    assert payload["audit"]["order_submitted"] is False
    assert payload["audit"]["quote_revalidated"] is True
    assert collector.call_count == 2
    assert collector.call_args.kwargs["http_get"] is get
    get.assert_not_called()
    post.assert_not_called()
    delete.assert_not_called()
    execute.assert_not_called()
    sync.assert_not_called()


def test_changed_entry_quote_fails_revalidation_closed(
    isolated_trend_endpoint, monkeypatch
):
    snapshots = iter(({"token": "initial"}, {"token": "recheck"}))
    monkeypatch.setattr(
        dashboard, "collect_delta_trend_snapshot", lambda **kwargs: next(snapshots)
    )

    def changing_quote(snapshot, config):
        decision = _actionable_decision(snapshot, config)
        if snapshot["token"] == "recheck":
            decision["order_plan"]["entry_price"] = 102
        return decision

    monkeypatch.setattr(dashboard, "evaluate_trend", changing_quote)

    response, payload = _get_trend_engine("/api/trend-engine?refresh=1")

    assert response.status_code == 200
    assert payload["decision"] == "NO_TRADE"
    assert payload["reason_codes"] == ["QUOTE_REVALIDATION_FAILED"]
    assert payload["order_plan"]["quantity_lots"] == 0
    assert payload["audit"]["quote_revalidated"] is False
    assert payload["audit"]["order_submitted"] is False


def test_cache_is_isolated_by_user_execution_mode_and_revision(
    isolated_trend_endpoint, monkeypatch
):
    state, _ = isolated_trend_endpoint
    calls = []

    def collect(**kwargs):
        token = (
            f"{state['user']}-{state['mode']['execution_mode']}-"
            f"{state['mode']['mode_revision']}"
        )
        calls.append((token, dict(kwargs)))
        return {"token": token}

    monkeypatch.setattr(dashboard, "collect_delta_trend_snapshot", collect)
    monkeypatch.setattr(dashboard, "evaluate_trend", _cacheable_decision)

    _, alice_live_a = _get_trend_engine()
    _, alice_live_a_cached = _get_trend_engine()
    assert alice_live_a_cached["decision_id"] == alice_live_a["decision_id"]
    assert len(calls) == 1

    state["user"] = "bob"
    _, bob_live_a = _get_trend_engine()
    assert bob_live_a["decision_id"] != alice_live_a["decision_id"]

    state["user"] = "alice"
    state["mode"] = {
        "dry_run_mode": True,
        "trading_mode": "DRY RUN",
        "execution_mode": "dry_run",
        "mode_revision": "rev-dry-a",
    }
    _, alice_dry_a = _get_trend_engine()
    assert alice_dry_a["decision_id"] != alice_live_a["decision_id"]
    assert calls[-1][1]["dry_run"] is True

    state["mode"] = {
        "dry_run_mode": False,
        "trading_mode": "LIVE",
        "execution_mode": "live",
        "mode_revision": "rev-live-b",
    }
    _, alice_live_b = _get_trend_engine()
    assert alice_live_b["decision_id"] != alice_live_a["decision_id"]
    assert calls[-1][1]["mode_revision"] == "rev-live-b"

    state["mode"]["mode_revision"] = "rev-live-a"
    _, alice_live_a_again = _get_trend_engine()
    assert alice_live_a_again["decision_id"] == alice_live_a["decision_id"]
    assert len(calls) == 4


def test_unknown_event_risk_override_is_scoped_to_phase1_dry_run(
    isolated_trend_endpoint, monkeypatch,
):
    state, _ = isolated_trend_endpoint
    seen_configs = []
    monkeypatch.setattr(
        dashboard, "collect_delta_trend_snapshot", lambda **kwargs: {"token": "x"}
    )

    def evaluate(snapshot, config):
        seen_configs.append(dict(config))
        return _cacheable_decision(snapshot, config)

    monkeypatch.setattr(dashboard, "evaluate_trend", evaluate)

    _get_trend_engine("/api/trend-engine?refresh=1")
    assert "allow_unknown_event_risk" not in seen_configs[-1]

    state["mode"] = {
        "dry_run_mode": True,
        "trading_mode": "DRY RUN",
        "execution_mode": "dry_run",
        "mode_revision": "rev-dry-event-policy",
    }
    _get_trend_engine("/api/trend-engine?refresh=1")
    assert seen_configs[-1]["allow_unknown_event_risk"] is True


def test_cache_is_invalidated_when_kill_switch_configuration_changes(
    isolated_trend_endpoint, monkeypatch
):
    calls = []
    adapter_config = {"TREND_ENGINE_KILL_SWITCH": "false"}

    def collect(**kwargs):
        calls.append(dict(kwargs))
        return {"token": f"snapshot-{len(calls)}"}

    monkeypatch.setattr(
        dashboard, "_trend_engine_strategy_config", lambda: dict(adapter_config)
    )
    monkeypatch.setattr(dashboard, "collect_delta_trend_snapshot", collect)
    monkeypatch.setattr(dashboard, "evaluate_trend", _cacheable_decision)

    _get_trend_engine()
    _get_trend_engine()
    assert len(calls) == 1

    adapter_config["TREND_ENGINE_KILL_SWITCH"] = "true"
    _get_trend_engine()
    assert len(calls) == 2


def test_trend_engine_page_has_navigation_read_only_notice_and_get_only_fetch():
    base = Path(dashboard.BASE)
    shell = (base / "templates" / "base.html").read_text(encoding="utf-8")
    page = (base / "templates" / "trend_engine.html").read_text(
        encoding="utf-8"
    )

    assert dashboard._PAGES["trend-engine"] == (
        "trend_engine.html",
        "Trend Engine",
    )
    assert "('trend-engine', '/trend-engine', 'Trend Engine'" in shell
    assert 'class="te-readonly-notice"' in page
    assert "This engine never submits an order" in page
    assert "const endpoint = '/api/trend-engine'" in page
    assert "`${endpoint}?refresh=1`" in page
    assert "fetch(url, { cache: 'no-store'" in page
    assert "/api/trend-entry" not in page
    assert "method: 'POST'" not in page
    assert "method: 'DELETE'" not in page


def test_trend_engine_page_explains_reasons_in_plain_english():
    page = (Path(dashboard.BASE) / "templates" / "trend_engine.html").read_text(
        encoding="utf-8"
    )

    assert 'id="te-reason-headline"' in page
    assert 'id="te-reason-explanation"' in page
    assert 'id="te-reason-cards"' in page
    assert 'id="te-next-step-copy"' in page
    assert '<details class="te-technical-details">' in page
    assert page.index('<details class="te-technical-details">') < page.index(
        'id="te-reasons"'
    )
    assert "This open position does not have a complete risk plan" in page
    assert "the Trend Engine has not placed an exit order" in page
    assert "Live market or account data could not be loaded" in page
    assert "The latest data did not pass safety validation" in page
    assert "this dashboard version does not yet have a dedicated plain-English" in page
    assert "decision.decision === 'EXIT' && item.exitAction" in page
    assert "do not wait passively for the blackout window to end" in page
    assert "recovered exactly from an authoritative original entry record" in page
    assert "Do not invent a new risk plan after entry" in page
    assert "add the missing or corrected risk-plan values" not in page
    assert "safeText(item.title)" in page
    assert "safeText(item.explanation)" in page
    assert "safeText(item.action)" in page
    assert "positionEntryOnlyGates" in page
    assert "isNotApplicable" in page
    assert "not applicable" in page
    for code in REASON_CODES_WITH_PLAIN_ENGLISH_COPY:
        assert f"{code}: {{" in page
