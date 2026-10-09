"""Event store crash-safety and replay determinism; WS reconnect behaviour
with an injected transport (§6.4, §17)."""

from __future__ import annotations

import asyncio
import gzip
import json
import random
from datetime import datetime, timedelta, timezone

import pytest

from btc_trend_engine.clock import FixedClock
from btc_trend_engine.config import ReconnectConfig
from btc_trend_engine.market_data.delta_public_ws import (
    BackoffSchedule,
    DeltaPublicWs,
)
from btc_trend_engine.market_data.messages import EventType, MarketEvent
from btc_trend_engine.storage.event_store import (
    CorruptEventFile,
    EventStore,
    finalize_stale_plain_files,
)

T0 = datetime(2026, 7, 25, 10, 30, tzinfo=timezone.utc)


def _event(offset_seconds: float = 0.0, seq: int | None = None) -> MarketEvent:
    ts = T0 + timedelta(seconds=offset_seconds)
    return MarketEvent(
        event_type=EventType.TRADE, symbol="BTCUSD",
        exchange_timestamp=ts, receive_timestamp=ts, sequence_no=seq,
        payload={"price": "63900", "size": "1"},
    )


# ── event store ─────────────────────────────────────────────────────────
def test_append_and_replay_round_trip(tmp_path):
    store = EventStore(tmp_path, "BTCUSD", clock=FixedClock(T0))
    for i in range(5):
        store.append(_event(i, seq=i))
    store.close()
    records = list(store.replay("2026-07-25"))
    assert len(records) == 5
    assert [r["seq"] for r in records] == [0, 1, 2, 3, 4]
    assert records[0]["type"] == "trade"


def test_hour_rotation_gzips_previous_file(tmp_path):
    store = EventStore(tmp_path, "BTCUSD", clock=FixedClock(T0))
    store.append(_event(0))
    store.append(_event(3600))  # 11:30 → new hour file
    store.close()
    directory = tmp_path / "events" / "BTCUSD" / "2026-07-25"
    names = sorted(p.name for p in directory.iterdir())
    assert names == ["10.ndjson.gz", "11.ndjson"]
    # replay reads both transparently, in order
    assert len(list(store.replay("2026-07-25"))) == 2


def test_torn_final_line_is_dropped_not_fatal(tmp_path):
    store = EventStore(tmp_path, "BTCUSD", clock=FixedClock(T0))
    for i in range(3):
        store.append(_event(i, seq=i))
    store.close()
    path = tmp_path / "events" / "BTCUSD" / "2026-07-25" / "10.ndjson"
    with open(path, "ab") as handle:  # simulate a hard kill mid-append
        handle.write(b'{"v":"1.0.0","type":"trade","sym')
    records = list(store.replay("2026-07-25"))
    assert [r["seq"] for r in records] == [0, 1, 2]


def test_torn_line_in_finalized_file_is_corruption(tmp_path):
    directory = tmp_path / "events" / "BTCUSD" / "2026-07-25"
    directory.mkdir(parents=True)
    with gzip.open(directory / "09.ndjson.gz", "wb") as handle:
        handle.write(b'{"seq": 1}\n{"broken\n{"seq": 2}\n')
    store = EventStore(tmp_path, "BTCUSD", clock=FixedClock(T0))
    with pytest.raises(CorruptEventFile):
        list(store.replay("2026-07-25"))


def test_crash_recovery_continues_same_hour_file_appending(tmp_path):
    first = EventStore(tmp_path, "BTCUSD", clock=FixedClock(T0))
    first.append(_event(0, seq=1))
    first.close()
    # process restart within the same hour
    second = EventStore(tmp_path, "BTCUSD", clock=FixedClock(T0))
    second.append(_event(10, seq=2))
    second.close()
    assert [r["seq"] for r in second.replay("2026-07-25")] == [1, 2]


def test_stale_plain_files_are_finalized_on_startup(tmp_path):
    store = EventStore(tmp_path, "BTCUSD", clock=FixedClock(T0))
    store.append(_event(0))
    store.close()  # crash left 10.ndjson plain
    count = finalize_stale_plain_files(tmp_path, "BTCUSD")
    assert count == 1
    directory = tmp_path / "events" / "BTCUSD" / "2026-07-25"
    assert [p.name for p in directory.iterdir()] == ["10.ndjson.gz"]


def test_replay_is_deterministic(tmp_path):
    store = EventStore(tmp_path, "BTCUSD", clock=FixedClock(T0))
    for i in range(50):
        store.append(_event(i * 60, seq=i))  # spans two hours
    store.close()
    first = list(store.replay("2026-07-25"))
    second = list(store.replay("2026-07-25"))
    assert first == second
    assert [r["seq"] for r in first] == list(range(50))


