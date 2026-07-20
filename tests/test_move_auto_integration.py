from datetime import datetime, timedelta, timezone
import json
from unittest.mock import Mock, patch

import pytest

import Delta_Straddle_Live as bot
from move_decision import LONG_MOVE, NO_TRADE, SHORT_MOVE


def _contract():
    return {
        "id": 91,
        "symbol": "MV-BTC-64000-190726",
        "strike_price": "64000",
        "contract_value": ".001",
        "settlement_time": "2026-07-19T12:00:00Z",
    }


def _context(action=LONG_MOVE, side="buy"):
    return {
        "decision_id": "decision-1",
        "normalized_input": {
            "underlying": {"btc_index_price": 64_100},
        },
        "decision": {
            "action": action,
            "side": side,
            "metrics": {
                "long_edge_per_contract": .03,
                "short_edge_per_contract": -.04,
            },
            "failed_gates": {"common": [], "long": [], "short": []},
        },
    }


def _sideways_signal():
    return {
        "available": True,
        "all_sideways": True,
        "observed_at_utc": "2026-07-20T00:15:00+00:00",
        "timeframes": {
            key: {
                "trend": "neutral",
                "display": "sideways",
                "candle_time": index,
                "live_candle": key == "1h",
            }
            for index, key in enumerate(("5m", "15m", "1h"), start=1)
        },
    }


def _sideways_candidate_decision():
    return {
        "action": NO_TRADE,
        "side": None,
        "common_passed": True,
        "long_signal": False,
        "short_signal": False,
        "conflict": False,
        "gates": {
            "common": {"quote_fresh": True},
            "long": {"edge": False},
            "short": {
                "allowed": True,
                "edge": False,
                "bid_liquidity": True,
                "jump_event_risk": False,
                "p99_risk_per_contract": True,
                "available_margin": True,
                "liquidation_buffer": True,
            },
        },
        "failed_gates": {
            "common": [],
            "long": ["edge"],
            "short": ["edge", "jump_event_risk"],
        },
    }


def test_fresh_dashboard_snapshot_maps_three_neutral_windows_to_all_sideways(
        tmp_path):
    now = datetime.now(timezone.utc)
    snapshot = {
        "observed_at_utc": now.isoformat(),
        "timeframes": {
            "5m": {"trend": "neutral", "candle_time": 1,
                   "live_candle": False},
            "15m": {"trend": "neutral", "candle_time": 2,
                    "live_candle": False},
            "1h": {"trend": "neutral", "candle_time": 3,
                   "live_candle": True},
        },
    }
    path = tmp_path / "trend_signal_snapshot.json"
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    with patch.object(bot, "TREND_SIGNAL_SNAPSHOT_FILE", path):
        signal = bot._load_morning_sideways_signal(now=now)

    assert signal["available"] is True
    assert signal["all_sideways"] is True
    assert {row["display"] for row in signal["timeframes"].values()} == {
        "sideways"}


def test_stale_dashboard_snapshot_never_authorises_morning_short(tmp_path):
    now = datetime.now(timezone.utc)
    snapshot = {
        "observed_at_utc": (
            now - timedelta(
                seconds=bot.MORNING_SIDEWAYS_SNAPSHOT_MAX_AGE_SEC + 1)
        ).isoformat(),
        "timeframes": {
            "5m": {"trend": "neutral", "candle_time": 1,
                   "live_candle": False},
            "15m": {"trend": "neutral", "candle_time": 2,
                    "live_candle": False},
            "1h": {"trend": "neutral", "candle_time": 3,
                   "live_candle": True},
        },
    }
    path = tmp_path / "trend_signal_snapshot.json"
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    with patch.object(bot, "TREND_SIGNAL_SNAPSHOT_FILE", path):
        signal = bot._load_morning_sideways_signal(now=now)

    assert signal["available"] is False
    assert signal["all_sideways"] is False
    assert "stale" in signal["reason"]


def test_all_sideways_replaces_only_short_edge_and_event_gates():
    decision, override = bot._apply_morning_sideways_short_decision(
        _sideways_candidate_decision(), _sideways_signal())

    assert decision["action"] == SHORT_MOVE
    assert decision["side"] == "sell"
    assert decision["short_signal"] is True
    assert decision["failed_gates"]["short"] == []
    assert override["applied"] is True
    assert override["replaced_gates"] == ["edge", "jump_event_risk"]


