import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from trend_engine import _volume_vwap, evaluate_trend, evaluate_trend_json


NOW = datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)


def _candles(timeframe, direction=1, *, flat=False):
    minutes = {"5m": 5, "15m": 15, "60m": 60}[timeframe]
    start = NOW - timedelta(minutes=minutes * 60)
    price = 20_000.0
    rows = []
    for index in range(60):
        opened = price
        if flat:
            change = (1 if index % 2 == 0 else -1) * 0.00005
        else:
            change = direction * 0.0015
        price *= 1 + change
        rows.append({
            "timestamp": (start + timedelta(minutes=minutes * index)).isoformat(),
            "open": opened,
            "high": max(opened, price) + 1,
            "low": min(opened, price) - 1,
            "close": price,
            "volume": 1_000 + index * 20,
            "complete": True,
        })
    return rows


def _contract(symbol, option_type, delta, spot, *, spread=5, iv_percentile=30,
              contract_value=0.001):
    timestamp = (NOW - timedelta(seconds=5)).isoformat()
    return {
        "symbol": symbol,
        "option_type": option_type,
        "strike": round(spot / 100) * 100,
        "expiry": (NOW + timedelta(days=14)).isoformat(),
        "quote_timestamp": timestamp,
        "bid": 500 - spread,
        "ask": 500,
        "volume": 10_000,
        "open_interest": 20_000,
        "implied_volatility": 50,
        "iv_percentile": iv_percentile,
        "delta": delta,
        "theta": -2,
        "vega": 3,
        "lot_size": 1,
        "contract_value": contract_value,
        "bid_quantity": 100_000,
        "ask_quantity": 100_000,
        "tick_size": 0.5,
    }


def _snapshot(direction=1, *, flat=False):
    candles = {
        timeframe: _candles(timeframe, direction, flat=flat)
        for timeframe in ("5m", "15m", "60m")
    }
    spot = candles["5m"][-1]["close"]
    timestamp = (NOW - timedelta(seconds=5)).isoformat()
    target = spot + direction * 700
    invalidation = spot - direction * 300
    return {
        "timestamp": NOW.isoformat(),
        "underlying": "BTC",
        "market": {
            "market_data_timestamp": timestamp,
            "spot": spot,
            "spot_timestamp": timestamp,
            "futures_timestamp": timestamp,
            "option_chain_timestamp": timestamp,
            "event_data_available": True,
            "breadth": {
                "reliable": True,
                "advance_decline_ratio": 1.8 if direction > 0 else 0.5,
                "pct_above_20ema": 70 if direction > 0 else 30,
                "new_highs": 50 if direction > 0 else 5,
                "new_lows": 5 if direction > 0 else 50,
                "equal_weight_return": direction * 1.2,
                "cap_weight_return": direction * 0.8,
                "sector_return": direction * 1.0,
            },
            "derivatives": {
                "reliable": True,
                "futures_price_change_pct": direction * 1.0,
                "futures_oi_change_pct": 2.0,
                "futures_basis_pct": direction * 0.2,
                "put_call_ratio": 1.2 if direction > 0 else 0.7,
                "put_oi_change_pct": 8 if direction > 0 else 2,
                "call_oi_change_pct": 2 if direction > 0 else 8,
                "call_put_skew": direction * 0.1,
            },
        },
        "candles": candles,
        # The opposite contract has a better IV percentile.  It still must not
        # be considered until the underlying has selected an option type.
        "option_contracts": [
            _contract("C-BTC-ATM", "CE", 0.55, spot, iv_percentile=30),
            _contract("P-BTC-ATM", "PE", -0.55, spot, iv_percentile=5),
        ],
        "account": {
            "equity": 100_000,
            "available_funds": 10_000,
            "daily_pnl": 0,
            "consecutive_losses": 0,
            "current_exposure": 0,
        },
        "risk": {
            "kill_switch_active": False,
            "broker_connected": True,
            "exchange_operational": True,
            "position_state_consistent": True,
            "orders_state_known": True,
            "account_risk_state_known": True,
            "estimated_costs_per_lot": 0.005,
            "estimated_slippage_per_lot": 0.005,
        },
        "positions": [],
        "pending_orders": [],
        "events": [],
        "forecast": {
            "target_underlying": target,
            "invalidation_level": invalidation,
            "holding_days": 1,
            "expected_iv_change": 0,
            "probability_win": 0.70,
            "probability_validated": True,
            "cost_adjusted_required_move": 100,
        },
    }


