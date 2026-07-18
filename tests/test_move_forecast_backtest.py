import math
from datetime import datetime, timedelta, timezone

from move_backtest import walk_forward_forecast_backtest


def test_walk_forward_report_is_forecast_only_and_chronological():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    for index in range(10 * 288):
        timestamp = start + timedelta(minutes=5 * index)
        price = (
            64_000
            + 150 * math.sin(index / 37)
            + 20 * math.sin(index / 7)
            + index * 0.03
        )
        rows.append({
            "time": int(timestamp.timestamp()),
            "close": price,
            "high": price + 8,
            "low": price - 8,
        })

    report = walk_forward_forecast_backtest(
        rows,
        strike_step=200,
        lookback_days=7,
        evaluation_days=1,
        outer_scenarios=8,
        paths_per_scenario=32,
    )

    assert report["scope"] == "forecast_calibration_only"
    assert "not an executable trading-P&L backtest" in report["warning"]
    assert report["summary"]["cycles"] >= 1
    assert report["summary"]["mae_mid"] >= 0
    timestamps = [
        record["decision_at_utc"] for record in report["records"]
    ]
    assert timestamps == sorted(timestamps)
    assert all(
        record["settlement_at_utc"] > record["decision_at_utc"]
        for record in report["records"]
    )
