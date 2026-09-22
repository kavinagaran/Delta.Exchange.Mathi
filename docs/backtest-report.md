# Backtest & walk-forward report

Generated 2026-07-27 03:58 UTC · `scripts/backtest.py`

## What this does and does not measure

**Read this before any number below.**

- **Candle-level, not order-flow-level.** Fills are simulated against 5m OHLC plus an assumed spread, because no multi-month L2/trade archive exists yet. Microstructure effects are absent.
- **Measured on the perpetual, not on the options actually traded.** The live system converts a direction into an ITM option purchase. These figures measure *directional edge*; they are not a forecast of the live account's P&L.
- **The order-flow score component is weighted 0** (ADR 0004), so this measures the transparent score as it would actually ship today.
- Costs applied: taker fee, assumed spread, decision-to-arrival latency, and IOC cancellation when the market moves beyond the limit.

- **The score-zone replay below is the current CE/PE/HOLD lifecycle.** It deliberately reports SHORT_MOVE candidates as unpriced rather than inventing historical MOVE premiums.

## Data

- Symbol: `BTCUSD` · resolution `5m`
- Candles: 63,351
- Range: 2025-12-17 17:35 → 2026-07-25 17:30 UTC (219 days)
- Warmup per window: 3,000 candles (the 1h timeframe needs 60 closed candles)

## Baseline — shipped defaults, whole period, no selection

This is in-sample in the weak sense that the shipped thresholds were not chosen from this data, but it involves no walk-forward discipline either. It is context, not evidence.

| Window | Trades | Win rate | Total P&L | Expectancy | Expectancy 95% CI | Max DD | Profit factor |
|---|---|---|---|---|---|---|---|
| Full period | 745 | 21.7% | -72296.87 | -97.04 | [-149.35, -38.00] | -73587.08 | 0.62 |

- Entry-eligible signals: 25,495
- Signals skipped by gates: 735
- Fills rejected (IOC cancelled): 0

### Baseline by regime at entry

| Regime | Trades | Win rate | Total P&L | Expectancy | Expectancy 95% CI | Max DD | Profit factor |
|---|---|---|---|---|---|---|---|
| BREAKOUT_DOWN | 56 | 21.4% | -12586.55 | -224.76 | [-340.50, -110.55] | -12586.55 | 0.25 |
| BREAKOUT_UP | 63 | 23.8% | -5934.36 | -94.20 | [-224.77, +51.32] | -7459.15 | 0.61 |
| TREND_DOWN | 301 | 23.9% | -7678.73 | -25.51 | [-132.29, +99.03] | -18704.02 | 0.90 |
| TREND_UP | 325 | 19.4% | -46097.22 | -141.84 | [-196.15, -85.49] | -47451.19 | 0.44 |

## Score-zone policy replay — current execution logic

Directional entries require the current zone's confidence and regime alignment gates. CE/PE positions stay open through a supportive HOLD band, but exit in an opposite-direction HOLD band. This remains a perpetual directional proxy, not option P&L.

| Window | Trades | Win rate | Total P&L | Expectancy | Expectancy 95% CI | Max DD | Profit factor |
|---|---|---|---|---|---|---|---|
| CE/PE directional proxy | 650 | 21.7% | -62126.06 | -95.58 | [-157.25, -26.47] | -64021.49 | 0.66 |

- Directional entry-eligible signals: 650
- Directional invalidation exits: 36
- SHORT_MOVE candidates after 15-minute confirmation: 9,572
- SHORT_MOVE candidates excluded from P&L for missing historical premium/quote data: 9,572

**Interpretation:** this section validates the current directional state machine and its anti-churn/exit behaviour. It does **not** validate short-MOVE profitability or option P&L; an archived MOVE/option quote series is required before either can be presented as backtested account returns.

## Legacy directional walk-forward

Not run for this score-zone validation. The legacy direction-only policy is retired and its prior report is not evidence for the current CE/PE/HOLD/SHORT_MOVE lifecycle.

## Threshold sensitivity

Not run: sensitivity over the retired direction-only thresholds is not a substitute for option/MOVE quote history.

## Cutover relevance

Do not enable LIVE until the score-zone directional proxy is positive out-of-sample and a historical MOVE/option quote archive supports end-to-end replay.
