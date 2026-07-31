import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def test_android_embedded_pages_hide_web_chrome_and_accept_native_theme():
    template = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
    styles = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")
    flutter = (ROOT / "mv_btc_bot" / "lib" / "main.dart").read_text(
        encoding="utf-8")

    assert "request.args.get('app') == '1'" in template
    assert "new URLSearchParams(window.location.search).get('theme')" in template
    assert 'theme-red" aria-hidden="true">Red<' in template
    assert 'theme-blue" aria-hidden="true">Blue<' in template
    assert "body.native-app .sidebar" in styles
    assert "body.native-app .topbar" in styles
    assert "body.native-app .content" in styles
    assert "PageView.builder(" not in flutter
    assert "TabBar(" not in flutter
    assert "IndexedStack(" in flutter
    assert "NavigationBar(" in flutter
    assert "setVerticalScrollBarEnabled(true)" in flutter
    assert "Switch.adaptive(" in flutter
    assert "'RED'" in flutter
    assert "'BLUE'" in flutter
    assert "kRedBackgroundAsset = 'assets/crimson-dashboard-bg.png'" in flutter
    assert "kBlueBackgroundAsset = 'assets/sparkling-blue-dashboard-bg.png'" in flutter
    assert "label: 'Nithi Bot'" in flutter
    assert "label: 'Trend Engine'" in flutter
    assert "path: '/trend-engine'" in flutter
    assert "path: '/dry-run'" in flutter
    assert "path: '/logs'" not in flutter


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
if (live.cls !== 'live-loss' || live.text !== 'LIVE -$1.25') {
  throw new Error(`live position did not take precedence: ${JSON.stringify(live)}`);
}

const liveProfit = statusFromSlots({status: 'OPEN', live_pnl: 8.4});
if (liveProfit.cls !== 'live-profit' || liveProfit.text !== 'LIVE +$8.40') {
  throw new Error(`live profit was not color-coded: ${JSON.stringify(liveProfit)}`);
}

const liveFlat = statusFromSlots({status: 'OPEN', live_pnl: 0});
if (liveFlat.cls !== 'live' || liveFlat.text !== 'LIVE +$0.00') {
  throw new Error(`flat live position was not neutral: ${JSON.stringify(liveFlat)}`);
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
if (attributes['aria-pressed'] !== 'false' || !attributes['aria-label'].includes('Blue')) {
  throw new Error('Red theme toggle state was not initialized');
}
toggle.click();
if (root.dataset.theme !== 'dark' || storage.get('nithi-theme') !== 'dark') {
  throw new Error('Blue theme preference was not persisted');
}
if (attributes['aria-pressed'] !== 'true' || !attributes['aria-label'].includes('Red')) {
  throw new Error('Blue theme toggle accessibility state was not updated');
}
toggle.click();
if ('theme' in root.dataset || storage.get('nithi-theme') !== 'light') {
  throw new Error('Red theme preference was not restored');
}
if (attributes['aria-pressed'] !== 'false' || !attributes['aria-label'].includes('Blue')) {
  throw new Error('Red theme toggle accessibility state was not restored');
}
"""
    result = subprocess.run(
        [NODE, "-e", script], cwd=ROOT, text=True, capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(NODE is None, reason="Node.js is required for frontend JavaScript tests")
def test_closed_positions_never_appear_on_the_unified_open_positions_card():
    """UI-4 replaced slotHtml's per-slot CLOSED-state branch (dashboard_visible
    hides a stale close, same-day shows it) with a structural filter: the
    unified card only ever iterates OPEN, non-dry-run slots. A CLOSED slot --
    stale or same-day -- can no longer leak financial detail onto this card
    at all; same-day detail lives in Today's Trades instead."""
    script = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('templates/overview.html', 'utf8');
const start = source.indexOf('const SLOT_ICON');
const end = source.indexOf('function openProtectionDrawer');
if (start < 0 || end <= start) throw new Error('Overview position-row functions not found');

const elements = {};
function fakeElement() { return { innerHTML: '', className: '', textContent: '' }; }
global.document = { getElementById: id => elements[id] || (elements[id] = fakeElement()) };
global.fN = value => String(value ?? '');
global.f$ = value => '$' + String(value ?? '');
global.esc = value => String(value ?? '');
global.utcToIst = value => String(value ?? '');
global.tradeTimeIst = () => '—';
global.pnlCls = () => 'c-neg';
vm.runInThisContext(source.slice(start, end));

const displaySlots = {
  morning: { status: 'IDLE' },
  evening: { status: 'IDLE' },
  trend: {
    status: 'CLOSED', dashboard_visible: false,
    symbol: 'OLD-CONTRACT', pnl_usd: -99,
  },
};
renderPositions(displaySlots, [], {});
const html = elements['positions-body'].innerHTML;
for (const stale of ['OLD-CONTRACT', '-99']) {
  if (html.includes(stale)) throw new Error(`stale CLOSED detail leaked onto the open-positions card: ${stale}`);
}
if (!html.includes('No open positions')) {
  throw new Error(`clean idle state was not rendered: ${html}`);
}

const withOpenTrend = {
  morning: { status: 'IDLE' },
  evening: { status: 'IDLE' },
  trend: {
    status: 'OPEN', symbol: 'TODAY-CONTRACT', side: 'long', lots: 3,
    live_pnl: 5, control_slot: 'trend',
  },
};
renderPositions(withOpenTrend, [], {});
const current = elements['positions-body'].innerHTML;
for (const currentDetail of ['TODAY-CONTRACT', '<span class="badge live">BOT</span>']) {
  if (!current.includes(currentDetail)) {
    throw new Error(`open position detail was hidden: ${currentDetail}`);
  }
}
for (const inactive of ['aggregate lots targeted', 'Full-size exchange coverage',
                        'current reconciliation', 'Auto-starts on entry']) {
  if (current.includes(inactive)) {
    throw new Error(`inactive protection remained on closed card: ${inactive}`);
  }
}
"""
    result = subprocess.run(
        [NODE, "-e", script], cwd=ROOT, text=True, capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
