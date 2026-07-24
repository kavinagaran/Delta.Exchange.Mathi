import json
import shutil
import subprocess
import time
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


def test_real_overview_has_only_current_trend_engine_position_copy():
    source = (ROOT / "templates" / "overview.html").read_text(encoding="utf-8")

    assert "Trend Engine positions" in source
    assert "Morning MOVE" in source
    assert "Evening MOVE" in source
    assert "Trend-based position (CE / PE)" in source
    assert "st.display_slots ||" in source
    for obsolete in (
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
    ):
        assert obsolete not in source


@pytest.mark.skipif(NODE is None, reason="Node.js is required for frontend JavaScript tests")
def test_real_cards_route_actions_to_source_slot_and_show_trade_time_first():
    script = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('templates/overview.html', 'utf8');
const start = source.indexOf('function liveMoveDisplaySlotFromUtc');
const end = source.indexOf('function renderExternalOptions');
if (start < 0 || end <= start) throw new Error('REAL card functions not found');

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

if (liveMoveDisplaySlotFromUtc('05:29:59') !== 'morning') {
  throw new Error('10:59:59 AM IST did not route to Morning');
}
if (liveMoveDisplaySlotFromUtc('05:30:00') !== 'evening') {
  throw new Error('11:00 AM IST did not route to Evening');
}

const state = {
  status: 'OPEN',
  source_slot: 'trend',
  control_slot: 'trend',
  display_slot: 'morning',
  entry_time_utc: '12:00:00',
  entry_at_utc: '2026-07-23T01:50:00Z',
  symbol: 'MV-BTC-65800-230726',
  side: 'short',
  lots: 1000,
  entry_mark: 445,
  current_mark: 414,
  live_pnl: 17.45,
  total_cost_usd: 445,
};
const protection = {
  trend: {
    running: true,
    protection_established: true,
    target_pnl: 500,
    sl_pnl: 300,
    tsl_arm_pnl: 125,
    tsl_trail_pnl: 125,
    tsl_lock_min_pnl: 0,
    poll_secs: 30,
  },
};
const html = slotHtml(state, 'morning', protection);
const tradeTime = html.indexOf('<dt>Time of trade</dt>');
const contract = html.indexOf('<dt>Contract</dt>');
if (tradeTime < 0 || contract < 0 || tradeTime > contract) {
  throw new Error(`trade time is not the first detail row: ${html}`);
}
if (!html.includes('7:20 AM IST')) {
  throw new Error(`actual IST trade time is missing: ${html}`);
}
if (html.includes('<dt>Entered</dt>')) {
  throw new Error('obsolete Entered row remains');
}
for (const required of [
  "squareOff('trend', 'morning', 'live')",
  "saveTp('morning', 'trend')",
  "toggleTp('trend', true)",
  "showPayoff('morning')",
]) {
  if (!html.includes(required)) {
    throw new Error(`source-aware action is missing (${required}): ${html}`);
  }
}
if (html.includes('Automatic MOVE Forecast')) {
  throw new Error(`routed score position inherited scheduled copy: ${html}`);
}

const closed = slotHtml({
  ...state,
  status: 'CLOSED',
  pnl_usd: 10,
  exit_mark: 400,
  exit_time_utc: '02:50:00',
}, 'morning', protection);
if (closed.indexOf('<dt>Time of trade</dt>') > closed.indexOf('<dt>Contract</dt>')) {
  throw new Error(`closed trade time is not first: ${closed}`);
}
if (closed.includes('TP / SL / TSL Monitor')) {
  throw new Error(`closed trade retained inactive protection controls: ${closed}`);
}

const pending = slotHtml({
  ...state,
  status: 'ENTRY_PENDING',
}, 'morning', protection);
if (!pending.includes('ENTRY PENDING') || pending.includes('Closed') ||
    pending.includes('NaN')) {
  throw new Error(`pending entry was rendered as a closed trade: ${pending}`);
}
"""
    result = subprocess.run(
        [NODE, "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
