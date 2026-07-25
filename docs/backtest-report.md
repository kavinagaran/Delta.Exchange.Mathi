# Backtest & walk-forward report

Generated 2026-07-25 18:44 UTC · `scripts/backtest.py`

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
| Full period | 210 | 26.7% | -11302.68 | -53.82 | [-170.94, +90.26] | -23139.89 | 0.82 |

- Entry-eligible signals: 5,153
- Signals skipped by gates: 232
- Fills rejected (IOC cancelled): 0

### Baseline by regime at entry

| Regime | Trades | Win rate | Total P&L | Expectancy | Expectancy 95% CI | Max DD | Profit factor |
|---|---|---|---|---|---|---|---|
| BREAKOUT_DOWN | 38 | 31.6% | +3064.02 | +80.63 | [-207.90, +446.59] | -5032.61 | 1.30 |
| BREAKOUT_UP | 41 | 26.8% | -8155.41 | -198.91 | [-341.47, -51.75] | -9012.30 | 0.38 |
| TREND_DOWN | 67 | 32.8% | +5471.66 | +81.67 | [-222.29, +462.83] | -5372.35 | 1.25 |
| TREND_UP | 64 | 17.2% | -11682.96 | -182.55 | [-277.56, -69.16] | -12172.28 | 0.37 |

## Walk-forward

Thresholds are selected on each training window and measured on the untouched window that follows. Training and out-of-sample are always shown side by side; a large gap between them is the finding.

**Warnings**

- sensitivity sweep covers the most recent 20,000 candles, not the full 63,351 (runtime bound)

| Fold | Train range | Selected | Train P&L | Train trades | OOS range | OOS P&L | OOS trades |
|---|---|---|---|---|---|---|---|
| 0 | 2025-12-17→2026-01-25 | `entry=75,conf=0.55` | -1549.86 | 14 | 2026-01-25→2026-02-10 | +5949.57 | 2 |
| 1 | 2026-02-10→2026-03-21 | `entry=55,conf=0.7` | +138.70 | 37 | 2026-03-21→2026-04-06 | -623.77 | 8 |
| 2 | 2026-04-06→2026-05-15 | `entry=75,conf=0.55` | -2283.50 | 9 | 2026-05-15→2026-05-31 | -1249.83 | 3 |
| 3 | 2026-05-31→2026-07-09 | `entry=75,conf=0.55` | -4365.78 | 12 | 2026-07-09→2026-07-25 | -449.97 | 4 |

**Folds with positive OOS P&L: 1 / 4**

### Pooled out-of-sample

| Window | Trades | Win rate | Total P&L | Expectancy | Expectancy 95% CI | Max DD | Profit factor |
|---|---|---|---|---|---|---|---|
| All OOS trades | 17 | 23.5% | +3625.99 | +213.29 | [-286.93, +926.84] | -2403.99 | 1.81 |

**The pooled OOS expectancy CI includes zero — this data does not demonstrate an edge.** More data, or a different configuration, is needed before Stage C.

## Threshold sensitivity

Each configuration run over the whole period. A result that survives only at one threshold is a result to distrust.

| Config | Trades | Win rate | Total P&L | Expectancy | Max DD |
|---|---|---|---|---|---|
| `entry=55,conf=0.55` | 97 | 24.7% | -10258.48 | -105.76 | -17937.63 |
| `entry=55,conf=0.62` | 97 | 24.7% | -10258.48 | -105.76 | -17937.63 |
| `entry=55,conf=0.7` | 97 | 24.7% | -10258.48 | -105.76 | -17937.63 |
| `entry=65,conf=0.55` | 62 | 22.6% | -5322.00 | -85.84 | -13930.99 |
| `entry=65,conf=0.62` | 62 | 22.6% | -5322.00 | -85.84 | -13930.99 |
| `entry=65,conf=0.7` | 62 | 22.6% | -5322.00 | -85.84 | -13930.99 |
| `entry=75,conf=0.55` | 25 | 12.0% | +264.87 | +10.59 | -7579.46 |
| `entry=75,conf=0.62` | 25 | 12.0% | +264.87 | +10.59 | -7579.46 |
| `entry=75,conf=0.7` | 25 | 12.0% | +264.87 | +10.59 | -7579.46 |

## Cutover relevance

Plan Phase 8 requires *walk-forward positive OOS on ≥3/4 folds* before Stage C. This report supplies that input; it does not by itself authorise a stage change, and the remaining criteria (uptime, data quality, agreement rate, ≥20 entry-eligible engine signals) are measured live during the shadow window, not here.
