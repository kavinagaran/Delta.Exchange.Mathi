"""Measure what the trend score's components actually predict.

    python scripts/backtest.py fetch --days 400      # cache candles first
    python scripts/signal_study.py
    python scripts/signal_study.py --horizons 30,60,120,240 --json out.json

Offline and read-only: it uses the cached public candle history and the
production feature/score code, needs no credentials, and places no orders.

Read the significance column, not the headline IC. Overlapping forward
windows make a raw t-stat on every 5-minute bar meaningless, so the ``n_ind``
and ``t`` columns come from a non-overlapping subsample.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from btc_trend_engine.market_data.messages import Candle  # noqa: E402
from btc_trend_engine.research.derivatives_history import (  # noqa: E402
    FUNDING_SYMBOL,
    MARK_SYMBOL,
    OI_SYMBOL,
    SPOT_SYMBOL,
    DerivativesHistory,
)
from btc_trend_engine.research.information_coefficient import (  # noqa: E402
    SCORE_KEY,
    collect_observations,
    component_correlation,
    information_coefficients,
    score_calibration,
    sideways_gate_profile,
)
from btc_trend_engine.signals.score import V1_WEIGHTS  # noqa: E402
from scripts.backtest import load_cached  # noqa: E402

DEFAULT_HORIZONS = (30, 60, 120, 240)
AUX_SYMBOLS = (MARK_SYMBOL, SPOT_SYMBOL, FUNDING_SYMBOL, OI_SYMBOL)
AUX_RESOLUTION = "1h"
AUX_CACHE = Path(__file__).resolve().parent.parent / "data" / "backtest"
_CANDLE_ROW_CAP = 2_000     # venue caps a response; page under it


def _aux_cache_path(symbol: str) -> Path:
    # ':' and '.' are not portable in filenames (':' is illegal on Windows).
    safe = symbol.replace(":", "_").replace(".", "")
    return AUX_CACHE / f"{safe}-{AUX_RESOLUTION}.json"


def fetch_aux_series(symbol: str, start: datetime, end: datetime) -> list[dict]:
    """Public auxiliary candles (mark/spot/funding/OI), cached on disk."""
    import requests

    path = _aux_cache_path(symbol)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))

    rows: dict[int, dict] = {}
    cursor = start
    step = timedelta(hours=_CANDLE_ROW_CAP)
    while cursor < end:
        window_end = min(cursor + step, end)
        response = requests.get(
            "https://api.india.delta.exchange/v2/history/candles",
            params={"resolution": AUX_RESOLUTION, "symbol": symbol,
                    "start": int(cursor.timestamp()),
                    "end": int(window_end.timestamp())},
            timeout=30,
        ).json()
        for row in response.get("result") or []:
            rows[int(row["time"])] = row
        cursor = window_end
    ordered = [rows[key] for key in sorted(rows)]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ordered), encoding="utf-8")
    return ordered


def aux_candles(rows: list[dict], symbol: str) -> list[Candle]:
    out: list[Candle] = []
    for row in rows:
        try:
            out.append(Candle(
                symbol=symbol, resolution=AUX_RESOLUTION,
                start=datetime.fromtimestamp(int(row["time"]), tz=timezone.utc),
                open=Decimal(str(row["open"])), high=Decimal(str(row["high"])),
                low=Decimal(str(row["low"])), close=Decimal(str(row["close"])),
                volume=Decimal(str(row.get("volume") or 0)), closed=True))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BTCUSD")
    parser.add_argument("--resolution", default="5m")
    parser.add_argument("--horizons", default=",".join(map(str, DEFAULT_HORIZONS)),
                        help="forward horizons in minutes (default 30,60,120,240)")
    parser.add_argument("--candles", type=int, default=0,
                        help="use only the most recent N cached candles (0 = all)")
    parser.add_argument("--json", dest="json_path", help="write full results as JSON")
    parser.add_argument("--derivatives", action="store_true",
                        help="reconstruct funding/OI/basis history so "
                             "derivatives_context can be measured")
    parser.add_argument("--as-running", action="store_true",
                        help="with --derivatives, omit funding_percentile to "
                             "match what production actually computes")
    parser.add_argument("--component-ceilings", default="50,35,25",
                        help="max|component| ceilings to profile for the "
                             "sideways gate (default 50,35,25)")
    args = parser.parse_args()

    horizons = [int(h) for h in args.horizons.split(",") if h.strip()]
    ceilings = [float(c) for c in args.component_ceilings.split(",") if c.strip()]

    # load_cached already returns Candle objects, not raw API rows.
    candles = load_cached(args.symbol, args.resolution)
    if args.candles:
        candles = candles[-args.candles:]
    print(f"loaded {len(candles):,} {args.resolution} candles "
          f"({candles[0].start:%Y-%m-%d} .. {candles[-1].start:%Y-%m-%d})")

    derivatives_at = None
    if args.derivatives:
        print(f"fetching derivatives history ({', '.join(AUX_SYMBOLS)}) ...")
        span_start = candles[0].start - timedelta(days=35)   # funding window
        span_end = candles[-1].start + timedelta(hours=2)
        series = {}
        for symbol in AUX_SYMBOLS:
            rows = fetch_aux_series(symbol, span_start, span_end)
            series[symbol] = aux_candles(rows, symbol)
            print(f"  {symbol:<16} {len(series[symbol]):>6,} hourly bars")
        derivatives_at = DerivativesHistory(
            mark=series[MARK_SYMBOL], spot=series[SPOT_SYMBOL],
            funding=series[FUNDING_SYMBOL], open_interest=series[OI_SYMBOL],
            include_funding_percentile=not args.as_running)
        mode = ("as running in production (no funding_percentile)"
                if args.as_running else "as designed (with funding_percentile)")
        print(f"  mode: {mode}")

    print("replaying the production scorer over history ...")
    observations = collect_observations(candles, horizons_minutes=horizons,
                                        derivatives_at=derivatives_at)
    if not observations:
        print("no observations -- not enough history after warmup", file=sys.stderr)
        return 1
    print(f"  {len(observations):,} scored decision bars\n")

    results = information_coefficients(observations, horizons)

    print("INFORMATION COEFFICIENT  (Spearman vs forward return)")
    print("  IC  = all overlapping bars; n_ind/t = non-overlapping subsample")
    print("  |t| > 2 flagged '*'  -- a screen, not proof\n")
    print(f"{'component':<26}{'weight':>7}{'horizon':>9}{'IC':>8}"
          f"{'IC_ind':>9}{'n_ind':>8}{'t':>7}")
    print("-" * 74)
    for result in results:
        weight = V1_WEIGHTS.get(result.component)
        ic = "    --" if result.ic is None else f"{result.ic:+.3f}"
        ic_i = "     --" if result.ic_independent is None else f"{result.ic_independent:+.3f}"
        t = "    --" if result.t_stat is None else f"{result.t_stat:+.1f}"
        flag = " *" if result.significant else ""
        weight_text = "  score" if weight is None else f"{weight:>6.2f}"
        print(f"{result.component:<26}{weight_text:>7}{result.horizon_minutes:>8}m"
              f"{ic:>8}{ic_i:>9}{result.n_independent:>8}{t:>7}{flag}")

    print("\n\nCOMPONENT CORRELATION  (Spearman between components)")
    print("  high values => the weighted sum is one opinion counted twice\n")
    correlation = component_correlation(observations)
    for (left, right), value in sorted(correlation.items(),
                                       key=lambda kv: -abs(kv[1])):
        marker = "  <-- redundant" if abs(value) > 0.7 else ""
        print(f"  {left:<26}{right:<26}{value:+.3f}{marker}")

    print("\n\nSIDEWAYS GATE PROFILE  (forward MAX EXCURSION, not signed return)")
    print("  Stages are cumulative, in the order the live engine applies them.")
    print("  A short straddle is indifferent to direction and is hurt by the")
    print("  largest move against it at any point -- so if these rows do not")
    print("  FALL as the filters tighten, the gate selects nothing.\n")
    for horizon in horizons:
        profiles = sideways_gate_profile(
            observations, horizon, component_ceilings=ceilings)
        if not profiles:
            continue
        baseline = profiles[0].median_excursion
        print(f"  horizon {horizon}m")
        print(f"    {'stage':<34}{'n':>8}{'share':>8}{'mean':>9}"
              f"{'median':>9}{'p90':>9}{'vs all':>9}")
        for profile in profiles:
            if not profile.n:
                print(f"    {profile.label:<34}{0:>8}{'--':>8}{'--':>9}"
                      f"{'--':>9}{'--':>9}{'--':>9}")
                continue
            relative = (profile.median_excursion / baseline
                        if baseline else float("nan"))
            print(f"    {profile.label:<34}{profile.n:>8}"
                  f"{profile.share:>8.1%}"
                  f"{profile.mean_excursion * 100:>8.3f}%"
                  f"{profile.median_excursion * 100:>8.3f}%"
                  f"{profile.p90_excursion * 100:>8.3f}%"
                  f"{relative:>9.2f}x")
        print()

    print("\nSCORE CALIBRATION  (bands are the live zone boundaries)")
    print("  hit_rate is signed by the band; 0.50 = coin toss\n")
    for horizon in horizons:
        print(f"  horizon {horizon}m")
        print(f"    {'band':<14}{'n':>8}{'mean_ret':>11}{'median':>11}{'hit_rate':>10}")
        for bucket in score_calibration(observations, horizon):
            if not bucket.n:
                print(f"    {bucket.label:<14}{0:>8}{'--':>11}{'--':>11}{'--':>10}")
                continue
            print(f"    {bucket.label:<14}{bucket.n:>8}"
                  f"{bucket.mean_return * 100:>10.3f}%"
                  f"{bucket.median_return * 100:>10.3f}%"
                  f"{bucket.hit_rate:>10.3f}")
        print()

    if args.json_path:
        payload = {
            "symbol": args.symbol,
            "candles": len(candles),
            "observations": len(observations),
            "horizons": horizons,
            "information_coefficients": [
                {"component": r.component, "horizon_minutes": r.horizon_minutes,
                 "weight": V1_WEIGHTS.get(r.component), "ic": r.ic,
                 "n_overlapping": r.n_overlapping,
                 "ic_independent": r.ic_independent,
                 "n_independent": r.n_independent, "t_stat": r.t_stat,
                 "significant": r.significant}
                for r in results
            ],
            "component_correlation": [
                {"a": a, "b": b, "spearman": v}
                for (a, b), v in sorted(correlation.items())
            ],
            "sideways_gate_profile": {
                str(h): [
                    {"stage": p.label, "n": p.n, "share": p.share,
                     "mean_excursion": p.mean_excursion,
                     "median_excursion": p.median_excursion,
                     "p90_excursion": p.p90_excursion}
                    for p in sideways_gate_profile(
                        observations, h, component_ceilings=ceilings)
                ]
                for h in horizons
            },
            "calibration": {
                str(h): [
                    {"band": b.label, "n": b.n, "mean_return": b.mean_return,
                     "median_return": b.median_return, "hit_rate": b.hit_rate}
                    for b in score_calibration(observations, h)
                ]
                for h in horizons
            },
        }
        Path(args.json_path).write_text(json.dumps(payload, indent=2),
                                        encoding="utf-8")
        print(f"wrote {args.json_path}")

    significant = [r for r in results if r.significant and r.component != SCORE_KEY]
    if not significant:
        print("NOTE: no component reached |t| > 2 on independent samples.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
