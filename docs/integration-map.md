# Integration map

Where the new engine touches the existing system, and — more importantly —
where it must not.

## Process and ownership

| Process | Owns | Credentials | Started by |
|---|---|---|---|
| `dashboard.py` :5001 | `users/<user>/*.json`, all order placement | trading key | Task Scheduler / systemd |
| `Delta_Straddle_Live.py` | scheduled MOVE entries/exits | trading key | Task Scheduler / systemd |
| `tp_monitor.py` (per user+slot) | TP/SL/TSL protection | trading key | spawned by dashboard |
| `btc_trend_engine` :5055 | `data/` | **none** (v1) · read-only key (P4) | new unit / `start_bots.ps1` |

## Modules that must not be destabilised

| Module | Why |
|---|---|
| `trend_score_live_execution.py` | the irreversible-order seam ([ADR 0003](adr/0003-execution-reuse.md)) |
| `risk_controls.py` | cross-process locking, audit, trading-day view |
| `move_decision.py` | MOVE forecast core, wrapped in Phase 7 |
| `Delta_Straddle_Live.py` | the live MOVE path; criterion 14 |
| `tp_monitor.py` | protection |

Their test suites are equally protected: `test_trend_score_live_execution.py`,
`test_dashboard_order_safety.py`, `test_tp_monitor_safety.py`,
`test_dashboard_dry_run_isolation.py`, `test_dashboard_account_credential_safety.py`,
`test_risk_controls.py`, `test_trend_risk_controls.py`, `test_move_*.py`.

## Seams, by phase

### Phase 3 — read-only
- New: `trend_engine_client.py` (root, sync, fail-closed — [ADR 0001](adr/0001-runtime-topology.md))
- New dashboard routes: `GET /api/engine/{snapshot,health,status}`
  (session-authed proxies; the browser never reaches :5055)
- **No trading path touched.**

### Phase UI — contract-driven
- `templates/base.html:36-45` — nav splits into primary items + `.nav-gap` +
  `trend-engine` at the bottom
- `mv_btc_bot/lib/main.dart:367-409` — `appPages` reordered to match;
  `kWebAssetRevision` (`:38`) **must** be bumped on any static/template change
  or the WebView serves stale assets
- New `static/js/{format,stats,slots,protection,charts,engine}.js` +
  `vendor/chart.umd.min.js`

### Phase 4 — risk
- `btc_trend_engine/risk/risk_manager.py` **wraps** `risk_controls.evaluate_entry`
  and `risk_controls.risk_based_lots`; `risk_controls.py` itself is unmodified
- Sizing returns `min(risk_based_lots, 1000)` — may only reduce
- Kill switches latch in `data/risk_kill_switches.json`; engine routes
  `GET /risk/status`, `POST /admin/{kill-switch,resume}`
- Dashboard `GET /api/engine/risk` is **read-only** — no proxy can fire or
  clear a switch, so a browser page cannot disarm the safety layer. It fails
  closed (`any_active: true`) when the engine is unreachable
- `market_data/delta_private_ws.py` exists but ships disabled (assumption A10)

### Phase 5 — backtest (no runtime seam)
- `btc_trend_engine/backtest/*` imports the production `features/`, `signals/`
  and `risk/` unmodified (§28) — there is no backtest-only calculation
- `scripts/backtest.py fetch|run`; cache in `data/backtest/` (gitignored),
  report at `docs/backtest-report.md` (committed)
- Measures directional edge on the **perpetual**, not the option overlay
  ([ADR 0005](adr/0005-backtest-scope.md)) — so it can *block* Stage C but
  cannot by itself settle the "counterfactual expectancy" criterion
- Touches no running process and no `users/` data

### Phase 7 — MOVE
- `btc_trend_engine/move/*` **wraps** `move_decision.forecast_move_distribution`
  and `move_decision.evaluate_move_decision`, feeding them
  `mu`/`sigma`/`p_jump`/`regime`/`confidence` from the snapshot with
  `drift_used = drift × confidence`
- Regression test: identical outputs through the wrapper for legacy inputs

### Phase 8a — shadow (done)
- Hooked at `dashboard.py:_collect_trend_score_auto_signal` — every completed
  5m score event, DRY RUN and LIVE alike (`_trend_auto_loop` reaches this
  through both `_maybe_auto_trend_score_cycle` and
  `_maybe_auto_trend_score_live_cycle`) — rather than at `_trend_auto_loop`
  itself as the plan sketch assumed; one hook, no duplicate posts
- `trend_engine_client.post_legacy_decision` is fire-and-forget through a
  bounded queue + daemon worker (`SHADOW_QUEUE_MAX=256`), not an inline
  blocking POST — an 8s timeout is not "zero latency" for a loop placing real
  orders. Survival test (`tests/test_shadow_survival.py`) written first,
  proving a 30s hang / exception / 500 / garbage response cannot reach the
  caller, and that a full queue drops (counted) rather than blocking
- Engine: `POST /shadow/legacy-decision` joins on `candle_close_utc`, never
  wall clock, and classifies with `btc_trend_engine/signals/shadow.py`
  (`engine_no_snapshot`/`engine_data_degraded` are NOT disagreements — an
  absent opinion is a different fact from a wrong one); `GET /shadow/summary`
- `dashboard.py: GET /api/engine/shadow` proxies read-only; no route can post
  a decision from a browser — only the trend-auto loop, via
  `trend_engine_client` directly
- Panel on `/trend-engine` (`kWebAssetRevision` bumped)

### Phase 8b–e — cutover (not started)
- Cutover flips `TREND_SIGNAL_SOURCE` (`legacy` | `engine`) in Bot Config
  behind an explicit confirmation; `dashboard.py:_maybe_auto_trend_score_cycle`
  reads the zone from the snapshot instead of `trend_score_auto.score_zone()`
- Rollback is always a config flip, never a code change

## Legacy retired at Stage E

Modules `trend_engine.py`, `trend_scenario.py`, `trend_score_auto.py`,
`trend_engine_live.py`; the `dashboard.py` legacy chain (`_trend_snapshot`,
`/api/trend`, `/api/trend-engine` and its `_trend_engine_*` preview/token
chain, `_maybe_auto_trend_entry`, `/api/trend-entry`, `_trend_lot_plan`,
`_debounced_hourly_trend`, the `/trend-engine-legacy` page); and ten legacy
trend test files. Only after 14 days at full lots.

## Already retired (Phase 0)

`/api/me`, `/api/external-options`, `/download/apk`, `/api/trend-entry/preview`,
`/api/trend-auto/status`, `/api/manual-entry`, `/api/manual-entry/preview`, and
the 18-function discretionary MOVE entry cluster they were the only callers of.
The protection-failure-forces-flatten property those tests covered was ported to
the live path in `test_move_safety.py`.
