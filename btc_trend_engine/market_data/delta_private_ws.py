"""Read-only private WebSocket consumer for position/order reconciliation
(security-checklist.md "Phase 4: engine gets a separate read-only key").

**Assumption, unverified — record alongside docs/assumptions.md A9/A10 class
of gaps:** the auth-frame shape below (``{"type": "auth", "payload": {...}}``
with ``signature = HMAC_SHA256(secret, method + timestamp + path)``, method
``GET`` and path ``/live``) mirrors ``dashboard.py:_sign``'s REST scheme
applied to Delta's documented WS auth convention. WebFetch/WebSearch were
unavailable when this was written (same tooling fault as A2b/A9); confirm
against official Delta India docs, or by a successful auth on the EC2 host,
before this is wired into ``EngineService``. Until then this module is built
and unit-tested but **not started** by the service — ``enabled=false`` by
default (config.py) and nothing calls ``DeltaPrivateWs.run()`` in production.

The credentials are never trading keys: this reads positions/orders only, and
the engine still cannot place, amend or cancel anything (ADR 0001).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import random
import time
from datetime import datetime
from typing import Any

from ..clock import Clock, SystemClock
from ..config import ReconnectConfig
from .delta_public_ws import (
    BackoffSchedule,
    ConnectFn,
    DisconnectHandler,
    EventHandler,
    WsConnection,
    _close_quietly,
)
from .messages import MarketEvent, NormalizationError
from .normalizer import normalize_ws_message

log = logging.getLogger(__name__)

AUTH_METHOD = "GET"
AUTH_PATH = "/live"


def sign_ws_auth(api_key: str, api_secret: str, timestamp: int | None = None) -> dict[str, str]:
    """Pure, injectable-clock-free HMAC signer so the auth frame is testable
    without a network. ``timestamp`` defaults to wall-clock seconds."""
    ts = str(timestamp if timestamp is not None else int(time.time()))
    message = AUTH_METHOD + ts + AUTH_PATH
    signature = hmac.new(api_secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return {"api-key": api_key, "signature": signature, "timestamp": ts}


class DeltaPrivateWs:
    """Structurally mirrors ``DeltaPublicWs``: reconnect/backoff/parse are
    identical, plus one authenticated frame sent immediately after connect
    and before subscribing. Kept as a separate class rather than a subclass
    so the well-tested public consumer is never put at risk by this change.
    """

    def __init__(
        self,
        connect: ConnectFn,
        api_key: str,
        api_secret: str,
        channels: list[dict[str, Any]],
        on_event: EventHandler,
        on_disconnect: DisconnectHandler,
        reconnect: ReconnectConfig,
        clock: Clock | None = None,
    ) -> None:
        if not api_key or not api_secret:
            raise ValueError("DeltaPrivateWs requires a read-only api_key and api_secret")
        self._connect = connect
        self._api_key = api_key
        self._api_secret = api_secret
        self._channels = channels
        self._on_event = on_event
        self._on_disconnect = on_disconnect
        self._backoff = BackoffSchedule(reconnect, random.Random())
        self._clock = clock or SystemClock()
        self._stopping = asyncio.Event()
        self._current: WsConnection | None = None
        self.connect_count = 0
        self.normalization_errors = 0
        self.auth_failures = 0

    def stop(self) -> None:
        self._stopping.set()
        connection = self._current
        if connection is not None:
            try:
                asyncio.get_running_loop().create_task(_close_quietly(connection))
            except RuntimeError:
                pass

    async def run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self._run_once()
                if not self._stopping.is_set():
                    self._on_disconnect("connection closed by venue")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._on_disconnect(f"{type(exc).__name__}: {exc}")
            if self._stopping.is_set():
                return
            delay = self._backoff.next_delay()
            log.warning("private ws reconnect in %.2fs", delay)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=delay)
                return
            except asyncio.TimeoutError:
                pass

    async def _run_once(self) -> None:
        connection = await self._connect()
        self._current = connection
        self.connect_count += 1
        try:
            auth = sign_ws_auth(self._api_key, self._api_secret,
                                timestamp=int(self._clock.now().timestamp()))
            await connection.send(json.dumps({"type": "auth", "payload": auth}))
            await connection.send(json.dumps({
                "type": "subscribe", "payload": {"channels": self._channels}}))
            async for frame in connection:
                if self._stopping.is_set():
                    return
                self._handle_frame(frame)
        finally:
            self._current = None
            await _close_quietly(connection)

    def _handle_frame(self, frame: str) -> None:
        receive_ts = self._clock.now()
        try:
            raw = json.loads(frame)
        except ValueError:
            self.normalization_errors += 1
            return
        if not isinstance(raw, dict):
            self.normalization_errors += 1
            return
        if raw.get("type") in ("auth", "success") and raw.get("success") is False:
            self.auth_failures += 1
            log.error("private ws auth rejected: %r", raw)
            return
        event = self._normalize(raw, receive_ts)
        if event is not None:
            self._on_event(event)

    def _normalize(self, raw: dict[str, Any], receive_ts: datetime) -> MarketEvent | None:
        try:
            return normalize_ws_message(raw, receive_ts)
        except NormalizationError:
            self.normalization_errors += 1
            return None
