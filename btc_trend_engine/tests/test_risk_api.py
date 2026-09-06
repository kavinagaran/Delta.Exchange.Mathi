"""/risk/status and POST /admin/{kill-switch,resume}: token-gated like every
non-/health route, and the latch is only ever cleared by an explicit resume."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from btc_trend_engine.api.app import create_app
from btc_trend_engine.clock import FixedClock
from btc_trend_engine.config import load_config
from btc_trend_engine.service import EngineService

T0 = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
AUTH = {"X-Engine-Token": "test-token"}


@pytest.fixture
def config(tmp_path: Path):
    return load_config(environ={
        "ENGINE_TOKEN": "test-token",
        "ENGINE_STORAGE__DATA_DIR": str(tmp_path / "data"),
    })


@pytest.fixture
def client(config):
    service = EngineService(config, clock=FixedClock(T0))
    try:
        with TestClient(create_app(config, service=service)) as test_client:
            yield test_client
    finally:
        service.event_store.close()
        service.data_lock.release()


def test_risk_endpoints_require_the_engine_token(client):
    assert client.get("/risk/status").status_code == 401
    assert client.post("/admin/kill-switch", json={"name": "manual"}).status_code == 401
    assert client.post("/admin/resume", json={}).status_code == 401


def test_a_fresh_engine_reports_no_active_switches(client):
    body = client.get("/risk/status", headers=AUTH).json()
    assert body["any_active"] is False
    assert body["active"] == []


def test_firing_a_switch_shows_up_in_risk_status(client):
    fired = client.post("/admin/kill-switch", headers=AUTH,
                        json={"name": "manual", "reason": "operator halt"})
    assert fired.status_code == 200
    body = client.get("/risk/status", headers=AUTH).json()
    assert body["any_active"] is True
    assert body["active"] == ["manual"]
    assert body["switches"]["manual"]["reason"] == "operator halt"


def test_an_unknown_switch_name_is_rejected_not_silently_created(client):
    response = client.post("/admin/kill-switch", headers=AUTH,
                           json={"name": "not_a_real_switch"})
    assert response.status_code == 400
    assert client.get("/risk/status", headers=AUTH).json()["active"] == []


def test_a_switch_stays_latched_until_resume_is_called(client):
    client.post("/admin/kill-switch", headers=AUTH, json={"name": "data_quality"})
    # Repeated status reads never clear it.
    for _ in range(3):
        assert client.get("/risk/status", headers=AUTH).json()["any_active"] is True
    client.post("/admin/resume", headers=AUTH, json={"name": "data_quality"})
    assert client.get("/risk/status", headers=AUTH).json()["any_active"] is False


def test_resume_without_a_name_clears_every_switch(client):
    client.post("/admin/kill-switch", headers=AUTH, json={"name": "manual"})
    client.post("/admin/kill-switch", headers=AUTH, json={"name": "data_quality"})
    cleared = client.post("/admin/resume", headers=AUTH, json={}).json()["cleared"]
    assert cleared == ["data_quality", "manual"]
    assert client.get("/risk/status", headers=AUTH).json()["active"] == []


def test_refiring_reports_a_bumped_count_rather_than_a_new_latch(client):
    client.post("/admin/kill-switch", headers=AUTH,
                json={"name": "manual", "reason": "first"})
    second = client.post("/admin/kill-switch", headers=AUTH,
                         json={"name": "manual", "reason": "second"}).json()
    assert second["fired_count"] == 2
    assert client.get("/risk/status", headers=AUTH).json()["active"] == ["manual"]