def test_all_sideways_preserves_independent_short_safety_gates():
    candidate = _sideways_candidate_decision()
    candidate["action"] = LONG_MOVE
    candidate["side"] = "buy"
    candidate["long_signal"] = True
    candidate["gates"]["short"]["bid_liquidity"] = False
    candidate["failed_gates"]["short"].append("bid_liquidity")

    decision, override = bot._apply_morning_sideways_short_decision(
        candidate, _sideways_signal())

    assert decision["action"] == NO_TRADE
    assert decision["side"] is None
    assert override["applied"] is False
    assert override["preserved_safety_blockers"] == ["bid_liquidity"]


def test_flat_morning_all_sideways_is_due_without_a_schedule_time():
    with patch.object(bot, "MORNING_ENABLED", True), \
            patch.object(bot, "MOVE_AUTO_ENTRY_MODE", "live"):
        due, reason, _ = bot._immediate_morning_sideways_due(
            "2026-07-20",
            state={"status": "CLOSED", "entry_date": "2026-07-19"},
            signal=_sideways_signal(),
        )

    assert due is True
    assert reason == "all_sideways_flat_morning"


@pytest.mark.parametrize("state, expected_reason", [
    ({"status": "OPEN", "entry_date": "2026-07-20"}, "morning_open"),
    ({"status": "ENTRY_PENDING"}, "morning_entry_pending"),
    ({"status": "CLOSED", "entry_date": "2026-07-20"},
     "morning_already_recorded_today"),
])
def test_immediate_morning_sideways_never_duplicates_position_or_daily_cycle(
        state, expected_reason):
    with patch.object(bot, "MORNING_ENABLED", True), \
            patch.object(bot, "MOVE_AUTO_ENTRY_MODE", "live"):
        due, reason, _ = bot._immediate_morning_sideways_due(
            "2026-07-20", state=state, signal=_sideways_signal())

    assert due is False
    assert reason == expected_reason


def test_immediate_morning_sideways_requires_fresh_all_sideways_snapshot():
    with patch.object(bot, "MORNING_ENABLED", True), \
            patch.object(bot, "MOVE_AUTO_ENTRY_MODE", "live"):
        unavailable, unavailable_reason, _ = \
            bot._immediate_morning_sideways_due(
                "2026-07-20",
                state={"status": "IDLE"},
                signal={"available": False, "all_sideways": False},
            )
        directional, directional_reason, _ = \
            bot._immediate_morning_sideways_due(
                "2026-07-20",
                state={"status": "IDLE"},
                signal={"available": True, "all_sideways": False},
            )

    assert unavailable is False
    assert unavailable_reason == "trend_snapshot_unavailable"
    assert directional is False
    assert directional_reason == "timeframes_not_all_sideways"


def test_evening_shadow_records_decision_without_planning_or_order():
    plan = Mock(side_effect=AssertionError("shadow mode reached entry planning"))
    submit = Mock(side_effect=AssertionError("shadow mode reached order submission"))
    with patch.object(bot, "MOVE_AUTO_ENTRY_MODE", "shadow"), \
            patch.object(bot, "already_traded_today", return_value=False), \
            patch.object(bot, "find_active_mv_contract", return_value=_contract()), \
            patch.object(bot, "build_move_auto_decision",
                         return_value=_context()) as decide, \
            patch.object(bot, "build_move_entry_plan", plan), \
            patch.object(bot, "place_entry_order", submit):
        result = bot._entry_job_locked()

    assert result["status"] == "shadow_recorded"
    decide.assert_called_once_with(_contract(), "evening", bot.LOTS)
    plan.assert_not_called()
    submit.assert_not_called()


