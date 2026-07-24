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


def _forecast_history_5m(*, favourable=True, direction=1):
    start = NOW - timedelta(minutes=5 * 4_000)
    rows = []
    for index in range(4_000):
        opened = 20_000.0
        rows.append({
            "timestamp": (start + timedelta(minutes=5 * index)).isoformat(),
            "open": opened,
            "high": opened * (
                1.05 if favourable and direction > 0 else 1.01
            ),
            "low": opened * (
                0.95 if favourable and direction < 0 else 0.99
            ),
            "close": opened,
            "volume": 1_000,
            "complete": True,
        })
    return {
        "source": {
            "provider": "delta_exchange",
            "transport": "public_rest",
            "endpoint": "/v2/history/candles",
            "symbol": "BTCUSD",
            "resolution": "5m",
            "interval_seconds": 300,
            "completed_only": True,
        },
        "requested_limit": 4_000,
        "returned_count": len(rows),
        "first_timestamp": rows[0]["timestamp"],
        "last_timestamp": rows[-1]["timestamp"],
        "candles": rows,
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

    assert result["decision"] in ("NO_TRADE", "EXIT")
    assert result["directional_bias"] in ("NEUTRAL", "BEARISH")
    assert result["selected_contract"]["symbol"] is None
    assert result["trade_score"] is None


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


def test_unknown_event_calendar_can_be_explicitly_approved_without_claiming_clear():
    snapshot = _snapshot(1)
    snapshot["events"] = None
    snapshot["market"]["event_data_available"] = False

    result = evaluate_trend(snapshot, {"allow_unknown_event_risk": True})

    assert result["decision"] == "BUY_CE"
    assert result["hard_gates"]["event_pass"] is True
    assert result["audit"]["event_policy"] == {
        "event_data_available": False,
        "unknown_risk_override_applied": True,
        "known_blackout_detected": False,
        "known_blackout_override_applied": False,
    }


def test_known_event_blackout_still_blocks_unknown_risk_override():
    snapshot = _snapshot(1)
    snapshot["events"] = [{
        "timestamp": (NOW + timedelta(minutes=15)).isoformat(),
        "name": "KNOWN_EVENT",
        "prohibited": True,
    }]

    result = evaluate_trend(snapshot, {"allow_unknown_event_risk": True})

    assert result["decision"] == "NO_TRADE"
    assert result["hard_gates"]["event_pass"] is False
    assert "EVENT_BLACKOUT" in result["reason_codes"]


def test_known_blackout_is_audited_even_when_event_trading_is_overridden():
    snapshot = _snapshot(1)
    snapshot["events"] = [{
        "timestamp": (NOW + timedelta(minutes=15)).isoformat(),
        "name": "KNOWN_EVENT",
        "prohibited": True,
    }]

    result = evaluate_trend(snapshot, {"allow_event_trading": True})

    assert result["decision"] == "BUY_CE"
    assert result["audit"]["event_policy"]["known_blackout_detected"] is True
    assert result["audit"]["event_policy"][
        "known_blackout_override_applied"
    ] is True


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


def test_probability_free_history_supplies_auditable_conservative_edge():
    snapshot = _snapshot(1)
    snapshot["forecast"] = {
        "target_underlying": snapshot["market"]["spot"] + 700,
        "invalidation_level": snapshot["market"]["spot"] - 300,
        "holding_days": 1,
        "cost_adjusted_required_move": 10,
    }
    snapshot["option_contracts"][0].update(bid=49.5, ask=50)
    snapshot["forecast_history_5m"] = _forecast_history_5m(favourable=True)

    result = evaluate_trend(snapshot)

    assert result["decision"] == "BUY_CE"
    scenario = result["audit"]["scenario"]
    assert scenario["expected_value_method"] == (
        "btc_historical_replay_intrinsic_floor_v1"
    )
    assert scenario["pricing_method"] == "historical_replay_intrinsic_floor"
    assert scenario["probability_win"] is None
    assert scenario["probability_validated"] is False
    assert scenario["net_expected_value_per_lot"] > 0
    assert scenario["complete_day_count"] >= 7
    assert scenario["path_count"] >= 7
    assert len(scenario["history_hash"]) == 64
    assert scenario["scenario_evidence"]["source"]["provider"] == (
        "delta_exchange"
    )


def test_probability_free_history_with_nonpositive_p20_blocks_entry():
    snapshot = _snapshot(1)
    snapshot["forecast"] = {
        "target_underlying": snapshot["market"]["spot"] + 700,
        "invalidation_level": snapshot["market"]["spot"] - 300,
        "holding_days": 1,
        "cost_adjusted_required_move": 10,
    }
    snapshot["option_contracts"][0].update(bid=49.5, ask=50)
    snapshot["forecast_history_5m"] = _forecast_history_5m(favourable=False)

    result = evaluate_trend(snapshot)

    assert result["decision"] == "NO_TRADE"
    assert "NEGATIVE_EXPECTED_VALUE" in result["reason_codes"]
    assert result["audit"]["scenario"]["net_expected_value_per_lot"] < 0


def test_unvalidated_partial_forecast_cannot_disable_historical_replay():
    snapshot = _snapshot(1)
    snapshot["forecast"].update({
        "expected_iv_change": 0,
        "probability_win": 0.9,
        "probability_validated": False,
        "cost_adjusted_required_move": 10,
    })
    snapshot["option_contracts"][0].update(bid=49.5, ask=50)
    snapshot["forecast_history_5m"] = _forecast_history_5m(favourable=True)

    result = evaluate_trend(snapshot)

    assert result["decision"] == "BUY_CE"
    assert result["audit"]["scenario"]["pricing_method"] == (
        "historical_replay_intrinsic_floor"
    )
    assert result["audit"]["scenario"]["probability_win"] is None


def test_probability_free_pe_uses_exact_90_minute_contract_and_safe_exit():
    snapshot = _snapshot(-1)
    snapshot["forecast"] = {
        "target_underlying": snapshot["market"]["spot"] - 700,
        "invalidation_level": snapshot["market"]["spot"] + 300,
        "holding_days": 1,
        "cost_adjusted_required_move": 10,
    }
    snapshot["option_contracts"][1].update(
        bid=49.5,
        ask=50,
        expiry=(NOW + timedelta(hours=1, minutes=30)).isoformat(),
    )
    snapshot["forecast_history_5m"] = _forecast_history_5m(
        favourable=True, direction=-1,
    )

    result = evaluate_trend(snapshot)

    assert result["decision"] == "BUY_PE"
    assert result["selected_contract"]["time_to_expiry_hours"] == 1.5
    assert result["order_plan"]["time_exit"] == (
        NOW + timedelta(hours=1)
    ).isoformat().replace("+00:00", "Z")
    assert result["audit"]["scenario"]["probability_validated"] is False


def test_history_provenance_is_verified_before_it_can_supply_edge():
    snapshot = _snapshot(1)
    snapshot["forecast"] = {}
    snapshot["forecast_history_5m"] = _forecast_history_5m()
    snapshot["forecast_history_5m"]["source"]["provider"] = "unverified"

    result = evaluate_trend(snapshot)

    assert result["decision"] == "NO_TRADE"
    assert result["reason_codes"] == ["INVALID_OR_STALE_DATA"]
    assert "source.provider is not approved" in result["audit"][
        "validation_error"
    ]


def test_implicit_atr_target_and_same_explicit_target_score_identically():
    implicit_snapshot = _snapshot(1)
    implicit_snapshot["forecast"].pop("target_underlying")
    implicit = evaluate_trend(implicit_snapshot)
    resolved_target = implicit["order_plan"]["underlying_target"]

    explicit_snapshot = _snapshot(1)
    explicit_snapshot["forecast"]["target_underlying"] = resolved_target
    explicit = evaluate_trend(explicit_snapshot)

    assert implicit["selected_contract"]["contract_components"] == explicit[
        "selected_contract"
    ]["contract_components"]
    assert implicit["selected_contract"]["contract_score"] == explicit[
        "selected_contract"
    ]["contract_score"]


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
        (NOW + timedelta(days=15), "RISK_CALCULATION_FAILED"),
    ],
)
def test_entry_time_exit_must_be_future_dated_and_before_expiry(time_exit, reason):
    snapshot = _snapshot(1)
    snapshot["forecast"]["time_exit"] = time_exit.isoformat()

    result = evaluate_trend(snapshot)

    assert result["decision"] == "NO_TRADE"
    assert reason in result["reason_codes"]


