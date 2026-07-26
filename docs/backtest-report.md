# Backtest & walk-forward report

Generated 2026-07-26 04:22 UTC · `scripts/backtest.py`

## What this does and does not measure

**Read this before any number below.**

- **Candle-level, not order-flow-level.** Fills are simulated against 5m OHLC plus an assumed spread, because no multi-month L2/trade archive exists yet. Microstructure effects are absent.
- **Measured on the perpetual, not on the options actually traded.** The live system converts a direction into an ITM option purchase. These figures measure *directional edge*; they are not a forecast of the live account's P&L.
- **The order-flow score component is weighted 0** (ADR 0004), so this measures the transparent score as it would actually ship today.
- Costs applied: taker fee, assumed spread, decision-to-arrival latency, and IOC cancellation when the market moves beyond the limit.

## Data

- Symbol: `BTCUSD` · resolution `5m`
- Candles: 63,351
- Range: 2025-12-17 17:35 → 2026-07-25 17:30 UTC (219 days)
- Warmup per window: 3,000 candles (the 4h timeframe needs 60 closed candles)

## Baseline — shipped defaults, whole period, no selection

This is in-sample in the weak sense that the shipped thresholds were not chosen from this data, but it involves no walk-forward discipline either. It is context, not evidence.

| Window | Trades | Win rate | Total P&L | Expectancy | Expectancy 95% CI | Max DD | Profit factor |
|---|---|---|---|---|---|---|---|
| Full period | 442 | 22.4% | -37633.47 | -85.14 | [-155.19, -4.43] | -43171.99 | 0.68 |

- Entry-eligible signals: 16,389
- Signals skipped by gates: 11,059
- Fills rejected (IOC cancelled): 0

### Baseline by regime at entry

| Regime | Trades | Win rate | Total P&L | Expectancy | Expectancy 95% CI | Max DD | Profit factor |
|---|---|---|---|---|---|---|---|
| BREAKOUT_DOWN | 122 | 20.5% | -10001.86 | -81.98 | [-193.63, +59.21] | -13478.93 | 0.66 |
| BREAKOUT_UP | 131 | 19.8% | -16768.56 | -128.00 | [-205.30, -37.80] | -17093.56 | 0.46 |
| TREND_DOWN | 101 | 26.7% | +2605.48 | +25.80 | [-191.88, +315.18] | -6525.83 | 1.08 |
| TREND_UP | 88 | 23.9% | -13468.53 | -153.05 | [-257.16, -39.47] | -14928.20 | 0.47 |

## Walk-forward

Thresholds are selected on each training window and measured on the untouched window that follows. Training and out-of-sample are always shown side by side; a large gap between them is the finding.

**Warnings**

- sensitivity sweep covers the most recent 20,000 candles, not the full 63,351 (runtime bound)

| Fold | Train range | Selected | Train P&L | Train trades | OOS range | OOS P&L | OOS trades |
|---|---|---|---|---|---|---|---|
| 0 | 2025-12-17→2026-01-25 | `entry=35,conf=0.55` | -9468.29 | 66 | 2026-01-25→2026-02-10 | +5928.23 | 9 |
| 1 | 2026-02-10→2026-03-21 | `entry=45,conf=0.62` | +2674.33 | 44 | 2026-03-21→2026-04-06 | -1012.13 | 10 |
| 2 | 2026-04-06→2026-05-15 | `entry=30,conf=0.7` | -10532.92 | 55 | 2026-05-15→2026-05-31 | -1713.86 | 12 |
| 3 | 2026-05-31→2026-07-09 | `entry=45,conf=0.7` | -9413.38 | 54 | 2026-07-09→2026-07-25 | -2748.87 | 13 |

**Folds with positive OOS P&L: 1 / 4**

### Pooled out-of-sample

| Window | Trades | Win rate | Total P&L | Expectancy | Expectancy 95% CI | Max DD | Profit factor |
|---|---|---|---|---|---|---|---|
| All OOS trades | 44 | 18.2% | +453.38 | +10.30 | [-227.12, +317.60] | -6835.89 | 1.04 |

**The pooled OOS expectancy CI includes zero — this data does not demonstrate an edge.** More data, or a different configuration, is needed before Stage C.

## Threshold sensitivity

Each configuration run over the whole period. A result that survives only at one threshold is a result to distrust.

| Config | Trades | Win rate | Total P&L | Expectancy | Max DD |
|---|---|---|---|---|---|
| `entry=30,conf=0.55` | 145 | 20.0% | -15391.02 | -106.14 | -23005.32 |
| `entry=30,conf=0.62` | 137 | 19.7% | -15282.08 | -111.55 | -22965.53 |
| `entry=30,conf=0.7` | 109 | 23.9% | -11726.46 | -107.58 | -19434.15 |
| `entry=35,conf=0.55` | 132 | 21.2% | -14518.30 | -109.99 | -22132.59 |
| `entry=35,conf=0.62` | 129 | 20.9% | -14356.32 | -111.29 | -22039.77 |
| `entry=35,conf=0.7` | 109 | 23.9% | -11726.46 | -107.58 | -19434.15 |
| `entry=45,conf=0.55` | 115 | 23.5% | -12158.80 | -105.73 | -19797.33 |
| `entry=45,conf=0.62` | 114 | 23.7% | -12089.65 | -106.05 | -19797.33 |
| `entry=45,conf=0.7` | 106 | 24.5% | -11183.23 | -105.50 | -18890.91 |

## Cutover relevance

Plan Phase 8 requires *walk-forward positive OOS on ≥3/4 folds* before Stage C. This report supplies that input; it does not by itself authorise a stage change, and the remaining criteria (uptime, data quality, agreement rate, ≥20 entry-eligible engine signals) are measured live during the shadow window, not here.
