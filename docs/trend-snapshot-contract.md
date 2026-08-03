# TrendSnapshot contract — v1.4.0

**Frozen at v1.0.0:** 2026-07-25 · **Source:** `Trend_Engine.md` §5.3
**Producer:** `btc_trend_engine` · **Consumer:** `trend_engine_client.py` → `dashboard.py`

Freezing this unblocks the UI track to proceed in parallel with Phases 2–4.
Breaking changes require a major bump and a new document; the client rejects a
mismatched major with `CONTRACT_VERSION_MISMATCH`.

## Changelog

**v1.1.0 (2026-07-26)** — additive only, no consumer changes required. Adds
the zone decision surface (`zone`, `zone_action_allowed`, `zone_reason`,
`zone_option_type`, `zone_itm_steps`) for the operator score-band spec; see
`btc_trend_engine/signals/zones.py`. The client compares major only, so a
v1.0.0 consumer keeps working unchanged against a v1.1.0 producer — pinned by
`test_the_minor_bump_to_1_1_0_is_not_a_breaking_change`.

**v1.2.0 (2026-07-27)** — additive gate detail for the deployed policy:
directional-zone confidence and regime-alignment gates are included in the
matrix. `risk_lock_clear` is now explicitly `status: "DEFERRED"` and
`required: false`: account risk is checked by the per-user execution
controller, not falsely reported as an engine pass.

**v1.3.0 (2026-07-29)** — the production score profile now reports closed
`1h`, `30m`, `15m`, and `5m` candles. The decision remains committed on the
closed 5m candle. The model version changed so the new profile creates a
distinct signal identity for both paper and real execution controllers.

**v1.4.0 (2026-08-03)** — additive `trigger_adx` field containing the raw
committed 5-minute ADX. The execution controller uses it to close an open
`SHORT_MOVE` when the calm-market condition ends at ADX >= 25.

## Payload

```json
{
  "schema_version": "1.4.0",
  "symbol": "BTCUSD",
  "timestamp": "2026-07-25T10:15:00Z",
  "candle_close_utc": "2026-07-25T10:15:00Z",
  "signal_id": "0f3c9a2e1b7d4a86",

  "regime": "TREND_UP",
  "regime_since": "2026-07-25T09:40:00Z",
  "direction": 1,
  "trend_score": 72.0,
  "confidence": 0.68,
  "trigger_adx": 31.4,

  "forecast_horizon_seconds": 900,
  "expected_return_bps": 18.0,
  "expected_absolute_move_bps": 43.0,
  "forecast_volatility_bps": 39.0,
  "jump_probability": 0.07,

  "invalidation_price": "116250.0",
  "suggested_stop_bps": 29.0,

  "entry_allowed": true,
  "signal_ttl_seconds": 300,

  "components": [
    {"name": "higher_timeframe_trend", "weight": 0.40, "score": 78.0, "available": true},
    {"name": "market_structure",       "weight": 0.20, "score": 65.0, "available": true},
    {"name": "lower_timeframe_momentum","weight": 0.20, "score": 71.0, "available": true},
    {"name": "order_flow",             "weight": 0.00, "score": null, "available": false},
    {"name": "breakout_quality",       "weight": 0.10, "score": 60.0, "available": true},
    {"name": "derivatives_context",    "weight": 0.10, "score": 55.0, "available": true}
  ],
  "timeframes": [
    {"timeframe": "1h",  "bias": 1, "score": 62.0, "closed_candle_utc": "2026-07-25T10:00:00Z"},
    {"timeframe": "30m", "bias": 1, "score": 74.0, "closed_candle_utc": "2026-07-25T10:00:00Z"},
    {"timeframe": "15m", "bias": 1, "score": 70.0, "closed_candle_utc": "2026-07-25T10:15:00Z"},
    {"timeframe": "5m",  "bias": 1, "score": 66.0, "closed_candle_utc": "2026-07-25T10:15:00Z"}
  ],
  "gates": [
    {"name": "data_fresh",        "passed": true,  "detail": null},
    {"name": "book_valid",        "passed": true,  "detail": null},
    {"name": "clock_in_sync",     "passed": true,  "detail": null},
    {"name": "spread_acceptable", "passed": true,  "detail": null},
    {"name": "depth_sufficient",  "passed": true,  "detail": null},
    {"name": "edge_exceeds_cost", "passed": true,  "detail": null},
    {"name": "regime_tradeable",  "passed": true,  "detail": null},
    {"name": "directional_confidence", "passed": true, "detail": "confidence meets the minimum"},
    {"name": "regime_matches_directional_zone", "passed": true, "detail": "TREND_UP agrees with CE_2_ITM"},
    {"name": "risk_lock_clear", "passed": false, "required": false, "status": "DEFERRED", "detail": "verified per account at entry"}
  ],

  "reason_codes": ["1H_TREND_UP", "15M_BREAKOUT_CONFIRMED", "5M_PULLBACK_RECOVERY"],
  "data_quality": "OK",
  "feature_set_version": "v1.0.0",
  "model_version": "trend-rules-v1.1.0"
}
```

## Field rules

