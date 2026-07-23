import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def test_dry_run_trend_ui_uses_only_the_signed_engine_confirmation_flow():
    template = (ROOT / "templates" / "dry_run.html").read_text(encoding="utf-8")

    assert "Trend-based position (CE / PE)" in template
    assert "Trend Engine position (CE / PE / MOVE)" not in template
    assert "dryStatus.display_slots || dryStatus" in template
    assert "/api/trend-engine/dry-run-preview" in template
    assert "/api/trend-engine/dry-run-entry" in template
    assert "/api/trend-entry/preview" not in template
    assert "jpost('/api/trend-entry'" not in template

    post_start = template.index("jpost('/api/trend-engine/dry-run-entry'")
    post_end = template.index("});", post_start)
    request = template[post_start:post_end]
    assert "confirmation_token:" in request
    assert "expected_mode: 'dry_run'" in request
    assert "mode_revision:" in request
    for client_order_field in (
        "symbol:",
        "product_id:",
        "option_type:",
        "lots:",
        "quantity_lots:",
        "entry_price:",
        "maximum_entry_price:",
        "stop_option_price:",
        "target_option_price:",
    ):
        assert client_order_field not in request

    for visible_copy in (
        "Direction score",
        "Contract score",
        "Combined trade score",
        "Entry / maximum",
        "Stop / target",
        "Planned exit",
        "No exchange order will be placed",
        "EXIT is advisory in Phase 1",
    ):
        assert visible_copy in template


@pytest.mark.skipif(NODE is None, reason="Node.js is required for frontend JavaScript tests")
def test_dry_run_cards_route_actions_to_source_slot_and_show_trade_time_first():
    script = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('templates/dry_run.html', 'utf8');
const start = source.indexOf('function dryMoveDisplaySlotFromUtc');
const end = source.indexOf('function dryEngineDecisionLabel');
if (start < 0 || end <= start) throw new Error('DRY RUN card functions not found');

global.dryModeActive = true;
global.fN = value => String(value ?? '—');
global.f$ = value => String(value ?? '—');
global.pnlCls = () => 'c-pos';
global.esc = value => String(value ?? '').replace(/&/g, '&amp;')
  .replace(/</g, '&lt;').replace(/>/g, '&gt;');
global.utcToIst = value => {
  const [h, m] = String(value || '').split(':').map(Number);
  if (!Number.isFinite(h) || !Number.isFinite(m)) return '';
  const total = (h * 60 + m + 330) % 1440;
  const hh = Math.floor(total / 60);
  const mm = total % 60;
  const ap = hh >= 12 ? 'PM' : 'AM';
  return `${((hh + 11) % 12) + 1}:${String(mm).padStart(2, '0')} ${ap} IST`;
};
vm.runInThisContext(source.slice(start, end));

if (dryMoveDisplaySlotFromUtc('05:29:59') !== 'morning') {
  throw new Error('10:59:59 AM IST did not route to Morning');
}
if (dryMoveDisplaySlotFromUtc('05:30:00') !== 'evening') {
  throw new Error('11:00 AM IST did not route to Evening');
}

const state = {
  status: 'OPEN',
  source_slot: 'trend',
  control_slot: 'trend',
  display_slot: 'morning',
  entry_time_utc: '01:50:00',
  symbol: 'MV-BTC-65800-230726',
  side: 'short',
  lots: 1000,
  entry_mark: 445,
  current_mark: 414,
  live_pnl: 17.45,
  dry_protection: {
    status: 'running',
    tp_target_pnl: 500,
    sl_target_pnl: 300,
    tsl_arm_pnl: 125,
    tsl_trail_pnl: 125,
    tsl_lock_min_pnl: 0,
    poll_secs: 30,
  },
};
const html = dryPositionDetails(state, 'morning');
const tradeTime = html.indexOf('<dt>Time of trade</dt>');
const contract = html.indexOf('<dt>Contract</dt>');
if (tradeTime < 0 || contract < 0 || tradeTime > contract) {
  throw new Error(`trade time is not the first detail row: ${html}`);
}
if (!html.includes('7:20 AM IST')) {
  throw new Error(`actual IST trade time is missing: ${html}`);
}
if (html.includes('<dt>Started</dt>')) {
  throw new Error('obsolete Started row remains');
}
if (!html.includes("endDrySimulation('trend', 'morning')")) {
  throw new Error(`Exit no longer targets source trend slot: ${html}`);
}
if (!html.includes("saveDryProtection('morning', 'trend')")) {
  throw new Error(`Protection save no longer targets source trend slot: ${html}`);
}
if (html.includes('Automatic MOVE Forecast')) {
  throw new Error(`routed score position inherited an unrelated scheduled forecast: ${html}`);
}

