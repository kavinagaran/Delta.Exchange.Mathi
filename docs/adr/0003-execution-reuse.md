# ADR 0003 — Reuse the existing execution seam; do not rebuild the OMS

**Status:** Accepted · 2026-07-25
**Context:** `Trend_Engine.md` §15, §21; `trend_score_live_execution.py`

## Problem

§21 lists `execution/oms.py`, `execution/ioc_executor.py`, and an order-state
machine inside the new package. §15 specifies marketable IOC limits, slippage
caps, idempotent client order IDs, partial-fill handling, and reconciliation.

The repo already has all of that. `trend_score_live_execution.py` is 2,023
lines implementing exactly this contract, with 1,303 lines of tests:

- every submitted entry requests exactly 1,000 lots;
- entries are bounded IOC limit orders, never an implicit market fallback;
- a deterministic client id and durable `ENTRY_PENDING` state exist **before**
  the POST;
- response-loss recovery uses an exact, conclusive order lookup — a timeout or
  partial history scan returns `conclusive=False`;
- explicit IOC partial fills are accepted once, persisted, and protected;
- a fill is never called OPEN until the real-time exchange position agrees;
- protection failure immediately invokes a reduce-only flatten, and an
  unverified flatten remains visibly OPEN.

## Decision

**`btc_trend_engine/execution/` contains no code that can place an order.**

It contains only:

| Module | Contents |
|---|---|
| `intents.py` | `OrderIntent` / `RiskDecision` models (§5.4, §5.5) — data only |
| `order_state_machine.py` | the §15.2 state machine as a **pure validator**: asserts an observed transition is legal, raises otherwise |
| `slippage_guard.py` | pure pre-trade spread / slippage / quote-age check |
| `reconciliation.py` | pure comparator: local state vs exchange state → mismatch list |

The engine emits an `OrderIntent`. The dashboard consumes it and executes
through the existing seam. The dashboard remains the only process that POSTs to
`/v2/orders`.

## Why

Rebuilding a hardened irreversible-order path is the largest avoidable risk in
this programme. Every defect class the existing module already handles —
duplicate submission after response loss, partial fills, unverified flattens,
same-cycle monitor races — would have to be rediscovered under real money.
Acceptance criterion 14 ("existing LONG/SHORT MOVE functionality continues to
work") is directly endangered by touching it.

The spec's own §2 says "reuse compatible modules instead of duplicating
functionality" and §21 says "adapt to the existing repository". This is that
adaptation, made explicit.

## Sizing at cutover

`trend_score_auto.py` uses a fixed 1,000 lots. §14.1 wants
`risk_capital / stop_distance`. Introducing variable sizing *at the same moment*
as a new signal source changes two variables at once on real money.

**The risk manager returns `min(risk_based_lots, 1000)`** — it may only ever
*reduce* size relative to today. Lifting the ceiling is a later change with its
own observation window.

## Consequences

- Criterion 19 (trace every order to its signal) needs `signal_id` threaded
  into the module we refuse to destabilise. Done as an **opaque pass-through
  field** persisted alongside state, read by no control path, with a test
  asserting identical behaviour with and without it.
- The §15.2 state machine is validated against reality rather than driving it.
  That is weaker than the spec implies, and is the correct trade here.
