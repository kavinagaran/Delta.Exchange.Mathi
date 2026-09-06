"""/api/all-positions margin/liquidation_price fields (Exposure page, UI-2).

Field names are an unverified venue assumption (docs/assumptions.md A9) — the
API is read defensively so a missing or renamed field degrades to `None`
("not reported") in the response rather than crashing or fabricating a value.
"""
from contextlib import contextmanager
from pathlib import Path
import shutil
import subprocess
from unittest.mock import patch

import pytest

import dashboard


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
TEMPLATE = (ROOT / "templates" / "positions.html").read_text(encoding="utf-8")


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


def test_wallet_uses_delta_fixed_usd_inr_conversion(tmp_path):
    def get(url, **_kwargs):
        assert url.endswith("/v2/wallet/balances")
        return _Response({
            "success": True,
            "result": [{
                "asset_symbol": "USD",
                "balance": "590.62",
                "available_balance": "580.25",
            }],
        })

    with _authenticated_client(tmp_path) as client, \
            patch.object(dashboard, "_active_creds", return_value=("key", "secret")), \
            patch.object(dashboard, "_sign", return_value={}), \
            patch.object(dashboard.req, "get", side_effect=get):
        resp = client.get("/api/wallet")

    assert resp.status_code == 200
    wallet = resp.get_json()
    assert wallet["usd_inr_rate"] == 85.0
    assert wallet["inr_balance"] == 50202.7


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


def test_exposure_page_is_one_compact_snapshot_without_legacy_reconciliation():
    for required in (
        'class="stats exposure-summary"',
        'id="w-usd"',
        'id="w-avail"',
        'id="w-inr"',
        'id="x-count"',
        'id="x-pnl"',
        'id="x-margin"',
        'id="exposure-updated"',
        'id="exposure-refresh"',
        'class="exposure-positions-table"',
        "No open exposure",
        "Ownership",
    ):
        assert required in TEMPLATE

    for obsolete in (
        "Reconciliation",
        'id="recon-banner"',
        'id="recon-body"',
        'id="recon-external-note"',
        "renderReconciliation",
        "Bot-tracked slots vs.",
        "for (const slotName of ['morning', 'evening', 'trend'])",
    ):
        assert obsolete not in TEMPLATE


@pytest.mark.skipif(NODE is None, reason="Node.js is required for frontend JavaScript tests")
def test_exposure_summary_and_position_rows_render_semantic_live_values():
    script = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('templates/positions.html', 'utf8');
const start = source.indexOf('function liqDistancePct');
const end = source.indexOf('let exposureLoadPending');
if (start < 0 || end <= start) throw new Error('Exposure render functions not found');

const elements = {};
function element(id) {
  if (!elements[id]) elements[id] = { textContent: '', innerHTML: '', className: '' };
  return elements[id];
}
global.document = { getElementById: element };
global.fN = (v, d = 0) => (v == null || isNaN(+v)) ? '—' : (+v).toFixed(d);
global.f$ = v => (v < 0 ? '-$' : '+$') + Math.abs(+v).toFixed(2);
global.pnlCls = v => +v >= 0 ? 'c-pos' : 'c-neg';
global.esc = value => String(value ?? '');
vm.runInThisContext(source.slice(start, end));

const position = {
  symbol: 'C-BTC-64000-310726', product_id: 42, side: 'LONG', size: 1000,
  entry_price: 600, mark_price: 660, live_pnl: 60,
  margin: null, liquidation_price: null,
};
renderPositions([position], {trend: {status: 'OPEN', product_id: 42}});
if (elements['x-count'].textContent !== '1') throw new Error('open count is wrong');
if (elements['x-pnl'].textContent !== '+$60.00') throw new Error('P/L is wrong');
if (elements['x-margin'].textContent !== '—') throw new Error('missing margin was fabricated');
if (elements['x-margin-note'].textContent !== 'Not reported by Delta') throw new Error('margin note is unclear');
if (!elements['pos-body'].innerHTML.includes('<span class="badge live">BOT</span>')) throw new Error('bot ownership missing');
if (!elements['pos-body'].innerHTML.includes('class="numeric c-pos"')) throw new Error('numeric P/L treatment missing');

renderPositions([], {});
if (elements['x-count'].textContent !== '0') throw new Error('empty count is wrong');
if (elements['x-pnl'].textContent !== '$0.00') throw new Error('empty P/L is wrong');
if (elements['x-margin'].textContent !== '$0.00') throw new Error('empty margin is wrong');
if (!elements['pos-body'].innerHTML.includes('No open exposure')) throw new Error('empty state is unclear');

if (Math.abs(liqDistancePct({side: 'LONG', mark_price: 100, liquidation_price: 80}) - .2) > 1e-9) {
  throw new Error('long liquidation distance is wrong');
}
if (liqDistanceCls(.09) !== 'c-neg' || liqDistanceCls(.15) !== 'c-warn' || liqDistanceCls(.25) !== 'c-pos') {
  throw new Error('liquidation risk colours are wrong');
}
"""
    result = subprocess.run([NODE, "-e", script], cwd=ROOT, text=True,
                            capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
