import json
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

import dashboard


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def isolated_live_dashboard(tmp_path, monkeypatch):
    users = tmp_path / "users"
    account = users / "alice"
    account.mkdir(parents=True)
    monkeypatch.setattr(dashboard, "USERS_DIR", users)
    monkeypatch.setattr(dashboard, "DASH_USER", "alice")
    monkeypatch.setattr(dashboard, "BOT_USER", "alice")
    monkeypatch.setattr(dashboard, "_sync_states_from_exchange", lambda: None)
    monkeypatch.setattr(dashboard, "_revive_tp_monitors", lambda: None)
    monkeypatch.setattr(dashboard, "_import_legacy_dry_records", lambda: None)
    monkeypatch.setattr(dashboard, "_user_cfg", lambda: {})
    monkeypatch.setitem(dashboard._last_revive, "ts", time.time())
    dashboard._external_options.clear()

    class TickerResponse:
        @staticmethod
        def json():
            return {"result": {"mark_price": "414.8433"}}

    monkeypatch.setattr(
        dashboard.req, "get", lambda *args, **kwargs: TickerResponse(),
    )
    return account


def _live_state(**overrides):
    state = {
        "slot": "trend",
        "status": "OPEN",
        "symbol": "MV-BTC-65800-230726",
        "product_id": 42,
        "instrument_kind": "BTC_MOVE",
        "option_type": "MOVE",
        "side": "short",
        "lots": 1000,
        "entry_date": "2026-07-23",
        "entry_time_utc": "01:50:00",
        "entry_mark": 445,
        "contract_value": 0.001,
        "total_cost_usd": 445,
        "ownership": "trend_score_auto",
    }
    state.update(overrides)
    return state


@pytest.mark.parametrize(
    ("entry_time_utc", "expected_slot"),
    [
        ("05:29:59", "morning"),  # 10:59:59 AM IST
        ("05:30:00", "evening"),  # 11:00:00 AM IST
        ("01:50:00", "morning"),  # 7:20 AM IST
    ],
)
def test_real_status_projects_trend_owned_move_by_actual_ist_trade_time(
        isolated_live_dashboard, entry_time_utc, expected_slot):
    state = _live_state(entry_time_utc=entry_time_utc)
    state_path = isolated_live_dashboard / "trend_state.json"
    _write(state_path, state)

    with dashboard.app.test_request_context("/api/status"):
        payload = dashboard.api_status().get_json()

    displayed = payload["display_slots"]
    other_slot = "evening" if expected_slot == "morning" else "morning"
    assert displayed[expected_slot]["symbol"] == state["symbol"]
    assert displayed[expected_slot]["display_slot"] == expected_slot
    assert displayed[expected_slot]["source_slot"] == "trend"
    assert displayed[expected_slot]["control_slot"] == "trend"
    assert displayed[expected_slot]["display_instrument_group"] == "move"
    assert displayed[other_slot]["status"] == "IDLE"
    assert displayed["trend"]["status"] == "IDLE"

    # Existing clients and all trading controls continue to use the raw,
    # storage-owned state. Presentation routing must never move it on disk.
    assert payload["trend"]["symbol"] == state["symbol"]
    assert json.loads(state_path.read_text(encoding="utf-8")) == state
    assert not (isolated_live_dashboard / "morning_state.json").exists()
    assert not (isolated_live_dashboard / "straddle_state.json").exists()


@pytest.mark.parametrize(
    ("option_type", "symbol"),
    [
        ("CE", "C-BTC-65400-230726"),
        ("PE", "P-BTC-66400-230726"),
        ("CE", "B-BTC-64400_210726"),
    ],
)
def test_real_status_keeps_ce_pe_positions_in_third_frame(
        isolated_live_dashboard, option_type, symbol):
    _write(
        isolated_live_dashboard / "trend_state.json",
        _live_state(
            symbol=symbol,
            instrument_kind="BTC_OPTION",
            option_type=option_type,
            side="long",
        ),
    )

    with dashboard.app.test_request_context("/api/status"):
        payload = dashboard.api_status().get_json()

    displayed = payload["display_slots"]
    assert displayed["trend"]["symbol"] == symbol
    assert displayed["trend"]["source_slot"] == "trend"
    assert displayed["trend"]["control_slot"] == "trend"
    assert displayed["trend"]["display_instrument_group"] == "trend_option"
    assert displayed["morning"]["status"] == "IDLE"
    assert displayed["evening"]["status"] == "IDLE"


def test_real_status_prefers_authoritative_entry_timestamp_for_move_bucket(
        isolated_live_dashboard):
    _write(
        isolated_live_dashboard / "trend_state.json",
        _live_state(
            entry_time_utc="12:00:00",
            entry_at_utc="2026-07-23T01:50:00Z",
        ),
    )

    with dashboard.app.test_request_context("/api/status"):
        payload = dashboard.api_status().get_json()

    assert (
        payload["display_slots"]["morning"]["symbol"]
        == "MV-BTC-65800-230726"
    )
    assert payload["display_slots"]["evening"]["status"] == "IDLE"


