import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def test_android_native_pages_use_compact_navigation_and_native_theme():
    template = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
    styles = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")
    flutter = (ROOT / "mv_btc_bot" / "lib" / "main.dart").read_text(
        encoding="utf-8")

    assert "request.args.get('app') == '1'" in template
    assert "new URLSearchParams(window.location.search).get('theme')" in template
    assert 'id="theme-picker"' in template
    assert 'role="radiogroup"' in template
    assert 'data-theme-choice="{{ name }}"' in template
    assert "('red', 'blue', 'green', 'violet', 'amber')" in template
    assert '.theme-swatch.is-red' in styles
    assert '.theme-swatch.is-blue' in styles
    assert '.theme-swatch.is-green' in styles
    assert '.theme-swatch.is-violet' in styles
    assert '.theme-swatch.is-amber' in styles
    assert '#tb-btc.btc-market.up' in styles
    assert '#tb-btc.btc-market.down' in styles
    assert '#tb-btc small' in styles
    assert "body.native-app .sidebar" in styles
    assert "body.native-app .topbar" in styles
    assert "body.native-app .content" in styles
    assert "PageView.builder(" not in flutter
    assert "TabBar(" not in flutter
    assert "IndexedStack(" in flutter
    assert "NavigationBar(" in flutter
    assert "NavigationRail(" in flutter
    assert "primaryPageIndexes" in flutter
    assert "Switch.adaptive(" in flutter
    assert "'RED'" in flutter
    assert "'BLUE'" in flutter
    assert "kRedBackgroundAsset = 'assets/crimson-dashboard-bg.png'" in flutter
    assert "kBlueBackgroundAsset = 'assets/sparkling-blue-dashboard-bg.png'" in flutter
    assert "label: 'Today'" in flutter
    assert "label: 'Trend Engine'" in flutter
    assert "path: '/trend-engine'" in flutter
    assert "path: '/dry-run'" in flutter
    assert "path: '/logs'" in flutter


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
def test_theme_picker_persists_five_colour_choices_and_updates_accessibility_state():
    script = r"""
const fs = require('fs');
const vm = require('vm');
const root = { dataset: {palette: 'blue', theme: 'dark'} };
function swatch(name) {
  const attrs = {};
  const classes = new Set();
  return {
    dataset: {themeChoice: name}, attrs, classes,
    setAttribute(k, v) { attrs[k] = String(v); },
    classList: { toggle(k, on) { if (on) classes.add(k); else classes.delete(k); } },
    closest() { return this; },
  };
}
const swatches = ['red', 'blue', 'green', 'violet', 'amber'].map(swatch);
const picker = {
  querySelectorAll() { return swatches; },
  addEventListener(kind, handler) { if (kind === 'click') this.click = target => handler({target}); },
};
const storage = new Map();
global.document = {
  documentElement: root,
  addEventListener() {},
  getElementById(id) { return id === 'theme-picker' ? picker : null; },
  dispatchEvent() {},
};
global.localStorage = { setItem(k, v) { storage.set(k, v); } };
global.CustomEvent = function(type, init) { this.type = type; this.detail = init.detail; };
vm.runInThisContext(fs.readFileSync('static/js/app.js', 'utf8'));

initThemePicker();
if (swatches[1].attrs['aria-checked'] !== 'true' || !swatches[1].classes.has('is-selected')) {
  throw new Error('Blue palette state was not initialized');
}
picker.click(swatches[2]);
if (root.dataset.palette !== 'green' || root.dataset.theme !== 'dark' || storage.get('nithi-theme') !== 'green') {
  throw new Error('Green palette preference was not persisted');
}
if (swatches[2].attrs['aria-checked'] !== 'true' || !swatches[2].classes.has('is-selected')) {
  throw new Error('Green palette accessibility state was not updated');
}
picker.click(swatches[0]);
if (root.dataset.palette !== 'red' || 'theme' in root.dataset || storage.get('nithi-theme') !== 'red') {
  throw new Error('Red palette preference was not restored');
}
"""
    result = subprocess.run(
        [NODE, "-e", script], cwd=ROOT, text=True, capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(NODE is None, reason="Node.js is required for frontend JavaScript tests")
def test_btc_market_pill_shows_small_change_and_direction_class():
    script = r"""
const fs = require('fs');
const vm = require('vm');
global.document = { addEventListener() {}, getElementById() { return null; }, dispatchEvent() {} };
global.CustomEvent = function() {};
vm.runInThisContext(fs.readFileSync('static/js/app.js', 'utf8'));
global.fN = (value, decimals = 0) => Number(value).toFixed(decimals);
const el = {className: '', innerHTML: '', attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }};
setBtcMarketPill(el, 63051, 1.245);
if (!el.className.endsWith('up') || !el.innerHTML.includes('+1.25%')) throw new Error('positive movement not rendered');
setBtcMarketPill(el, 62900, -0.4814);
if (!el.className.endsWith('down') || !el.innerHTML.includes('-0.48%')) throw new Error('negative movement not rendered');
setBtcMarketPill(el, 62900, 0);
if (!el.className.endsWith('flat') || !el.innerHTML.includes('+0.00%')) throw new Error('flat movement not rendered');
"""
    result = subprocess.run(
        [NODE, "-e", script], cwd=ROOT, text=True, capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_today_page_has_contextual_live_actions_without_a_position_section():
    source = (ROOT / "templates" / "overview.html").read_text(encoding="utf-8")

    assert 'id="today-trades-body"' in source
    assert "jget('/api/today-trades')" in source
    assert "closeTodayLiveTrade(" in source
    current_renderer = source.split(
        "function renderTodayCurrentTrade", 1
    )[1].split("function todayDecisionLabel", 1)[0]
    assert '>Exit</button>' in current_renderer
    for removed in (
        'id="positions-body"',
        "renderPositions",
        "botPosRowHtml",
        ">Close Position</button>",
        ">Protection</button>",
        ">Payoff</button>",
        "openProtectionDrawer",
        "saveDrawerProtection",
        "showPayoff",
        "squareOff(",
    ):
        assert removed not in source
