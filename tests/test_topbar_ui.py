import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="Node.js is required for frontend JavaScript tests")
def test_closed_trade_capsule_classifies_pnl_and_uses_latest_trade():
    script = r"""
const fs = require('fs');
const vm = require('vm');
global.document = {
  addEventListener() {}, getElementById() { return null; }, dispatchEvent() {}
};
global.CustomEvent = function() {};
vm.runInThisContext(fs.readFileSync('static/js/app.js', 'utf8'));

const cases = [
  [{pnl_usd: 12.34}, 'closed-profit', 'CLOSED +$12.34'],
  [{pnl_usd: -0.18}, 'closed-loss', 'CLOSED -$0.18'],
  [{pnl_usd: 0}, 'closed', 'CLOSED +$0.00'],
  [{pnl_usd: null}, 'closed', 'CLOSED \u2014'],
  [{}, 'closed', 'CLOSED \u2014'],
  [{pnl_usd: 'not-a-number'}, 'closed', 'CLOSED \u2014'],
];
for (const [trade, expectedClass, expectedText] of cases) {
  const actual = _closedPill(trade);
  if (actual.cls !== expectedClass || actual.text !== expectedText) {
    throw new Error(JSON.stringify({trade, actual, expectedClass, expectedText}));
  }
}

const latestLoss = statusFromSlots({
  status: 'CLOSED', entry_date: '2026-07-14', entry_time_utc: '12:00:00',
  exit_time_utc: '20:00:00', pnl_usd: 99,
  morning: {status: 'CLOSED', entry_date: '2026-07-15',
            entry_time_utc: '05:00:00', exit_time_utc: '11:00:00', pnl_usd: 8},
  latest_closed_trade: {pnl_usd: -0.18, closed_at_utc: '2026-07-15T11:57:21Z'},
});
if (latestLoss.cls !== 'closed-loss' || latestLoss.text !== 'CLOSED -$0.18') {
  throw new Error(`authoritative latest close was ignored: ${JSON.stringify(latestLoss)}`);
}

const live = statusFromSlots({
  status: 'OPEN', live_pnl: -1.25,
  latest_closed_trade: {pnl_usd: 20, closed_at_utc: '2026-07-15T11:57:21Z'},
});
if (live.cls !== 'live' || live.text !== 'LIVE -$1.25') {
  throw new Error(`live position did not take precedence: ${JSON.stringify(live)}`);
}

const overnight = _closedAtMs({
  entry_date: '2026-07-15', entry_time_utc: '23:00:00', exit_time_utc: '01:00:00'
});
const sameDay = _closedAtMs({
  entry_date: '2026-07-15', entry_time_utc: '12:00:00', exit_time_utc: '22:00:00'
});
if (!(overnight > sameDay)) throw new Error('overnight close ordering failed');
"""
    result = subprocess.run(
        [NODE, "-e", script], cwd=ROOT, text=True, capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(NODE is None, reason="Node.js is required for frontend JavaScript tests")
def test_theme_toggle_persists_choice_and_updates_accessibility_state():
    script = r"""
const fs = require('fs');
const vm = require('vm');
const root = { dataset: {} };
const attributes = {};
const toggle = {
  title: '',
  setAttribute(k, v) { attributes[k] = String(v); },
  addEventListener(kind, handler) { if (kind === 'click') this.click = handler; },
};
const storage = new Map();
global.document = {
  documentElement: root,
  addEventListener() {},
  getElementById(id) { return id === 'theme-toggle' ? toggle : null; },
  dispatchEvent() {},
};
global.localStorage = { setItem(k, v) { storage.set(k, v); } };
global.CustomEvent = function(type, init) { this.type = type; this.detail = init.detail; };
vm.runInThisContext(fs.readFileSync('static/js/app.js', 'utf8'));

initThemeToggle();
if (attributes['aria-pressed'] !== 'false' || !attributes['aria-label'].includes('dark')) {
  throw new Error('light toggle state was not initialized');
}
toggle.click();
if (root.dataset.theme !== 'dark' || storage.get('nithi-theme') !== 'dark') {
  throw new Error('dark theme was not persisted');
}
if (attributes['aria-pressed'] !== 'true' || !attributes['aria-label'].includes('light')) {
  throw new Error('dark toggle accessibility state was not updated');
}
toggle.click();
if ('theme' in root.dataset || storage.get('nithi-theme') !== 'light') {
  throw new Error('light theme was not restored');
}
"""
    result = subprocess.run(
        [NODE, "-e", script], cwd=ROOT, text=True, capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(NODE is None, reason="Node.js is required for frontend JavaScript tests")
def test_stale_closed_card_renders_clean_idle_state_without_old_protection_health():
    script = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('templates/overview.html', 'utf8');
const start = source.indexOf('function tpRow');
const end = source.indexOf('function renderExternalOptions');
if (start < 0 || end <= start) throw new Error('Overview card functions not found');

global.manualBtns = () => '<button>MANUAL</button>';
global.fN = value => String(value ?? '');
global.f$ = value => '$' + String(value ?? '');
global.esc = value => String(value ?? '');
global.utcToIst = value => String(value ?? '');
global.pnlCls = () => 'c-neg';
vm.runInThisContext(source.slice(start, end));

const html = slotHtml({
  status: 'CLOSED',
  dashboard_visible: false,
  symbol: 'OLD-CONTRACT',
  pnl_usd: -99,
}, 'trend', {
  trend: {
    running: true,
    protection_established: true,
    protected_lots: 6,
    bot_entry_lots: 3,
    external_protected_lots: 3,
    coverage_status: 'exchange_protected',
    monitor_error: 'stale monitor error',
  },
});

for (const stale of ['OLD-CONTRACT', '-99', 'aggregate lots targeted',
                     'Full-size exchange coverage', 'stale monitor error']) {
  if (html.includes(stale)) throw new Error(`stale detail remained: ${stale}`);
}
if (!html.includes('No position') || !html.includes('Auto-starts on entry')) {
  throw new Error(`clean idle state was not rendered: ${html}`);
}

const current = slotHtml({
  status: 'CLOSED',
  dashboard_visible: true,
  symbol: 'TODAY-CONTRACT',
  pnl_usd: 5,
  exit_time_utc: '05:00:00',
}, 'trend', {
  trend: {
    running: true,
    protected_lots: 6,
    bot_entry_lots: 3,
    external_protected_lots: 3,
    coverage_status: 'exchange_protected',
    monitor_error: 'current reconciliation',
  },
});
for (const currentDetail of ['TODAY-CONTRACT', 'aggregate lots targeted',
                             'Full-size exchange coverage',
                             'current reconciliation']) {
  if (!current.includes(currentDetail)) {
    throw new Error(`same-day detail was hidden: ${currentDetail}`);
  }
}
"""
    result = subprocess.run(
        [NODE, "-e", script], cwd=ROOT, text=True, capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
