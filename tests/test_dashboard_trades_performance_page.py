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
    assert '<th>Opened</th><th>Closed</th><th>Contract</th>' in TEMPLATE
    assert "Delta Exchange · trade history" in TEMPLATE
    assert "/api/performance/delta-trades" in TEMPLATE
    assert "closed trades valued" in TEMPLATE
    assert "Total P/L" in TEMPLATE
    assert "Gross P/L" in TEMPLATE
    assert "Total fees &amp; charges" in TEMPLATE
    assert "Average win" in TEMPLATE
    assert "Average loss" in TEMPLATE
    assert "Risk : reward" in TEMPLATE
    assert "Profit factor" in TEMPLATE
    assert "Expectancy / trade" in TEMPLATE
    assert "Maximum drawdown" in TEMPLATE
    assert "performanceMetrics" in TEMPLATE
    assert "performance-metrics" in TEMPLATE
    assert "Daily P/L" in TEMPLATE
    assert "Green = profit" not in TEMPLATE
    assert "Daily net P/L" in TEMPLATE
    assert "Cumulative net P/L" in TEMPLATE
    assert "type: 'bar'" in TEMPLATE
    assert "performance-history-card" in TEMPLATE
    assert 'class="numeric">Lots</th>' in TEMPLATE
    assert 'class="numeric ${pnlCls(trade.net_pnl_usd)}"' in TEMPLATE
    assert "function tableTime(value)" in TEMPLATE
    assert "Opened (IST)" not in TEMPLATE
    assert "Closed (IST)" not in TEMPLATE
    assert "Trade activity" not in TEMPLATE
    assert "Refresh history" in TEMPLATE
    assert "slot-filter-chips" not in TEMPLATE
    assert "tradeMatchesFilter" not in TEMPLATE
    for forbidden in ("Order ID", "Fill ID", "exchange-fill-details", "exchange_details"):
        assert forbidden not in TEMPLATE


@pytest.mark.skipif(NODE is None, reason="Node.js is required for frontend JavaScript tests")
def test_daily_trade_series_aggregates_daily_pnl_for_the_line_chart():
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

"""
    result = subprocess.run([NODE, "-e", script], cwd=ROOT, text=True,
                            capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(NODE is None, reason="Node.js is required for frontend JavaScript tests")
def test_real_trade_performance_metrics_are_fee_aware_and_chronological():
    script = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('templates/trades.html', 'utf8');
const start = source.indexOf('function performanceMetrics');
const end = source.indexOf('function feeMetricText');
if (start < 0 || end <= start) throw new Error('performanceMetrics not found');
vm.runInThisContext(source.slice(start, end));

const trades = [
  {status: 'CLOSED', symbol: 'PE', lots: 10, gross_pnl_usd: 22, net_pnl_usd: 20,
   fees: [{asset: 'USD', amount: 2}], exit_at_utc: '2026-07-01T03:00:00Z'},
  {status: 'OPEN', symbol: 'MV', lots: 5, gross_pnl_usd: null, net_pnl_usd: null,
   fees: [{asset: 'USD', amount: 1}]},
  {status: 'CLOSED', symbol: 'PE', lots: 10, gross_pnl_usd: 110, net_pnl_usd: 100,
   fees: [{asset: 'USD', amount: 10}], exit_at_utc: '2026-07-01T01:00:00Z'},
  {status: 'CLOSED', symbol: 'CE', lots: 10, gross_pnl_usd: -45, net_pnl_usd: -50,
   fees: [{asset: 'USD', amount: 5}], exit_at_utc: '2026-07-01T02:00:00Z'},
];
const m = performanceMetrics(trades);
const close = (actual, expected) => Math.abs(actual - expected) < 1e-9;
if (!close(m.net, 70) || !close(m.gross, 87)) throw new Error(`P/L totals wrong: ${JSON.stringify(m)}`);
if (m.winners !== 2 || m.losers !== 1 || !close(m.winRate, 200 / 3)) throw new Error('win metrics wrong');
if (!close(m.averageWin, 60) || !close(m.averageLoss, -50)) throw new Error('average outcomes wrong');
if (!close(m.rewardRisk, 1.2) || !close(m.profitFactor, 2.4)) throw new Error('ratio metrics wrong');
if (!close(m.expectancy, 70 / 3)) throw new Error('expectancy wrong');
if (!close(m.maxDrawdown, 50)) throw new Error(`chronological drawdown wrong: ${m.maxDrawdown}`);
if (!close(m.fees.get('USD'), 18)) throw new Error('fee total wrong');
if (m.open !== 1 || m.closed !== 3 || m.symbols !== 3 || !close(m.lots, 35)) throw new Error('activity totals wrong');
"""
    result = subprocess.run([NODE, "-e", script], cwd=ROOT, text=True,
                            capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
