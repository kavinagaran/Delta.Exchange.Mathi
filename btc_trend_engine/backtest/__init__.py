"""Event-driven backtester (Trend_Engine.md §23.3, plan Phase 5).

**This package imports the production `features/`, `signals/` and `risk/`
code and drives it unmodified** (§28). There is no backtest-only copy of any
calculation: a divergence between what is measured here and what trades live
is the single failure mode this design exists to prevent.

Two limitations are load-bearing and are repeated in the generated report
rather than buried here:

1. **Candle-level, not order-flow-level.** Phase 2 recording started
   recently, so there is no multi-month L2/trade archive to replay. Fills are
   therefore simulated against candle OHLC plus a configured spread, not a
   reconstructed book. Order-flow-level replay is deferred until 3–6 months
   of recordings exist — the same reason ADR 0004 ships the order-flow score
   weight at zero.
2. **The signal is measured on the perpetual, not on the options actually
   traded.** The live system converts a direction into an ITM option
   purchase. Simulating that overlay would require a historical option chain
   and a pricing model whose error would swamp the thing being measured.
   Perp P&L here is a measure of *directional edge*, and is not a forecast of
   the live account's P&L.
"""
