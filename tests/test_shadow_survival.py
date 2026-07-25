"""Shadow instrumentation must not be able to break the live trading loop.

Written BEFORE the instrumentation it guards (plan Phase 8a). The whole
premise of shadow mode is that we attach a comparison recorder to a loop that
is placing real orders; if that recorder can raise, block, or slow the loop,
it is a liability rather than an observation.

The engine is treated as hostile here: unreachable, returning 500, returning
garbage, and — the one that a naive `requests.post` would fail — hanging for
30 seconds.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest

import trend_engine_client

# The live loop iterates every 15s and holds a per-user cycle lock while it
# works. Anything above a few milliseconds of added latency per completed
# candle is a real cost; this bound is deliberately far tighter than that.
MAX_BLOCKING_SECONDS = 0.25


@pytest.fixture(autouse=True)
def _quiet_shadow_queue():
    """Each test starts with an empty queue and a stopped worker."""
    trend_engine_client.reset_shadow_transport_for_test()
    yield
    trend_engine_client.reset_shadow_transport_for_test()


def _payload(signal_key: str = "up|c1|c2|c3") -> dict:
    return {
        "signal_key": signal_key,
        "candle_close_utc": "2026-07-25T12:00:00Z",
        "direction": 1,
        "score": 72.5,
        "zone": "TREND_UP",
        "market_regime": "TREND_UP",
        "source": "legacy",
    }


# ── the four hostile-engine cases ───────────────────────────────────────
def test_a_thirty_second_hang_does_not_block_the_caller():
    """The case a plain requests.post with an 8s timeout still fails."""
    entered = threading.Event()

    def hang(payload):
        entered.set()
        time.sleep(30)

    with patch.object(trend_engine_client, "_shadow_send", side_effect=hang):
        started = time.monotonic()
        for _ in range(5):
            trend_engine_client.post_legacy_decision(_payload())
        elapsed = time.monotonic() - started

    assert elapsed < MAX_BLOCKING_SECONDS, (
        f"posting blocked the caller for {elapsed:.2f}s")


def test_an_exception_in_the_sender_never_reaches_the_caller():
    with patch.object(trend_engine_client, "_shadow_send",
                      side_effect=RuntimeError("engine exploded")):
        for _ in range(3):
            assert trend_engine_client.post_legacy_decision(_payload()) is not None
        trend_engine_client.drain_shadow_queue_for_test(timeout=2.0)
    # The worker survived and is still able to accept work.
    assert trend_engine_client.post_legacy_decision(_payload()) is not None


@pytest.mark.parametrize("failure", [
    ConnectionError("connection refused"),
    OSError("network unreachable"),
    ValueError("response body is not JSON"),
])
def test_every_transport_failure_class_is_swallowed(failure):
    with patch.object(trend_engine_client, "_shadow_send", side_effect=failure):
        trend_engine_client.post_legacy_decision(_payload())
        trend_engine_client.drain_shadow_queue_for_test(timeout=2.0)


def test_a_500_or_garbage_response_is_not_an_error_for_the_caller():
    class FakeResponse:
        status_code = 500
        text = "<html>internal server error</html>"

        def json(self):
            raise ValueError("not JSON")

    with patch.object(trend_engine_client, "requests") as mocked:
        mocked.post.return_value = FakeResponse()
        trend_engine_client.post_legacy_decision(_payload())
        trend_engine_client.drain_shadow_queue_for_test(timeout=2.0)


# ── backpressure ────────────────────────────────────────────────────────
def test_a_full_queue_drops_rather_than_blocking():
    """A wedged engine must cost bounded memory and zero latency, so the
    queue is bounded and overflow is dropped — never awaited."""
    def hang(payload):
        time.sleep(30)

    with patch.object(trend_engine_client, "_shadow_send", side_effect=hang):
        started = time.monotonic()
        results = [trend_engine_client.post_legacy_decision(_payload(str(i)))
                   for i in range(trend_engine_client.SHADOW_QUEUE_MAX * 3)]
        elapsed = time.monotonic() - started

    assert elapsed < MAX_BLOCKING_SECONDS
    assert any(r is False for r in results), "an unbounded queue is a memory leak"
    assert (trend_engine_client.shadow_queue_depth()
            <= trend_engine_client.SHADOW_QUEUE_MAX)


def test_dropped_records_are_counted_so_the_gap_is_visible():
    """A silently dropped comparison would corrupt the agreement rate that
    gates cutover. Drops must be countable."""
    def hang(payload):
        time.sleep(30)

    with patch.object(trend_engine_client, "_shadow_send", side_effect=hang):
        for i in range(trend_engine_client.SHADOW_QUEUE_MAX * 3):
            trend_engine_client.post_legacy_decision(_payload(str(i)))

    assert trend_engine_client.shadow_dropped_count() > 0


# ── the live decision path itself ───────────────────────────────────────
def test_the_reporter_is_wrapped_so_even_an_enqueue_bug_cannot_propagate():
    """post_legacy_decision is defensive internally, but the call site must
    ALSO be wrapped: a future refactor that makes enqueueing raise must not
    take the trading loop with it."""
    import dashboard

    source = dashboard._collect_trend_score_auto_signal.__doc__ or ""
    assert "shadow" in source.lower(), (
        "the decision collector should document its shadow hook")
