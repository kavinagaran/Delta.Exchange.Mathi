"""Snapshot production, the /trend/* endpoints, and the fail-closed client.

The client tests are the important ones: that module is the seam between a
market-data service and a system that places real orders, and every failure
path must block entry rather than propagate.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

import trend_engine_client as client
from btc_trend_engine.api.app import create_app
from btc_trend_engine.clock import FixedClock
from btc_trend_engine.config import load_config
from btc_trend_engine.market_data.messages import Candle
from btc_trend_engine.service import EngineService
from btc_trend_engine.signals.producer import SnapshotProducer, spread_bps_from
from btc_trend_engine.signals.regime import Regime

T0 = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
SECONDS = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400}


def _series(resolution: str, closes: list[float], symbol: str = "BTCUSD"):
    step = SECONDS[resolution]
    out = []
    previous = closes[0]
    for i, close in enumerate(closes):
        out.append(Candle(
            symbol=symbol, resolution=resolution,
            start=T0 - timedelta(seconds=step * (len(closes) - i)),
            open=Decimal(str(previous)),
            high=Decimal(str(max(previous, close) + 5)),
            low=Decimal(str(min(previous, close) - 5)),
            close=Decimal(str(close)), volume=Decimal("10"), closed=True,
            trade_count=5))
        previous = close
    return out


def _zigzag(n=120, start=60000.0):
    closes = [start]
    for i in range(1, n):
        closes.append(closes[-1] + (30.0 if i % 7 < 5 else -40.0))
    return closes


def _all_timeframes(closes=None):
    closes = closes or _zigzag()
    return {tf: _series(tf, closes) for tf in ("4h", "1h", "15m", "5m")}


TICKER = {"mark_price": "63941.7", "spot_price": "63940.0",
          "oi": "1186.5", "oi_change_usd_6h": "3713941.12",
          "funding_rate": "0.01"}


# ── producer ────────────────────────────────────────────────────────────
def test_produces_schema_valid_snapshot_from_closed_candles():
    producer = SnapshotProducer("BTCUSD")
    snapshot = producer.produce(
        now=T0, candles=_all_timeframes(), ticker=TICKER,
        data_quality="OK", book_valid=True, spread_bps=2.0)
    assert snapshot is not None
    for field in ("schema_version", "symbol", "timestamp", "candle_close_utc",
                  "signal_id", "regime", "direction", "trend_score",
                  "confidence", "entry_allowed", "signal_ttl_seconds",
                  "components", "timeframes", "gates", "reason_codes",
                  "data_quality", "feature_set_version", "model_version"):
        assert field in snapshot, field
    assert snapshot["schema_version"] == "1.2.0"
    assert -100.0 <= snapshot["trend_score"] <= 100.0
    assert snapshot["signal_ttl_seconds"] > 0
    assert abs(sum(c["weight"] for c in snapshot["components"]) - 1.0) < 1e-9


def test_no_closed_trigger_candle_yields_none_not_a_fake_snapshot():
    producer = SnapshotProducer("BTCUSD")
    assert producer.produce(
        now=T0, candles={"5m": []}, ticker=TICKER, data_quality="OK",
        book_valid=True, spread_bps=1.0) is None


@pytest.mark.parametrize("quality", ["STALE_L1", "STALE_L2", "BOOK_INVALID",
                                     "CLOCK_DRIFT", "EXCHANGE_UNSAFE"])
def test_degraded_quality_always_blocks_entry(quality):
    producer = SnapshotProducer("BTCUSD")
    snapshot = producer.produce(
        now=T0, candles=_all_timeframes(), ticker=TICKER,
        data_quality=quality, book_valid=True, spread_bps=1.0)
    assert snapshot["entry_allowed"] is False
    assert snapshot["regime"] == Regime.DEGRADED.value


def test_invalid_book_blocks_entry_and_names_the_gate():
    producer = SnapshotProducer("BTCUSD")
    snapshot = producer.produce(
        now=T0, candles=_all_timeframes(), ticker=TICKER,
        data_quality="OK", book_valid=False, spread_bps=1.0)
    gate = next(g for g in snapshot["gates"] if g["name"] == "book_valid")
    assert gate["passed"] is False and gate["detail"]
    assert snapshot["entry_allowed"] is False


def test_short_history_is_reported_not_silently_defaulted():
    producer = SnapshotProducer("BTCUSD")
    short = {tf: _series(tf, _zigzag(n=10)) for tf in ("4h", "1h", "15m", "5m")}
    snapshot = producer.produce(
        now=T0, candles=short, ticker=TICKER, data_quality="OK",
        book_valid=True, spread_bps=1.0)
    assert snapshot["data_quality"] == "FEATURES_INCOMPLETE"
    assert snapshot["entry_allowed"] is False
    gate = next(g for g in snapshot["gates"] if g["name"] == "features_complete")
    assert gate["passed"] is False


def test_wide_spread_blocks_entry():
    producer = SnapshotProducer("BTCUSD")
    snapshot = producer.produce(
        now=T0, candles=_all_timeframes(), ticker=TICKER,
        data_quality="OK", book_valid=True, spread_bps=50.0)
    gate = next(g for g in snapshot["gates"] if g["name"] == "spread_acceptable")
    assert gate["passed"] is False
    assert snapshot["entry_allowed"] is False


def test_degraded_read_does_not_silently_exit_a_held_signal():
    """Hysteresis carries state; feeding a degraded reading in as a genuine
    neutral would drop a held direction for the wrong reason."""
    producer = SnapshotProducer("BTCUSD")
    producer.hysteresis.direction = 1
    snapshot = producer.produce(
        now=T0, candles=_all_timeframes(), ticker=TICKER,
        data_quality="STALE_L1", book_valid=True, spread_bps=1.0)
    assert producer.hysteresis.direction == 1  # preserved, not reset
    assert snapshot["entry_allowed"] is False  # but still cannot enter


def test_signal_id_is_deterministic_per_closed_candle():
    first = SnapshotProducer("BTCUSD").produce(
        now=T0, candles=_all_timeframes(), ticker=TICKER,
        data_quality="OK", book_valid=True, spread_bps=1.0)
    second = SnapshotProducer("BTCUSD").produce(
        now=T0 + timedelta(seconds=7), candles=_all_timeframes(),
        ticker=TICKER, data_quality="OK", book_valid=True, spread_bps=1.0)
    assert first["signal_id"] == second["signal_id"]


def test_history_is_bounded_and_ordered():
    producer = SnapshotProducer("BTCUSD", max_history=3)
    for i in range(5):
        producer.produce(now=T0 + timedelta(minutes=5 * i),
                         candles=_all_timeframes(), ticker=TICKER,
                         data_quality="OK", book_valid=True, spread_bps=1.0)
    assert len(producer.history(100)) == 3
    assert producer.latest() == producer.history(100)[-1]


def test_short_move_requires_three_consecutive_closed_neutral_bars():
    """±15 is only a candidate; three adjacent closed 5m scores confirm it."""
    producer = SnapshotProducer("BTCUSD")
    producer._history.extend([
        {
            "candle_close_utc": T0.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "trend_score": 0.0,
            "data_quality": "OK",
        },
        {
            "candle_close_utc": (T0 + timedelta(minutes=5)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "trend_score": 14.9,
            "data_quality": "OK",
        },
    ])
    assert producer._short_move_confirmed(
        score=-15.0,
        candle_close=T0 + timedelta(minutes=10),
        data_quality="OK",
    ) is True

    # A missing completed candle breaks the 15-minute continuity guarantee.
    producer._history.pop()
    assert producer._short_move_confirmed(
        score=0.0,
        candle_close=T0 + timedelta(minutes=10),
        data_quality="OK",
    ) is False


def test_spread_bps_helper_handles_missing_and_zero_sides():
    assert spread_bps_from(None, 100.0) is None
    assert spread_bps_from(0.0, 100.0) is None
    assert spread_bps_from(99.99, 100.01) == pytest.approx(2.0, abs=0.01)


# ── service + endpoints ─────────────────────────────────────────────────
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
    engine_service.last_ticker = dict(TICKER)
    yield engine_service
    engine_service.event_store.close()


def test_service_produces_and_persists_a_snapshot(service):
    snapshot = service.produce_snapshot()
    assert snapshot is not None
    rows = service.repos.recent_trend_snapshots("BTCUSD")
    assert len(rows) == 1
    assert rows[0].signal_id == snapshot["signal_id"]


def test_snapshot_persistence_is_idempotent_per_signal(service):
    service.produce_snapshot()
    service.produce_snapshot()
    assert len(service.repos.recent_trend_snapshots("BTCUSD")) == 1


def test_snapshot_failure_never_breaks_market_data_capture(service, monkeypatch):
    monkeypatch.setattr(service.producer, "produce",
                        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    assert service.produce_snapshot() is None
    assert service.snapshot_errors == 1
    categories = [e.category for e in service.repos.recent_health_events()]
    assert "snapshot_failed" in categories


def test_trend_endpoints_require_token_and_serve_the_contract(config, service):
    service.produce_snapshot()
    app = create_app(config, service=service)
    headers = {"X-Engine-Token": "test-token"}
    with TestClient(app) as http:
        assert http.get("/trend/latest").status_code == 401
        latest = http.get("/trend/latest", headers=headers)
        assert latest.status_code == 200
        assert latest.json()["schema_version"] == "1.2.0"
        assert http.get("/trend/history?limit=5",
                        headers=headers).json()["snapshots"]
        assert http.get("/regime/latest", headers=headers).json()["regime"]
        assert http.get("/features/latest",
                        headers=headers).json()["components"]


def test_wrong_symbol_is_an_error_not_the_configured_one(config, service):
    service.produce_snapshot()
    app = create_app(config, service=service)
    with TestClient(app) as http:
        response = http.get("/trend/latest?symbol=ETHUSD",
                            headers={"X-Engine-Token": "test-token"})
    assert response.status_code == 404


def test_latest_before_any_snapshot_is_503_not_a_fabricated_neutral(config):
    empty = EngineService(config, clock=FixedClock(T0))
    try:
        app = create_app(config, service=empty)
        with TestClient(app) as http:
            response = http.get("/trend/latest",
                                headers={"X-Engine-Token": "test-token"})
        assert response.status_code == 503
    finally:
        empty.event_store.close()


# ── fail-closed client ──────────────────────────────────────────────────
def _good(now=T0, **overrides):
    payload = {
        "schema_version": "1.0.0", "symbol": "BTCUSD",
        "timestamp": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "candle_close_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "signal_id": "abc123", "regime": "TREND_UP", "direction": 1,
        "trend_score": 72.0, "confidence": 0.68, "entry_allowed": True,
        "signal_ttl_seconds": 300, "data_quality": "OK",
        "reason_codes": ["1H_TREND_UP"],
    }
    payload.update(overrides)
    return payload


class _Response:
    def __init__(self, status_code=200, payload=None, raise_json=False):
        self.status_code = status_code
        self._payload = payload
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("not json")
        return self._payload


def _patch_get(monkeypatch, result):
    def fake_get(*args, **kwargs):
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(client.requests, "get", fake_get)


def test_client_passes_through_a_valid_snapshot(monkeypatch):
    _patch_get(monkeypatch, _Response(200, _good()))
    snapshot = client.get_snapshot("BTCUSD", now=T0)
    assert snapshot["entry_allowed"] is True
    assert snapshot["data_quality"] == "OK"


@pytest.mark.parametrize("failure,expected", [
    (ConnectionError("refused"), client.ENGINE_UNREACHABLE),
    (TimeoutError("timed out"), client.ENGINE_UNREACHABLE),
])
def test_transport_failures_block_entry(monkeypatch, failure, expected):
    _patch_get(monkeypatch, failure)
    snapshot = client.get_snapshot("BTCUSD", now=T0)
    assert snapshot["data_quality"] == expected
    assert snapshot["entry_allowed"] is False
    assert snapshot["regime"] == "DEGRADED"
    assert snapshot["direction"] == 0


@pytest.mark.parametrize("status", [500, 503, 401, 404])
def test_non_200_blocks_entry(monkeypatch, status):
    _patch_get(monkeypatch, _Response(status, None))
    snapshot = client.get_snapshot("BTCUSD", now=T0)
    assert snapshot["data_quality"] == client.ENGINE_UNREACHABLE
    assert snapshot["entry_allowed"] is False


def test_non_json_body_blocks_entry(monkeypatch):
    _patch_get(monkeypatch, _Response(200, None, raise_json=True))
    assert client.get_snapshot("BTCUSD", now=T0)["data_quality"] == \
        client.SCHEMA_MISMATCH


def test_missing_contract_fields_block_entry(monkeypatch):
    payload = _good()
    del payload["entry_allowed"]
    _patch_get(monkeypatch, _Response(200, payload))
    snapshot = client.get_snapshot("BTCUSD", now=T0)
    assert snapshot["data_quality"] == client.SCHEMA_MISMATCH
    assert snapshot["entry_allowed"] is False


def test_major_version_mismatch_blocks_entry(monkeypatch):
    _patch_get(monkeypatch, _Response(200, _good(schema_version="2.0.0")))
    assert client.get_snapshot("BTCUSD", now=T0)["data_quality"] == \
        client.CONTRACT_VERSION_MISMATCH


def test_minor_version_bump_is_accepted(monkeypatch):
    _patch_get(monkeypatch, _Response(200, _good(schema_version="1.3.0")))
    assert client.get_snapshot("BTCUSD", now=T0)["entry_allowed"] is True


def test_expired_signal_blocks_entry(monkeypatch):
    """Contract invariant 12: an expired signal cannot create an order —
    enforced here too, so it holds even if the engine misreports."""
    _patch_get(monkeypatch, _Response(200, _good()))
    snapshot = client.get_snapshot("BTCUSD", now=T0 + timedelta(seconds=301))
    assert snapshot["data_quality"] == client.SIGNAL_EXPIRED
    assert snapshot["entry_allowed"] is False


def test_zero_ttl_is_treated_as_expired(monkeypatch):
    _patch_get(monkeypatch, _Response(200, _good(signal_ttl_seconds=0)))
    assert client.get_snapshot("BTCUSD", now=T0)["data_quality"] == \
        client.SIGNAL_EXPIRED


def test_future_dated_snapshot_is_clock_skew(monkeypatch):
    _patch_get(monkeypatch, _Response(200, _good()))
    snapshot = client.get_snapshot("BTCUSD", now=T0 - timedelta(seconds=30))
    assert snapshot["data_quality"] == client.CLOCK_SKEW
    assert snapshot["entry_allowed"] is False


@pytest.mark.parametrize("age_seconds", [1, 30, 120, 299])
def test_normal_ageing_within_ttl_is_accepted(monkeypatch, age_seconds):
    """Snapshots are produced every closed 5m candle, so a healthy one is
    routinely minutes old by the time it is polled. Treating ordinary age as
    clock skew would block every real signal."""
    _patch_get(monkeypatch, _Response(200, _good()))
    snapshot = client.get_snapshot(
        "BTCUSD", now=T0 + timedelta(seconds=age_seconds))
    assert snapshot["data_quality"] == "OK"
    assert snapshot["entry_allowed"] is True


def test_wrong_symbol_in_payload_blocks_entry(monkeypatch):
    _patch_get(monkeypatch, _Response(200, _good(symbol="ETHUSD")))
    assert client.get_snapshot("BTCUSD", now=T0)["data_quality"] == \
        client.SCHEMA_MISMATCH


def test_engine_claiming_entry_while_degraded_is_rejected(monkeypatch):
    """Invariant 1 re-asserted client-side: a lying engine cannot enable a
    trade by contradicting its own data-quality field."""
    _patch_get(monkeypatch, _Response(
        200, _good(data_quality="STALE_L1", entry_allowed=True)))
    snapshot = client.get_snapshot("BTCUSD", now=T0)
    assert snapshot["entry_allowed"] is False
    assert snapshot["data_quality"] == client.SCHEMA_MISMATCH


def test_client_never_caches_a_previous_good_snapshot(monkeypatch):
    _patch_get(monkeypatch, _Response(200, _good()))
    assert client.get_snapshot("BTCUSD", now=T0)["entry_allowed"] is True
    _patch_get(monkeypatch, ConnectionError("dropped"))
    second = client.get_snapshot("BTCUSD", now=T0)
    assert second["entry_allowed"] is False
    assert second["data_quality"] == client.ENGINE_UNREACHABLE


def test_health_and_status_never_raise(monkeypatch):
    _patch_get(monkeypatch, ConnectionError("refused"))
    assert client.get_health()["ok"] is False
    assert client.get_status()["available"] is False


def test_degraded_snapshot_is_shaped_like_the_contract():
    snapshot = client.degraded_snapshot("BTCUSD", client.ENGINE_UNREACHABLE,
                                        "detail", T0)
    for field in ("schema_version", "symbol", "timestamp", "regime",
                  "direction", "trend_score", "confidence", "entry_allowed",
                  "signal_ttl_seconds", "gates", "reason_codes",
                  "data_quality"):
        assert field in snapshot, field
    assert snapshot["entry_allowed"] is False
    assert snapshot["regime"] == "DEGRADED"


def test_the_minor_bump_to_1_1_0_is_not_a_breaking_change():
    """The client compares MAJOR only, so an engine emitting 1.1.0 and a
    consumer written against 1.0.0 must both work. If this ever fails, the
    zone fields were added as a breaking change by accident."""
    for version in ("1.0.0", "1.1.0", "1.9.3"):
        snapshot = client._validate(_good(schema_version=version), "BTCUSD", T0)
        assert snapshot["data_quality"] == "OK", version
        assert snapshot["entry_allowed"] is True, version

    rejected = client._validate(_good(schema_version="2.0.0"), "BTCUSD", T0)
    assert rejected["data_quality"] == client.CONTRACT_VERSION_MISMATCH
    assert rejected["entry_allowed"] is False