def test_live_display_projection_reports_a_same_frame_conflict_without_mutation():
    morning = _live_state(
        slot="morning",
        symbol="MV-BTC-65000-230726",
        entry_time_utc="01:00:00",
        ownership="recovered_move",
    )
    trend = _live_state(entry_time_utc="02:00:00")
    original_morning = dict(morning)
    original_trend = dict(trend)

    displayed, conflicts = dashboard._position_display_slots({
        "morning": morning,
        "evening": {},
        "trend": trend,
    })

    assert displayed["morning"]["symbol"] == trend["symbol"]
    assert displayed["morning"]["control_slot"] == "trend"
    assert conflicts["morning"] == [{
        "source_slot": "morning",
        "status": "OPEN",
        "symbol": morning["symbol"],
        "entry_at_utc": morning["entry_time_utc"],
    }]
    assert morning == original_morning
    assert trend == original_trend


def test_real_overview_is_a_same_day_trade_ledger_only():
    source = (ROOT / "templates" / "overview.html").read_text(encoding="utf-8")

    for required in (
        'id="today-summary"',
        'id="today-trades-body"',
        "jget('/api/today-trades')",
        "Today's Trades",
        '>Exit</button>',
        "renderTodayTrades(",
        "todayInlineProtectionHtml(",
        "jget('/api/tp-monitor')",
    ):
        assert required in source
    for non_daily in (
        'id="positions-body"',
        "No open positions",
        "st.display_slots ||",
        "openProtectionDrawer",
        "showPayoff(",
        "squareOff(",
        "Trading mode",
        "Engine health",
        "Scheduled forecast-driven entries only",
        "Automatic MOVE Forecast",
        "Waiting for the next scheduled decision cycle",
        "SIDEWAYS SELL immediate",
        "when-morning",
        "when-evening",
        "function moveDecisionHtml",
        "function loadTrend",
        "function trendEntry",
        "/api/trend-entry/preview",
        "jpost('/api/trend-entry'",
        'id="today-body"',
        ">Close Position</button>",
        ">Protection</button>",
        ">Payoff</button>",
    ):
        assert non_daily not in source


def test_today_trades_uses_the_active_accounts_dry_run_history(
        isolated_live_dashboard, monkeypatch):
    now = datetime.now(timezone.utc)
    dry_history = isolated_live_dashboard / "dry_run" / "trade_history.json"
    _write(dry_history, [{
        "slot": "trend", "status": "CLOSED", "dry_run": True,
        "symbol": "MV-BTC-62600-020826", "side": "short", "lots": 1000,
        "entry_date": now.strftime("%Y-%m-%d"),
        "entry_time_utc": now.strftime("%H:%M:%S"),
        "exit_date": now.strftime("%Y-%m-%d"),
        "exit_time_utc": now.strftime("%H:%M:%S"),
        "entry_mark": 358, "exit_mark": 500, "pnl_usd": -142,
    }])
    monkeypatch.setattr(dashboard, "_user_cfg", lambda: {
        "DRY_RUN": "true", "TREND_ENGINE_SCORE_AUTO_MODE": "dry_run",
    })

    with dashboard.app.test_request_context("/api/today-trades"):
        rows = dashboard.api_today_trades().get_json()

    assert len(rows) == 1
    assert rows[0]["symbol"] == "MV-BTC-62600-020826"
    assert rows[0]["dry_run"] is True


@pytest.mark.skipif(NODE is None, reason="Node.js is required for frontend JavaScript tests")
def test_real_overview_summarises_and_renders_only_today_rows():
    script = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('templates/overview.html', 'utf8');
const start = source.indexOf('const TODAY_CONTROL_SLOTS');
const end = source.indexOf('async function loadAll');
if (start < 0 || end <= start) throw new Error('Today ledger functions not found');

const elements = {};
function element(id) {
  if (!elements[id]) {
    const card = { className: '' };
    const classes = new Set();
    elements[id] = {
      innerHTML: '', textContent: '', className: '',
      closest(selector) { return selector === '.stat' ? card : null; },
      classList: {
        add(value) { classes.add(value); },
        remove(value) { classes.delete(value); },
        contains(value) { return classes.has(value); },
      },
      card,
    };
  }
  return elements[id];
}
global.document = { getElementById: element, addEventListener() {} };
global.window = {};
global.fN = value => String(value ?? '—');
global.f$ = value => {
  const number = Number(value);
  return `${number < 0 ? '-$' : '+$'}${Math.abs(number).toFixed(2)}`;
};
global.pnlCls = value => Number(value) < 0 ? 'c-neg' : 'c-pos';
global.esc = value => String(value ?? '').replace(/&/g, '&amp;')
  .replace(/</g, '&lt;').replace(/>/g, '&gt;');
global.confirm = () => true;
global.toast = () => {};
global.protectionDrawerFieldsHtml = record =>
  `<input id="pdw-target" value="${record.target_pnl}">`;
