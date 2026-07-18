import math

import pytest

from move_decision import (
    LONG_MOVE,
    MANAGE_EXISTING_POSITION,
    NO_TRADE,
    SHORT_MOVE,
    MoveInputError,
    aggregate_risk_lot_caps,
    evaluate_move_decision,
    forecast_move_distribution,
)


NOW_MS = 1_800_000_000_000


def _candles(days=8, interval_seconds=300, volatility=0.0015):
    bars = days * 24 * 60 * 60 // interval_seconds
    start = NOW_MS // 1000 - bars * interval_seconds - interval_seconds
    price = 64_000.0
    result = []
    for index in range(bars):
        # Deterministic alternating returns with slower volatility regimes.
        wave = math.sin(index / 17.0) + 0.45 * math.sin(index / 53.0)
        change = volatility * wave
        opened = price
        price = max(price * math.exp(change), 1.0)
        high = max(opened, price) * 1.0004
        low = min(opened, price) * 0.9996
        result.append({
            "time": start + index * interval_seconds,
            "open": opened,
            "high": high,
            "low": low,
            "close": price,
        })
    return result


def _snapshot():
    return {
        "timestamp": {"now_ms": NOW_MS},
        "contract": {
            "symbol": "MV-BTC-64000-190127",
            "strike": 64_000,
            "expiry_ts_ms": NOW_MS + 12 * 60 * 60 * 1000,
            "settlement_window_start_ts_ms": (
                NOW_MS + (12 * 60 - 30) * 60 * 1000),
            "settlement_window_end_ts_ms": NOW_MS + 12 * 60 * 60 * 1000,
            "contract_multiplier": 0.001,
            "tick_size": 0.1,
            "lot_size": 1,
            "min_order_size": 1,
            "max_position_size": 70_000,
        },
        "market": {
            "bid": 900,
            "ask": 920,
            "bid_size": 2_000,
            "ask_size": 2_000,
            "quote_timestamp_ms": NOW_MS - 500,
            "mark_price": 910,
        },
        "underlying": {
            "btc_index_price": 64_100,
            "current_30m_twap": 0,
            "fraction_of_final_twap_fixed": 0,
        },
        "forecast": {
            "expected_payoff_low": 1_050,
            "expected_payoff_mid": 1_100,
            "expected_payoff_high": 1_150,
            "payoff_p99": 2_500,
            "jump_event_score": 0.1,
            "model_timestamp_ms": NOW_MS - 1_000,
        },
        "costs": {
            "long_round_trip_cost_per_contract": 0.01,
            "short_round_trip_cost_per_contract": 0.01,
            "long_slippage_per_contract": 0.005,
            "short_slippage_per_contract": 0.005,
        },
        "account": {
            "current_position_qty": 0,
            "average_entry_price": 0,
            "available_margin": 1_000,
            "liquidation_buffer": 0.8,
            "open_orders_count": 0,
        },
        "exchange": {
            "system_operational": True,
            "product_operational": True,
            "trading_enabled": True,
        },
    }


def _config():
    return {
        "allow_long": True,
        "allow_short": True,
        "min_long_edge_absolute": 0.01,
        "min_short_edge_absolute": 0.02,
        "min_long_edge_pct": 0.05,
        "min_short_edge_pct": 0.10,
        "max_spread_pct": 0.03,
        "max_quote_age_ms": 3_000,
        "max_model_age_ms": 600_000,
        "min_bid_size": 1,
        "min_ask_size": 1,
        "max_jump_event_score_for_short": 0.30,
        "max_long_premium_risk": 200,
        "max_short_p99_loss": 200,
        "max_short_margin_usage": 0.30,
        "max_contracts": 1_000,
        "max_total_position": 1_000,
        "no_new_entry_seconds_before_settlement": 3_600,
        "max_open_loss": 200,
        "require_no_existing_orders": True,
        "require_flat_before_entry": True,
        "required_liquidation_buffer": 0.50,
    }


def test_distribution_forecast_is_deterministic_and_separates_fair_band_from_p99():
    kwargs = {
        "current_index_price": 64_100,
        "strike": 64_000,
        "now_ms": NOW_MS,
        "settlement_end_ts_ms": NOW_MS + 12 * 60 * 60 * 1000,
        "outer_scenarios": 8,
        "paths_per_scenario": 32,
        "minimum_history_days": 7,
    }
    first = forecast_move_distribution(_candles(), **kwargs)
    second = forecast_move_distribution(_candles(), **kwargs)

    assert first == second
    assert (
        0 <= first["expected_payoff_low"]
        <= first["expected_payoff_mid"]
        <= first["expected_payoff_high"]
        <= first["payoff_p99"]
    )
    assert first["simulation"]["total_paths"] == 256
    assert first["model_features"]["source_interval_seconds"] == 300
    assert first["event_score_available"] is False
    assert first["event_risk_source"] == "unknown_high_risk"
    assert first["jump_event_score"] == 1.0


