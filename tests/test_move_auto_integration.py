from unittest.mock import Mock, patch

import pytest

import Delta_Straddle_Live as bot
from move_decision import LONG_MOVE, NO_TRADE


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
