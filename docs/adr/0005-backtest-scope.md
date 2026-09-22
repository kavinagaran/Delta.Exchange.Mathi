# ADR 0005 — The backtester measures directional edge on the perp, not option P&L

**Status:** Accepted · 2026-07-25
**Context:** `Trend_Engine.md` §23.3, §28; plan Phase 5; [ADR 0003](0003-execution-reuse.md)

## Problem

Phase 5 gates Stage C of the cutover: no live order may be driven by the new
signal until walk-forward shows positive out-of-sample results. That requires
deciding *what* is being backtested.

The engine emits a direction on `BTCUSD` (perpetual). The live system does not
trade the perpetual — `trend_score_live_execution.py` converts a direction into
a purchase of an ITM `C-BTC-*` / `P-BTC-*` option. So a faithful end-to-end
backtest would have to simulate the option leg: strike selection, the option's
own bid/ask, implied volatility, time decay, and settlement.

## Decision

**The backtester simulates the perpetual and reports directional edge.** It
does not model the option overlay.

Every generated report states this in its own opening section, above any
number, rather than in a footnote.

## Why

Simulating the option leg requires a historical option chain (strikes, quotes,
IV surface) that we do not have and cannot reconstruct: Delta's public history
endpoint serves candles, and Phase 2 capture is days old, not months. The
alternative — pricing synthetic options with Black-Scholes against an assumed
IV — would introduce a model whose error is plausibly larger than the signal
being measured. A backtest whose dominant error term is an invented volatility
assumption is worse than no backtest, because it produces a confident number.

Directional edge is also the thing actually in question at cutover. The option
overlay is unchanged between the legacy engine and the new one; only the
direction and timing differ. Measuring the part that changes, and being
explicit that it is only that part, answers the real question.

## Consequences

- **Phase 8's "counterfactual expectancy ≥ legacy actual" criterion cannot be
  settled by this report alone.** It compares a perp-P&L proxy against realised
  option P&L — different units. That criterion must be evaluated during the
  shadow window against the engine's own recorded decisions, not here.
- Absolute P&L figures in the report are not a forecast of account P&L and must
  never be quoted as one.
- **Sign and consistency are what transfer**: if directional edge is absent on
  the perp, the option overlay cannot manufacture it, so a negative result here
  is a genuine blocker even though a positive one is not sufficient.
- Order-flow-level replay is separately deferred until 3–6 months of Phase 2
  recordings exist — the same data shortage that keeps the order-flow score
  weight at zero ([ADR 0004](0004-order-flow-weight.md)).
- Revisit if an option-chain archive becomes available, or after enough live
  shadow data accumulates to compare option-level outcomes directly.