def test_bullish_direction_selects_ce_only_after_direction_and_sizes_delta_lots():
    snapshot = _snapshot(1)
    result = evaluate_trend(snapshot)

    assert result["decision"] == "BUY_CE"
    assert result["directional_bias"] == "BULLISH"
    assert result["direction_score"] >= 40
    assert result["direction_components"]["price_action"] >= 10
    assert result["selected_contract"]["symbol"] == "C-BTC-ATM"
    assert result["selected_contract"]["option_type"] == "CE"
    assert all(result["hard_gates"].values())
    assert result["reason_codes"] == ["ALL_ENTRY_GATES_PASSED"]

    plan = result["order_plan"]
    assert plan["quantity_lots"] > 0
    # Delta BTC monetary risk uses premium points * 0.001 contract value,
    # never lot_size as a monetary multiplier.
    loss_per_lot = (
        (plan["entry_price"] - plan["stop_option_price"]) * 0.001 + 0.01
    )
    risk_cap = 100_000 * 0.005
    assert plan["maximum_estimated_loss"] <= risk_cap + 1e-8
    assert plan["quantity_lots"] == int(risk_cap // loss_per_lot)


def test_bearish_direction_selects_pe_and_scores_symmetrically():
    result = evaluate_trend(_snapshot(-1))

    assert result["decision"] == "BUY_PE"
    assert result["directional_bias"] == "BEARISH"
    assert result["direction_score"] <= -40
    assert result["direction_components"]["price_action"] <= -10
    assert result["selected_contract"]["option_type"] == "PE"


def test_neutral_underlying_does_not_rank_an_attractive_option():
    snapshot = _snapshot(1, flat=True)
    # Remove confirmatory data so derivatives cannot manufacture direction.
    snapshot["market"].pop("breadth")
    snapshot["market"].pop("derivatives")
    result = evaluate_trend(snapshot)

    assert result["decision"] == "NO_TRADE"
    assert result["directional_bias"] == "NEUTRAL"
    assert result["selected_contract"]["symbol"] is None
    assert result["trade_score"] is None
    assert "DIRECTION_SCORE_TOO_LOW" in result["reason_codes"]


def test_low_volume_breakdown_penalizes_bearish_confirmation_symmetrically():
    candles = _candles("5m", -1)
    candles[-1]["volume"] = 1
    score, features = _volume_vwap(candles, {
        "vwap": candles[-1]["close"],
        "breakout": False,
        "breakdown": True,
    })

    assert features["relative_volume"] < 1.2
    assert score == 2


def test_unknown_event_calendar_preserves_scores_but_blocks_entry():
    snapshot = _snapshot(1)
    snapshot["events"] = None
    snapshot["market"]["event_data_available"] = False
    result = evaluate_trend(snapshot)

    assert result["hard_gates"]["data_valid"] is True
    assert result["direction_score"] >= 40
    assert result["hard_gates"]["event_pass"] is False
    assert result["decision"] == "NO_TRADE"
    assert "EVENT_DATA_UNAVAILABLE" in result["reason_codes"]
    assert "INVALID_OR_STALE_DATA" not in result["reason_codes"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda snapshot: snapshot["market"].update(
            market_data_timestamp=(NOW - timedelta(minutes=10)).isoformat()),
        lambda snapshot: snapshot["option_contracts"][0].update(bid=510, ask=500),
        lambda snapshot: snapshot["candles"]["5m"][-1].update(complete=False),
        lambda snapshot: snapshot["account"].pop("equity"),
        lambda snapshot: snapshot["risk"].pop("orders_state_known"),
        lambda snapshot: snapshot["option_contracts"][0].pop("contract_value"),
        lambda snapshot: snapshot["option_contracts"][0].update(
            evaluated_ask="not-a-number"
        ),
        lambda snapshot: snapshot["option_contracts"][0].update(
            expected_exit_price="not-a-number"
        ),
    ],
)
def test_required_data_errors_fail_closed_with_full_schema(mutation):
    snapshot = _snapshot(1)
    mutation(snapshot)
    result = evaluate_trend(snapshot)

    assert result["decision"] == "NO_TRADE"
    assert result["reason_codes"] == ["INVALID_OR_STALE_DATA"]
    assert result["hard_gates"]["data_valid"] is False
    assert set(result["selected_contract"]["contract_components"]) == {
        "liquidity", "spread", "delta", "expiry", "iv_value",
        "breakeven", "theta_efficiency",
    }


