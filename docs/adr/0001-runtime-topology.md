# ADR 0001 — Runtime topology

**Status:** Accepted · 2026-07-25
**Context:** `Trend_Engine.md` §2, §4, §22; repo reality per `docs/assumptions.md`

## Problem

The spec wants an asynchronous, WebSocket-driven service with Docker support.
The repo is synchronous `requests` + Flask + JSON files, started by Windows
Task Scheduler locally and systemd on EC2, with zero async and zero WebSocket
usage anywhere. Live money flows through it daily.

## Decision

**Two processes, one direction of dependency.**

```
┌───────────────────────────────┐        ┌──────────────────────────────────┐
│ dashboard.py (Flask, sync)    │        │ btc_trend_engine (async)         │
│ :5001, public via nginx       │───────▶│ uvicorn 127.0.0.1:5055           │
│ owns users/*.json             │  HTTP  │ LOOPBACK ONLY, never proxied     │
│ owns ALL order placement      │◀───────│ owns data/, never touches users/ │
│ 2 daemon threads              │ shadow │ Delta public WS + REST           │
└───────────────────────────────┘        └──────────────────────────────────┘
```

1. **The engine holds no trading credentials.** v1 and the whole shadow window
   use public market data only. Phase 4 adds a *read-only* key for the private
   feed. The trading key stays exclusively with `trend_score_live_execution.py`.
2. **The engine never reads or writes `users/`.** `risk_controls.py` owns the
   cross-process file locking; a second writer would break the contract that
   module exists to enforce.
3. **The dashboard is the only process that can POST an order.** Unchanged.
4. **Docker is built for research and CI reproducibility, not production.**
   systemd and Task Scheduler already start these processes reliably; adding a
   container runtime to a working deploy is a net-negative change to the one
   thing that currently works. Documented in the runbook.

## The fail-closed seam

`trend_engine_client.py` (repo root, sync, `requests`) is the only way the
dashboard talks to the engine. `get_snapshot()` **never raises into a trading
loop**. It substitutes a synthetic snapshot with `regime=DEGRADED,
entry_allowed=false` when any of:

| Condition | `data_quality` |
|---|---|
| connection error / timeout / non-200 | `ENGINE_UNREACHABLE` |
| response fails schema validation | `SCHEMA_MISMATCH` |
| `timestamp + signal_ttl_seconds < now` | `SIGNAL_EXPIRED` |
| `schema_version` major ≠ expected | `CONTRACT_VERSION_MISMATCH` |
| engine timestamp vs local now > 5 s | `CLOCK_SKEW` |

**No caching of the last good snapshot.** A stale-but-recent value is precisely
the failure mode §3.2 forbids. TTL is re-checked client-side as well as
engine-side, so acceptance criterion 12 holds even if the engine misreports.

Auth is `X-Engine-Token` from `.env`, bound to loopback. This is not a security
boundary — same host, same operator. It exists so a misconfigured proxy or a
stray `curl` cannot become an input to a trading decision.

## Supervision

**EC2** — `deploy/systemd/btc-trend-engine.service`: `Restart=always`,
`RestartSec=5`, `StartLimitIntervalSec=300`, `StartLimitBurst=10`. The burst
limit is deliberate: if the engine crash-loops ten times in five minutes,
systemd stops trying, the dashboard sees `ENGINE_UNREACHABLE`, and trading
fails closed. Do not set `StartLimitBurst=0`.

**Windows** — extend `start_bots.ps1` with a third process check, copying the
two workarounds that file already documents: WMI `Win32_Process.Create` to
escape the Task Scheduler job object's kill-on-close, and `cmd.exe /c … >> file
2>&1` to give the child real file-backed stdout/stderr. Add a port-5055 owner
check mirroring the existing port-5001 logic.

**Shutdown** — SIGTERM/SIGINT cancel all asyncio tasks, flush the event store,
close the WebSocket normally. WMI-created children get no console, so also poll
a `data/engine.stop` sentinel every 2 s; `scripts/stop_engine.ps1` writes it.

## Consequences

- The dashboard keeps working with the engine stopped; it just cannot trade on
  engine signals. That is the correct degraded state.
- Two runtimes and two virtualenvs to operate. Accepted: it is the price of not
  rewriting a working sync trading loop as async.
- An engine compromise cannot place an order or read account credentials.

## Alternatives rejected

- **In-process async inside Flask.** Would put a WebSocket event loop inside
  the process that places orders, coupling market-data failures to execution.
- **Engine calls the dashboard.** Inverts the dependency and would give the
  engine a path to trigger orders.
- **Docker in production.** See point 4.
