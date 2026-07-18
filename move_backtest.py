"""Walk-forward calibration harness for the internal BTC MOVE payoff model.

This evaluates forecast quality only. Historical BTC index candles can prove
how well the model estimated ``abs(final 30m TWAP - strike)``; they cannot
reconstruct historical executable MOVE bid/ask, spread or depth. Entry
performance is therefore measured separately by production shadow records.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import requests

from move_decision import MoveInputError, forecast_move_distribution


API_BASE = "https://api.india.delta.exchange"


def _candle_rows(candles: Iterable[dict[str, Any]]) -> list[dict[str, float]]:
    unique: dict[int, dict[str, float]] = {}
    for row in candles:
        if not isinstance(row, dict):
            continue
        try:
            timestamp = int(float(row.get("time") or 0))
            while timestamp > 100_000_000_000:
                timestamp //= 1000
            close = float(row.get("close") or 0)
            high = float(row.get("high") or close)
            low = float(row.get("low") or close)
        except (TypeError, ValueError, OverflowError):
            continue
        if timestamp > 0 and close > 0 and high >= close >= low > 0:
            unique[timestamp] = {
                "time": timestamp,
                "close": close,
                "high": high,
                "low": low,
            }
    return [unique[key] for key in sorted(unique)]


def fetch_index_candles(
    *,
    symbol: str = ".DEXBTUSD",
    start: datetime,
    end: datetime,
    session: requests.Session | None = None,
) -> list[dict[str, Any]]:
    """Fetch 5M candles in bounded chunks from Delta's public endpoint."""
    client = session or requests.Session()
    cursor = int(start.astimezone(timezone.utc).timestamp())
    stop = int(end.astimezone(timezone.utc).timestamp())
    rows: list[dict[str, Any]] = []
    chunk = 6 * 86_400
    while cursor < stop:
        chunk_end = min(cursor + chunk, stop)
        response = client.get(
            f"{API_BASE}/v2/history/candles",
            params={
                "resolution": "5m",
                "symbol": symbol,
                "start": cursor,
                "end": chunk_end,
            },
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        batch = payload.get("result")
        if not isinstance(batch, list):
            raise RuntimeError("Delta candle response is invalid")
        rows.extend(batch)
        cursor = chunk_end
    result = _candle_rows(rows)
    if not result:
        raise RuntimeError("Delta returned no BTC index candles")
    return result


def _decision_schedule(
    first_day: datetime,
    last_day: datetime,
) -> Iterable[tuple[str, datetime, datetime]]:
    day = first_day.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0)
    final_day = last_day.astimezone(timezone.utc).date()
    while day.date() <= final_day:
        morning = day.replace(hour=0, minute=15)
        yield "morning", morning, day.replace(hour=12)
        evening = day.replace(hour=12, minute=5)
        yield "evening", evening, (day + timedelta(days=1)).replace(hour=12)
        day += timedelta(days=1)


