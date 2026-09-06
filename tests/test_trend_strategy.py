import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import dashboard


class TrendCalculationTests(unittest.TestCase):
    def test_rising_series_is_up(self):
        result = dashboard._trend_metrics([float(i) for i in range(100, 170)])
        self.assertEqual(result["trend"], "up")
        self.assertGreater(result["ema9"], result["ema21"])
        self.assertGreater(result["rsi"], 50)

    def test_falling_series_is_down(self):
        result = dashboard._trend_metrics([float(i) for i in range(170, 100, -1)])
        self.assertEqual(result["trend"], "down")
        self.assertLess(result["ema9"], result["ema21"])
        self.assertLess(result["rsi"], 50)

    def test_flat_series_is_neutral(self):
        result = dashboard._trend_metrics([100.0] * 70)
        self.assertEqual(result["trend"], "neutral")

    def test_only_hourly_trend_uses_the_in_progress_candle(self):
        self.assertFalse(dashboard.TREND_TIMEFRAMES["5m"]["include_live"])
        self.assertFalse(dashboard.TREND_TIMEFRAMES["15m"]["include_live"])
        self.assertTrue(dashboard.TREND_TIMEFRAMES["1h"]["include_live"])

    def test_snapshot_publishes_the_exact_displayed_timeframes(self):
        candles = [
            {"time": index, "open": 100, "high": 100, "low": 100,
             "close": 100}
            for index in range(1, 71)
        ]
        response = type("Response", (), {
            "json": lambda self: {"success": True, "result": candles},
        })()
        filters = {
            "ema_gap_pct": 0.05,
            "rsi_up": 55,
            "rsi_down": 45,
            "slope_bars": 3,
            "min_slope_pct": 0,
            "adx_min": 18,
            "hour_confirm_samples": 2,
        }
        dashboard._trend_cache.clear()
        dashboard._trend_debounce.clear()
        with patch.object(dashboard, "_active_user", return_value="alice"), \
                patch.object(dashboard, "_trend_filter_config",
                             return_value=filters), \
                patch.object(dashboard.req, "get", return_value=response), \
                patch.object(
                    dashboard, "_persist_trend_signal_snapshot") as persist:
            snapshot = dashboard._trend_snapshot(force=True)

        self.assertTrue(all(
            row["trend"] == "neutral"
            for row in snapshot["timeframes"].values()))
        persist.assert_called_once_with(snapshot)


class TrendAffordabilityTests(unittest.TestCase):
    """Wallet affordability for LIVE trend entries.

    These previously ran through `_manual_entry_lots`, a duplicate of this
    logic reachable only from the retired manual-entry route.  They now target
    `_affordable_option_lots` directly — the function `_trend_lot_plan`
    actually calls — so the fail-closed contract is asserted where it is used.
    """

    @staticmethod
    def _wallet(payload):
        return type("Response", (), {"json": lambda self: payload})()

    def test_affordable_lots_are_bounded_by_the_usd_balance(self):
        response = self._wallet({
            "success": True,
            "result": [{"asset_symbol": "USD", "available_balance": "0.50"}],
        })
        with patch.object(dashboard.req, "get", return_value=response), \
             patch.object(dashboard, "_sign", return_value={}):
            lots = dashboard._affordable_option_lots(1.0, 0.001, 64000)
        self.assertIsNotNone(lots)
        self.assertGreaterEqual(lots, 1)
        self.assertLess(lots, 1000)

    def test_unreadable_wallet_is_unverified_not_zero(self):
        """None means "unknown"; a caller must block rather than read it as 0
        affordable lots, which would look like an ordinary sizing refusal."""
        with patch.object(dashboard.req, "get",
                          return_value=self._wallet({"success": False})), \
             patch.object(dashboard, "_sign", return_value={}):
            self.assertIsNone(dashboard._affordable_option_lots(1.0, 0.001, 64000))

    def test_wallet_transport_failure_is_unverified(self):
        with patch.object(dashboard.req, "get", side_effect=OSError("no route")), \
             patch.object(dashboard, "_sign", return_value={}):
            self.assertIsNone(dashboard._affordable_option_lots(1.0, 0.001, 64000))

    def test_live_trend_sizing_fails_closed_on_unverified_affordability(self):
        config = {"TREND_LOTS": "1000", "MAX_ORDER_LOTS": "1000",
                  "TREND_ORDER_CHUNK_LOTS": "1000",
                  "TREND_RISK_BUDGET_USD": "100"}
        with patch.object(dashboard, "_user_cfg", return_value=config), \
             patch.object(dashboard, "_affordable_option_lots", return_value=None), \
             patch.object(dashboard, "_tp_env", return_value=(100, 30, 50, 0)), \
             patch.object(dashboard, "_open_long_premium_usd", return_value=0):
            plan = dashboard._trend_lot_plan(
                {"contract_value": 0.001, "strike_price": 64000},
                {"ask": 1.0, "ask_size": 5000, "spot": 64000},
                dry_run=False)
        self.assertEqual(plan["lots"], 0)
        self.assertEqual(plan["affordability_source"], "exchange_wallet")


if __name__ == "__main__":
    unittest.main()