def test_morning_shadow_records_decision_without_planning_or_order():
    plan = Mock(side_effect=AssertionError("shadow mode reached entry planning"))
    submit = Mock(side_effect=AssertionError("shadow mode reached order submission"))
    with patch.object(bot, "MOVE_AUTO_ENTRY_MODE", "shadow"), \
            patch.object(bot, "load_morning_state", return_value=None), \
            patch.object(bot, "get_mv_contract", return_value=_contract()), \
            patch.object(bot, "build_move_auto_decision",
                         return_value=_context()) as decide, \
            patch.object(bot, "build_move_entry_plan", plan), \
            patch.object(bot, "place_entry_order", submit):
        result = bot._morning_entry_job_locked()

    assert result["status"] == "shadow_recorded"
    assert decide.call_args.args[1:] == ("morning", bot.MORNING_LOTS)
    plan.assert_not_called()
    submit.assert_not_called()


def test_live_auto_no_trade_never_builds_or_submits_an_order():
    context = _context(NO_TRADE, None)
    context["decision"]["failed_gates"]["long"] = ["edge"]
    context["decision"]["failed_gates"]["short"] = ["jump_event_risk"]
    plan = Mock(side_effect=AssertionError("NO_TRADE reached entry planning"))
    submit = Mock(side_effect=AssertionError("NO_TRADE reached order submission"))
    with patch.object(bot, "MOVE_AUTO_ENTRY_MODE", "live"), \
            patch.object(bot, "already_traded_today", return_value=False), \
            patch.object(bot, "find_active_mv_contract", return_value=_contract()), \
            patch.object(bot, "build_move_auto_decision", return_value=context), \
            patch.object(bot, "build_move_entry_plan", plan), \
            patch.object(bot, "place_entry_order", submit):
        result = bot._entry_job_locked()

    assert result["status"] == "no_trade"
    plan.assert_not_called()
    submit.assert_not_called()


def test_live_auto_long_uses_forecast_side_and_persists_decision_identity():
    contract = _contract()
    context = _context()
    plan = {
        "snapshot": {"bid": 99.0, "ask": 101.0},
        "lots": 5,
        "value_signal": {"automatic": True},
        "auto_context": context,
        "risk_at_entry_usd": 10.0,
        "risk_decision": {"allowed": True},
        "protection_config": {"tp_target_pnl": 10.0},
    }
    persisted = []
    order = {
        "result": {
            "id": 72,
            "average_fill_price": "100.5",
            "order_ids": [72],
            "client_order_id": "auto-1",
            "client_order_ids": ["auto-1"],
        },
    }
    with patch.object(bot, "MOVE_AUTO_ENTRY_MODE", "live"), \
            patch.object(bot, "DRY_RUN", True), \
            patch.object(bot, "already_traded_today", return_value=False), \
            patch.object(bot, "find_active_mv_contract", return_value=contract), \
            patch.object(bot, "build_move_auto_decision", return_value=context), \
            patch.object(bot, "build_move_entry_plan", return_value=plan) as build, \
            patch.object(bot, "cancel_product_stops"), \
            patch.object(bot, "place_entry_order",
                         return_value=(order, 5)) as submit, \
            patch.object(bot, "_entry_fee_accounting",
                         return_value=(0.0, "dry_run")), \
            patch.object(
                bot, "_persist_entry_state",
                side_effect=lambda slot, state, save: persisted.append((slot, state))), \
            patch.object(bot, "_protect_or_flatten_entry", return_value=True), \
            patch.object(bot, "audit_event"), \
            patch.object(bot, "send_telegram"):
        result = bot._entry_job_locked()

    assert result["status"] == "opened"
    assert result["side"] == "buy"
    build.assert_called_once_with(
        contract, bot.LOTS, "buy", "evening", auto_context=context)
    assert submit.call_args.args[2] == "buy"
    assert persisted[0][1]["entry_trigger"] == "evening_auto_long"
    assert persisted[0][1]["move_auto_decision_id"] == "decision-1"


