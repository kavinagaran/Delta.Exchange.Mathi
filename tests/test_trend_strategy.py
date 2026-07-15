import unittest
from datetime import datetime, timezone, timedelta

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


class OptionSelectionTests(unittest.TestCase):
    @staticmethod
    def products(expiry, strikes):
        out = []
        for strike in strikes:
            out.extend([
                {"symbol": f"C-BTC-{strike}-TEST", "id": strike,
                 "strike_price": str(strike), "settlement_time": expiry},
                {"symbol": f"P-BTC-{strike}-TEST", "id": strike + 1,
                 "strike_price": str(strike), "settlement_time": expiry},
            ])
        return out

    def test_ce_uses_two_ladder_steps_below_atm(self):
        expiry = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        selected = dashboard._pick_two_step_itm(
            self.products(expiry, [64000, 64200, 64400, 64600, 64800, 65000]),
            64850, "CE")
        self.assertEqual(float(selected["strike_price"]), 64400)

    def test_pe_uses_two_ladder_steps_above_atm(self):
        expiry = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        selected = dashboard._pick_two_step_itm(
            self.products(expiry, [64600, 64800, 65000, 65200, 65400]),
            64850, "PE")
        self.assertEqual(float(selected["strike_price"]), 65200)

    def test_expiry_with_less_than_one_hour_is_skipped(self):
        soon = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        later = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        products = self.products(soon, [64000, 64200, 64400, 64600, 64800]) + \
            self.products(later, [63800, 64000, 64200, 64400, 64600, 64800, 65000])
        selected = dashboard._pick_two_step_itm(products, 64850, "CE")
        self.assertEqual(selected["settlement_time"], later)


if __name__ == "__main__":
    unittest.main()