@pytest.mark.parametrize(
    ("time_to_expiry", "eligible"),
    [
        (timedelta(hours=1, minutes=29, seconds=59), False),
        (timedelta(hours=1, minutes=30), True),
        (timedelta(days=45), True),
    ],
)
def test_daily_btc_expiry_uses_exact_90_minute_floor_without_maximum_dte(
        time_to_expiry, eligible):
    snapshot = _snapshot(1)
    snapshot["option_contracts"][0]["expiry"] = (
        NOW + time_to_expiry
    ).isoformat()

    result = evaluate_trend(snapshot)

    assert (result["decision"] == "BUY_CE") is eligible
    if eligible:
        assert result["selected_contract"]["time_to_expiry_hours"] == pytest.approx(
            time_to_expiry.total_seconds() / 3600.0, abs=1e-4
        )
    else:
        assert "EXPIRY_RESTRICTION" in result["reason_codes"]


def test_same_day_contract_exit_is_capped_before_settlement_buffer():
    snapshot = _snapshot(1)
    snapshot["option_contracts"][0]["expiry"] = (
        NOW + timedelta(hours=1, minutes=30)
    ).isoformat()

    result = evaluate_trend(snapshot)

    assert result["decision"] == "BUY_CE"
    assert result["order_plan"]["time_exit"] == (
        NOW + timedelta(hours=1)
    ).isoformat().replace("+00:00", "Z")
    assert result["audit"]["scenario"]["effective_holding_hours"] == pytest.approx(1)