const closed = dryPositionDetails({
  ...state,
  status: 'CLOSED',
  pnl_usd: 10,
  exit_mark: 400,
  exit_time_utc: '02:50:00',
}, 'morning');
if (closed.indexOf('<dt>Time of trade</dt>') > closed.indexOf('<dt>Contract</dt>')) {
  throw new Error(`closed trade time is not first: ${closed}`);
}
"""
    result = subprocess.run(
        [NODE, "-e", script], cwd=ROOT, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(NODE is None, reason="Node.js is required for frontend JavaScript tests")
def test_dry_run_trend_panel_exposes_action_only_for_confirmable_buy_decisions():
    script = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('templates/dry_run.html', 'utf8');
const start = source.indexOf('function dryEngineDecisionLabel');
const end = source.indexOf('function renderDryMode');
if (start < 0 || end <= start) throw new Error('Trend Engine panel functions not found');

global.dryModeActive = true;
global.dryStatus = {trend: {status: 'IDLE'}};
global.dryTrendApplying = false;
global.dryKnownNumber = value => value !== null && value !== undefined &&
  value !== '' && Number.isFinite(Number(value));
global.fN = (value, digits = 2) => Number(value).toFixed(digits);
global.esc = value => String(value ?? '').replace(/&/g, '&amp;')
  .replace(/</g, '&lt;').replace(/>/g, '&gt;');
vm.runInThisContext(source.slice(start, end));

const buy = {
  ok: true,
  dry_run: true,
  decision: 'BUY_CE',
  decision_summary: 'Bullish setup passed every entry gate.',
  direction_score: 74,
  trade_score: 82,
  selected_contract: {symbol: 'C-BTC-TEST', contract_score: 79},
  order_plan: {
    quantity_lots: 25,
    entry_price: 100,
    maximum_entry_price: 101,
    stop_option_price: 88,
    target_option_price: 126,
    time_exit: '2026-07-22T12:00:00Z',
  },
  can_apply: true,
  confirmation_token: 'signed-token',
  mode_revision: 'dry-revision',
};
const buyHtml = dryTrendEngineHtml(buy);
for (const expected of [
  'dry-trend-engine-start', 'Start CE simulation', 'C-BTC-TEST',
  '74.0', '79.0', '82.0', '25.00', 'Simulation only',
]) {
  if (!buyHtml.includes(expected)) throw new Error(`BUY panel omitted ${expected}: ${buyHtml}`);
}

for (const decision of ['NO_TRADE', 'HOLD', 'EXIT']) {
  const html = dryTrendEngineHtml({
    ...buy,
    decision,
    can_apply: false,
    confirmation_token: null,
    selected_contract: null,
    trade_score: null,
    order_plan: {},
  });
  if (html.includes('dry-trend-engine-start')) {
    throw new Error(`${decision} exposed a start action`);
  }
  if (!html.includes('Not calculated')) {
    throw new Error(`${decision} did not explain its missing trade score`);
  }
}

const escaped = dryTrendEngineHtml({...buy, decision_summary: '<img src=x>'});
if (escaped.includes('<img src=x>') || !escaped.includes('&lt;img src=x&gt;')) {
  throw new Error('decision summary was not escaped');
}

global.dryStatus = {trend: {status: 'OPEN'}};
if (dryTrendEngineHtml(buy).includes('dry-trend-engine-start')) {
  throw new Error('an open Trend simulation did not block a second entry');
}
"""
    result = subprocess.run(
        [NODE, "-e", script], cwd=ROOT, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(NODE is None, reason="Node.js is required for frontend JavaScript tests")
def test_dry_run_trend_submit_sends_no_client_selected_order_details():
    script = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('templates/dry_run.html', 'utf8');
const start = source.indexOf('async function startDryTrendEngineSimulation');
const end = source.indexOf('async function endDrySimulation');
if (start < 0 || end <= start) throw new Error('Trend Engine submit function not found');

global.dryTrendEnginePreview = {
  ok: true,
  dry_run: true,
  decision: 'BUY_PE',
  direction_score: -72,
  trade_score: 84,
  selected_contract: {symbol: 'P-BTC-TEST', contract_score: 81},
  order_plan: {
    quantity_lots: 40,
    entry_price: 90,
    maximum_entry_price: 91,
    stop_option_price: 99,
    target_option_price: 66,
    time_exit: '2026-07-22T12:00:00Z',
  },
  confirmation_token: 'server-signed-token',
  mode_revision: 'dry-revision-7',
  can_apply: true,
};
global.dryStatus = {trend: {status: 'IDLE'}};
global.dryModeActive = true;
global.dryTrendApplying = false;
global.dryEngineDecisionLabel = value => String(value).replace(/_/g, ' ');
global.dryEnginePair = (left, right) => `${left} / ${right}`;
global.dryEngineDateTime = value => String(value);
global.fN = value => String(value);
global.confirm = () => true;
global.document = {getElementById() { return {disabled: false, textContent: ''}; }};
global.toast = () => {};
global.loadDryStatus = async () => {};
global.loadDryToday = async () => {};
let submitted = null;
global.jpost = async (path, body) => {
  submitted = {path, body};
  return {ok: true, dry_run: true, option_type: 'PE', lots: 40};
};
vm.runInThisContext(source.slice(start, end));

(async () => {
  await startDryTrendEngineSimulation();
  if (!submitted || submitted.path !== '/api/trend-engine/dry-run-entry') {
    throw new Error(`wrong submit path: ${JSON.stringify(submitted)}`);
  }
  const keys = Object.keys(submitted.body).sort();
  const expected = ['confirmation_token', 'expected_mode', 'mode_revision'];
  if (JSON.stringify(keys) !== JSON.stringify(expected)) {
    throw new Error(`unexpected client order fields: ${JSON.stringify(submitted.body)}`);
  }
  if (submitted.body.confirmation_token !== 'server-signed-token' ||
      submitted.body.expected_mode !== 'dry_run' ||
      submitted.body.mode_revision !== 'dry-revision-7') {
    throw new Error(`confirmation binding changed: ${JSON.stringify(submitted.body)}`);
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run(
        [NODE, "-e", script], cwd=ROOT, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