def test_spread_hard_gate_cannot_be_overridden_by_direction_score():
    snapshot = _snapshot(1)
    snapshot["option_contracts"][0].update(bid=400, ask=500)
    result = evaluate_trend(snapshot)

    assert result["direction_score"] >= 40
    assert result["decision"] == "NO_TRADE"
    assert "SPREAD_TOO_WIDE" in result["reason_codes"]
    assert "NO_ELIGIBLE_CONTRACT" in result["reason_codes"]


def test_unvalidated_probability_is_reported_not_invented():
    snapshot = _snapshot(1)
    snapshot["forecast"]["probability_validated"] = False
    result = evaluate_trend(snapshot)

    assert result["decision"] == "NO_TRADE"
    assert "EXPECTED_VALUE_UNAVAILABLE" in result["reason_codes"]
    assert result["hard_gates"]["expected_value_pass"] is False
    assert result["audit"]["scenario"]["probability_win"] is None
    assert result["audit"]["scenario"]["net_expected_value_per_lot"] is None


def test_position_size_respects_visible_depth_and_exchange_order_limit():
    snapshot = _snapshot(1)
    snapshot["option_contracts"][0]["ask_quantity"] = 7
    snapshot["option_contracts"][0]["max_order_lots"] = 5

    result = evaluate_trend(snapshot)

    assert result["decision"] == "BUY_CE"
    assert result["order_plan"]["quantity_lots"] == 5
    assert result["audit"]["scenario"]["lots_by_depth"] == 7


def test_contract_ranking_selects_highest_scoring_candidate_that_passes_edge():
    snapshot = _snapshot(1)
    spot = snapshot["market"]["spot"]
    high_score_bad_edge = snapshot["option_contracts"][0]
    high_score_bad_edge.update(
        expected_exit_price=450,
        stop_option_price=400,
        neutral_exit_price=480,
    )
    lower_score_good_edge = _contract(
        "C-BTC-GOOD-EDGE", "CE", 0.50, spot, iv_percentile=65
    )
    lower_score_good_edge.update(
        volume=5_000,
        open_interest=10_000,
        expected_exit_price=850,
        stop_option_price=400,
        neutral_exit_price=490,
    )
    snapshot["option_contracts"].append(lower_score_good_edge)

    result = evaluate_trend(snapshot)

    assert result["decision"] == "BUY_CE"
    assert result["selected_contract"]["symbol"] == "C-BTC-GOOD-EDGE"
    rankings = result["audit"]["contract_rankings"]
    assert rankings[0]["symbol"] == "C-BTC-ATM"
    assert "NEGATIVE_EXPECTED_VALUE" in rankings[0]["entry_reasons"]


