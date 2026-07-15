import unittest
from dataclasses import replace
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from Delta_Backtest_MV_2026 import (
    MoveBacktestConfig,
    MoveQuoteBook,
    analyze_backtest,
    calculate_option_fee,
    expected_absolute_normal,
    move_value_signal,
    performance_metrics,
    risk_sized_lots,
    run_backtest,
    walk_forward_analysis,
)


START = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp())


def candle_frame(days=6, seed=4):
    rng = np.random.default_rng(seed)
    count = days * 96
    close = 45_000 + np.cumsum(rng.normal(0, 45, count))
    return pd.DataFrame(
        {
            "timestamp": START + np.arange(count) * 900,
            "open": close,
            "high": close + rng.uniform(20, 60, count),
            "low": close - rng.uniform(20, 60, count),
            "close": close,
            "volume": 1.0,
        }
    )


def permissive_config(**changes):
    config = MoveBacktestConfig(
        value_filter_enabled=False,
        starting_balance_usd=100_000,
        validation_min_trades=1,
        fallback_liquidity_lots=5_000,
        **changes,
    )
    return replace(
        config,
        morning=replace(
            config.morning, configured_lots=100, risk_budget_usd=2_000
        ),
        evening=replace(
            config.evening, configured_lots=100, risk_budget_usd=2_000
        ),
    )


class MoveValueModelTests(unittest.TestCase):
    def test_displacement_is_part_of_expected_absolute_payoff(self):
        self.assertGreaterEqual(expected_absolute_normal(2_000, 500), 2_000)
        self.assertAlmostEqual(expected_absolute_normal(-2_000, 0), 2_000)

    def test_value_gate_supports_long_and_short_edges(self):
        data = candle_frame(days=2)
        cfg = replace(
            MoveBacktestConfig(),
            min_edge_pct=5,
            vol_lookback=96,
        )
        probe = move_value_signal(
            data.tail(96), 46_000, 45_000, 12 * 3600, 1.0, "buy", cfg
        )
        forecast = float(probe["forecast_abs_move"])
        long_signal = move_value_signal(
            data.tail(96), 46_000, 45_000, 12 * 3600, forecast * 0.90, "buy", cfg
        )
        short_signal = move_value_signal(
            data.tail(96), 46_000, 45_000, 12 * 3600, forecast * 1.10, "sell", cfg
        )

        self.assertTrue(long_signal["passed"])
        self.assertTrue(short_signal["passed"])
        self.assertEqual(long_signal["current_displacement"], 1_000)
        self.assertGreater(long_signal["edge_pct"], 5)
        self.assertGreater(short_signal["edge_pct"], 5)

        asymmetric = replace(cfg, long_min_edge_pct=20, short_min_edge_pct=5)
        self.assertFalse(
            move_value_signal(
                data.tail(96), 46_000, 45_000, 12 * 3600,
                forecast * 0.90, "buy", asymmetric,
            )["passed"]
        )
        self.assertTrue(
            move_value_signal(
                data.tail(96), 46_000, 45_000, 12 * 3600,
                forecast * 1.10, "sell", asymmetric,
            )["passed"]
        )

    def test_tte_and_completed_history_are_fail_closed(self):
        cfg = MoveBacktestConfig()
        with self.assertRaisesRegex(ValueError, "minimum TTE"):
            move_value_signal(
                candle_frame(days=1).tail(96), 45_000, 45_000, 60, 100, "buy", cfg
            )
        with self.assertRaisesRegex(ValueError, "not enough completed"):
            move_value_signal(
                candle_frame(days=1).head(20), 45_000, 45_000,
                12 * 3600, 100, "buy", cfg,
            )


class MoveSizingTests(unittest.TestCase):
    def test_current_fee_model_uses_notional_rate_with_premium_cap(self):
        config = MoveBacktestConfig(
            contract_value=0.001,
            option_fee_rate=0.00010,
            option_fee_cap_pct=0.035,
        )
        # Notional fee is $4.50/BTC; premium cap is $3.50/BTC.
        self.assertAlmostEqual(calculate_option_fee(45_000, 100, 1_000, config), 3.5)

    def test_lots_are_minimum_of_all_caps_and_risk_capital(self):
        lots = risk_sized_lots(
            configured=1_000,
            affordable=800,
            liquidity_cap=600,
            max_order_lots=5_000,
            risk_budget_usd=200,
            stop_loss_usd=0,
            premium_per_lot=0.40,
            round_trip_fee_per_lot=0.01,
            slippage_per_lot=0.01,
        )
        self.assertEqual(lots, 476)

    def test_short_requires_an_explicit_stop(self):
        common = dict(
            configured=100,
            affordable=100,
            liquidity_cap=100,
            max_order_lots=5_000,
            risk_budget_usd=200,
            premium_per_lot=0.5,
            round_trip_fee_per_lot=0.01,
            slippage_per_lot=0.01,
            short=True,
        )
        self.assertEqual(risk_sized_lots(stop_loss_usd=0, **common), 0)
        self.assertGreater(risk_sized_lots(stop_loss_usd=50, **common), 0)


