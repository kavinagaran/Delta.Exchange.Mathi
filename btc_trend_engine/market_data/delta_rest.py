"""Async REST client for Delta India public endpoints.

Strict response validation in the style of ``trend_engine_live.py`` — a
malformed page raises instead of yielding partial data — plus a client-side
token bucket, because the venue exposes no rate-limit headers (assumption A4)
and four processes share one key's budget.  This module is public-data only;
it holds no credentials by design (ADR 0001).
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from ..clock import Clock, SystemClock
from ..config import RateLimitConfig
from .messages import Candle, NormalizationError
from .normalizer import rest_candle


class RestError(RuntimeError):
    """The venue response could not be proven well-formed."""


class TokenBucket:
    def __init__(self, config: RateLimitConfig, clock: Clock) -> None:
        self._rate = config.requests_per_second
        self._capacity = float(config.burst)
        self._tokens = float(config.burst)
        self._clock = clock
        self._last = clock.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self._clock.monotonic()
                self._tokens = min(self._capacity,
                                   self._tokens + (now - self._last) * self._rate)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await asyncio.sleep((1.0 - self._tokens) / self._rate)


def validate_success_envelope(data: Any, context: str) -> Any:
    """Delta wraps everything as {"success": bool, "result": ...}."""
    if not isinstance(data, dict):
        raise RestError(f"{context}: response is not an object")
    if data.get("success") is not True:
        raise RestError(f"{context}: success is not true: {data.get('error')!r}")
    if "result" not in data:
        raise RestError(f"{context}: missing result")
    return data["result"]


class DeltaRestClient:
    def __init__(
        self,
        base_url: str,
        rate_limit: RateLimitConfig,
        clock: Clock | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        self._bucket = TokenBucket(rate_limit, clock or SystemClock())
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_seconds,
            transport=transport,
            headers={"User-Agent": "btc-trend-engine/phase2"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        await self._bucket.acquire()
        response = await self._client.get(path, params=params)
        if response.status_code != 200:
            raise RestError(f"GET {path}: HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise RestError(f"GET {path}: body is not JSON") from exc
        return validate_success_envelope(data, f"GET {path}")

    # ── endpoints ────────────────────────────────────────────────────────
    async def history_candles(
        self, symbol: str, resolution: str, start_epoch: int, end_epoch: int
    ) -> list[Candle]:
        result = await self._get("/v2/history/candles", {
            "symbol": symbol, "resolution": resolution,
            "start": start_epoch, "end": end_epoch,
        })
        if not isinstance(result, list):
            raise RestError("history/candles: result is not a list")
        try:
            candles = [rest_candle(row, symbol, resolution) for row in result]
        except NormalizationError as exc:
            raise RestError(f"history/candles: {exc}") from exc
        # The venue returns newest-first; the engine standardises oldest-first
        # and requires strict monotonicity — duplicates are data corruption.
        candles.sort(key=lambda c: c.start)
        for previous, current in zip(candles, candles[1:]):
            if current.start <= previous.start:
                raise RestError("history/candles: non-monotonic candle series")
        return candles

    async def ticker(self, symbol: str) -> dict[str, Any]:
        result = await self._get(f"/v2/tickers/{symbol}")
        if not isinstance(result, dict) or not result.get("symbol"):
            raise RestError(f"tickers/{symbol}: malformed result")
        return result

    async def l2_orderbook(self, symbol: str) -> dict[str, Any]:
        result = await self._get(f"/v2/l2orderbook/{symbol}")
        if not isinstance(result, dict):
            raise RestError(f"l2orderbook/{symbol}: malformed result")
        return result

    async def products(self, **params: str) -> list[dict[str, Any]]:
        """Single page; the engine has no pagination needs in Phase 2."""
        result = await self._get("/v2/products", dict(params))
        if not isinstance(result, list):
            raise RestError("products: result is not a list")
        return result