def walk_forward_forecast_backtest(
    candles: Iterable[dict[str, Any]],
    *,
    strike_step: float = 200,
    lookback_days: int = 30,
    evaluation_days: int = 7,
    outer_scenarios: int = 8,
    paths_per_scenario: int = 32,
) -> dict[str, Any]:
    """Run chronological Morning/Evening forecast calibration cycles."""
    rows = _candle_rows(candles)
    if len(rows) < 8 * 288:
        raise ValueError("at least eight days of 5M candles are required")
    if strike_step <= 0:
        raise ValueError("strike_step must be positive")
    lookup = {int(row["time"]): row for row in rows}
    first_timestamp = int(rows[0]["time"])
    last_timestamp = int(rows[-1]["time"])
    first_evaluation = max(
        first_timestamp + lookback_days * 86_400,
        last_timestamp - evaluation_days * 86_400,
    )
    first_day = datetime.fromtimestamp(first_evaluation, tz=timezone.utc)
    last_day = datetime.fromtimestamp(last_timestamp, tz=timezone.utc)
    records = []

    for slot, decision_at, settlement_at in _decision_schedule(
            first_day, last_day):
        decision_ts = int(decision_at.timestamp())
        settlement_ts = int(settlement_at.timestamp())
        if settlement_ts > last_timestamp + 300:
            continue
        history = [
            row for row in rows
            if decision_ts - (lookback_days + 1) * 86_400
            <= int(row["time"]) < decision_ts
        ]
        if not history:
            continue
        spot_row = max(
            (row for row in history if int(row["time"]) <= decision_ts - 300),
            key=lambda row: row["time"],
            default=None,
        )
        if not spot_row:
            continue
        settlement_closes = [
            lookup[timestamp]["close"]
            for timestamp in range(settlement_ts - 1800, settlement_ts, 300)
            if timestamp in lookup
        ]
        if len(settlement_closes) != 6:
            continue
        spot = float(spot_row["close"])
        strike = round(spot / strike_step) * strike_step
        final_twap = float(np.mean(settlement_closes))
        actual_payoff = abs(final_twap - strike)
        try:
            forecast = forecast_move_distribution(
                history,
                current_index_price=spot,
                strike=strike,
                now_ms=decision_ts * 1000,
                settlement_end_ts_ms=settlement_ts * 1000,
                scheduled_event_score=None,
                outer_scenarios=outer_scenarios,
                paths_per_scenario=paths_per_scenario,
                minimum_history_days=lookback_days,
            )
        except MoveInputError:
            continue
        records.append({
            "slot": slot,
            "decision_at_utc": decision_at.isoformat().replace("+00:00", "Z"),
            "settlement_at_utc": settlement_at.isoformat().replace(
                "+00:00", "Z"),
            "spot": round(spot, 8),
            "strike": round(strike, 8),
            "final_30m_twap": round(final_twap, 8),
            "actual_payoff": round(actual_payoff, 8),
            "expected_payoff_low": forecast["expected_payoff_low"],
            "expected_payoff_mid": forecast["expected_payoff_mid"],
            "expected_payoff_high": forecast["expected_payoff_high"],
            "payoff_p99": forecast["payoff_p99"],
            "absolute_error_mid": round(abs(
                actual_payoff - forecast["expected_payoff_mid"]), 8),
            "within_fair_band": (
                forecast["expected_payoff_low"]
                <= actual_payoff
                <= forecast["expected_payoff_high"]
            ),
            "p99_breach": actual_payoff > forecast["payoff_p99"],
        })

    if not records:
        raise RuntimeError("no complete walk-forward evaluation cycles were available")
    errors = np.asarray([
        row["actual_payoff"] - row["expected_payoff_mid"] for row in records
    ], dtype=float)
    return {
        "schema_version": 1,
        "scope": "forecast_calibration_only",
        "warning": (
            "Historical MOVE bid/ask/spread/depth are not reconstructed; "
            "this report is not an executable trading-P&L backtest. The "
            "P20/P50/P80 band represents uncertainty around fair value, not "
            "a prediction interval for one realized settlement payoff."
        ),
        "parameters": {
            "strike_step": strike_step,
            "lookback_days": lookback_days,
            "evaluation_days": evaluation_days,
            "outer_scenarios": outer_scenarios,
            "paths_per_scenario": paths_per_scenario,
        },
        "summary": {
            "cycles": len(records),
            "mean_actual_payoff": round(float(np.mean([
                row["actual_payoff"] for row in records])), 8),
            "mean_forecast_mid": round(float(np.mean([
                row["expected_payoff_mid"] for row in records])), 8),
            "mae_mid": round(float(np.mean(np.abs(errors))), 8),
            "rmse_mid": round(float(math.sqrt(np.mean(errors * errors))), 8),
            "bias_mid": round(float(np.mean(errors)), 8),
            "actual_within_fair_value_band_pct": round(
                100 * sum(row["within_fair_band"] for row in records)
                / len(records), 2),
            "p99_breach_pct": round(
                100 * sum(row["p99_breach"] for row in records)
                / len(records), 2),
        },
        "records": records,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Walk-forward calibration for the internal MOVE forecast")
    parser.add_argument("--days", type=int, default=45)
    parser.add_argument("--evaluation-days", type=int, default=7)
    parser.add_argument("--lookback-days", type=int, default=30)
    parser.add_argument("--strike-step", type=float, default=200)
    parser.add_argument(
        "--output", type=Path,
        default=Path("artifacts/move_forecast_backtest.json"))
    args = parser.parse_args()
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = end - timedelta(days=max(args.days, args.lookback_days + 8))
    report = walk_forward_forecast_backtest(
        fetch_index_candles(start=start, end=end),
        strike_step=args.strike_step,
        lookback_days=args.lookback_days,
        evaluation_days=args.evaluation_days,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report["summary"], indent=2, sort_keys=True))
    print(f"Report: {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