global.protectionDrawerIdFor = () => ({});
global.saveProtectionConfig = async () => ({ok: true});
let posted = null;
global.jpost = async (url, body) => {
  posted = {url, body};
  return {ok: false, error: 'intentional test stop'};
};
global.jget = async url => {
  if (url === '/api/today-trades') {
    return [
      {symbol: 'C-BTC-65000', side: 'long', strike: 65000, lots: 1000,
       entry_mark: 500, _live: true, current_mark: 525, live_pnl: 25,
       slot: 'trend'},
      {symbol: 'P-BTC-64000', side: 'long', strike: 64000, lots: 1000,
       entry_mark: 450, exit_mark: 400, pnl_usd: -50, slot: 'trend',
       entry_date: '2026-08-02', entry_time_utc: '02:11:00',
       exit_date: '2026-08-02', exit_time_utc: '02:31:00',
       exit_trigger: 'trailing_stop'},
    ];
  }
  if (url === '/api/tp-monitor') {
    return {trend: {
      running: true, target_pnl: 500, sl_pnl: 250, tsl_arm_pnl: 125,
      tsl_trail_pnl: 100, tsl_lock_min_pnl: 25, poll_secs: 10,
      protection_source: 'automatic_filled_premium',
      coverage_status: 'exchange_protected',
      health: {peak_pnl: 180, heartbeat_utc: new Date().toISOString()},
    }};
  }
  if (url === '/api/trend-engine/score-auto/status') {
    return {status: 'signal_consumed', engine_zone: 'CE_2_ITM',
      direction_score: 43.2, market_regime: 'trend_up', lots: 1000,
      symbol: 'C-BTC-65000',
      last_action: 'this completed LIVE signal was already handled; waiting for the next one'};
  }
  if (url === '/api/engine/snapshot') {
    return {trend_score: 43.2, data_quality: 'OK'};
  }
  if (url === '/api/engine/live') {
    return {available: true, live_score: 47.8, data_quality: 'OK'};
  }
  throw new Error(`unexpected endpoint: ${url}`);
};
vm.runInThisContext(source.slice(start, end));

(async () => {
  await loadToday();
  if (elements['today-trade-total'].textContent !== '2') throw new Error('trade total is wrong');
  if (elements['today-open-total'].textContent !== '1') throw new Error('open total is wrong');
  if (elements['today-closed-total'].textContent !== '1') throw new Error('closed total is wrong');
  if (elements['today-pnl'].textContent !== '-$25.00') throw new Error('day P&L is wrong');
  const currentCard = elements['today-current-position'].innerHTML;
  if (!currentCard.includes('C-BTC-65000') || !currentCard.includes('>Exit</button>')) {
    throw new Error(`current trade card is incomplete: ${currentCard}`);
  }
  for (const detail of [
    'Live protection', 'TP / SL / TSL Monitor', 'RUNNING',
    'Take profit $', '500', 'Stop loss $', '250',
    'TSL arm P&amp;L $', '125', 'TSL trail $', '100',
    'Minimum locked $', '25', 'EXCHANGE PROTECTED',
  ]) {
    if (!currentCard.includes(detail)) throw new Error(`missing protection detail: ${detail}`);
  }
  for (const removed of ['Close Position', '>Protection</button>', '>Payoff</button>']) {
    if (currentCard.includes(removed)) {
      throw new Error(`current trade card exposed removed control: ${removed}`);
    }
  }
  const todayTable = elements['today-trades-body'].innerHTML;
  for (const detail of [
    'C-BTC-65000', 'P-BTC-64000', 'OPEN', 'CLOSED', '-$50.00',
  ]) {
    if (!todayTable.includes(detail)) throw new Error(`missing Today table detail: ${detail}`);
  }
  if (todayTable.includes(' IST')) throw new Error('Today table still prints IST');
  const decisionCard = elements['today-engine-decision'].innerHTML;
  for (const detail of ['Live preview', 'Committed decision', 'today-score-dial',
                        'today-trade-decision-pill']) {
    if (!decisionCard.includes(detail)) throw new Error(`missing engine dial detail: ${detail}`);
  }
  if (decisionCard.includes('View Trend Engine')) {
    throw new Error('obsolete Trend Engine link is still present');
  }
  if (!decisionCard.includes('This completed LIVE signal was already handled; waiting for the next one')) {
    throw new Error('last automatic action is not sentence-cased');
  }
  await closeTodayLiveTrade(0);
  if (!posted || posted.url !== '/api/square-off?slot=trend' ||
      posted.body?.target_mode !== 'live') {
    throw new Error(`LIVE close was not explicitly routed: ${JSON.stringify(posted)}`);
  }
  for (const removed of ['Open Positions', 'Engine health']) {
    if (todayTable.includes(removed)) throw new Error(`non-daily section leaked: ${removed}`);
  }
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
"""
    result = subprocess.run(
        [NODE, "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