# ── backoff ─────────────────────────────────────────────────────────────
def test_backoff_grows_caps_and_resets():
    config = ReconnectConfig(initial_backoff_seconds=1.0,
                             max_backoff_seconds=60.0, jitter_fraction=0.0)
    schedule = BackoffSchedule(config, random.Random(7))
    delays = [schedule.next_delay() for _ in range(8)]
    assert delays == [1, 2, 4, 8, 16, 32, 60, 60]
    schedule.reset()
    assert schedule.next_delay() == 1


def test_backoff_jitter_stays_within_fraction():
    config = ReconnectConfig(initial_backoff_seconds=4.0,
                             max_backoff_seconds=60.0, jitter_fraction=0.25)
    schedule = BackoffSchedule(config, random.Random(7))
    first = schedule.next_delay()
    assert 3.0 <= first <= 5.0


# ── ws consumer with fake transport ─────────────────────────────────────
class FakeConnection:
    """fail_after=True drops with ConnectionError after the frames;
    fail_after=False holds the stream open until close() (a live feed does
    not end politely mid-session, and run() treats stream end as a
    disconnect, which would race the assertions here)."""

    def __init__(self, frames: list[str], fail_after: bool = True) -> None:
        self._frames = list(frames)
        self._fail_after = fail_after
        self._closed_event = asyncio.Event()
        self.sent: list[str] = []
        self.closed = False

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for frame in self._frames:
            yield frame
        if self._fail_after:
            raise ConnectionError("feed dropped")
        await self._closed_event.wait()

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        self.closed = True
        self._closed_event.set()


def _ws_config() -> ReconnectConfig:
    return ReconnectConfig(initial_backoff_seconds=0.01,
                           max_backoff_seconds=0.02, jitter_fraction=0.0)


TS_US = 1_784_968_499_014_486
FRAME = json.dumps({"type": "all_trades", "symbol": "BTCUSD",
                    "timestamp": TS_US, "price": "63900", "size": "1",
                    "buyer_role": "taker"})


@pytest.mark.asyncio
async def test_ws_subscribes_dispatches_and_reconnects_after_drop():
    first = FakeConnection([FRAME])
    second = FakeConnection([FRAME], fail_after=False)
    connections = [first, second]
    events, disconnects = [], []

    async def connect():
        return connections.pop(0)

    ws = DeltaPublicWs(
        connect=connect,
        channels=[{"name": "all_trades", "symbols": ["BTCUSD"]}],
        on_event=events.append, on_disconnect=disconnects.append,
        reconnect=_ws_config(),
    )
    task = asyncio.create_task(ws.run())
    for _ in range(200):
        if len(events) >= 2:
            break
        await asyncio.sleep(0.01)
    ws.stop()
    await asyncio.wait_for(task, timeout=2)

    assert ws.connect_count == 2
    assert len(disconnects) == 1 and "ConnectionError" in disconnects[0]
    # Re-subscription happened on the fresh connection (→ fresh l2 snapshot).
    assert first.sent and second.sent
    assert first.closed and second.closed
    assert len(events) == 2
    assert events[0].event_type is EventType.TRADE


@pytest.mark.asyncio
async def test_ws_records_malformed_frames_without_dying():
    connection = FakeConnection(
        ["not json", json.dumps({"type": "all_trades", "symbol": "",
                                 "timestamp": TS_US}), FRAME],
        fail_after=False)
    events = []

    async def connect():
        return connection

    ws = DeltaPublicWs(
        connect=connect, channels=[], on_event=events.append,
        on_disconnect=lambda r: None, reconnect=_ws_config(),
    )
    task = asyncio.create_task(ws.run())
    for _ in range(200):
        if events:
            break
        await asyncio.sleep(0.01)
    ws.stop()
    await asyncio.wait_for(task, timeout=2)
    assert len(events) == 1
    assert ws.normalization_errors == 2


@pytest.mark.asyncio
async def test_ws_sends_subscription_on_every_connect():
    connection = FakeConnection([], fail_after=False)

    async def connect():
        return connection

    channels = [{"name": "l2_updates", "symbols": ["BTCUSD"]}]
    ws = DeltaPublicWs(
        connect=connect, channels=channels, on_event=lambda e: None,
        on_disconnect=lambda r: None, reconnect=_ws_config(),
    )
    task = asyncio.create_task(ws.run())
    for _ in range(100):
        if connection.sent:
            break
        await asyncio.sleep(0.01)
    ws.stop()
    await asyncio.wait_for(task, timeout=2)
    payload = json.loads(connection.sent[0])
    assert payload == {"type": "subscribe", "payload": {"channels": channels}}
