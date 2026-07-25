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

## What Phase 2 does NOT do

No signals, no features, no `/trend/latest` (Phase 3); no private feed, no
risk state (Phase 4); no order placement (never — ADR 0003).
