# Engine runbook — Phase 2 (market data)

Operating `btc_trend_engine` on the two deployment targets. The engine holds
no trading credentials and cannot place orders; the worst failure mode of this
process is *missing data*, which the dashboard experiences as
`ENGINE_UNREACHABLE` and treats as fail-closed.

## Prerequisites (one-time)

```bash
# both hosts, from the repo root
python -m venv .venv-engine
.venv-engine/bin/pip install -r requirements-engine.txt          # EC2
.venv-engine\Scripts\pip install -r requirements-engine.txt      # Windows
```

**Current EC2 deploy host:** `ubuntu@13.207.78.56` (also reachable at
`mathibot.duckdns.org`), checked out at `/home/ubuntu/mathi`. This is the
*real* path — an earlier draft of `deploy/systemd/btc-trend-engine.service`
assumed `/home/ubuntu/Delta.Exchange.Mathi`, which never existed on the box;
that only surfaced once an actual deploy needed the path. The live legacy
dashboard/bot also run from this same directory
(`mathi-dashboard.service`, `mathi-bot@<account>.service`) — the engine is a
separate systemd unit alongside them, not a replacement for anything.

Set in `.env` (see `.env.example`): `ENGINE_TOKEN` (any random string).
Before first capture on EC2 verify assumption A6:

```bash
df -h /          # want comfortably more than storage.min_free_gb
ss -lntp | grep 5055   # must be empty (assumption A8)
```

## Start / stop

**EC2 (systemd):**

```bash
sudo cp deploy/systemd/btc-trend-engine.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now btc-trend-engine
systemctl status btc-trend-engine
sudo systemctl stop btc-trend-engine        # graceful (SIGTERM)
```

**Windows:** `start_bots.ps1` (Task Scheduler watchdog) now starts the engine
alongside the bot and dashboard, via the same WMI pattern. Manual control:

```powershell
powershell -File scripts\stop_engine.ps1     # graceful (sentinel file)
```

A stale sentinel is cleared automatically on the next watchdog start.

## Health

```bash
curl -s http://127.0.0.1:5055/health
curl -s -H "X-Engine-Token: $ENGINE_TOKEN" http://127.0.0.1:5055/status | python -m json.tool
```

`/status` fields that matter operationally:

| Field | Healthy | Action when not |
|---|---|---|
| `data_quality` | `OK` | see table below |
| `book.state` | `valid` | transient after (re)connect; persistent → check `book.gaps` growth |
| `feeds.*.freshness` | `fresh` | `stale` on all feeds → venue or network outage |
| `clock_drift_ms` | \|x\| ≤ 500 | fix NTP/chrony on the host |
| `raw_capture_enabled` | `true` | disk below floor — prune or grow the volume |
| `ws_connects` | small, stable | rapid growth → reconnect churn; check venue status |

| `data_quality` | Meaning | Typical cause |
|---|---|---|
| `STALE_L1` | trades/ticker feed silent | disconnect, venue halt |
| `STALE_L2` | book feed silent | l2 channel trouble |
| `BOOK_INVALID` | rebuilding after gap/disconnect | normal for seconds; investigate if persistent |
| `CLOCK_DRIFT` | host clock vs venue > 500 ms sustained | NTP broken |

All of these are *engine-side*; the dashboard client adds its own
(`ENGINE_UNREACHABLE`, …) per the contract doc.

## Data store

```
data/engine.db                         SQLite (WAL). Single writer = engine.
data/events/BTCUSD/<date>/<HH>.ndjson[.gz]   raw capture, hourly
```

- Retention: schedule `python scripts/prune_data.py` daily (systemd timer or
  Task Scheduler). `--dry-run` previews.
- Below `storage.min_free_gb` free disk the engine stops **raw capture only**
  — processing and `/status` keep running — and records a `capture_stopped`
  health event. Capture resumes automatically at 2× the floor.
- Back up `data/engine.db` with `sqlite3 data/engine.db ".backup ..."` if
  desired; the raw event files are the authoritative replay source.

## Failure playbook

**Engine down / crash-looping.** systemd stops retrying after 10 crashes in
5 min — deliberately (ADR 0001). `journalctl -u btc-trend-engine -n 200` or
`logs/engine.log`, fix, then `sudo systemctl reset-failed && sudo systemctl
start btc-trend-engine`. Trading was never at risk: the dashboard fails closed.

**Persistent BOOK_INVALID with growing `gaps`.** The venue is dropping
sequence numbers on us — check their status page; nothing local to fix. The
engine resubscribes automatically and each resubscribe gets a fresh snapshot.

**`schema mismatch` at startup.** The data store was written by a different
code version. Do not delete the DB reflexively — check `git log` for the
migration note that accompanied the schema change.

**Recover events after a hard kill.** Nothing to do: the interrupted hour is
finalized on next startup, a torn final line is dropped at replay, and the
same hour continues appending. Verify with:

```bash
python - <<'PY'
from pathlib import Path
from btc_trend_engine.storage.event_store import EventStore
store = EventStore(Path("data"), "BTCUSD")
count = sum(1 for _ in store.replay("<YYYY-MM-DD>"))
print(count, "events")
PY
```

## Kill switches (Phase 4)

A kill switch latches: it survives restarts and clears only when an operator
resumes it. State lives in `data/risk_kill_switches.json`.

```bash
# what is currently latched
curl -H "X-Engine-Token: $ENGINE_TOKEN" http://127.0.0.1:5055/risk/status

# halt entries now (names: manual, max_daily_loss, consecutive_losses,
# data_quality, reconciliation_mismatch)
curl -X POST -H "X-Engine-Token: $ENGINE_TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"name":"manual","reason":"operator halt"}' \
     http://127.0.0.1:5055/admin/kill-switch

# clear one, or omit "name" to clear all
curl -X POST -H "X-Engine-Token: $ENGINE_TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"name":"manual"}' http://127.0.0.1:5055/admin/resume
```

The dashboard shows kill-switch state read-only at `/api/engine/risk` and
**cannot** fire or clear one — that is deliberate, so a browser page cannot
disarm the safety layer. If the engine is unreachable the dashboard reports
`any_active: true`, because an unreachable engine cannot prove nothing is
latched.

## Backtest (Phase 5)

```bash
python scripts/backtest.py fetch --days 220     # cache public candles
python scripts/backtest.py run                  # writes docs/backtest-report.md
```

`fetch` needs no credentials (public endpoint, 4,000 rows per request). The
cache lands in `data/backtest/` and is gitignored; the report is committed.
A full `run` over 220 days takes tens of minutes — the sensitivity sweep is
one whole-series replay per configuration, bounded by
`--sensitivity-candles`.

Read the report's opening section before quoting any number from it: results
are candle-level and measured on the perpetual, not on the options actually
traded ([ADR 0005](adr/0005-backtest-scope.md)).

## What the engine still does NOT do

No order placement, ever (ADR 0003). The private feed exists but ships
disabled pending an unverified auth shape (assumption A10). Shadow-mode
comparison and the cutover ladder are Phase 8.