def test_provided_event_score_can_make_short_risk_evaluable():
    forecast = forecast_move_distribution(
        _candles(),
        current_index_price=64_100,
        strike=64_000,
        now_ms=NOW_MS,
        settlement_end_ts_ms=NOW_MS + 12 * 60 * 60 * 1000,
        scheduled_event_score=0.12,
        outer_scenarios=8,
        paths_per_scenario=32,
        minimum_history_days=7,
    )
    assert forecast["event_score_available"] is True
    assert forecast["scheduled_event_score"] == 0.12
    assert forecast["jump_event_score"] >= forecast["market_jump_score"]
    assert forecast["jump_event_score"] < 1


def test_forecast_rejects_coarse_or_insufficient_source_data():
    with pytest.raises(MoveInputError, match="1m/5m"):
        forecast_move_distribution(
            _candles(interval_seconds=900),
            current_index_price=64_100,
            strike=64_000,
            now_ms=NOW_MS,
            settlement_end_ts_ms=NOW_MS + 12 * 60 * 60 * 1000,
            outer_scenarios=8,
            paths_per_scenario=32,
        )
    with pytest.raises(MoveInputError, match="completed bars"):
        forecast_move_distribution(
            _candles(days=2),
            current_index_price=64_100,
            strike=64_000,
            now_ms=NOW_MS,
            settlement_end_ts_ms=NOW_MS + 12 * 60 * 60 * 1000,
            outer_scenarios=8,
            paths_per_scenario=32,
        )


def test_long_decision_uses_conservative_low_forecast_and_executable_ask():
    decision = evaluate_move_decision(_snapshot(), _config())
    assert decision["action"] == LONG_MOVE
    assert decision["side"] == "buy"
    assert decision["long_signal"] is True
    assert decision["short_signal"] is False
    assert decision["metrics"]["long_edge_per_contract"] == pytest.approx(0.115)
    assert decision["gates"]["long"]["edge"] is True


def test_short_decision_uses_high_forecast_p99_event_and_margin_gates():
    snapshot = _snapshot()
    snapshot["market"].update(bid=1_300, ask=1_320)
    snapshot["forecast"].update(
        expected_payoff_low=900,
        expected_payoff_mid=950,
        expected_payoff_high=1_000,
        payoff_p99=2_000,
        jump_event_score=0.20,
    )
    decision = evaluate_move_decision(snapshot, _config())
    assert decision["action"] == SHORT_MOVE
    assert decision["short_signal"] is True
    assert decision["metrics"]["short_edge_per_contract"] == pytest.approx(0.285)
    assert decision["metrics"]["short_p99_loss_per_contract"] == pytest.approx(0.715)

    snapshot["forecast"]["jump_event_score"] = 1.0
    blocked = evaluate_move_decision(snapshot, _config())
    assert blocked["action"] == NO_TRADE
    assert "jump_event_risk" in blocked["failed_gates"]["short"]


@pytest.mark.parametrize(
    ("section", "field", "value", "failed"),
    [
        ("market", "quote_timestamp_ms", NOW_MS - 30_000, "quote_fresh"),
        ("forecast", "model_timestamp_ms", NOW_MS - 700_000, "model_fresh"),
        ("market", "ask", 1_100, "spread_within_limit"),
        ("account", "open_orders_count", 1, "no_existing_orders"),
        ("exchange", "trading_enabled", False, "trading_enabled"),
    ],
)
def test_common_gate_failures_are_no_trade(section, field, value, failed):
    snapshot = _snapshot()
    snapshot[section][field] = value
    result = evaluate_move_decision(snapshot, _config())
    assert result["action"] == NO_TRADE
    assert failed in result["failed_gates"]["common"]


def test_existing_position_is_managed_instead_of_opening_opposite_exposure():
    snapshot = _snapshot()
    snapshot["account"]["current_position_qty"] = 10
    decision = evaluate_move_decision(snapshot, _config())
    assert decision["action"] == MANAGE_EXISTING_POSITION
    assert decision["side"] is None


def test_conflicting_forecast_signals_fail_closed():
    snapshot = _snapshot()
    snapshot["market"].update(bid=1_000, ask=1_000)
    snapshot["forecast"].update(
        expected_payoff_low=1_200,
        expected_payoff_mid=1_250,
        expected_payoff_high=800,
        payoff_p99=2_000,
    )
    with pytest.raises(MoveInputError, match="low <= mid <= high"):
        evaluate_move_decision(snapshot, _config())


def test_aggregate_lot_caps_apply_long_premium_and_short_p99_margin():
    long_decision = evaluate_move_decision(_snapshot(), _config())
    long_caps = aggregate_risk_lot_caps(
        long_decision,
        _config(),
        available_margin=1_000,
        short_initial_margin_per_contract=1,
    )
    assert long_caps["effective"] == min(
        long_caps["position"], long_caps["premium_risk"])

    snapshot = _snapshot()
    snapshot["market"].update(bid=1_300, ask=1_320)
    snapshot["forecast"].update(
        expected_payoff_low=900,
        expected_payoff_mid=950,
        expected_payoff_high=1_000,
        payoff_p99=2_000,
        jump_event_score=0.2,
    )
    short_decision = evaluate_move_decision(snapshot, _config())
    short_caps = aggregate_risk_lot_caps(
        short_decision,
        _config(),
        available_margin=1_000,
        short_initial_margin_per_contract=0.50,
    )
    assert short_caps["margin"] == 600
    assert short_caps["effective"] == min(
        short_caps["position"], short_caps["p99_risk"], short_caps["margin"])