class ScheduledReplayTests(unittest.TestCase):
    def test_morning_and_evening_use_the_same_fixed_strike_for_same_expiry(self):
        trades = run_backtest(candle_frame(days=6), permissive_config())

        self.assertIn("morning", set(trades["slot"]))
        self.assertIn("evening", set(trades["slot"]))
        evening = trades.loc[
            (trades["date"] == "2024-01-01") & (trades["slot"] == "evening")
        ].iloc[0]
        morning = trades.loc[
            (trades["date"] == "2024-01-02") & (trades["slot"] == "morning")
        ].iloc[0]
        self.assertEqual(evening["settlement_timestamp"], morning["settlement_timestamp"])
        self.assertEqual(evening["strike"], morning["strike"])
        self.assertEqual(evening["pricing_source"], "black_scholes_fallback")
        self.assertNotEqual(morning["current_displacement"], 0)

    def test_actual_quote_execution_uses_bid_ask_slippage_fees_and_depth(self):
        data = candle_frame(days=3)
        config = permissive_config()
        config = replace(
            config,
            evening=replace(config.evening, enabled=False),
            morning=replace(
                config.morning, configured_lots=10, risk_budget_usd=10_000
            ),
        )
        entry = int(datetime(2024, 1, 2, 0, 15, tzinfo=timezone.utc).timestamp())
        exit_time = int(datetime(2024, 1, 2, 11, 30, tzinfo=timezone.utc).timestamp())
        expiry = int(datetime(2024, 1, 2, 12, 0, tzinfo=timezone.utc).timestamp())
        quotes = pd.DataFrame(
            [
                {
                    "timestamp": entry,
                    "slot": "morning",
                    "symbol": "MV-BTC-45000-020124",
                    "strike": 45_000,
                    "expiry_timestamp": expiry,
                    "bid": 490,
                    "ask": 500,
                    "mark": 495,
                    "bid_depth_lots": 1_000,
                    "ask_depth_lots": 1_000,
                    "mark_iv": 0.18,
                },
                {
                    "timestamp": exit_time,
                    "slot": "morning",
                    "symbol": "MV-BTC-45000-020124",
                    "strike": 45_000,
                    "expiry_timestamp": expiry,
                    "bid": 600,
                    "ask": 610,
                    "mark": 605,
                    "bid_depth_lots": 1_000,
                    "ask_depth_lots": 1_000,
                    "mark_iv": 0.18,
                },
            ]
        )

        trades = run_backtest(data, config, quotes)
        trade = trades.loc[trades["pricing_source"] == "move_option_quotes"].iloc[0]
        self.assertEqual(trade["lots"], 10)
        self.assertAlmostEqual(trade["entry_premium"], 505.0)
        self.assertAlmostEqual(trade["exit_premium"], 594.0)
        self.assertGreater(trade["fees_usd"], 0)
        self.assertAlmostEqual(
            trade["pnl_usd"], trade["gross_pnl_usd"] - trade["fees_usd"], places=2
        )

    def test_disabled_scheduled_exit_uses_intrinsic_settlement_not_bs_spread(self):
        config = permissive_config()
        config = replace(
            config,
            evening=replace(config.evening, enabled=False),
            morning=replace(config.morning, scheduled_exit_enabled=False),
        )
        trades = run_backtest(candle_frame(days=3), config)
        trade = trades.loc[trades["date"] == "2024-01-02"].iloc[0]

        self.assertEqual(trade["exit_reason"], "SETTLEMENT")
        self.assertEqual(trade["exit_pricing_source"], "settlement_payoff")
        self.assertIn("settlement_payoff", trade["pricing_source"])
        self.assertAlmostEqual(
            trade["exit_premium"], abs(trade["btc_exit"] - trade["strike"]), places=2
        )

    def test_an_available_but_wide_quote_is_rejected_not_replaced_by_bs(self):
        config = permissive_config(max_spread_pct=3)
        timestamp = int(datetime(2024, 1, 2, 0, 15, tzinfo=timezone.utc).timestamp())
        expiry = int(datetime(2024, 1, 2, 12, 0, tzinfo=timezone.utc).timestamp())
        book = MoveQuoteBook(
            pd.DataFrame(
                [
                    {
                        "timestamp": timestamp,
                        "slot": "morning",
                        "symbol": "MV-WIDE",
                        "strike": 45_000,
                        "expiry_timestamp": expiry,
                        "bid": 400,
                        "ask": 600,
                        "mark": 500,
                        "bid_depth_lots": 1_000,
                        "ask_depth_lots": 1_000,
                    }
                ]
            ),
            config,
        )
        quote = book.entry_quote(timestamp, expiry, "morning", "buy")
        self.assertIsNone(quote)
        self.assertTrue(book.last_had_candidate)
        self.assertIn("spread", book.last_rejection_reason)


class MoveValidationTests(unittest.TestCase):
    def test_walk_forward_and_per_slot_reports_use_net_pnl(self):
        timestamps = START + np.arange(8) * 1_000
        trades = pd.DataFrame(
            {
                "entry_timestamp": timestamps,
                "slot": ["morning", "evening"] * 4,
                "pnl_usd": [2.0, -1.0, 3.0, -1.0, 4.0, -1.0, 5.0, -1.0],
                "fees_usd": [0.1] * 8,
            }
        )
        config = replace(
            MoveBacktestConfig(),
            walk_forward_folds=2,
            walk_forward_train_fraction=0.5,
            validation_min_trades=1,
            validation_min_profit_factor=0,
            validation_max_drawdown_usd=100,
        )
        report = walk_forward_analysis(trades, START, START + 8_000, config)
        trades.attrs.update(
            start_timestamp=START,
            end_timestamp=START + 8_000,
            config=config,
        )
        analysis = analyze_backtest(trades, config)

        self.assertEqual(int(report["oos_trades"].sum()), 4)
        self.assertEqual(int(analysis["per_slot"].loc["morning", "trades"]), 4)
        self.assertEqual(int(analysis["per_slot"].loc["evening", "trades"]), 4)
        self.assertEqual(performance_metrics(trades)["net_pnl_usd"], 10.0)
        self.assertEqual(analysis["evaluation_scope"], "out_of_sample")


if __name__ == "__main__":
    unittest.main()
