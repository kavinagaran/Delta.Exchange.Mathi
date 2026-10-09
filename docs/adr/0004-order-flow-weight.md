# ADR 0004 — Ship v1 with order-flow weighted at zero

**Status:** Accepted · 2026-07-25
**Context:** `Trend_Engine.md` §11, §9.4, §18.2

## Problem

§11 fixes the initial transparent score weighting:

```
30% higher-timeframe trend      20% order-flow confirmation
15% market structure            10% breakout quality
15% lower-timeframe momentum    10% derivatives context
```

and §11 also requires that "all weights and thresholds must be configurable and
validated using walk-forward testing".

These two requirements conflict on day one. Order-flow features (§9.4: trade
imbalance over 10 s / 30 s / 2 m / 5 m, depth imbalance, microprice, book
pressure persistence, replenishment rate) require recorded microstructure. **The
repo has none.** Candle history is available from REST back years; L2 and trade
tick data only begins accumulating when Phase 2 starts recording.

Assigning 20% of a live trading signal to features that cannot be walk-forward
validated for another 3–6 months would mean shipping an unvalidated fifth of
the score.

## Decision

Ship v1 with order-flow at **weight 0**, redistributed proportionally:

| Component | §11 | v1 | Rationale |
|---|---:|---:|---|
| Higher-timeframe trend | 30% | **40%** | validated on REST candle history |
| Market structure | 15% | **20%** | validated on REST candle history |
| Lower-timeframe momentum | 15% | **20%** | validated on REST candle history |
| Order-flow confirmation | 20% | **0%** | no recorded history to validate against |
| Breakout quality | 10% | **10%** | validated on REST candle history |
| Derivatives context | 10% | **10%** | OI/funding available live on `v2/ticker` (A7) |

Order-flow **features are still computed and persisted** from Phase 2 onward —
they simply do not contribute to the score. That is what builds the history
needed to validate them.

## Enabling it later

Weights live in `config/default.yaml`. Order-flow moves off zero only when:

1. ≥ 3 months of recorded L2 and trade data exist for `BTCUSD` perpetual;
2. a walk-forward run (Phase 5) shows out-of-sample improvement over the v1
   weighting on ≥ 3 of 4 folds;
3. a parameter-sensitivity chart shows the improvement is not a single-point
   artefact (§18.4).

At that point this ADR is superseded, not silently edited.

## Scope note

Per assumption A3, order-flow applies to `BTCUSD` perpetual only. MOVE books
carry 10–12 levels quoted by what appears to be a single market maker;
microstructure inferred from them would be noise presented as signal.

## Consequences

- v1 divergence from the spec's stated weighting is explicit, bounded, and
  reversible — not a silent omission.
- The engine will be *more* conservative than the spec baseline in exactly the
  regime order-flow is meant to confirm. Consistent with §30: "the primary
  objective is not to trade frequently."
- Deferred acceptance-criteria coverage: §18.1 order-flow-level fill realism
  and part of §23.3's replay fixtures depend on the same recorded data. Both are
  labelled deferred in the Phase 5 report rather than quietly skipped.
