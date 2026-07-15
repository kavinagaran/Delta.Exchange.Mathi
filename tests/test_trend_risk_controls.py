import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import dashboard


class MonitorHealthTests(unittest.TestCase):
    def test_health_freshness_accepts_recent_and_rejects_stale_heartbeats(self):
        recent = {
            "heartbeat_utc": datetime.now(timezone.utc).isoformat(),
            "next_poll_secs": 30,
        }
        stale = {
            "heartbeat_utc": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
            "next_poll_secs": 30,
        }
        self.assertTrue(dashboard._tp_health_fresh(recent))
        self.assertFalse(dashboard._tp_health_fresh(stale))


class TrendFilterTests(unittest.TestCase):
    def setUp(self):
        dashboard._trend_debounce.clear()

    def test_rsi_band_and_ema_gap_are_real_entry_filters(self):
        closes = [100.0 + i * 0.01 for i in range(70)]
        result = dashboard._trend_metrics(
            closes, rsi_up=55, rsi_down=45, min_ema_gap_pct=1.0)
        self.assertEqual(result["trend"], "neutral")
        self.assertLess(result["ema_gap_pct"], 1.0)

    def test_live_hourly_direction_requires_configured_persistence(self):
        first, state1 = dashboard._debounced_hourly_trend("alice", "up", 123, 2)
        second, state2 = dashboard._debounced_hourly_trend("alice", "up", 123, 2)
        self.assertEqual(first, "neutral")
        self.assertEqual(state1["count"], 1)
        self.assertEqual(second, "up")
        self.assertEqual(state2["confirmed"], "up")

    def test_hourly_opposite_flip_is_not_immediate(self):
        dashboard._debounced_hourly_trend("alice", "up", 123, 1)
        direction, state = dashboard._debounced_hourly_trend("alice", "down", 123, 2)
        self.assertEqual(direction, "neutral")
        self.assertEqual(state["candidate"], "down")


class TrendOptionQualityTests(unittest.TestCase):
    @staticmethod
    def _product(strike, expiry):
        return {"id": strike, "symbol": f"C-BTC-{strike}-TEST",
                "strike_price": str(strike), "settlement_time": expiry,
                "contract_value": "0.001"}

    @staticmethod
    def _ticker(strike, delta, bid=100, ask=102, ask_size=1000):
        return {
            "symbol": f"C-BTC-{strike}-TEST", "mark_price": "101",
            "timestamp": int(time.time() * 1_000_000),
            "tick_size": "0.1", "product_trading_status": "operational",
            "quotes": {"best_bid": str(bid), "best_ask": str(ask),
                       "bid_size": "1000", "ask_size": str(ask_size),
                       "mark_iv": "0.5"},
            "greeks": {"delta": str(delta)},
        }

    def test_target_delta_wins_within_first_liquid_expiry(self):
        expiry = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        products = [self._product(s, expiry) for s in (63000, 63500, 64000)]
        tickers = [self._ticker(63000, .80), self._ticker(63500, .66),
                   self._ticker(64000, .55)]
        config = {"TREND_MIN_TTE_HOURS": "4", "TREND_TARGET_DELTA": ".65",
                  "TREND_MAX_SPREAD_PCT": "5", "TREND_MIN_BOOK_DEPTH_LOTS": "10",
                  "TREND_QUOTE_MAX_AGE_SECS": "60"}
        product, quote, _ = dashboard._select_trend_option(
            products, tickers, 64500, "CE", config)
        self.assertEqual(product["strike_price"], "63500")
        self.assertAlmostEqual(quote["delta"], .66)

    def test_wide_spread_contract_is_rejected(self):
        expiry = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        products = [self._product(63500, expiry), self._product(64000, expiry)]
        tickers = [self._ticker(63500, .65, bid=50, ask=100),
                   self._ticker(64000, .58, bid=99, ask=101)]
        config = {"TREND_MIN_TTE_HOURS": "4", "TREND_TARGET_DELTA": ".65",
                  "TREND_MAX_SPREAD_PCT": "5", "TREND_MIN_BOOK_DEPTH_LOTS": "10",
                  "TREND_QUOTE_MAX_AGE_SECS": "60"}
        product, _, notes = dashboard._select_trend_option(
            products, tickers, 64500, "CE", config)
        self.assertEqual(product["strike_price"], "64000")
        self.assertTrue(any("spread" in note for note in notes))


