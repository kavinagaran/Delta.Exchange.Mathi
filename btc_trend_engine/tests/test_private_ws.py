"""Read-only private feed: auth frame shape, credential handling, and the
fact that it stays disabled by default until the shape is confirmed.

The auth-frame format itself is an UNVERIFIED assumption (see the module
docstring in market_data/delta_private_ws.py and docs/assumptions.md A10).
These tests pin the code's behaviour, not the venue's contract — they prove
the signer is deterministic and the consumer degrades safely, which is what
we can actually verify from here.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from btc_trend_engine.clock import FixedClock
from btc_trend_engine.config import ReconnectConfig, load_config
from btc_trend_engine.market_data.delta_private_ws import DeltaPrivateWs, sign_ws_auth

T0 = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
RECONNECT = ReconnectConfig(initial_backoff_seconds=0.01, max_backoff_seconds=0.02,
                            jitter_fraction=0.0)


class FakeConnection:
    def __init__(self, frames: list[str]) -> None:
        self.frames = frames
        self.sent: list[str] = []
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self):
        async def gen():
            for frame in self.frames:
                yield frame
        return gen()


def test_the_private_feed_is_disabled_by_default():
    config = load_config(environ={"ENGINE_TOKEN": "t"})
    assert config.market_data.private.enabled is False


def test_config_names_env_vars_never_secret_values():
    config = load_config(environ={"ENGINE_TOKEN": "t"})
    private = config.market_data.private
    assert private.api_key_env == "ENGINE_READ_ONLY_API_KEY"
    assert private.api_secret_env == "ENGINE_READ_ONLY_API_SECRET"


def test_signing_is_deterministic_for_a_fixed_timestamp():
    first = sign_ws_auth("key-1", "secret-1", timestamp=1_800_000_000)
    second = sign_ws_auth("key-1", "secret-1", timestamp=1_800_000_000)
    assert first == second
    assert first["api-key"] == "key-1"
    assert first["timestamp"] == "1800000000"
    assert len(first["signature"]) == 64


def test_the_secret_never_appears_in_the_signed_frame():
    frame = sign_ws_auth("key-1", "super-secret-value", timestamp=1_800_000_000)
    assert "super-secret-value" not in json.dumps(frame)


def test_a_different_secret_produces_a_different_signature():
    a = sign_ws_auth("key-1", "secret-a", timestamp=1_800_000_000)
    b = sign_ws_auth("key-1", "secret-b", timestamp=1_800_000_000)
    assert a["signature"] != b["signature"]


def test_construction_without_credentials_is_refused():
    with pytest.raises(ValueError):
        DeltaPrivateWs(connect=lambda: None, api_key="", api_secret="",
                       channels=[], on_event=lambda e: None,
                       on_disconnect=lambda r: None, reconnect=RECONNECT)


def test_auth_is_sent_before_subscribe(monkeypatch):
    connection = FakeConnection(frames=[])
    events: list = []

    async def connect():
        return connection

    ws = DeltaPrivateWs(
        connect=connect, api_key="k", api_secret="s",
        channels=[{"name": "positions", "symbols": ["BTCUSD"]}],
        on_event=events.append, on_disconnect=lambda r: None,
        reconnect=RECONNECT, clock=FixedClock(T0))

    asyncio.run(ws._run_once())
    assert len(connection.sent) == 2
    first, second = (json.loads(f) for f in connection.sent)
    assert first["type"] == "auth"
    assert second["type"] == "subscribe"
    assert connection.closed


def test_an_auth_rejection_is_counted_and_does_not_raise():
    connection = FakeConnection(frames=[json.dumps({"type": "auth", "success": False})])
    events: list = []

    async def connect():
        return connection

    ws = DeltaPrivateWs(
        connect=connect, api_key="k", api_secret="s", channels=[],
        on_event=events.append, on_disconnect=lambda r: None,
        reconnect=RECONNECT, clock=FixedClock(T0))

    asyncio.run(ws._run_once())
    assert ws.auth_failures == 1
    assert events == []


def test_a_malformed_frame_is_counted_and_dropped_not_guessed_at():
    connection = FakeConnection(frames=["{not json", json.dumps(["a", "list"])])
    events: list = []

    async def connect():
        return connection

    ws = DeltaPrivateWs(
        connect=connect, api_key="k", api_secret="s", channels=[],
        on_event=events.append, on_disconnect=lambda r: None,
        reconnect=RECONNECT, clock=FixedClock(T0))

    asyncio.run(ws._run_once())
    assert ws.normalization_errors == 2
    assert events == []
