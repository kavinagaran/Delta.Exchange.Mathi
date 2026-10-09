"""Service dispatch (gap → rebuild request, disconnect → invalid book,
disk-floor capture stop) and the /health + /status API surface."""

from __future__ import annotations

import json
import zlib
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from btc_trend_engine.api.app import create_app
from btc_trend_engine.clock import FixedClock
from btc_trend_engine.config import load_config
from btc_trend_engine.market_data.messages import EventType
from btc_trend_engine.market_data.normalizer import normalize_ws_message
from btc_trend_engine.service import EngineService

T0 = datetime(2026, 7, 25, 10, 0, tzinfo=timezone.utc)
TS_US = int(T0.timestamp() * 1_000_000)


@pytest.fixture
def config(tmp_path: Path):
    environ = {
        "ENGINE_TOKEN": "test-token",
        "ENGINE_STORAGE__DATA_DIR": str(tmp_path / "data"),
    }
    return load_config(environ=environ)


@pytest.fixture
def service(config):
    engine_service = EngineService(config, clock=FixedClock(T0))
    yield engine_service
    engine_service.event_store.close()


def _feed(service: EngineService, raw: dict) -> None:
    event = normalize_ws_message(raw, service.clock.now())
    assert event is not None
    service.handle_event(event)


def _l2(action: str, seq: int) -> dict:
    checksum = zlib.crc32(b"63936.0:5|63935.0:10") & 0xFFFFFFFF
    return {"type": "l2_updates", "action": action, "symbol": "BTCUSD",
            "sequence_no": seq, "cs": checksum, "timestamp": TS_US,
            "bids": [["63935.0", "10"]], "asks": [["63936.0", "5"]]}


# ── service dispatch ────────────────────────────────────────────────────
def test_snapshot_then_update_builds_book_and_persists_events(service):
    _feed(service, _l2("snapshot", 100))
    _feed(service, _l2("update", 101))
    assert service.book.is_valid
    assert service.book.sequence_no == 101
    assert service.event_store.written == 2


def test_sequence_gap_invalidates_book_and_requests_ws_restart(service):
    restarts = []
    service.ws.request_restart = lambda: restarts.append(True)
    _feed(service, _l2("snapshot", 100))
    _feed(service, _l2("update", 105))  # gap
    assert not service.book.is_valid
    assert restarts == [True]
    categories = [e.category for e in service.repos.recent_health_events()]
    assert "book_gap" in categories


def test_disconnect_invalidates_book_before_reconnect(service):
    _feed(service, _l2("snapshot", 100))
    assert service.book.is_valid
    service._on_disconnect("ConnectionError: feed dropped")
    assert not service.book.is_valid
    categories = [e.category for e in service.repos.recent_health_events()]
    assert "ws_disconnect" in categories


def test_trades_build_candles_and_close_writes_rows(service):
    for i, minute in enumerate(range(0, 11, 5)):
        _feed(service, {
            "type": "all_trades", "symbol": "BTCUSD",
            "timestamp": TS_US + minute * 60 * 1_000_000,
            "price": str(63900 + i), "size": "1", "buyer_role": "taker"})
    # two 5m candles closed (10:00, 10:05); 10:10 still open
    assert service.repos.candle_count("BTCUSD", "5m") == 2


def test_malformed_market_payload_is_counted_not_fatal(service):
    _feed(service, {"type": "l2_updates", "action": "mystery",
                    "symbol": "BTCUSD", "sequence_no": 1, "timestamp": TS_US,
                    "bids": [], "asks": []})
    assert service.dispatch_errors == 1
    assert not service.book.is_valid


def test_data_quality_reflects_feeds_and_book(service):
    assert service.current_data_quality() == "STALE_L1"  # nothing seen yet
    for minute in range(3):
        _feed(service, {"type": "all_trades", "symbol": "BTCUSD",
                        "timestamp": TS_US + minute * 1_000_000,
                        "price": "63900", "size": "1", "buyer_role": "taker"})
        _feed(service, {"type": "v2/ticker", "symbol": "BTCUSD",
                        "timestamp": TS_US, "mark_price": "63941.7"})
        _feed(service, _l2("snapshot", 100 + minute))
    assert service.current_data_quality() == "OK"
    service.book.invalidate("test")
    assert service.current_data_quality() == "BOOK_INVALID"


def test_disk_floor_stops_raw_capture_but_not_the_service(service, monkeypatch):
    import shutil as shutil_module

    class Usage:
        free = 1 << 30  # 1 GB < 2 GB floor
    monkeypatch.setattr(shutil_module, "disk_usage", lambda path: Usage)
    monkeypatch.setattr("btc_trend_engine.service.shutil.disk_usage",
                        lambda path: Usage)
    service._check_disk()
    assert service.raw_capture_enabled is False
    before = service.event_store.written
    _feed(service, _l2("snapshot", 100))
    assert service.event_store.written == before  # not captured
    assert service.book.is_valid                  # still processed


def test_status_shape_is_complete(service):
    _feed(service, _l2("snapshot", 100))
    status = service.status()
    for key in ("version", "symbol", "uptime_seconds", "data_quality", "book",
                "feeds", "ws_connects", "events_written",
                "raw_capture_enabled", "candles"):
        assert key in status, key
    assert status["book"]["state"] == "valid"
    assert json.dumps(status)  # JSON-serialisable end to end


# ── api ─────────────────────────────────────────────────────────────────
def test_health_is_open_and_status_needs_token(config, service):
    app = create_app(config, service=service)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/status").status_code == 401
        response = client.get("/status",
                              headers={"X-Engine-Token": "test-token"})
        assert response.status_code == 200
        assert response.json()["symbol"] == "BTCUSD"


def test_app_refuses_to_start_without_token(tmp_path):
    config = load_config(environ={
        "ENGINE_STORAGE__DATA_DIR": str(tmp_path / "data")})
    with pytest.raises(RuntimeError, match="ENGINE_TOKEN"):
        create_app(config)


def test_config_refuses_non_loopback_bind(tmp_path):
    with pytest.raises(Exception, match="loopback"):
        load_config(environ={
            "ENGINE_TOKEN": "t",
            "ENGINE_ENGINE__HOST": "0.0.0.0",
            "ENGINE_STORAGE__DATA_DIR": str(tmp_path)})


def test_default_clock_tolerance_matches_delta_timestamp_window(tmp_path):
    config = load_config(environ={
        "ENGINE_TOKEN": "t",
        "ENGINE_STORAGE__DATA_DIR": str(tmp_path),
    })
    assert config.data_quality.max_clock_drift_ms == 5000.0