class TrendSizingAndReentryTests(unittest.TestCase):
    def test_lots_are_minimum_of_risk_affordability_and_depth(self):
        contract = {"contract_value": "0.001", "strike_price": "64000"}
        quote = {"ask": 100.0, "mark": 99.0, "ask_size": 200}
        config = {
            "TREND_LOTS": "1000", "TREND_BOOK_PARTICIPATION_PCT": "25",
            "TREND_RISK_BUDGET_USD": "100", "TREND_MAX_SLIPPAGE_PCT": "1",
            "MAX_ACCOUNT_PREMIUM_AT_RISK_USD": "500",
        }
        with patch.object(dashboard, "_user_cfg", return_value=config), \
             patch.object(dashboard, "_affordable_option_lots", return_value=700), \
             patch.object(dashboard, "_open_long_premium_usd", return_value=0), \
             patch.object(dashboard, "_tp_env", return_value=(100, 30, 50, 50)), \
             patch.object(dashboard, "_option_fee_per_lot", return_value=.001):
            plan = dashboard._trend_lot_plan(contract, quote)
        self.assertEqual(plan["liquidity_cap"], 50)
        self.assertEqual(plan["lots"], 50)

    def test_same_15m_candle_cannot_reenter_and_neutral_rearms(self):
        state = {"status": "CLOSED", "last_entry_15m_candle": "100",
                 "last_entry_direction": "up", "trend_rearmed": False,
                 "pnl_usd": 10}
        same = {"combined": "up", "timeframes": {"15m": {"candle_time": "100"}}}
        self.assertIn("already triggered", dashboard._trend_reentry_reason(state, same, persist=False))
        neutral = {"combined": "neutral", "timeframes": {"15m": {"candle_time": "100"}}}
        dashboard._trend_reentry_reason(state, neutral, persist=False)
        next_bar = {"combined": "up", "timeframes": {"15m": {"candle_time": "200"}}}
        self.assertTrue(state["trend_rearmed"])
        self.assertIsNone(dashboard._trend_reentry_reason(state, next_bar, persist=False))


class TrendOwnershipAndExecutionTests(unittest.TestCase):
    def test_average_price_without_quantity_is_not_treated_as_full_fill(self):
        order = {"id": 1, "state": "closed", "average_fill_price": "10"}
        self.assertEqual(dashboard._filled_order_size(order, 100), 0)

    def test_latest_manual_order_prevents_historical_bot_adoption(self):
        orders = [
            {"id": 1, "product_id": 9, "side": "buy", "created_at": "2026-01-01T01:00:00Z",
             "client_order_id": "trend-alice-old", "reduce_only": False},
            {"id": 2, "product_id": 9, "side": "buy", "created_at": "2026-01-01T02:00:00Z",
             "client_order_id": "manual-new", "reduce_only": False},
        ]
        self.assertIsNone(dashboard._owned_trend_order(orders, 9))

    def test_ioc_partial_plus_enabled_fallback_has_correct_weighted_fill(self):
        ticker = {
            "symbol": "C-BTC-X", "mark_price": "10", "timestamp": int(time.time() * 1_000_000),
            "tick_size": ".1", "product_trading_status": "operational",
            "quotes": {"best_bid": "9.8", "best_ask": "10", "ask_size": "100",
                       "bid_size": "100"}, "greeks": {"delta": ".65"},
        }
        response = type("R", (), {"json": lambda self: {"result": ticker}})()
        orders = [
            ({"id": 1, "average_fill_price": "10", "filled_size": 1,
              "paid_commission": ".1"}, {"success": True}),
            ({"id": 2, "average_fill_price": "12", "filled_size": 1,
              "paid_commission": ".2"}, {"success": True}),
        ]
        preview = {"symbol": "C-BTC-X", "product_id": 9}
        with patch.object(dashboard.req, "get", return_value=response), \
             patch.object(dashboard, "_trend_quote_reasons", return_value=[]), \
             patch.object(dashboard, "_journal_order_intent", return_value=True), \
             patch.object(dashboard, "_submit_trend_order", side_effect=orders), \
             patch.object(dashboard, "_cfg", side_effect=lambda k, d="": {
                 "TREND_MAX_SLIPPAGE_PCT": "1", "TREND_ORDER_CHUNK_LOTS": "2"}.get(k, d)), \
             patch.object(dashboard, "_cfg_bool", return_value=True), \
             patch.object(dashboard, "_active_user", return_value="alice"):
            result = dashboard._execute_trend_chunks(preview, 2)
        self.assertEqual(result["filled_lots"], 2)
        self.assertAlmostEqual(result["fill_price"], 11.0)
        self.assertAlmostEqual(result["paid_commission_usd"], .3)
        self.assertEqual(result["executions"][0]["kind"], "ioc_limit")
        self.assertEqual(result["executions"][1]["kind"], "configured_market_fallback")


if __name__ == "__main__":
    unittest.main()
