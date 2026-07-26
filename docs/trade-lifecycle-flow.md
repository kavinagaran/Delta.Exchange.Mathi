# Trade lifecycle — score to order to exit (2026-07-26 zone spec)

Both **Paper (DRY RUN)** and **Real (LIVE)** run the *same* decision path and
the *same* score source. The only differences are which namespace state is
written to and whether an order actually reaches the venue.

## Decision flow

```mermaid
flowchart TD
    A["_trend_auto_loop<br/>every 15s"] --> B{"TREND_SCORE_AUTO_MODE"}
    B -->|disabled| Z1["stop: no cycle"]
    B -->|dry_run| C["_maybe_auto_trend_score_cycle<br/>PAPER namespace"]
    B -->|live| D["_maybe_auto_trend_score_live_cycle<br/>LIVE namespace"]

    C --> E["_collect_trend_score_auto_signal()"]
    D --> E

    E --> F["trend_engine_client.get_snapshot()<br/>GET :5055/trend/latest"]
    F --> G{"data_quality == OK ?"}
    G -->|"no — unreachable / stale /<br/>schema or clock mismatch"| Z2["RAISE — fail closed<br/>(score 0.0 would be SHORT_MOVE,<br/>so an outage must not fall through)"]
    G -->|yes| H["score = snapshot.trend_score<br/>zone  = snapshot.zone"]

    H --> I{"zone"}
    I -->|HOLD<br/>25 &lt; abs&#40;score&#41; &lt; 35| Z3["RAISE — no new action.<br/>Open position is KEPT"]
    I -->|"CE_2_ITM / PE_2_ITM / SHORT_MOVE"| J["plan_score_transition&#40;score, signal_key&#41;"]

    J --> K{"signal_key already consumed?"}
    K -->|yes| Z4["NOOP — idempotent per closed candle<br/>(one candle cannot fire twice)"]
    K -->|no| L{"owned position?"}

    L -->|none| M["action = OPEN"]
    L -->|"zone == position zone"| Z5["HOLD — already correct, no churn"]
    L -->|"zone != position zone"| N["action = CLOSE_THEN_OPEN"]

    N --> O["close existing position"]
    O --> M
    M --> P{"target zone"}
    P -->|CE_2_ITM| Q["select_directional_option<br/>ATM index − 2 → C-BTC-*"]
    P -->|PE_2_ITM| R["select_directional_option<br/>ATM index + 2 → P-BTC-*"]
    P -->|SHORT_MOVE| S["select_move_contract<br/>ATM → MV-BTC-*, SHORT"]

    Q --> T["validate_fixed_entry&#40;&#41;<br/>ZONE_EXECUTION table"]
    R --> T
    S --> T
    T --> U{"zone ↔ instrument/side/prefix<br/>consistent?"}
    U -->|no| Z6["RAISE — unsupported zone.<br/>Never guesses a label"]
    U -->|yes| V["risk_controls.evaluate_entry<br/>daily cap · loss lock · open risk"]
    V -->|refused| Z7["no order"]
    V -->|allowed| W["place bounded IOC limit order<br/>(LIVE) / simulate (PAPER)"]
    W --> X["persist state + trend_score_zone<br/>spawn tp_monitor"]
```

## Exit flow — two independent paths

A position leaves **only** by one of these. They cannot both be disabled by
the same failure, which is the point.

```mermaid
flowchart LR
    subgraph SIG["Path 1 — signal (candle close, every 5m)"]
      A1["new committed snapshot"] --> A2{"zone vs position zone"}
      A2 -->|same| A3["hold"]
      A2 -->|"HOLD band"| A4["hold — NOT an exit"]
      A2 -->|different| A5["CLOSE_THEN_OPEN"]
    end

    subgraph PROT["Path 2 — protection (continuous, price-driven)"]
      B1["tp_monitor.py<br/>separate process per user+slot"] --> B2{"price vs levels"}
      B2 -->|"TP hit"| B3["close"]
      B2 -->|"SL hit"| B3
      B2 -->|"trailing SL hit"| B3
    end

    A5 --> C1["position flat"]
    B3 --> C1
```

**Why `tp_monitor` is a separate process, not a branch of the loop:** it acts
on price continuously and must keep protecting an open position even if the
engine is unreachable, the dashboard is restarting, or the signal cycle is
raising. Protection is never gated on a candle close or on engine health.

## Zone bands

| Score | Zone | Action | Instrument |
|---|---|---|---|
| `+35 … +100` | `CE_2_ITM` | buy | 2-step ITM call (ATM − 2) |
| `+25 … +35` | `HOLD` | none — keep open position | — |
| `−25 … +25` | `SHORT_MOVE` | sell | ATM MOVE straddle |
| `−35 … −25` | `HOLD` | none — keep open position | — |
| `−100 … −35` | `PE_2_ITM` | buy | 2-step ITM put (ATM + 2) |

Boundaries are inclusive at ±35 and ±25. Bands live in exactly one place,
`btc_trend_engine/signals/zones.py`; `trend_score_auto.score_zone` delegates
to it so the two cannot drift.

## Idempotency and repaint safety

- The decision score is committed **only at candle close**. `signal_id` is a
  deterministic hash of that closed candle, and `completed_candle_signal_key`
  dedupes on it, so one candle can never produce two entries.
- `/trend/live` updates every ~5s and *does* repaint within the bar. It
  carries no `signal_id`, no `entry_allowed` and no `gates`, so the order
  path structurally cannot consume it. Display only.

## Paper vs Real — what actually differs

| | Paper (DRY RUN) | Real (LIVE) |
|---|---|---|
| Score source | `btc_trend_engine` | `btc_trend_engine` (same) |
| Zone logic | identical | identical |
| Exit rules | identical | identical |
| State namespace | `users/<u>/dry_run/` | `users/<u>/` |
| Risk ledger | dry-run history only | real history only |
| Order | simulated | bounded IOC to the venue |
