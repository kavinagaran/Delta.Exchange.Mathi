"""templates/trades.html — "Performance" page (UI-4): filter chips, the
equity-curve/drawdown chart view toggle, and the honest empty states for
the two fields that have no data source yet (regime breakdown, per-trade
signal linkage — both explicitly "empty pre-cutover" per the plan).
"""
import shutil
import subprocess
from pathlib import Path

import pytest

import dashboard

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
TEMPLATE = (ROOT / "templates" / "trades.html").read_text(encoding="utf-8")


def test_page_is_registered_as_performance():
    assert dashboard._PAGES["trades"] == ("trades.html", "Performance")
    shell = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
    assert "('trades',    '/trades',    'Performance'" in shell


def test_page_has_filter_chips_and_honest_empty_states():
    for chip in ("all", "move", "morning", "evening", "trend"):
        assert f'data-filter="{chip}"' in TEMPLATE
    # Regime breakdown and the signal column both have no data source until
    # trades carry a recorded regime/signal tag (Phase 7/8) -- the page must
    # say so plainly rather than showing a fabricated or silently blank cell.
    assert "Not available yet" in TEMPLATE
    assert "committed engine signal" in TEMPLATE
    assert '<th title="Links this trade to the snapshot/model/risk decision' in TEMPLATE


@pytest.mark.skipif(NODE is None, reason="Node.js is required for frontend JavaScript tests")
def test_move_filter_matches_morning_and_evening_but_not_trend():
    script = r"""
const fs = require('fs');
const vm = require('vm');
vm.runInThisContext(fs.readFileSync('static/js/slots.js', 'utf8'));
const source = fs.readFileSync('templates/trades.html', 'utf8');
const start = source.indexOf('function tradeMatchesFilter');
const end = source.indexOf('function setFilter');
if (start < 0 || end <= start) throw new Error('tradeMatchesFilter not found');
vm.runInThisContext(source.slice(start, end));

const morningMove = { symbol: 'MV-BTC-65800-230726', entry_at_utc: '2026-07-23T01:50:00Z' };
const eveningMove = { symbol: 'MV-BTC-65800-230726', entry_at_utc: '2026-07-23T13:50:00Z' };
const trendCall = { symbol: 'C-BTC-64000-170726' };

if (!tradeMatchesFilter(morningMove, 'move')) throw new Error('morning MOVE did not match the move filter');
if (!tradeMatchesFilter(eveningMove, 'move')) throw new Error('evening MOVE did not match the move filter');
if (tradeMatchesFilter(trendCall, 'move')) throw new Error('a Trend option incorrectly matched the move filter');
if (!tradeMatchesFilter(morningMove, 'morning')) throw new Error('morning MOVE did not match the morning filter');
if (tradeMatchesFilter(eveningMove, 'morning')) throw new Error('evening MOVE incorrectly matched the morning filter');
if (!tradeMatchesFilter(trendCall, 'trend')) throw new Error('a Trend option did not match the trend filter');
if (!tradeMatchesFilter(trendCall, 'all')) throw new Error('all did not match everything');
"""
    result = subprocess.run([NODE, "-e", script], cwd=ROOT, text=True,
                            capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(NODE is None, reason="Node.js is required for frontend JavaScript tests")
def test_daily_pnl_series_computes_a_running_drawdown_from_the_peak():
    script = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('templates/trades.html', 'utf8');
const start = source.indexOf('function dailyPnlSeries');
const end = source.indexOf('function drawChart');
if (start < 0 || end <= start) throw new Error('dailyPnlSeries not found');
vm.runInThisContext(source.slice(start, end));

// Day 1: +10 (peak 10, dd 0). Day 2: -4 -> cum 6 (peak stays 10, dd -4).
// Day 3: +2 -> cum 8 (peak stays 10, dd -2). Day 4: +5 -> cum 13 (new peak, dd 0).
const trades = [
  { date: '2026-07-20', pnl_usd: 10 },
  { date: '2026-07-21', pnl_usd: -4 },
  { date: '2026-07-22', pnl_usd: 2 },
  { date: '2026-07-23', pnl_usd: 5 },
];
const { cumulative, drawdown } = dailyPnlSeries(trades);
const expectedCum = [10, 6, 8, 13];
const expectedDd  = [0, -4, -2, 0];
if (JSON.stringify(cumulative) !== JSON.stringify(expectedCum)) {
  throw new Error(`cumulative mismatch: ${JSON.stringify(cumulative)}`);
}
if (JSON.stringify(drawdown) !== JSON.stringify(expectedDd)) {
  throw new Error(`drawdown mismatch: ${JSON.stringify(drawdown)}`);
}

// A dry-run row must never count toward real P&L.
const withDry = [...trades, { date: '2026-07-24', pnl_usd: 999, dry_run: true }];
const { cumulative: cum2 } = dailyPnlSeries(withDry);
if (cum2[cum2.length - 1] !== 13) {
  throw new Error(`a dry-run row leaked into the real equity curve: ${JSON.stringify(cum2)}`);
}
"""
    result = subprocess.run([NODE, "-e", script], cwd=ROOT, text=True,
                            capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
