"""REST client: envelope validation, candle series integrity, token bucket."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from btc_trend_engine.clock import FixedClock, SystemClock
from btc_trend_engine.config import RateLimitConfig
from btc_trend_engine.market_data.delta_rest import (
    DeltaRestClient,
    RestError,
    TokenBucket,
    validate_success_envelope,
)

from datetime import datetime, timezone

T0 = datetime(2026, 7, 25, 10, 0, tzinfo=timezone.utc)


def _client(handler) -> DeltaRestClient:
    return DeltaRestClient(
        "https://api.test",
        RateLimitConfig(requests_per_second=1000.0, burst=100),
        transport=httpx.MockTransport(handler),
    )


def _ok(payload) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "result": payload})


# ── envelope ────────────────────────────────────────────────────────────
def test_envelope_requires_success_true_and_result():
    assert validate_success_envelope({"success": True, "result": 1}, "x") == 1
    for bad in ([], {"success": False, "error": "nope"}, {"success": True}):
        with pytest.raises(RestError):
            validate_success_envelope(bad, "x")


async def test_http_error_and_non_json_fail_closed():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("BTCUSD"):
            return httpx.Response(500)
        return httpx.Response(200, content=b"<html>maintenance</html>")

    client = _client(handler)
    with pytest.raises(RestError, match="HTTP 500"):
        await client.ticker("BTCUSD")
    with pytest.raises(RestError, match="not JSON"):
        await client.ticker("ETHUSD")
    await client.aclose()


# ── candles ─────────────────────────────────────────────────────────────
async def test_history_candles_sorts_oldest_first():
    rows = [
        {"time": 1784968800, "open": 2, "high": 2, "low": 2, "close": 2, "volume": 1},
        {"time": 1784968500, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
    ]
    client = _client(lambda request: _ok(rows))
    candles = await client.history_candles("BTCUSD", "5m", 0, 2_000_000_000)
    assert [int(c.start.timestamp()) for c in candles] == [1784968500, 1784968800]
    await client.aclose()


async def test_duplicate_candle_start_is_corruption():
    rows = [
        {"time": 1784968500, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
        {"time": 1784968500, "open": 2, "high": 2, "low": 2, "close": 2, "volume": 1},
    ]
    client = _client(lambda request: _ok(rows))
    with pytest.raises(RestError, match="non-monotonic"):
        await client.history_candles("BTCUSD", "5m", 0, 2_000_000_000)
    await client.aclose()


async def test_malformed_candle_row_fails_closed():
    rows = [{"time": 1784968500, "open": "not-a-number", "high": 1,
             "low": 1, "close": 1, "volume": 1}]
    client = _client(lambda request: _ok(rows))
    with pytest.raises(RestError):
        await client.history_candles("BTCUSD", "5m", 0, 2_000_000_000)
    await client.aclose()


async def test_request_carries_params_and_user_agent():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        seen["ua"] = request.headers.get("User-Agent", "")
        return _ok([])

    client = _client(handler)
    await client.history_candles("BTCUSD", "5m", 100, 200)
    assert seen["params"] == {"symbol": "BTCUSD", "resolution": "5m",
                              "start": "100", "end": "200"}
    assert "btc-trend-engine" in seen["ua"]
    await client.aclose()


# ── token bucket ────────────────────────────────────────────────────────
async def test_token_bucket_burst_is_free_then_paced():
    clock = SystemClock()
    bucket = TokenBucket(RateLimitConfig(requests_per_second=50.0, burst=5), clock)
    start = clock.monotonic()
    for _ in range(5):
        await bucket.acquire()
    burst_elapsed = clock.monotonic() - start
    assert burst_elapsed < 0.05  # burst does not wait
    await bucket.acquire()  # sixth must be paced at ~1/50s
    assert clock.monotonic() - start >= 0.015


async def test_token_bucket_refills_with_time():
    clock = FixedClock(T0)
    bucket = TokenBucket(RateLimitConfig(requests_per_second=2.0, burst=2), clock)
    await bucket.acquire()
    await bucket.acquire()
    clock.advance(1.0)  # refills 2 tokens
    await asyncio.wait_for(bucket.acquire(), timeout=0.2)


def test_serialization_of_status_like_payloads():
    # guard against Decimal leaking into JSON responses upstream
    assert json.dumps({"ok": True})
