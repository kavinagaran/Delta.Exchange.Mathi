"""Single-writer enforcement (ADR 0002).

Found the hard way: starting a second engine against a live data directory
crashed on Windows, and on POSIX would have unlinked the running engine's
open capture file while it kept writing to an orphaned inode.
"""

from __future__ import annotations

import pytest

from btc_trend_engine.clock import FixedClock
from btc_trend_engine.config import load_config
from btc_trend_engine.service import EngineService
from btc_trend_engine.storage.lock import DataDirectoryLock, DataDirectoryLocked

from datetime import datetime, timezone

T0 = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)


def test_second_holder_is_refused(tmp_path):
    first = DataDirectoryLock(tmp_path)
    first.acquire()
    try:
        with pytest.raises(DataDirectoryLocked):
            DataDirectoryLock(tmp_path).acquire()
    finally:
        first.release()


def test_lock_is_reusable_after_release(tmp_path):
    with DataDirectoryLock(tmp_path):
        pass
    with DataDirectoryLock(tmp_path):  # must not be left stale
        pass


def test_lock_records_owning_pid_for_post_mortem(tmp_path):
    """The PID is written for diagnostics after the fact. On Windows the
    locked byte range makes the file unreadable while the lock is held, so
    it is only useful once the holder has gone — which is exactly when an
    operator asks who owned it."""
    import os

    with DataDirectoryLock(tmp_path) as lock:
        path = lock.path
    assert path.read_text().strip() == str(os.getpid())


def test_second_engine_cannot_start_against_a_live_data_dir(tmp_path):
    config = load_config(environ={
        "ENGINE_TOKEN": "t",
        "ENGINE_STORAGE__DATA_DIR": str(tmp_path / "data")})
    first = EngineService(config, clock=FixedClock(T0))
    try:
        with pytest.raises(DataDirectoryLocked):
            EngineService(config, clock=FixedClock(T0))
    finally:
        first.event_store.close()
        first.data_lock.release()


def test_startup_finalization_cannot_destroy_a_live_capture_file(tmp_path):
    """The regression this lock exists for: instance two must never reach the
    finalize-and-unlink pass while instance one holds the hour file open."""
    from btc_trend_engine.market_data.messages import (
        EventType, MarketEvent)

    config = load_config(environ={
        "ENGINE_TOKEN": "t",
        "ENGINE_STORAGE__DATA_DIR": str(tmp_path / "data")})
    first = EngineService(config, clock=FixedClock(T0))
    try:
        first.event_store.append(MarketEvent(
            event_type=EventType.TRADE, symbol="BTCUSD",
            exchange_timestamp=T0, receive_timestamp=T0, sequence_no=1,
            payload={"price": "1", "size": "1"}))
        first.event_store.flush()
        active = (config.storage.data_path / "events" / "BTCUSD"
                  / "2026-07-25" / "12.ndjson")
        assert active.exists()

        with pytest.raises(DataDirectoryLocked):
            EngineService(config, clock=FixedClock(T0))

        assert active.exists(), "live capture file was destroyed"
        assert list(first.event_store.replay("2026-07-25"))
    finally:
        first.event_store.close()
        first.data_lock.release()