def test_default_time_exit_is_anchored_to_closed_signal_not_wall_clock():
    first_snapshot = _snapshot(1)
    first = evaluate_trend(first_snapshot)
    second_snapshot = deepcopy(first_snapshot)
    second_now = NOW + timedelta(seconds=1)
    fresh_quote = (second_now - timedelta(seconds=5)).isoformat()
    second_snapshot["timestamp"] = second_now.isoformat()
    for key in (
        "market_data_timestamp", "spot_timestamp", "futures_timestamp",
        "option_chain_timestamp",
    ):
        second_snapshot["market"][key] = fresh_quote
    for contract in second_snapshot["option_contracts"]:
        contract["quote_timestamp"] = fresh_quote

    second = evaluate_trend(second_snapshot)

    assert first["decision"] == second["decision"] == "BUY_CE"
    assert first["order_plan"]["time_exit"] == second["order_plan"]["time_exit"]
    assert first["selected_contract"]["time_to_expiry_hours"] != second[
        "selected_contract"
    ]["time_to_expiry_hours"]


def test_explicit_exit_inside_settlement_buffer_is_capped_safely():
    snapshot = _snapshot(1)
    expiry = NOW + timedelta(hours=4)
    snapshot["option_contracts"][0]["expiry"] = expiry.isoformat()
    snapshot["forecast"]["time_exit"] = (
        expiry - timedelta(minutes=29)
    ).isoformat()

    result = evaluate_trend(snapshot)

    assert result["decision"] == "BUY_CE"
    assert result["order_plan"]["time_exit"] == (
        expiry - timedelta(minutes=30)
    ).isoformat().replace("+00:00", "Z")
    assert result["hard_gates"]["expiry_pass"] is True


def test_no_window_before_configured_settlement_buffer_fails_expiry_gate():
    snapshot = _snapshot(1)
    snapshot["option_contracts"][0]["expiry"] = (
        NOW + timedelta(hours=1, minutes=30)
    ).isoformat()

    result = evaluate_trend(snapshot, {
        "settlement_exit_buffer_minutes": 120,
    })

    assert result["decision"] == "NO_TRADE"
    assert "EXPIRY_RESTRICTION" in result["reason_codes"]
    assert result["hard_gates"]["expiry_pass"] is False


def test_expiry_and_scenario_configuration_boundaries_fail_closed():
    snapshot = _snapshot(1)
    for overrides in (
        {"min_time_to_expiry_hours": 1.49},
        {"settlement_exit_buffer_minutes": 0},
        {"scenario_lower_quantile": 0.5},
    ):
        result = evaluate_trend(snapshot, overrides)
        assert result["decision"] == "NO_TRADE"
        assert result["reason_codes"] == ["INVALID_OR_STALE_DATA"]


