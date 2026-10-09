# ADR 0002 — Storage

**Status:** Accepted · 2026-07-25
**Context:** `Trend_Engine.md` §17; `risk_controls.py` locking contract

## Problem

§17 asks for an append-only raw event store, a relational database for
normalised state, Parquet for research, and optionally TimescaleDB. The repo
stores everything in per-user JSON files guarded by `risk_controls.py` locks.

## Decision

| Layer | Technology | Path | Writer |
|---|---|---|---|
| Append-only raw events | gzip NDJSON, hourly files | `data/events/<symbol>/<date>/<HH>.ndjson.gz` | engine only |
| Normalised state + records | **SQLite (WAL)** | `data/engine.db` | engine only |
| Research datasets | Parquet (Phase 5) | `data/research/*.parquet` | offline script |
| Time-series DB | **not built** | — | — |

### SQLite, not PostgreSQL or TimescaleDB

One symbol, one operator, one writer, on the order of 10⁵–10⁶ rows/day. WAL
mode gives one writer plus many concurrent readers with no server, no port, no
credential, no backup daemon, and survives a reboot with zero operator action.
PostgreSQL adds a service to supervise and a second failure mode on a box whose
main risk is unattended operation; Timescale is unjustifiable at this scale.

Models are plain SQLAlchemy, so the migration path stays open if the data
outgrows this. Connection pragmas: `journal_mode=WAL`, `synchronous=NORMAL`,
`busy_timeout=5000`, `foreign_keys=ON`.

Tables (Alembic-migrated, each carrying `schema_version` and `created_at_utc`
per §17): `market_snapshot`, `feature_vector`, `trend_snapshot`,
`regime_transition`, `risk_decision`, `order_intent`, `order_event`,
`reconciliation`, `health_event`, `shadow_comparison`, `config_version`.

### The load-bearing invariant

> **The engine never reads or writes anything under `users/`.**

`risk_controls.py` (`account_file_lock`, `account_entry_lock`, the append-only
audit) and `tp_monitor.py` keep exclusive ownership. Consequences, all
intentional:

- Engine data loss can never corrupt live position state.
- The dashboard is the only process reading both sides; it joins them in memory.
- Post-cutover traceability (criterion 19) works by writing the engine's
  `signal_id` into `trend_state.json` as an **opaque pass-through field that no
  control path reads**, with a test asserting `trend_score_live_execution.py`
  behaves identically with and without it.

`data/` joins `users/` and `logs/` in `.gitignore`.

### Retention is Phase 2 work, not later

Raw L1/L2 for a 2,200-level book will fill an EC2 root volume. `scripts/prune_data.py`
runs daily: raw events 14 days then aggregate-to-Parquet-and-delete;
`trend_snapshot` and `feature_vector` 180 days; `market_snapshot` 30 days. Below
2 GB free, `monitoring/health.py` reports DEGRADED and stops **raw capture**
— not signal generation. Verify `df -h` on EC2 before enabling capture at all
(assumption A6).

## Consequences

- No new service to operate, back up, or secure.
- Single-writer discipline must be enforced by convention; add a test asserting
  no module under `btc_trend_engine/` references `users/`.
- Concurrent heavy analytical reads could contend with the writer. Acceptable at
  this volume; research runs against Parquet, not the live DB.
