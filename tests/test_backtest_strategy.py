import unittest

import numpy as np
import pandas as pd

from Delta_Backtest import (
    BacktestConfig,
    OptionQuoteBook,
    analyze_backtest,
    calculate_option_fee,
    evaluate_validation_gates,
    performance_metrics,
    prepare_trend_signals,
    run_backtest,
    walk_forward_analysis,
)


START = 1_704_067_200  # 2024-01-01 00:00 UTC, aligned to all timeframes.


def candles(closes):
    values = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {
            "timestamp": START + np.arange(len(values)) * 300,
            "open": values,
            "high": values + 5.0,
            "low": values - 5.0,
            "close": values,
            "volume": 1.0,
        }
    )


class ProductionSignalTests(unittest.TestCase):
    def test_all_three_timeframes_align_on_a_sustained_rise(self):
        data = candles(np.linspace(40_000, 50_000, 12 * 50))
        signals = prepare_trend_signals(data, BacktestConfig(hourly_mode="live"))

        latest = signals.iloc[-1]
        self.assertEqual(latest["trend_5m"], "up")
        self.assertEqual(latest["trend_15m"], "up")
        self.assertEqual(latest["trend_1h"], "up")
        self.assertEqual(latest["signal"], "up")
        self.assertGreater(latest["ema9_1h"], latest["ema21_1h"])
        self.assertGreater(latest["rsi_1h"], 50)

    def test_5m_and_15m_are_closed_but_live_1h_reacts_intrabar(self):
        # Fifty completed rising hours establish UP.  The first 5M observation
        # of the next hour collapses.  It must not leak into the incomplete 15M
        # bar, but it must affect the configured live 1H approximation.
        values = np.r_[np.linspace(40_000, 50_000, 12 * 50), 1_000.0, 900.0]
        data = candles(values)
        live = prepare_trend_signals(data, BacktestConfig(hourly_mode="live")).iloc[-1]
        closed = prepare_trend_signals(data, BacktestConfig(hourly_mode="closed")).iloc[-1]

        self.assertEqual(live["trend_5m"], "down")
        self.assertEqual(live["trend_15m"], "up")
        self.assertEqual(live["trend_1h"], "down")
        self.assertEqual(closed["trend_1h"], "up")

    def test_a_15m_source_file_is_rejected_instead_of_faking_5m_signals(self):
        data = candles(np.linspace(40_000, 41_000, 100)).iloc[::3].reset_index(drop=True)
        with self.assertRaisesRegex(ValueError, "requires 5-minute"):
            prepare_trend_signals(data, BacktestConfig())