def test_nondefault_scenario_quantile_has_generic_audit_fields():
    snapshot = _snapshot(1)
    snapshot["forecast"] = {
        "target_underlying": snapshot["market"]["spot"] + 700,
        "invalidation_level": snapshot["market"]["spot"] - 300,
        "holding_days": 1,
        "cost_adjusted_required_move": 10,
    }
    snapshot["option_contracts"][0].update(bid=49.5, ask=50)
    snapshot["forecast_history_5m"] = _forecast_history_5m(favourable=True)

    result = evaluate_trend(snapshot, {"scenario_lower_quantile": 0.3})

    scenario = result["audit"]["scenario"]
    assert scenario["scenario_lower_quantile"] == 0.3
    assert scenario["net_edge_lower_quantile"] is not None
    assert scenario["net_edge_median"] is not None
    assert scenario["net_edge_upper_quantile"] is not None


@pytest.mark.parametrize("pricing_mode", ["calibrated", "historical_replay"])
def test_entry_risk_plan_is_stable_across_one_second_clock_advance(
        pricing_mode):
    snapshot = _snapshot(1)
    if pricing_mode == "historical_replay":
        spot = snapshot["market"]["spot"]
        snapshot["forecast"] = {
            "target_underlying": spot + 700,
            "invalidation_level": spot - 300,
            "holding_days": 1,
            "cost_adjusted_required_move": 10,
        }
        snapshot["option_contracts"][0].update(bid=49.5, ask=50)
        snapshot["forecast_history_5m"] = _forecast_history_5m(
            favourable=True
        )

    advanced = deepcopy(snapshot)
    advanced["timestamp"] = (NOW + timedelta(seconds=1)).isoformat()

    first = evaluate_trend(snapshot)
    second = evaluate_trend(advanced)

    assert first["decision"] == second["decision"] == "BUY_CE"
    assert first["order_plan"] == second["order_plan"]
    assert (
        (first["audit"]["scenario"].get("scenario_evidence") or {}).get(
            "horizon_bars"
        )
        == (second["audit"]["scenario"].get("scenario_evidence") or {}).get(
            "horizon_bars"
        )
    )

    # Exercise the same binding used by DRY RUN preview/apply confirmation.
    import dashboard

    assert dashboard._trend_engine_risk_plan_fingerprint(
        first, snapshot["option_contracts"][0]
    ) == dashboard._trend_engine_risk_plan_fingerprint(
        second, advanced["option_contracts"][0]
    )


def test_replay_gap_loss_drives_sizing_and_reported_maximum_loss():
    snapshot = _snapshot(1)
    spot = snapshot["market"]["spot"]
    snapshot["forecast"] = {
        "target_underlying": spot + 700,
        "invalidation_level": spot - 300,
        "holding_days": 1,
        "cost_adjusted_required_move": 10,
    }
    contract = snapshot["option_contracts"][0]
    contract.update({
        "strike": round(spot / 100) * 100 - 1_000,
        "bid": 1_095,
        "ask": 1_100,
        "expiry": (NOW + timedelta(hours=1, minutes=30)).isoformat(),
    })
    history = _forecast_history_5m(favourable=True)
    # One path per UTC day opens through invalidation on its second bar; the
    # other 23 one-hour paths hit target.  The lower-day edge stays positive,
    # while worst-path loss is larger than an at-invalidation fill.
    for row in history["candles"]:
        stamp = datetime.fromisoformat(row["timestamp"])
        bar_of_day = (stamp.hour * 60 + stamp.minute) // 5
        if bar_of_day == 0:
            row.update(open=20_000, high=20_200, low=19_800, close=20_000)
        elif bar_of_day == 1:
            row.update(open=18_000, high=18_100, low=17_900, close=18_000)
    snapshot["forecast_history_5m"] = history

    result = evaluate_trend(snapshot, {"min_reward_risk": 0})

    scenario = result["audit"]["scenario"]
    assert scenario["expected_value_pass"] is True
    assert scenario["maximum_loss_basis"] == "worst_historical_replay_path"
    stop_fill_loss = (
        (scenario["entry"] - scenario["scenario_evidence"][
            "invalidation_option_intrinsic"
        ]) * contract["contract_value"]
        + scenario["costs_per_lot"]
    )
    assert scenario["net_loss_per_lot"] > stop_fill_loss
    assert result["order_plan"]["maximum_estimated_loss"] == pytest.approx(
        scenario["net_loss_per_lot"] * result["order_plan"]["quantity_lots"]
    )


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


