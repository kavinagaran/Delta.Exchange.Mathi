"""templates/trades.html — complete, read-only Delta trade history UI."""
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


def test_page_has_complete_exchange_history_disclosure_without_filters():
    assert "Delta Exchange · trade history" in TEMPLATE
    assert "/api/performance/delta-trades" in TEMPLATE
    assert "closed trades valued" in TEMPLATE
    assert "Est. net P/L" in TEMPLATE
    assert "Gross P/L" in TEMPLATE
    assert "Daily P/L" in TEMPLATE
    assert "Green = profit" not in TEMPLATE
    assert "pnlBarColors" in TEMPLATE
    assert "performance-history-card" in TEMPLATE
    assert 'class="numeric">Lots</th>' in TEMPLATE
    assert 'class="numeric ${pnlCls(trade.net_pnl_usd)}"' in TEMPLATE
    assert "Trade activity" not in TEMPLATE
    assert "Refresh history" in TEMPLATE
    assert "slot-filter-chips" not in TEMPLATE
    assert "tradeMatchesFilter" not in TEMPLATE
    for forbidden in ("Order ID", "Fill ID", "exchange-fill-details", "exchange_details"):
        assert forbidden not in TEMPLATE


@pytest.mark.skipif(NODE is None, reason="Node.js is required for frontend JavaScript tests")
def test_daily_trade_series_and_bar_density_reflect_daily_pnl():
    script = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('templates/trades.html', 'utf8');
const start = source.indexOf('function dailyTradeSeries');
const end = source.indexOf('function drawChart');
if (start < 0 || end <= start) throw new Error('dailyTradeSeries not found');
vm.runInThisContext(source.slice(start, end));

const trades = [
  { date: '2026-07-20', lots: 10 },
  { date: '2026-07-20', lots: 4 },
  { date: '2026-07-21', lots: 2 },
  { date: '2026-07-22', lots: 7 },
];
const { labels, trades: counts, lots } = dailyTradeSeries(trades);
if (JSON.stringify(labels) !== JSON.stringify(['2026-07-20', '2026-07-21', '2026-07-22'])) {
  throw new Error(`labels mismatch: ${JSON.stringify(labels)}`);
}
if (JSON.stringify(counts) !== JSON.stringify([2, 1, 1])) {
  throw new Error(`trade count mismatch: ${JSON.stringify(counts)}`);
}
if (JSON.stringify(lots) !== JSON.stringify([14, 2, 7])) {
  throw new Error(`lot totals mismatch: ${JSON.stringify(lots)}`);
}

const colorStart = source.indexOf('function pnlBarColors');
const colorEnd = source.indexOf('function drawChart');
if (colorStart < 0 || colorEnd <= colorStart) throw new Error('pnlBarColors not found');
vm.runInThisContext(source.slice(colorStart, colorEnd));
const colors = pnlBarColors([10, -100, 50, null]);
if (!colors[0].startsWith('hsla(155,')) throw new Error(`profit is not green: ${colors[0]}`);
if (!colors[1].startsWith('hsla(352,')) throw new Error(`loss is not red: ${colors[1]}`);
if (!(parseFloat(colors[1].match(/, ([.\d]+)\)$/)[1]) > parseFloat(colors[0].match(/, ([.\d]+)\)$/)[1]))) {
  throw new Error(`larger magnitude should have denser colour: ${JSON.stringify(colors)}`);
}
if (colors[3] !== 'rgba(140, 160, 180, .32)') throw new Error('unvalued P/L should remain neutral');
"""
    result = subprocess.run([NODE, "-e", script], cwd=ROOT, text=True,
                            capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