class OptionExecutionTests(unittest.TestCase):
    def test_quote_book_uses_target_delta_and_actual_bid_ask(self):
        now = START + 50 * 3600
        expiry = now + 86_400
        rows = []
        for strike in (46_000, 47_000, 48_000, 49_000, 50_000, 51_000):
            rows.append(
                {
                    "time": now,
                    "symbol": f"C-BTC-{strike}-TEST",
                    "option_type": "CE",
                    "strike_price": strike,
                    "settlement_time": expiry,
                    "best_bid": 99.0,
                    "best_ask": 101.0,
                    "mark_price": 100.0,
                    "ask_size": 100,
                    "bid_size": 100,
                    "delta": {46_000: 0.90, 47_000: 0.80, 48_000: 0.72,
                              49_000: 0.65, 50_000: 0.50, 51_000: 0.35}[strike],
                }
            )
        quote = OptionQuoteBook(pd.DataFrame(rows), BacktestConfig()).entry_quote(
            now, 49_800, "call"
        )

        self.assertIsNotNone(quote)
        self.assertEqual(quote.source, "option_data")
        self.assertEqual(quote.strike, 49_000)
        self.assertEqual(quote.bid, 99.0)
        self.assertEqual(quote.ask, 101.0)

    def test_fee_is_notional_based_and_capped_by_premium(self):
        cfg = BacktestConfig(
            contract_value=0.001,
            option_fee_rate=0.0003,
            option_fee_cap_pct=0.10,
        )
        # Notional fee is $15; 10% of the $100 premium exposure is $10.
        self.assertAlmostEqual(calculate_option_fee(50_000, 100, 1_000, cfg), 10.0)

    def test_backtest_prefers_option_data_and_reports_net_after_costs(self):
        full_data = candles(np.linspace(40_000, 50_000, 12 * 50))
        base_cfg = BacktestConfig(
            hourly_mode="live",
            synthetic_spread_pct=0,
            slippage_pct=0,
            take_profit_usd=100,
            stop_loss_usd=50,
            tsl_arm_usd=0,
            tsl_trail_usd=0,
            force_close_at_end=False,
        )
        all_signals = prepare_trend_signals(full_data, base_cfg)
        first_index = int(all_signals.index[all_signals["signal"] == "up"][0])
        data = full_data.iloc[: first_index + 2].copy()
        entry_signal = prepare_trend_signals(data, base_cfg).iloc[first_index]
        entry_time = int(entry_signal["decision_timestamp"])
        next_time = entry_time + 300
        spot = float(entry_signal["spot"])
        atm = round(spot / 1_000) * 1_000
        expiry = entry_time + 86_400

        quotes = []
        for offset in (-3, -2, -1, 0, 1, 2, 3):
            strike = atm + offset * 1_000
            symbol = f"C-BTC-{strike}-TEST"
            quotes.extend(
                [
                    {
                        "timestamp": entry_time,
                        "symbol": symbol,
                        "option_type": "call",
                        "strike": strike,
                        "expiry_timestamp": expiry,
                        "bid": 999,
                        "ask": 1_001,
                        "mark": 1_000,
                        "bid_size": 1_000,
                        "ask_size": 1_000,
                        "delta": 0.65 if offset == -1 else 0.80,
                    },
                    {
                        "timestamp": next_time,
                        "symbol": symbol,
                        "option_type": "call",
                        "strike": strike,
                        "expiry_timestamp": expiry,
                        "bid": 1_199,
                        "ask": 1_201,
                        "mark": 1_200,
                        "bid_size": 1_000,
                        "ask_size": 1_000,
                        "delta": 0.65 if offset == -1 else 0.80,
                    },
                ]
            )

        trades = run_backtest(data, base_cfg, pd.DataFrame(quotes))
        self.assertEqual(len(trades), 1)
        trade = trades.iloc[0]
        self.assertEqual(trade["pricing_source"], "option_data")
        self.assertEqual(trade["exit_reason"], "TP")
        self.assertGreater(trade["fees_usd"], 0)
        self.assertAlmostEqual(
            trade["pnl_usd"], trade["gross_pnl_usd"] - trade["fees_usd"], places=2
        )


class ValidationReportTests(unittest.TestCase):
    def test_metrics_and_validation_gates_use_after_cost_pnl(self):
        trades = pd.DataFrame(
            {
                "pnl_usd": [10.0, 10.0, -5.0],
                "fees_usd": [1.0, 1.0, 1.0],
                "signal": ["up", "down", "up"],
            }
        )
        metrics = performance_metrics(trades)
        cfg = BacktestConfig(
            validation_min_trades=3,
            validation_min_profit_factor=2.0,
            validation_max_drawdown_usd=10.0,
        )
        gates = evaluate_validation_gates(metrics, cfg)

        self.assertEqual(metrics["net_pnl_usd"], 15.0)
        self.assertEqual(metrics["profit_factor"], 4.0)
        self.assertEqual(metrics["max_drawdown_usd"], 5.0)
        self.assertTrue(gates["passed"].all())

    def test_walk_forward_has_non_overlapping_oos_folds_and_direction_stats(self):
        timestamps = START + np.arange(8) * 1_000
        trades = pd.DataFrame(
            {
                "entry_timestamp": timestamps,
                "pnl_usd": [1.0, -1.0, 2.0, -1.0, 3.0, -1.0, 4.0, -1.0],
                "fees_usd": [0.1] * 8,
                "signal": ["up", "down"] * 4,
            }
        )
        cfg = BacktestConfig(
            walk_forward_folds=2,
            walk_forward_train_fraction=0.5,
            validation_min_trades=1,
            validation_min_profit_factor=0,
            validation_max_drawdown_usd=100,
        )
        report = walk_forward_analysis(trades, START, START + 8_000, cfg)
        trades.attrs.update(start_timestamp=START, end_timestamp=START + 8_000, config=cfg)
        analysis = analyze_backtest(trades, cfg)

        self.assertEqual(len(report), 2)
        self.assertEqual(int(report["oos_trades"].sum()), 4)
        self.assertEqual(int(analysis["per_direction"].loc["up", "trades"]), 4)
        self.assertEqual(int(analysis["per_direction"].loc["down", "trades"]), 4)
        self.assertEqual(analysis["evaluation_scope"], "out_of_sample")


if __name__ == "__main__":
    unittest.main()
