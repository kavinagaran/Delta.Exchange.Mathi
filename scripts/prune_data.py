"""Retention enforcement for the engine data store (ADR 0002).

Run daily (systemd timer / Task Scheduler):

    python scripts/prune_data.py [--dry-run]

- raw event files older than storage.raw_retention_days are deleted
  (Parquet aggregation of expiring days arrives with Phase 5's research
  pipeline; until then retention is deletion, which is stated honestly here
  rather than pretending an aggregate exists);
- market_snapshot rows older than storage.market_snapshot_retention_days
  and feature/trend rows older than storage.snapshot_retention_days are
  deleted;
- a WAL checkpoint compacts the database afterwards.

Safe to run while the engine is up: SQLite WAL allows one writer and this
script only deletes closed (gzipped) raw files, never the active hour.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete  # noqa: E402

from btc_trend_engine.config import load_config  # noqa: E402
from btc_trend_engine.storage.db import (  # noqa: E402
    create_db_engine,
    init_schema,
    make_session_factory,
    wal_checkpoint,
)
from btc_trend_engine.storage.models import (  # noqa: E402
    FeatureVectorRow,
    MarketSnapshot,
    TrendSnapshotRow,
)


def prune_raw_events(data_dir: Path, retention_days: int, *,
                     dry_run: bool) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).date()
    removed = 0
    events_root = data_dir / "events"
    if not events_root.exists():
        return 0
    for symbol_dir in events_root.iterdir():
        for day_dir in sorted(p for p in symbol_dir.iterdir() if p.is_dir()):
            try:
                day = datetime.strptime(day_dir.name, "%Y-%m-%d").date()
            except ValueError:
                continue
            if day >= cutoff:
                continue
            for path in sorted(day_dir.iterdir()):
                if path.suffix == ".ndjson":
                    continue  # never touch a live (plain) file
                print(f"  {'would remove' if dry_run else 'removing'} {path}")
                if not dry_run:
                    path.unlink()
                removed += 1
            if not dry_run and not any(day_dir.iterdir()):
                day_dir.rmdir()
    return removed


def prune_rows(session_factory, cutoff_iso: str, *, dry_run: bool) -> int:
    if dry_run:
        return 0
    total = 0
    with session_factory() as session:
        for model, column in (
            (MarketSnapshot, MarketSnapshot.captured_at_utc),
            (FeatureVectorRow, FeatureVectorRow.as_of_utc),
            (TrendSnapshotRow, TrendSnapshotRow.candle_close_utc),
        ):
            result = session.execute(delete(model).where(column < cutoff_iso))
            total += result.rowcount or 0
        session.commit()
    return total


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config = load_config()
    storage = config.storage
    data_dir = storage.data_path

    removed = prune_raw_events(data_dir, storage.raw_retention_days,
                               dry_run=args.dry_run)
    print(f"raw event files removed: {removed}")

    engine = create_db_engine(data_dir)
    init_schema(engine)
    sessions = make_session_factory(engine)
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=storage.market_snapshot_retention_days))
    rows = prune_rows(sessions, cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
                      dry_run=args.dry_run)
    print(f"db rows removed: {rows}")
    if not args.dry_run:
        wal_checkpoint(engine)
    return 0


if __name__ == "__main__":
    sys.exit(main())