| Field | Rule |
|---|---|
| `schema_version` | semver; client compares **major** only |
| `timestamp` | UTC, RFC3339, `Z` suffix. Never local time |
| `candle_close_utc` | the closed candle this decision is derived from — the join key for shadow comparison. **Never wall clock** |
| `signal_id` | stable hash of (symbol, candle_close_utc, feature_set_version, model_version). Deterministic: same inputs ⇒ same id |
| `direction` | `-1` \| `0` \| `+1` |
| `trend_score` | `-100.0 … +100.0`, `100·tanh(raw)` |
| `confidence` | `0.0 … 1.0` |
| `trigger_adx` | raw ADX from the committed 5-minute trigger candle, or `null` when unavailable |
| `invalidation_price` | **string**, parsed with `Decimal`. Never a float |
| `entry_allowed` | `false` whenever `data_quality != "OK"` — invariant, tested |
| `zone` | `CE_2_ITM` \| `PE_2_ITM` \| `SHORT_MOVE` \| `HOLD` (v1.1.0) |
| `zone_action_allowed` | whether the zone's action may be taken now. Directional zones require minimum confidence and score/regime alignment; `SHORT_MOVE` requires neutral confirmation. It can differ from `entry_allowed`: `RANGE` is valid for a MOVE setup but not a legacy directional entry |
| `zone_itm_steps` | strike-index offset magnitude from ATM; `2` for both CE and PE under the 2026-07-26 spec (legacy used 3 for PE) |
| `signal_ttl_seconds` | `> 0`. Consumer rejects `timestamp + ttl < now` |
| `components[].score` | `null` when `available: false`; weights sum to 1.0 |
| `components[].weight` | v1 weighting per [ADR 0004](adr/0004-order-flow-weight.md) |
| `timeframes[].bias` | `-1` \| `0` \| `+1` |
| `timeframes[].timeframe` | exactly `1h`, `30m`, `15m`, `5m` in the production score profile; all are closed candles |
| `gates[]` | complete list always present; a failed required gate carries a human-readable `detail`. A `required: false, status: "DEFERRED"` gate is intentionally checked by the account execution path |
| `reason_codes` | stable machine-readable identifiers, §12.5. Treated as a public contract — snapshot-tested |

## Enumerations

**`regime`** — exactly one at a time (§8):
`TREND_UP` · `TREND_DOWN` · `RANGE` · `BREAKOUT_UP` · `BREAKOUT_DOWN` ·
`HIGH_VOL_SHOCK` · `LOW_LIQUIDITY` · `DEGRADED`

**`data_quality`** — engine-produced:
`OK` · `STALE_L1` · `STALE_L2` · `BOOK_INVALID` · `CLOCK_DRIFT` ·
`FEATURES_INCOMPLETE` · `EXCHANGE_UNSAFE`

**`data_quality`** — client-substituted, never sent by the engine
([ADR 0001](adr/0001-runtime-topology.md)):
`ENGINE_UNREACHABLE` · `SCHEMA_MISMATCH` · `SIGNAL_EXPIRED` ·
`CONTRACT_VERSION_MISMATCH` · `CLOCK_SKEW`

All client-substituted values carry `regime: "DEGRADED"`, `direction: 0`,
`entry_allowed: false`, `trend_score: 0.0`, `confidence: 0.0`.

## Invariants (property-tested, §23.4)

1. `data_quality != "OK"` ⇒ `entry_allowed == false`
2. `regime == "DEGRADED"` ⇒ `entry_allowed == false`
3. `abs(trend_score) <= 100`
4. `signal_ttl_seconds > 0`
5. No feature reads a timestamp later than `candle_close_utc`
6. `sum(c.weight for c in components) == 1.0`
7. Identical inputs produce an identical `signal_id`

## Endpoints (§22)

```
GET  /health                        liveness, no auth
GET  /status                        feed health, uptime, last snapshot age
GET  /trend/latest?symbol=BTCUSD    this contract
GET  /trend/history?symbol=&from=&to=
GET  /regime/latest?symbol=BTCUSD
GET  /features/latest?symbol=BTCUSD
GET  /risk/status                   Phase 4
POST /signals/evaluate              Phase 4
POST /admin/kill-switch             Phase 4
POST /admin/resume                  Phase 4
POST /shadow/legacy-decision        Phase 8
GET  /shadow/summary?days=N         Phase 8
```

All bind `127.0.0.1:5055` and require `X-Engine-Token` except `/health`.

## Mapping to the current zone model

`trend_score` maps directly to the executable zone policy:

| Score | Zone | Action |
|---:|---|---|
| `+40 … +100` | `CE_2_ITM` | Buy a 2-step ITM call |
| `+30 < score < +40` | `HOLD` | Keep the existing position |
| `−30 … +30` with 5m ADX below 30 | `SHORT_MOVE` | Sell the ATM MOVE straddle |
| `−40 < score < −30` | `HOLD` | Keep the existing position |
| `−100 … −40` | `PE_2_ITM` | Buy a 2-step ITM put |

At exactly ±30 the score is a `SHORT_MOVE` candidate. The current closed
5m score is actionable when its 5m ADX is below 30, subject to the remaining
execution safeguards. There is no multi-candle confirmation wait.

`HOLD` prevents a new entry and normally preserves the open position. The
execution controller makes one exit-only exception: a CE is closed below −30,
and a PE is closed above +30. It never reverses directly from that HOLD.