def test_phase1_position_recalculates_remaining_edge_from_current_bid():
    snapshot = _snapshot(1)
    spot = snapshot["market"]["spot"]
    snapshot["option_contracts"][0].update(bid=49.5, ask=50)
    snapshot["forecast_history_5m"] = _forecast_history_5m(favourable=True)
    snapshot["positions"] = [{
        "symbol": "C-BTC-ATM",
        "option_type": "CE",
        "side": "long",
        "quantity_lots": 100,
        "entry_price": 45,
        "current_price": 49.5,
        "underlying_invalidation": spot - 300,
        "underlying_target": spot + 700,
        "stop_option_price": 1,
        "target_option_price": 500,
        "time_exit": (NOW + timedelta(hours=6)).isoformat(),
        "remaining_expected_value": 0,
        "entry_trigger": "trend_engine_phase1_confirmed",
    }]

    result = evaluate_trend(snapshot)

    assert result["decision"] == "HOLD"
    recalculation = result["audit"]["remaining_edge_recalculation"]
    assert recalculation["valid"] is True
    assert recalculation["net_expected_value_per_lot"] > 0
    assert recalculation["probability_win"] is None
    assert "from_current_bid" in recalculation["edge_semantics"]


def test_phase1_position_exits_when_recalculated_remaining_edge_is_negative():
    snapshot = _snapshot(1)
    spot = snapshot["market"]["spot"]
    snapshot["option_contracts"][0].update(bid=49.5, ask=50)
    snapshot["forecast_history_5m"] = _forecast_history_5m(favourable=False)
    snapshot["positions"] = [{
        "symbol": "C-BTC-ATM",
        "option_type": "CE",
        "side": "long",
        "quantity_lots": 100,
        "entry_price": 45,
        "current_price": 49.5,
        "underlying_invalidation": spot - 300,
        "underlying_target": spot + 700,
        "stop_option_price": 1,
        "target_option_price": 500,
        "time_exit": (NOW + timedelta(hours=6)).isoformat(),
        "remaining_expected_value": 25,
        "entry_trigger": "trend_engine_phase1_confirmed",
    }]

    result = evaluate_trend(snapshot)

    assert result["decision"] == "EXIT"
    assert "NEGATIVE_EXPECTED_VALUE" in result["reason_codes"]
    assert result["audit"]["remaining_edge_recalculation"][
        "net_expected_value_per_lot"
    ] < 0


def test_incomplete_existing_position_explains_exact_missing_trade_plan_fields():
    snapshot = _snapshot(1)
    snapshot["positions"] = [{
        "source": "dry_run",
        "slot": "trend",
        "symbol": "C-BTC-LEGACY",
        "option_type": "CE",
        "quantity_lots": 1_000,
        "entry_price": 450,
    }]

    result = evaluate_trend(snapshot)

    assert result["decision"] in ("EXIT", "HOLD")
    assert result["order_plan"]["quantity_lots"] == 0
    assert "position_context" in result["audit"]
    assert result["audit"]["position_context"] == {
        "symbol": "C-BTC-LEGACY",
        "slot": "trend",
        "source": "dry_run",
    }
    assert "underlying_core_score" in result["audit"]
    assert "features" in result["audit"]
    assert "config" in result["audit"]


def test_invalid_existing_position_value_keeps_specific_audit_context():
    snapshot = _snapshot(1)
    spot = snapshot["market"]["spot"]
    snapshot["positions"] = [{
        "source": "dry_run",
        "slot": "trend",
        "symbol": "C-BTC-BAD-TIME",
        "option_type": "CE",
        "quantity_lots": 100,
        "entry_price": 450,
        "current_price": 510,
        "underlying_invalidation": spot - 300,
        "stop_option_price": 400,
        "target_option_price": 700,
        "time_exit": "tomorrow afternoon",
        "remaining_expected_value": 25,
    }]

    result = evaluate_trend(snapshot)

    assert result["decision"] in ("EXIT", "HOLD")
    assert "position_context" in result["audit"]
    assert result["audit"]["position_context"]["symbol"] == "C-BTC-BAD-TIME"


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
    assert first["model_version"] == "trend-engine-1.1.0"
