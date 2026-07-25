"""/api/all-positions margin/liquidation_price fields (Exposure page, UI-2).

Field names are an unverified venue assumption (docs/assumptions.md A9) — the
API is read defensively so a missing or renamed field degrades to `None`
("not reported") in the response rather than crashing or fabricating a value.
"""
from contextlib import contextmanager
from unittest.mock import patch

import pytest

import dashboard


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


@contextmanager
def _authenticated_client(tmp_path):
    with patch.object(dashboard, "DASH_PASS", ""), \
            patch.object(dashboard, "USERS_DIR", tmp_path / "no-accounts"):
        yield dashboard.app.test_client()


@pytest.fixture(autouse=True)
def _clear_product_cache():
    dashboard._product_cache.clear()
    yield
    dashboard._product_cache.clear()


def _get(margin_field=True):
    def get(url, **_kwargs):
        if url.endswith("/v2/positions/margined"):
            result = [{
                "product_id": 42, "product_symbol": "C-BTC-64000-170726",
                "size": "3", "entry_price": "1.5",
            }]
            if margin_field:
                result[0]["margin"] = "45.5"
                result[0]["liquidation_price"] = "61000.25"
            return _Response({"success": True, "result": result})
        if url.endswith("/v2/products/42"):
            return _Response({"result": {"contract_value": 0.001, "symbol": "C-BTC-64000-170726"}})
        if url.endswith("/v2/tickers/C-BTC-64000-170726"):
            return _Response({"result": {"mark_price": "1.4"}})
        raise AssertionError(f"unexpected fetch: {url}")
    return get


def test_all_positions_reports_margin_and_liquidation_when_the_venue_sends_them(tmp_path):
    with _authenticated_client(tmp_path) as client, \
            patch.object(dashboard, "_active_creds", return_value=("key", "secret")), \
            patch.object(dashboard, "_sign", return_value={}), \
            patch.object(dashboard.req, "get", side_effect=_get(margin_field=True)):
        resp = client.get("/api/all-positions")
    assert resp.status_code == 200
    rows = resp.get_json()
    assert len(rows) == 1
    assert rows[0]["margin"] == 45.5
    assert rows[0]["liquidation_price"] == 61000.25


def test_all_positions_degrades_to_none_when_the_venue_omits_the_fields(tmp_path):
    """The A9 case: field names unconfirmed, so absence must not crash or
    fabricate a number — it must come back as None ("—" in the UI)."""
    with _authenticated_client(tmp_path) as client, \
            patch.object(dashboard, "_active_creds", return_value=("key", "secret")), \
            patch.object(dashboard, "_sign", return_value={}), \
            patch.object(dashboard.req, "get", side_effect=_get(margin_field=False)):
        resp = client.get("/api/all-positions")
    assert resp.status_code == 200
    rows = resp.get_json()
    assert len(rows) == 1
    assert rows[0]["margin"] is None
    assert rows[0]["liquidation_price"] is None
    # Unaffected fields still populate normally.
    assert rows[0]["symbol"] == "C-BTC-64000-170726"
    assert rows[0]["side"] == "LONG"
