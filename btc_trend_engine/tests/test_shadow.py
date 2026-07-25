"""Shadow comparison classification, storage and the two endpoints.

The classification is the actual product of the 30–45 day shadow window, so
the distinctions it draws matter more than the headline agreement rate: an
absent snapshot, a degraded feed, and a genuine directional disagreement are
three different facts and must never be collapsed into one number.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from btc_trend_engine.api.app import create_app
from btc_trend_engine.clock import FixedClock
from btc_trend_engine.config import load_config
from btc_trend_engine.service import EngineService
from btc_trend_engine.signals import shadow

T0 = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
AUTH = {"X-Engine-Token": "test-token"}
CANDLE = "2026-07-25T11:55:00Z"


def _snapshot(**overrides) -> dict:
    snap = {
        "schema_version": "1.0.0", "symbol": "BTCUSD",
        "timestamp": "2026-07-25T12:00:00Z", "candle_close_utc": CANDLE,
        "signal_id": "eng-1", "regime": "TREND_UP", "regime_since": CANDLE,
        "direction": 1, "trend_score": 72.0, "confidence": 0.8,
        "entry_allowed": True, "signal_ttl_seconds": 300,
        "data_quality": "OK", "reason_codes": [],
    }
    snap.update(overrides)
    return snap


# ── classification ──────────────────────────────────────────────────────
def test_same_direction_agrees():
    agreed, reason = shadow.classify(1, _snapshot(direction=1))
    assert agreed is True and reason == shadow.AGREE


def test_a_missing_engine_snapshot_is_not_counted_as_a_disagreement():
    """None, not False. Scoring 'the engine said nothing' as a disagreement
    would understate the agreement rate that gates cutover."""
    agreed, reason = shadow.classify(1, None)
    assert agreed is None
    assert reason == shadow.ENGINE_MISSING


def test_a_degraded_engine_is_not_counted_as_a_disagreement():
    agreed, reason = shadow.classify(1, _snapshot(data_quality="STALE_L1",
                                                  direction=0))
    assert agreed is None
    assert reason == shadow.ENGINE_DEGRADED


def test_opposed_directions_are_a_real_disagreement():
    agreed, reason = shadow.classify(1, _snapshot(direction=-1))
    assert agreed is False and reason == shadow.DIRECTION_OPPOSED


def test_engine_flat_because_a_gate_blocked_it_is_distinguished():
    agreed, reason = shadow.classify(
        1, _snapshot(direction=0, entry_allowed=False))
    assert agreed is False and reason == shadow.ENGINE_GATED


def test_engine_flat_with_entry_allowed_is_a_genuine_read_difference():
    agreed, reason = shadow.classify(
        1, _snapshot(direction=0, entry_allowed=True))
    assert agreed is False
    assert reason == shadow.ENGINE_FLAT_LEGACY_DIRECTIONAL


def test_engine_directional_while_legacy_is_flat():
    agreed, reason = shadow.classify(0, _snapshot(direction=1))
    assert agreed is False
    assert reason == shadow.ENGINE_DIRECTIONAL_LEGACY_FLAT


# ── summary arithmetic ──────────────────────────────────────────────────
class _Row:
    def __init__(self, agreed, reason, legacy=1, engine=1, present=True,
                 entry_allowed=True, candle="2026-07-25T11:55:00Z"):
        self.agreed = agreed
        self.disagreement_reason = reason
        self.legacy_direction = legacy
        self.engine_direction = engine
        self.engine_present = present
        self.engine_entry_allowed = entry_allowed
        self.candle_close_utc = candle


def test_agreement_rate_excludes_incomparable_rows():
    """Two agreements, one real disagreement, two rows the engine could not
    speak to => 2/3, not 2/5."""
    rows = [
        _Row(True, shadow.AGREE), _Row(True, shadow.AGREE),
        _Row(False, shadow.DIRECTION_OPPOSED, engine=-1),
        _Row(None, shadow.ENGINE_MISSING, present=False, engine=None),
        _Row(None, shadow.ENGINE_DEGRADED),
    ]
    summary = shadow.summarise(rows)
    assert summary["total_candles"] == 5
    assert summary["comparable_candles"] == 3
    assert summary["agreements"] == 2
    assert summary["agreement_rate"] == pytest.approx(2 / 3)
    assert summary["engine_coverage"] == pytest.approx(3 / 5)


def test_every_disagreement_appears_in_the_histogram():
    rows = [_Row(False, shadow.DIRECTION_OPPOSED, engine=-1),
            _Row(False, shadow.ENGINE_GATED, engine=0),
            _Row(True, shadow.AGREE)]
    histogram = shadow.summarise(rows)["disagreement_histogram"]
    assert histogram[shadow.DIRECTION_OPPOSED] == 1
    assert histogram[shadow.ENGINE_GATED] == 1
    assert histogram[shadow.AGREE] == 1


def test_confusion_matrix_keys_are_signed_directions():
    rows = [_Row(False, shadow.DIRECTION_OPPOSED, legacy=1, engine=-1)]
    assert shadow.summarise(rows)["confusion_matrix"] == {"legacy+1_engine-1": 1}


def test_an_empty_history_reports_none_rather_than_a_fake_hundred_percent():
    summary = shadow.summarise([])
    assert summary["agreement_rate"] is None
    assert summary["total_candles"] == 0


def test_entry_eligible_signals_are_counted_for_the_cutover_criterion():
    rows = [_Row(True, shadow.AGREE, entry_allowed=True),
            _Row(True, shadow.AGREE, entry_allowed=False),
            _Row(None, shadow.ENGINE_MISSING, present=False, entry_allowed=None)]
    assert shadow.summarise(rows)["engine_entry_eligible_signals"] == 1


# ── endpoints ───────────────────────────────────────────────────────────
@pytest.fixture
def config(tmp_path: Path):
    return load_config(environ={
        "ENGINE_TOKEN": "test-token",
        "ENGINE_STORAGE__DATA_DIR": str(tmp_path / "data"),
    })


@pytest.fixture
def service(config):
    engine_service = EngineService(config, clock=FixedClock(T0))
    yield engine_service
    engine_service.event_store.close()
    engine_service.data_lock.release()


@pytest.fixture
def client(config, service):
    with TestClient(create_app(config, service=service)) as test_client:
        yield test_client


def _post(client, **overrides):
    body = {"signal_key": "up|a|b|c", "candle_close_utc": CANDLE,
            "direction": 1, "score": 72.5, "zone": "TREND_UP",
            "market_regime": "TREND_UP", "source": "legacy"}
    body.update(overrides)
    return client.post("/shadow/legacy-decision", headers=AUTH, json=body)


def test_shadow_endpoints_require_the_token(client):
    assert client.post("/shadow/legacy-decision", json={}).status_code == 401
    assert client.get("/shadow/summary").status_code == 401


def test_a_decision_with_no_engine_snapshot_is_recorded_as_such(client):
    response = _post(client)
    assert response.status_code == 200
    assert response.json()["reason"] == shadow.ENGINE_MISSING
    summary = client.get("/shadow/summary", headers=AUTH).json()
    assert summary["total_candles"] == 1
    assert summary["comparable_candles"] == 0
    assert summary["agreement_rate"] is None


def test_a_decision_is_joined_to_the_engine_snapshot_for_that_candle(
        client, service):
    service.repos.record_trend_snapshot(_snapshot(), now=T0)
    assert _post(client).json()["agreed"] is True
    summary = client.get("/shadow/summary", headers=AUTH).json()
    assert summary["agreement_rate"] == 1.0
    assert summary["comparable_candles"] == 1


def test_the_join_is_on_the_candle_not_the_wall_clock(client, service):
    """A snapshot for a DIFFERENT candle must not be matched, however
    recent it is."""
    service.repos.record_trend_snapshot(
        _snapshot(candle_close_utc="2026-07-25T11:50:00Z", signal_id="other"),
        now=T0)
    assert _post(client).json()["reason"] == shadow.ENGINE_MISSING


def test_reposting_the_same_candle_updates_rather_than_double_counts(client):
    _post(client)
    _post(client)
    summary = client.get("/shadow/summary", headers=AUTH).json()
    assert summary["total_candles"] == 1, "the agreement rate would drift"


def test_a_missing_candle_close_is_rejected_rather_than_stored_unjoinable(client):
    response = client.post("/shadow/legacy-decision", headers=AUTH,
                           json={"direction": 1})
    assert response.status_code == 400


def test_the_summary_lists_recent_rows_newest_first(client):
    for minute in ("11:40", "11:45", "11:50"):
        _post(client, candle_close_utc=f"2026-07-25T{minute}:00Z")
    recent = client.get("/shadow/summary", headers=AUTH).json()["recent"]
    assert [r["candle_close_utc"] for r in recent] == [
        "2026-07-25T11:50:00Z", "2026-07-25T11:45:00Z", "2026-07-25T11:40:00Z"]