def test_morning_all_sideways_opens_short_and_records_strategy_trigger():
    contract = _contract()
    context = _context(SHORT_MOVE, "sell")
    context["strategy_override"] = {
        "kind": "morning_all_sideways_short",
        "applied": True,
    }
    plan = {
        "snapshot": {"bid": 99.0, "ask": 101.0},
        "lots": 5,
        "value_signal": {"automatic": True},
        "auto_context": context,
        "risk_at_entry_usd": 10.0,
        "risk_decision": {"allowed": True},
        "protection_config": {"sl_target_pnl": 10.0},
    }
    persisted = []
    order = {
        "result": {
            "id": 73,
            "average_fill_price": "99.5",
            "order_ids": [73],
            "client_order_id": "sideways-1",
            "client_order_ids": ["sideways-1"],
        },
    }
    with patch.object(bot, "MOVE_AUTO_ENTRY_MODE", "live"), \
            patch.object(bot, "DRY_RUN", True), \
            patch.object(bot, "load_morning_state", return_value=None), \
            patch.object(bot, "get_mv_contract", return_value=contract), \
            patch.object(bot, "build_move_auto_decision",
                         return_value=context), \
            patch.object(bot, "build_move_entry_plan",
                         return_value=plan) as build, \
            patch.object(bot, "cancel_product_stops"), \
            patch.object(bot, "place_entry_order",
                         return_value=(order, 5)) as submit, \
            patch.object(bot, "_entry_fee_accounting",
                         return_value=(0.0, "dry_run")), \
            patch.object(
                bot, "_persist_entry_state",
                side_effect=lambda slot, state, save: persisted.append(
                    (slot, state))), \
            patch.object(bot, "_protect_or_flatten_entry", return_value=True), \
            patch.object(bot, "audit_event"), \
            patch.object(bot, "send_telegram"):
        result = bot._morning_entry_job_locked()

    assert result["status"] == "opened"
    assert result["side"] == "sell"
    build.assert_called_once_with(
        contract, bot.MORNING_LOTS, "sell", "morning",
        auto_context=context)
    assert submit.call_args.args[2] == "sell"
    assert persisted[0][1]["entry_trigger"] == "morning_all_sideways_short"
    assert persisted[0][1]["move_strategy_override"]["applied"] is True


def test_execution_revalidation_blocks_when_direction_is_no_longer_actionable():
    context = _context()
    snapshot = {
        "bid": 99.0,
        "ask": 101.0,
        "bid_size": 10,
        "ask_size": 10,
        "quote_timestamp_ms": 1_800_000_000_000,
        "mark": 100.0,
    }
    with patch.object(
            bot, "_move_account_decision_snapshot",
            return_value={
                "current_position_qty": 0,
                "average_entry_price": 0,
                "available_margin": 1000,
                "liquidation_buffer": 1,
                "open_orders_count": 0,
            }), \
            patch.object(bot, "_move_strategy_config", return_value={}), \
            patch.object(
                bot, "evaluate_move_decision",
                return_value={"action": NO_TRADE, "side": None}), \
            patch.object(bot, "_persist_move_decision") as persist:
        with pytest.raises(RuntimeError, match="changed before execution"):
            bot._refresh_move_auto_context_for_execution(
                context, _contract(), "evening", 100, snapshot)

    persist.assert_not_called()


def test_execution_revalidation_rechecks_fresh_all_sideways_signal():
    context = _context(SHORT_MOVE, "sell")
    context["strategy_override"] = {
        "kind": "morning_all_sideways_short",
        "applied": True,
    }
    snapshot = {
        "bid": 99.0,
        "ask": 101.0,
        "bid_size": 10,
        "ask_size": 10,
        "quote_timestamp_ms": 1_800_000_000_000,
        "mark": 100.0,
    }
    with patch.object(
            bot, "_move_account_decision_snapshot",
            return_value={
                "current_position_qty": 0,
                "average_entry_price": 0,
                "available_margin": 1000,
                "liquidation_buffer": 1,
                "open_orders_count": 0,
            }), \
            patch.object(bot, "_move_strategy_config", return_value={}), \
            patch.object(
                bot, "evaluate_move_decision",
                return_value=_sideways_candidate_decision()), \
            patch.object(
                bot, "_load_morning_sideways_signal",
                return_value=_sideways_signal()), \
            patch.object(bot, "_persist_move_decision") as persist:
        refreshed = bot._refresh_move_auto_context_for_execution(
            context, _contract(), "morning", 100, snapshot)

    assert refreshed["decision"]["action"] == SHORT_MOVE
    assert refreshed["decision"]["side"] == "sell"
    assert refreshed["execution_revalidated"] is True
    persist.assert_called_once()
