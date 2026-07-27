"""Fetch BTCUSD history and run the walk-forward backtest.

    python scripts/backtest.py fetch --days 120
    python scripts/backtest.py run --report docs/backtest-report.md

``fetch`` pages the public /v2/history/candles endpoint (4,000 rows per
request, no authentication) into data/backtest/, so ``run`` is offline and
reproducible. The cache is plain JSON on purpose: a reviewer should be able
to read exactly what the report was computed from.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from btc_trend_engine.backtest.event_replay import (  # noqa: E402
    ReplayConfig,
    candles_from_rows,
    replay,
    replay_score_zones,
)
from btc_trend_engine.backtest.performance import Performance, by_regime, evaluate  # noqa: E402
from btc_trend_engine.backtest.walk_forward import run_walk_forward  # noqa: E402
from btc_trend_engine.config import load_config  # noqa: E402
from btc_trend_engine.market_data.delta_rest import DeltaRestClient  # noqa: E402

CACHE_DIR = REPO_ROOT / "data" / "backtest"
MAX_ROWS_PER_REQUEST = 4000
RESOLUTION = "5m"


def cache_path(symbol: str, resolution: str) -> Path:
    return CACHE_DIR / f"{symbol}-{resolution}.json"


async def fetch(symbol: str, days: int, resolution: str) -> int:
    """Page backwards until ``days`` of history is cached."""
    config = load_config(environ={"ENGINE_TOKEN": "fetch-only"})
    client = DeltaRestClient(config.market_data.rest_url,
                             config.market_data.rate_limit)
    step = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400}[resolution]
    end = int(time.time())
    floor = end - days * 86_400
    rows: dict[int, dict] = {}
    try:
        while end > floor:
            start = max(floor, end - MAX_ROWS_PER_REQUEST * step)
            candles = await client.history_candles(symbol, resolution, start, end)
            if not candles:
                print(f"  no rows returned below {end}; stopping early")
                break
            for candle in candles:
                rows[int(candle.start.timestamp())] = {
                    "time": int(candle.start.timestamp()),
                    "open": str(candle.open), "high": str(candle.high),
                    "low": str(candle.low), "close": str(candle.close),
                    "volume": str(candle.volume),
                }
            oldest = int(candles[0].start.timestamp())
            print(f"  {len(rows):6d} rows cached, oldest "
                  f"{datetime.fromtimestamp(oldest, tz=timezone.utc):%Y-%m-%d %H:%M}")
            if oldest >= end:
                break  # no progress; venue has no more history
            end = oldest - step
    finally:
        await client.aclose()

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ordered = [rows[key] for key in sorted(rows)]
    cache_path(symbol, resolution).write_text(
        json.dumps(ordered, separators=(",", ":")), encoding="utf-8")
    return len(ordered)


def load_cached(symbol: str, resolution: str):
    path = cache_path(symbol, resolution)
    if not path.exists():
        raise SystemExit(
            f"no cached history at {path}. Run: "
            f"python scripts/backtest.py fetch --days 120")
    return candles_from_rows(json.loads(path.read_text(encoding="utf-8")),
                             symbol, resolution)


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _perf_row(label: str, p: Performance) -> str:
    ci = (f"[{p.expectancy_ci.low:+.2f}, {p.expectancy_ci.high:+.2f}]"
          if p.expectancy_ci else "n/a")
    pf = f"{p.profit_factor:.2f}" if p.profit_factor is not None else "n/a"
    return (f"| {label} | {p.trades} | {_pct(p.win_rate)} | {p.total_pnl:+.2f} | "
            f"{p.expectancy:+.2f} | {ci} | {p.max_drawdown:+.2f} | {pf} |")


PERF_HEADER = ("| Window | Trades | Win rate | Total P&L | Expectancy | "
               "Expectancy 95% CI | Max DD | Profit factor |\n"
               "|---|---|---|---|---|---|---|---|")


def build_report(symbol: str, candles, folds: int, warmup: int,
                 sensitivity_candles: int | None = None,
                 include_legacy_analysis: bool = True) -> str:
    first, last = candles[0].start, candles[-1].start
    baseline = None
    baseline_performance = None
    if include_legacy_analysis:
        baseline = replay(candles, ReplayConfig(), warmup=warmup)
        baseline_performance = evaluate(baseline.trades)
    zone_baseline = replay_score_zones(candles, ReplayConfig(), warmup=warmup)
    zone_performance = evaluate(zone_baseline.trades)
    report = None
    if include_legacy_analysis:
        report = run_walk_forward(
            candles, folds=folds, warmup=warmup,
            sensitivity_candles=sensitivity_candles,
            progress=lambda line: print(f"  {line}", flush=True),
        )

    out: list[str] = []
    add = out.append
    add("# Backtest & walk-forward report\n")
    add(f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC · "
        f"`scripts/backtest.py`\n")
    add("## What this does and does not measure\n")
    add("**Read this before any number below.**\n")
    add("- **Candle-level, not order-flow-level.** Fills are simulated against "
        "5m OHLC plus an assumed spread, because no multi-month L2/trade "
        "archive exists yet. Microstructure effects are absent.")
    add("- **Measured on the perpetual, not on the options actually traded.** "
        "The live system converts a direction into an ITM option purchase. "
        "These figures measure *directional edge*; they are not a forecast of "
        "the live account's P&L.")
    add("- **The order-flow score component is weighted 0** (ADR 0004), so this "
        "measures the transparent score as it would actually ship today.")
    add("- Costs applied: taker fee, assumed spread, decision-to-arrival "
        "latency, and IOC cancellation when the market moves beyond the limit.\n")
    add("- **The score-zone replay below is the current CE/PE/HOLD lifecycle.** "
        "It deliberately reports SHORT_MOVE candidates as unpriced rather than "
        "inventing historical MOVE premiums.\n")

    add("## Data\n")
    add(f"- Symbol: `{symbol}` · resolution `{RESOLUTION}`")
    add(f"- Candles: {len(candles):,}")
    add(f"- Range: {first:%Y-%m-%d %H:%M} → {last:%Y-%m-%d %H:%M} UTC "
        f"({(last - first).days} days)")
    add(f"- Warmup per window: {warmup:,} candles "
        f"(the 4h timeframe needs 60 closed candles)\n")

    if baseline is not None and baseline_performance is not None:
        add("## Baseline — retired direction-only defaults\n")
        add("This is in-sample in the weak sense that the shipped thresholds were "
            "not chosen from this data, but it involves no walk-forward "
            "discipline either. It is context, not evidence.\n")
        add(PERF_HEADER)
        add(_perf_row("Full period", baseline_performance))
        add("")
        add(f"- Entry-eligible signals: {baseline.entry_eligible_signals:,}")
        add(f"- Signals skipped by gates: {baseline.skipped_no_entry:,}")
        add(f"- Fills rejected (IOC cancelled): {baseline.rejected_fills:,}\n")

        if baseline.trades:
            add("### Baseline by regime at entry\n")
            add(PERF_HEADER.replace("| Window |", "| Regime |"))
            for regime, performance in by_regime(baseline.trades).items():
                add(_perf_row(regime, performance))
            add("")

    add("## Score-zone policy replay — current execution logic\n")
    add("Directional entries require the current zone's confidence and regime "
        "alignment gates. CE/PE positions stay open through a supportive HOLD "
        "band, but exit in an opposite-direction HOLD band. This remains a "
        "perpetual directional proxy, not option P&L.\n")
    add(PERF_HEADER)
    add(_perf_row("CE/PE directional proxy", zone_performance))
    add("")
    add(f"- Directional entry-eligible signals: {zone_baseline.entry_eligible_signals:,}")
    add(f"- Directional invalidation exits: {zone_baseline.directional_invalidation_exits:,}")
    add(f"- SHORT_MOVE candidates after 15-minute confirmation: {zone_baseline.short_move_candidates:,}")
    add(f"- SHORT_MOVE candidates excluded from P&L for missing historical premium/quote data: {zone_baseline.short_move_unpriced:,}")
    add("")
    add("**Interpretation:** this section validates the current directional "
        "state machine and its anti-churn/exit behaviour. It does **not** "
        "validate short-MOVE profitability or option P&L; an archived MOVE/"
        "option quote series is required before either can be presented as "
        "backtested account returns.\n")

    add("## Legacy directional walk-forward\n")
    if report is None:
        add("Not run for this score-zone validation. The legacy direction-only "
            "policy is retired and its prior report is not evidence for the "
            "current CE/PE/HOLD/SHORT_MOVE lifecycle.\n")
    else:
        add("Thresholds are selected on each training window and measured on the "
            "untouched window that follows. Training and out-of-sample are always "
            "shown side by side; a large gap between them is the finding.\n")
    if report is not None and report.warnings:
        add("**Warnings**\n")
        for warning in report.warnings:
            add(f"- {warning}")
        add("")
    if report is not None and report.folds:
        add("| Fold | Train range | Selected | Train P&L | Train trades | "
            "OOS range | OOS P&L | OOS trades |")
        add("|---|---|---|---|---|---|---|---|")
        for fold in report.folds:
            add(f"| {fold.index} | {fold.train_start[:10]}→{fold.train_end[:10]} "
                f"| `{fold.selected.label()}` | {fold.train.total_pnl:+.2f} "
                f"| {fold.train.trades} | {fold.test_start[:10]}→{fold.test_end[:10]} "
                f"| {fold.test.total_pnl:+.2f} | {fold.test.trades} |")
        add("")
        add(f"**Folds with positive OOS P&L: {report.positive_oos_folds} / "
            f"{len(report.folds)}**\n")
        if report.pooled_oos:
            add("### Pooled out-of-sample\n")
            add(PERF_HEADER)
            add(_perf_row("All OOS trades", report.pooled_oos))
            add("")
            pooled = report.pooled_oos
            if pooled.is_significant:
                verdict = ("The pooled OOS expectancy CI excludes zero.")
            else:
                verdict = ("**The pooled OOS expectancy CI includes zero — this "
                           "data does not demonstrate an edge.** More data, or a "
                           "different configuration, is needed before Stage C.")
            add(f"{verdict}\n")
    elif report is not None:
        add("_No fold produced a usable train/test split. See warnings above._\n")

    add("## Threshold sensitivity\n")
    if report is None:
        add("Not run: sensitivity over the retired direction-only thresholds is "
            "not a substitute for option/MOVE quote history.\n")
    else:
        add("Each configuration run over the whole period. A result that survives "
            "only at one threshold is a result to distrust.\n")
        add("| Config | Trades | Win rate | Total P&L | Expectancy | Max DD |")
        add("|---|---|---|---|---|---|")
        for label, performance in report.sensitivity.items():
            add(f"| `{label}` | {performance.trades} | {_pct(performance.win_rate)} "
                f"| {performance.total_pnl:+.2f} | {performance.expectancy:+.2f} "
                f"| {performance.max_drawdown:+.2f} |")
        add("")

    add("## Cutover relevance\n")
    add("Do not enable LIVE until the score-zone directional proxy is positive "
        "out-of-sample and a historical MOVE/option quote archive supports "
        "end-to-end replay.\n")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    fetch_parser = sub.add_parser("fetch", help="cache candle history")
    fetch_parser.add_argument("--symbol", default="BTCUSD")
    fetch_parser.add_argument("--days", type=int, default=120)
    fetch_parser.add_argument("--resolution", default=RESOLUTION)

    run_parser = sub.add_parser("run", help="run the backtest and write a report")
    run_parser.add_argument("--symbol", default="BTCUSD")
    run_parser.add_argument("--resolution", default=RESOLUTION)
    run_parser.add_argument("--folds", type=int, default=4)
    run_parser.add_argument("--warmup", type=int, default=3000)
    run_parser.add_argument(
        "--sensitivity-candles", type=int, default=20_000,
        help="bound the sensitivity sweep to the most recent N candles "
             "(0 = whole series; the sweep costs one full replay per config)")
    run_parser.add_argument(
        "--skip-legacy-analysis", action="store_true",
        help="write the full current score-zone replay without the retired "
            "directional walk-forward/sensitivity sweep")
    run_parser.add_argument(
        "--max-candles", type=int, default=0,
        help="limit the replay to the most recent N cached candles (includes "
             "warmup; 0 = all cached candles)")
    run_parser.add_argument("--report", type=Path,
                            default=REPO_ROOT / "docs" / "backtest-report.md")

    args = parser.parse_args()
    if args.command == "fetch":
        print(f"fetching {args.days}d of {args.symbol} {args.resolution}...")
        count = asyncio.run(fetch(args.symbol, args.days, args.resolution))
        print(f"cached {count:,} candles -> "
              f"{cache_path(args.symbol, args.resolution)}")
        return 0

    candles = load_cached(args.symbol, args.resolution)
    if args.max_candles:
        if args.max_candles <= args.warmup + 1:
            raise SystemExit("--max-candles must exceed warmup + 1")
        candles = candles[-args.max_candles:]
    print(f"loaded {len(candles):,} candles; running backtest...", flush=True)
    report = build_report(args.symbol, candles, args.folds, args.warmup,
                          args.sensitivity_candles or None,
                          include_legacy_analysis=not args.skip_legacy_analysis)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report, encoding="utf-8")
    print(f"wrote {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
