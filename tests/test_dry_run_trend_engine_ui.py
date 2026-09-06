import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def test_dry_run_ui_has_one_score_zone_workspace_and_no_legacy_trade_frames():
    template = (ROOT / "templates" / "dry_run.html").read_text(encoding="utf-8")

    assert "Dry Run trade" in template
    assert "Score-zone paper trade" not in template
    assert "Current position" in template
    assert "Latest engine decision" in template
    assert "One automated dry-run position per user" in template
    for obsolete in (
        "Morning MOVE",
        "Evening MOVE",
        "Trend-based position (CE / PE)",
        "dry-slot-morning",
        "dry-slot-evening",
        "dry-slot-trend",
        "display_slots",
        "dry-run-preview",
        "dry-run-entry",
        "startDryTrendEngineSimulation",
        "Combined trade score",
        "Automatic MOVE Forecast",
    ):
        assert obsolete not in template


@pytest.mark.skipif(NODE is None, reason="Node.js is required for frontend JavaScript tests")
def test_score_zone_position_keeps_trade_time_first_and_uses_the_trend_owner():
    script = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('templates/dry_run.html', 'utf8');
const start = source.indexOf('function dryEntryUtcClock');
const end = source.indexOf('function dryEngineDecisionLabel');
if (start < 0 || end <= start) throw new Error('score-zone card functions not found');

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

const state = {
  status: 'OPEN',
  entry_time_utc: '01:50:00',
  trend_score_zone: 'SHORT_MOVE',
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
const html = dryPositionDetails(state);
const tradeTime = html.indexOf('<dt>Time of trade</dt>');
    const tradeType = html.indexOf('<dt>Trade type</dt>');
    const contract = html.indexOf('<dt>Contract</dt>');
if (tradeTime < 0 || tradeType < 0 || contract < 0 || tradeTime > tradeType || tradeType > contract) {
  throw new Error(`dry-run trade fields are not ordered correctly: ${html}`);
}
if (!html.includes('7:20 AM IST')) {
  throw new Error(`actual IST trade time is missing: ${html}`);
}
if (html.includes('<dt>Started</dt>')) {
  throw new Error('obsolete Started row remains');
}
if (html.includes('<dt>Score zone</dt>')) {
  throw new Error('obsolete Score zone row remains');
}
if (!html.includes("endDrySimulation('trend')")) {
  throw new Error(`Exit no longer targets the score-zone owner: ${html}`);
}
if (!html.includes("saveDryProtection('trend', 'trend')")) {
  throw new Error(`Protection save no longer targets the score-zone owner: ${html}`);
}
if (html.includes('Morning') || html.includes('Evening')) {
  throw new Error(`time-bucket copy remains in score-zone position: ${html}`);
}

const closed = dryPositionDetails({
  ...state,
  status: 'CLOSED',
  pnl_usd: 10,
  exit_mark: 400,
  exit_time_utc: '02:50:00',
});
if (closed.indexOf('<dt>Time of trade</dt>') > closed.indexOf('<dt>Contract</dt>')) {
  throw new Error(`closed trade time is not first: ${closed}`);
}
"""
    result = subprocess.run(
        [NODE, "-e", script], cwd=ROOT, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr

@pytest.mark.skipif(NODE is None, reason="Node.js is required for frontend JavaScript tests")
def test_score_zone_decision_uses_the_configured_zone_bands_and_confirmation_copy():
    script = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('templates/dry_run.html', 'utf8');
const start = source.indexOf('function dryScoreZoneLabel');
const end = source.indexOf('function renderDryMode');
if (start < 0 || end <= start) throw new Error('Trend Engine panel functions not found');

global.dryModeActive = true;
global.dryKnownNumber = value => value !== null && value !== undefined &&
  value !== '' && Number.isFinite(Number(value));
global.fN = (value, digits = 2) => Number(value).toFixed(digits);
global.esc = value => String(value ?? '').replace(/&/g, '&amp;')
  .replace(/</g, '&lt;').replace(/>/g, '&gt;');
vm.runInThisContext(source.slice(start, end));

if (dryTrendScoreTarget({engine_zone: 'CE_2_ITM'}) !== 'Buy 2-step ITM Call') {
  throw new Error('CE zone mapping changed');
}
if (dryTrendScoreTarget({engine_zone: 'PE_2_ITM'}) !== 'Buy 2-step ITM Put') {
  throw new Error('PE zone mapping changed');
}
if (dryTrendScoreTarget({engine_zone: 'HOLD'}) !== 'Hold the open position — no new entry') {
  throw new Error('HOLD zone mapping changed');
}
if (dryTrendScoreTarget({engine_zone: 'SHORT_MOVE'}) !== 'Sell ATM MOVE straddle') {
  throw new Error('SHORT_MOVE action copy changed');
}
global.dryTrendScoreAutoStatus = {
  enabled: true, mode: 'dry_run', status: 'active',
  direction_score: 0, market_regime: 'RANGE', engine_zone: 'SHORT_MOVE',
  lots: 1000, signal_bar_close_utc: '2026-07-26T10:00:00Z',
  last_cycle_utc: '2026-07-26T10:00:01Z',
};
global.dryTrendScoreAutoReachable = true;
const html = dryTrendScoreAutoHtml();
if (!html.includes('Sell ATM MOVE straddle') ||
    !html.includes('One user-owned dry-run position may be open at a time.') ||
    !html.includes('<dt>Trade type</dt><dd>MV</dd>')) {
  throw new Error(`dry-run trade decision copy is incomplete: ${html}`);
}
"""
    result = subprocess.run(
        [NODE, "-e", script], cwd=ROOT, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
