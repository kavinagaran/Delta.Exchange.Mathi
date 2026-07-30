"""Two-speed output: a continuously-updating display score alongside the
committed, candle-close decision score.

The safety property under test is that the two cannot be confused. The
committed score is idempotent per closed candle — dashboard.py dedupes on it
via completed_candle_signal_key — so a repainting score in the order path
would let a single candle fire two entries as the score crossed a band and
came back. The live view is therefore shaped so the order path cannot
consume it at all.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from btc_trend_engine.api.app import create_app
from btc_trend_engine.clock import FixedClock
from btc_trend_engine.config import load_config
from btc_trend_engine.market_data.messages import Candle
from btc_trend_engine.service import EngineService
from btc_trend_engine.signals.producer import SnapshotProducer
from btc_trend_engine.signals.score import ScoreResult

T0 = datetime(2026, 7, 26, 6, 0, tzinfo=timezone.utc)
AUTH = {"X-Engine-Token": "test-token"}
SECONDS = {"5m": 300, "15m": 900, "30m": 1800, "1h": 3600}


def _series(resolution: str, closes: list[float]) -> list[Candle]:
    step = SECONDS[resolution]
    out, previous = [], closes[0]
    for i, close in enumerate(closes):
        out.append(Candle(
            symbol="BTCUSD", resolution=resolution,
            start=T0 - timedelta(seconds=step * (len(closes) - i)),
            open=Decimal(str(previous)), high=Decimal(str(max(previous, close) + 5)),
            low=Decimal(str(min(previous, close) - 5)), close=Decimal(str(close)),
            volume=Decimal("10"), closed=True, trade_count=5))
        previous = close
    return out


def _rising(n=120, start=60000.0, step=25.0):
    return [start + i * step for i in range(n)]


def _all_timeframes(closes=None):
    closes = closes or _rising()
    return {tf: _series(tf, closes) for tf in ("1h", "30m", "15m", "5m")}


def _forming(close: float) -> Candle:
    return Candle(
        symbol="BTCUSD", resolution="5m", start=T0,
        open=Decimal("60000"), high=Decimal(str(close + 10)),
        low=Decimal("59990"), close=Decimal(str(close)),
        volume=Decimal("3"), closed=False, trade_count=2)


# ── shape: cannot be mistaken for a decision ────────────────────────────
def test_the_live_view_carries_no_decision_fields():
    """No signal_id, no entry_allowed, no gates. The dashboard order path
    reads all three, so it cannot consume this even by mistake."""
    producer = SnapshotProducer("BTCUSD")
    view = producer.produce_live(
        now=T0, candles=_all_timeframes(), forming=_forming(63000.0),
        data_quality="OK")
    assert view is not None
    for forbidden in ("signal_id", "entry_allowed", "gates", "zone",
                      "trend_score", "schema_version"):
        assert forbidden not in view, forbidden
    assert view["provisional"] is True
    assert "live_score" in view and "live_zone" in view


def test_the_live_view_reports_the_committed_decision_alongside_it():
    """A reader must be able to see both numbers and tell which one trades."""
    producer = SnapshotProducer("BTCUSD")
    producer.produce(now=T0, candles=_all_timeframes(), ticker={},
                     data_quality="OK", book_valid=True, spread_bps=2.0)
    view = producer.produce_live(
        now=T0, candles=_all_timeframes(), forming=_forming(63000.0),
        data_quality="OK")
    committed = producer.latest()
    assert view["committed_signal_id"] == committed["signal_id"]
    assert view["committed_zone"] == committed["zone"]
    assert view["committed_score"] == committed["trend_score"]


# ── it actually moves within the bar ────────────────────────────────────
def test_the_live_score_moves_with_the_forming_candle():
    """The whole point: the number changes between candle closes."""
    producer = SnapshotProducer("BTCUSD")
    candles = _all_timeframes()
    low = producer.produce_live(now=T0, candles=candles,
                                forming=_forming(60500.0), data_quality="OK")
    high = producer.produce_live(now=T0, candles=candles,
                                 forming=_forming(66000.0), data_quality="OK")
    assert low["live_score"] != high["live_score"]
    assert high["live_score"] > low["live_score"]


def test_live_view_keeps_extra_precision_for_the_display_only_chart(monkeypatch):
    """A rounded dial must not turn real intrabar movement into fake dojis."""
    monkeypatch.setattr(
        "btc_trend_engine.signals.producer.compute_score",
        lambda **_kwargs: ScoreResult(
            trend_score=12.3, raw_trend_score=12.3456,
            components=[], available_weight=1.0),
    )
    producer = SnapshotProducer("BTCUSD")
    view = producer.produce_live(
        now=T0, candles=_all_timeframes(), forming=_forming(63000.0),
        data_quality="OK")
    assert view is not None
    assert view["live_score"] == 12.3
    assert view["chart_score"] == 12.3456


def test_the_committed_score_does_NOT_move_with_the_forming_candle():
    """The safety half. Whatever the forming candle does, the committed
    decision stays put until the candle actually closes."""
    producer = SnapshotProducer("BTCUSD")
    candles = _all_timeframes()
    committed = producer.produce(now=T0, candles=candles, ticker={},
                                 data_quality="OK", book_valid=True, spread_bps=2.0)
    before = (committed["trend_score"], committed["signal_id"], committed["zone"])
    for close in (60500.0, 66000.0, 55000.0):
        producer.produce_live(now=T0, candles=candles,
                              forming=_forming(close), data_quality="OK")
    latest = producer.latest()
    assert (latest["trend_score"], latest["signal_id"], latest["zone"]) == before


def test_a_missing_forming_candle_still_yields_a_view():
    producer = SnapshotProducer("BTCUSD")
    view = producer.produce_live(now=T0, candles=_all_timeframes(),
                                 forming=None, data_quality="OK")
    assert view is not None and view["forming_candle_start"] is None


def test_no_trigger_candles_at_all_yields_nothing():
    producer = SnapshotProducer("BTCUSD")
    assert producer.produce_live(now=T0, candles={"5m": []}, forming=None,
                                 data_quality="OK") is None


def test_degraded_quality_is_reported_not_hidden():
    producer = SnapshotProducer("BTCUSD")
    view = producer.produce_live(now=T0, candles=_all_timeframes(),
                                 forming=_forming(63000.0),
                                 data_quality="STALE_L1")
    assert view["data_quality"] == "STALE_L1"


# ── endpoint ────────────────────────────────────────────────────────────
@pytest.fixture
def config(tmp_path):
    return load_config(environ={
        "ENGINE_TOKEN": "test-token",
        "ENGINE_STORAGE__DATA_DIR": str(tmp_path / "data")})


@pytest.fixture
def service(config):
    engine_service = EngineService(config, clock=FixedClock(T0))
    for resolution, candles in _all_timeframes().items():
        engine_service.candles.series[resolution].seed_closed(candles)
    yield engine_service
    engine_service.event_store.close()
    engine_service.data_lock.release()


def test_live_endpoint_requires_the_token(config, service):
    with TestClient(create_app(config, service=service)) as http:
        assert http.get("/trend/live").status_code == 401


def test_live_endpoint_serves_a_provisional_view(config, service):
    with TestClient(create_app(config, service=service)) as http:
        body = http.get("/trend/live", headers=AUTH).json()
    assert body["provisional"] is True
    assert "live_score" in body
    assert "entry_allowed" not in body


def test_live_endpoint_rejects_a_different_symbol(config, service):
    with TestClient(create_app(config, service=service)) as http:
        assert http.get("/trend/live?symbol=ETHUSD",
                        headers=AUTH).status_code == 404


def test_preview_score_history_builds_5m_display_only_ohlc(service):
    """The chart history follows only provisional scores and stays separate
    from committed snapshots/order permissions."""
    first_start = "2026-07-26T06:00:00Z"
    second_start = "2026-07-26T06:05:00Z"
    samples = iter([
        {"forming_candle_start": first_start, "as_of": first_start,
         "live_score": 10.0, "data_quality": "OK"},
        {"forming_candle_start": first_start, "as_of": "2026-07-26T06:00:20Z",
         "live_score": 18.0, "data_quality": "OK"},
        {"forming_candle_start": second_start, "as_of": second_start,
         "live_score": -8.0, "data_quality": "OK"},
    ])
    service.producer.produce_live = lambda **_kwargs: next(samples)

    service.refresh_live_view(T0)
    service.refresh_live_view(T0 + timedelta(seconds=20))
    service.refresh_live_view(T0 + timedelta(minutes=5))
    history = service.live_score_history()

    assert history["display_only"] is True
    assert history["resolution"] == "5m"
    assert len(history["candles"]) == 2
    completed, forming = history["candles"]
    assert completed == {
        "start_utc": first_start,
        "end_utc": second_start,
        "open": 10.0,
        "high": 18.0,
        "low": 10.0,
        "close": 18.0,
        "samples": 2,
        "partial": False,
        "last_sample_utc": "2026-07-26T06:00:20Z",
        "data_quality": "OK",
        "forming": False,
    }
    assert forming["start_utc"] == second_start
    assert forming["open"] == forming["close"] == -8.0
    assert forming["forming"] is True
    assert "signal_id" not in completed
    assert "entry_allowed" not in completed


def test_preview_score_history_uses_chart_precision_and_marks_late_capture(service):
    view = {
        "forming_candle_start": "2026-07-26T06:00:00Z",
        "as_of": "2026-07-26T06:01:00Z",
        "live_score": 10.0,
        "chart_score": 10.0473,
        "data_quality": "OK",
    }
    service._record_preview_score(view)
    current = service.live_score_history()["candles"][-1]
    assert current["open"] == current["close"] == 10.0473
    assert current["partial"] is True


def test_live_history_endpoint_requires_token_and_is_display_only(config, service):
    with TestClient(create_app(config, service=service)) as http:
        assert http.get("/trend/live/history").status_code == 401
        body = http.get("/trend/live/history?limit=8", headers=AUTH).json()
    assert body == {
        "symbol": "BTCUSD",
        "resolution": "5m",
        "display_only": True,
        "candles": [],
    }


def test_preview_score_history_keeps_a_full_24_hours(service):
    base = datetime(2026, 7, 26, 0, 0, tzinfo=timezone.utc)
    for index in range(300):
        start = base + timedelta(minutes=index * 5)
        service._preview_score_candles.append({
            "start_utc": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_utc": (start + timedelta(minutes=5)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "open": float(index),
            "high": float(index + 1),
            "low": float(index - 1),
            "close": float(index),
            "samples": 2,
            "partial": False,
            "last_sample_utc": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "data_quality": "OK",
            "forming": False,
        })
    history = service.live_score_history(limit=999)
    assert len(history["candles"]) == 288
    assert history["candles"][0]["open"] == 12.0
    assert history["candles"][-1]["open"] == 299.0


def test_completed_preview_score_history_survives_restart(config):
    first = EngineService(config, clock=FixedClock(T0))
    first._record_preview_score({
        "forming_candle_start": "2026-07-26T06:00:00Z",
        "as_of": "2026-07-26T06:00:05Z",
        "chart_score": 12.5,
        "data_quality": "OK",
    })
    first._record_preview_score({
        "forming_candle_start": "2026-07-26T06:05:00Z",
        "as_of": "2026-07-26T06:05:05Z",
        "chart_score": 18.5,
        "data_quality": "OK",
    })
    first.event_store.close()
    first.data_lock.release()

    restored = EngineService(config, clock=FixedClock(T0))
    try:
        history = restored.live_score_history()
        assert len(history["candles"]) == 1
        assert history["candles"][0]["start_utc"] == "2026-07-26T06:00:00Z"
        assert history["candles"][0]["close"] == 12.5
        assert history["candles"][0]["forming"] is False
        assert "signal_id" not in history["candles"][0]
        assert "entry_allowed" not in history["candles"][0]
    finally:
        restored.event_store.close()
        restored.data_lock.release()


def test_refresh_live_view_never_raises_into_the_housekeeping_loop(service):
    """It runs on the same loop that flushes the event store; an exception
    here would take market-data capture down with it."""
    service.producer.produce_live = lambda **kw: (_ for _ in ()).throw(
        RuntimeError("boom"))
    assert service.refresh_live_view(T0) is None  # swallowed, loop survives
