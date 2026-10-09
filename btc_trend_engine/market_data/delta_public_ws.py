"""Reconnecting Delta public WebSocket consumer (§6.4).

Connection policy on any disconnect or protocol error:

1. The owner is notified (``on_disconnect``) so it can block entries and mark
   the book invalid *before* any reconnect attempt.
2. Reconnect with capped exponential backoff plus jitter.
3. Re-subscribe; the venue answers ``l2_updates`` with a fresh snapshot, which
   rebuilds the book.
4. Health checks (data_quality) gate resumption — this module only moves bytes.

The transport is injected as an async-callable returning an async iterator of
raw text frames, so every reconnect/backoff/parse path is testable without a
network.  Production wires in ``websockets.connect``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any, AsyncIterator, Awaitable, Callable, Protocol

from ..clock import Clock, SystemClock
from ..config import ReconnectConfig
from .messages import MarketEvent, NormalizationError
from .normalizer import normalize_ws_message

log = logging.getLogger(__name__)


class WsConnection(Protocol):
    def __aiter__(self) -> AsyncIterator[str]: ...
    async def send(self, message: str) -> None: ...
    async def close(self) -> None: ...


ConnectFn = Callable[[], Awaitable[WsConnection]]
EventHandler = Callable[[MarketEvent], None]
DisconnectHandler = Callable[[str], None]


def backoff_delays(config: ReconnectConfig,
                   rng: random.Random | None = None) -> "BackoffSchedule":
    return BackoffSchedule(config, rng or random.Random())


class BackoffSchedule:
    """Capped exponential backoff with jitter; reset on a healthy connection."""

    def __init__(self, config: ReconnectConfig, rng: random.Random) -> None:
        self._config = config
        self._rng = rng
        self._attempt = 0

    def next_delay(self) -> float:
        base = min(
            self._config.initial_backoff_seconds * (2 ** self._attempt),
            self._config.max_backoff_seconds,
        )
        self._attempt += 1
        jitter = base * self._config.jitter_fraction
        return max(0.05, base + self._rng.uniform(-jitter, jitter))

    def reset(self) -> None:
        self._attempt = 0


class DeltaPublicWs:
    def __init__(
        self,
        connect: ConnectFn,
        channels: list[dict[str, Any]],
        on_event: EventHandler,
        on_disconnect: DisconnectHandler,
        reconnect: ReconnectConfig,
        clock: Clock | None = None,
        rng: random.Random | None = None,
        healthy_after_seconds: float = 30.0,
    ) -> None:
        self._connect = connect
        self._channels = channels
        self._on_event = on_event
        self._on_disconnect = on_disconnect
        self._backoff = BackoffSchedule(reconnect, rng or random.Random())
        self._clock = clock or SystemClock()
        self._healthy_after = healthy_after_seconds
        self._stopping = asyncio.Event()
        self._current: WsConnection | None = None
        self.connect_count = 0
        self.normalization_errors = 0

    def stop(self) -> None:
        self._stopping.set()
        connection = self._current
        if connection is not None:
            # Unblock a consumer parked on a healthy stream; without this the
            # run() task only exits on the next frame or on cancellation.
            try:
                asyncio.get_running_loop().create_task(_close_quietly(connection))
            except RuntimeError:
                pass  # no running loop (sync caller); cancellation handles it

    def request_restart(self) -> None:
        """Force a reconnect (and therefore a fresh l2 snapshot on
        resubscribe).  Used after a sequence gap invalidates the book — the
        venue only sends a snapshot at subscription time."""
        connection = self._current
        if connection is not None:
            asyncio.get_running_loop().create_task(_close_quietly(connection))

    async def run(self) -> None:
        """Consume until stopped. Never raises on feed trouble — it reports
        via on_disconnect and retries; only cancellation/stop exits."""
        while not self._stopping.is_set():
            try:
                await self._run_once()
                if not self._stopping.is_set():
                    # A polite server close is still a dead feed: the book
                    # must be invalidated before any reconnect attempt.
                    self._on_disconnect("connection closed by venue")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # transport/protocol failure
                self._on_disconnect(f"{type(exc).__name__}: {exc}")
            if self._stopping.is_set():
                return
            delay = self._backoff.next_delay()
            log.warning("ws reconnect in %.2fs", delay)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=delay)
                return  # stopped during backoff
            except asyncio.TimeoutError:
                pass  # backoff elapsed; retry

    async def _run_once(self) -> None:
        connection = await self._connect()
        self._current = connection
        self.connect_count += 1
        connected_at = self._clock.monotonic()
        try:
            await connection.send(json.dumps({
                "type": "subscribe",
                "payload": {"channels": self._channels},
            }))
            async for frame in connection:
                if self._stopping.is_set():
                    return
                self._handle_frame(frame)
                if (self._clock.monotonic() - connected_at) >= self._healthy_after:
                    self._backoff.reset()
                    connected_at = float("inf")  # reset once per connection
        finally:
            self._current = None
            await _close_quietly(connection)

    def _handle_frame(self, frame: str) -> None:
        receive_ts = self._clock.now()
        try:
            raw = json.loads(frame)
        except ValueError:
            self.normalization_errors += 1
            log.warning("ws frame is not JSON (%d bytes)", len(frame))
            return
        if not isinstance(raw, dict):
            self.normalization_errors += 1
            return
        try:
            event = normalize_ws_message(raw, receive_ts)
        except NormalizationError as exc:
            # Malformed market data is recorded and dropped; the data-quality
            # monitor sees the resulting silence. Never guessed at (§3.2).
            self.normalization_errors += 1
            log.warning("ws normalization failed: %s", exc)
            return
        if event is not None:
            self._on_event(event)


async def _close_quietly(connection: WsConnection) -> None:
    try:
        await connection.close()
    except Exception:
        pass