@pytest.mark.parametrize(
    ("time_exit", "reason"),
    [
        (NOW - timedelta(minutes=1), "RISK_CALCULATION_FAILED"),
        (NOW + timedelta(days=15), "EXPIRY_RESTRICTION"),
    ],
)
def test_entry_time_exit_must_be_future_dated_and_before_expiry(time_exit, reason):
    snapshot = _snapshot(1)
    snapshot["forecast"]["time_exit"] = time_exit.isoformat()

    result = evaluate_trend(snapshot)

    assert result["decision"] == "NO_TRADE"
    assert reason in result["reason_codes"]


def test_daily_loss_and_kill_switch_are_hard_portfolio_gates():
    snapshot = _snapshot(1)
    snapshot["account"]["daily_pnl"] = -1_500
    snapshot["risk"]["kill_switch_active"] = True
    result = evaluate_trend(snapshot)

    assert result["decision"] == "NO_TRADE"
    assert result["hard_gates"]["portfolio_risk_pass"] is False
    assert "DAILY_LOSS_LIMIT" in result["reason_codes"]
    assert "KILL_SWITCH_ACTIVE" in result["reason_codes"]


def test_unknown_account_risk_state_blocks_new_entry_without_hiding_direction():
    snapshot = _snapshot(1)
    snapshot["risk"]["account_risk_state_known"] = False

    result = evaluate_trend(snapshot)

    assert result["decision"] == "NO_TRADE"
    assert result["directional_bias"] == "BULLISH"
    assert result["hard_gates"]["data_valid"] is True
    assert result["hard_gates"]["portfolio_risk_pass"] is False
    assert "ACCOUNT_RISK_STATE_UNKNOWN" in result["reason_codes"]


def test_existing_position_hold_and_mandatory_exit_are_independent_of_entry():
    snapshot = _snapshot(1)
    spot = snapshot["market"]["spot"]
    snapshot["positions"] = [{
        "symbol": "C-BTC-ATM",
        "option_type": "CE",
        "quantity_lots": 100,
        "entry_price": 450,
        "current_price": 510,
        "underlying_invalidation": spot - 300,
        "stop_option_price": 400,
        "target_option_price": 700,
        "time_exit": (NOW + timedelta(hours=6)).isoformat(),
        "remaining_expected_value": 25,
    }]
    held = evaluate_trend(snapshot)
    assert held["decision"] == "HOLD"
    assert held["directional_bias"] == "BULLISH"
    assert held["reason_codes"] == ["EXISTING_POSITION_THESIS_VALID"]

    snapshot["positions"][0]["current_price"] = 390
    exited = evaluate_trend(snapshot)
    assert exited["decision"] == "EXIT"
    assert "EMERGENCY_OPTION_STOP_REACHED" in exited["reason_codes"]


def test_short_option_position_is_never_returned_as_hold():
    snapshot = _snapshot(1)
    spot = snapshot["market"]["spot"]
    snapshot["positions"] = [{
        "symbol": "C-BTC-SHORT",
        "option_type": "CE",
        "side": "short",
        "quantity_lots": 10,
        "entry_price": 500,
        "current_price": 450,
        "underlying_invalidation": spot - 300,
        "stop_option_price": 600,
        "target_option_price": 300,
        "time_exit": (NOW + timedelta(hours=6)).isoformat(),
        "remaining_expected_value": 10,
    }]

    result = evaluate_trend(snapshot)

    assert result["decision"] == "EXIT"
    assert result["reason_codes"] == ["NAKED_OPTION_SELLING_PROHIBITED"]


def test_decision_and_json_are_reproducible_and_machine_readable():
    snapshot = _snapshot(1)
    first = evaluate_trend(snapshot)
    second = evaluate_trend(deepcopy(snapshot))

    assert first == second
    encoded = evaluate_trend_json(snapshot)
    assert json.loads(encoded) == first
    assert first["decision_id"].startswith("trend-")
    assert first["schema_version"] == "1.0"
    assert first["model_version"] == "trend-engine-1.0.0"
