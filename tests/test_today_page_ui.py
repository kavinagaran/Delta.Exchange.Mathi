import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def test_today_page_contains_only_same_day_trade_content():
    source = (ROOT / "templates" / "overview.html").read_text(encoding="utf-8")

    assert '<th>Opened</th><th>Closed</th><th>Contract</th>' in source

    for required in (
        'id="today-summary"',
        'id="today-trade-total"',
        'id="today-open-total"',
        'id="today-closed-total"',
        'id="today-pnl"',
        "Today's Trades",
        'id="today-trades-body"',
        'id="today-table-count"',
        'class="today-trades-table"',
        "jget('/api/today-trades')",
        "function updateTodaySummary(rows)",
        "function todayTableTimestamp(trade, phase = 'entry')",
        "function renderTodayTrades(rows)",
        "function todayInlineProtectionHtml(protection)",
        "jget('/api/tp-monitor')",
        "jget('/api/engine/snapshot')",
        "jget('/api/engine/live')",
        "function todayEngineScoreDials(snapshot, liveView, status)",
        "function todayTradeDecisionPill(status, snapshot)",
        "WAIT — MOVE PREMIUM BELOW $300",
        "entry_blocked_reason",
        "Live preview",
        "Committed decision",
        'aria-label="Live TP, SL and TSL monitor"',
        '>Exit</button>',
        "function closeTodayLiveTrade(index)",
        "'/api/square-off?slot='",
        "target_mode: simulated ? 'dry_run' : 'live'",
        'aria-label="Today trading dashboard"',
        'data-metric="pnl"',
        'class="today-score-dial"',
        "--today-score-fill",
        'Gross P/L',
        'Net P/L',
        'cockpit-card',
        'id="cockpit-buy-ce"',
        'id="cockpit-buy-pe"',
        'id="cockpit-buy-move"',
        'id="cockpit-sell-move"',
        "function cockpitEnter(action)",
        "'/api/cockpit/preview'",
        "'/api/cockpit/enter'",
        'id="cockpit-setup-lock-reset"',
        "function cockpitResetZoneLock()",
        "'/api/trend-engine/score-auto/setup-lock/reset'",
        'id="cockpit-bot-toggle"',
        "function cockpitToggleBot(checked)",
        "TREND_ENGINE_SCORE_AUTO_MODE: checked ? 'live' : 'disabled'",
        "function updateCockpitState(hasOpenPosition, engineStatus)",
    ):
        assert required in source
    assert 'Math.abs(score) * 3.6' in source

    for removed in (
        "Welcome Back",
        "Your Trades, at a glance!",
        "Trading mode",
        "Engine health",
        "Next scheduled action",
        'id="stats"',
        "Open Positions",
        'id="positions-body"',
        "loadStatusTiles",
        "loadStatsInto",
        "loadSlots",
        'id="today-body"',
        "Latest Trade",
        'id="today-latest-trade"',
        ">Close Position</button>",
        ">Protection</button>",
        ">Payoff</button>",
        "openProtectionDrawer",
        "showPayoff",
        "squareOff",
        "/api/engine/health",
        "View Trend Engine",
        # Retired with the Cockpit redesign: the automation text block that
        # used to sit under the decision pill (score dials + pill remain).
        'class="dry-engine-metrics"',
        "Current automatic action",
        "Selected / open contract",
        "Signal bar closed",
        "Last controller cycle",
        # The old retired discretionary manual-entry UI must never come back
        # under a different name -- Cockpit is a distinct, newly-built path.
        "manualEntry(",
        "manualBtns(",
    ):
        assert removed not in source


def test_today_page_uses_compact_responsive_terminal_layout():
    styles = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")

    for required in (
        "body:has(.overview-page) .main",
        ".today-layout",
        ".today-right-col",
        ".today-current-trade-body",
        ".lever-switch",
        ".overview-page .today-summary .performance-stat",
        ".overview-page .today-summary .today-stat-icon",
        ".overview-page .today-summary .today-stat-icon svg",
        ".today-ledger-empty td > div",
        "#today-current-position > .score-zone-empty",
        ".today-inline-protection",
        ".today-protection-grid",
        ".today-protection-metric.tone-sl",
        ".today-protection-metric.tone-tsl-arm",
        ".today-protection-metric.tone-tsl-trail",
        ".today-engine-score-dials",
        ".today-score-dial",
        ".today-score-dial::before",
        ".today-score-dial-core > strong",
        "text-align: center",
        "@media (max-width: 920px)",
        "@media (max-width: 560px)",
        "prefers-reduced-motion",
    ):
        assert required in styles
    score_styles = styles.split('.today-score-dial-core > strong {', 1)[1].split('}', 1)[0]
    assert 'color: var(--today-score-tone)' in score_styles
    assert 'font-variant-numeric: tabular-nums' in score_styles
    # The score uses the app's normal UI font, not a monospace stack --
    # tabular-nums (a font-feature, not a family) is what keeps digit
    # widths steady.
    assert 'monospace' not in score_styles
    assert '-apple-system' in score_styles
    assert '.today-trade-decision-pill' in styles
    core_styles = styles.split('.today-score-dial-core {', 1)[1].split('}', 1)[0]
    assert 'background: #081a2e' in core_styles
    assert '.today-score-needle' not in styles
    assert '.today-score-limit' not in styles


def test_today_protection_footer_is_bold_colored_and_hides_local_fallback():
    source = (ROOT / "templates" / "overview.html").read_text(encoding="utf-8")
    styles = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")

    assert "rawCoverage === 'local_fallback' ? ''" in source
    assert 'class="today-telemetry-time"' in source
    assert "today-telemetry-positive" in source
    assert "today-telemetry-negative" in source
    assert "today-telemetry-warn" in source
    assert ".today-inline-protection .dry-protection-telemetry" in styles
    assert ".today-inline-protection > p" in styles


def test_today_summary_cards_use_meaningful_svg_icons_not_empty_boxes():
    source = (ROOT / "templates" / "overview.html").read_text(encoding="utf-8")
    styles = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")
    summary = source.split('id="today-summary"', 1)[1].split(
        'class="card overview-today-card today-trades-card"', 1)[0]

    assert summary.count('class="today-stat-icon"') == 4
    assert summary.count('<svg viewBox="0 0 24 24"') == 4
    assert summary.count('aria-hidden="true"') == 4
    assert 'stroke: currentColor' in styles
    assert '.today-summary .performance-stat::before' not in styles
    assert '.performance-stat[data-metric="open"]::after' not in styles


def test_topbar_btc_pill_uses_theme_gradient_and_live_pnl_tones():
    styles = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")
    script = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")

    btc_rule = styles[styles.index("#tb-btc {"):styles.index("#tb-btc b")]
    assert "linear-gradient" in btc_rule
    assert "var(--accent)" in btc_rule
    assert "var(--accent-dark)" in btc_rule
    assert ".pill.live-profit" in styles
    assert ".pill.live-loss" in styles
    assert "'live-profit'" in script
    assert "'live-loss'" in script


def test_today_score_dial_css_starts_at_12_oclock():
    styles = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")
    dial_rule = styles[
        styles.index(".today-score-dial::before"):
        styles.index(".today-score-dial::after")
    ]
    assert "from 0deg" in dial_rule
    assert "from -90deg" not in styles
    assert (
        "var(--today-score-tone) var(--today-score-fill-start) "
        "var(--today-score-fill-end)"
    ) in dial_rule


@pytest.mark.skipif(NODE is None, reason="Node.js is required for frontend tests")
def test_today_score_dial_starts_at_12_oclock_and_shades_by_magnitude():
    """A positive score must sweep clockwise from 12 o'clock (0deg); a
    negative score must sweep anti-clockwise from 12 o'clock instead, by
    the same angular distance for the same magnitude. Colour shading
    strength (how far the tone sits from the neutral baseline) must scale
    with |score|, independent of direction."""
    script = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('templates/overview.html', 'utf8');
const start = source.indexOf('function todayScoreColor(value, quality) {');
const end = source.indexOf('function todayEngineScoreDials(');
if (start < 0 || end <= start) throw new Error('score dial helpers not found');
vm.runInThisContext(source.slice(start, end) + `
  const pos = todayScoreFillAngles(40);
  if (pos.start !== 0) throw new Error('positive fill must start at 0deg: ' + JSON.stringify(pos));
  if (Math.abs(pos.end - 144) > 0.001) throw new Error('positive fill end is wrong: ' + JSON.stringify(pos));

  const neg = todayScoreFillAngles(-40);
  if (neg.end !== 360) throw new Error('negative fill must end at 360deg (12 oclock position): ' + JSON.stringify(neg));
  if (Math.abs(neg.start - (360 - 144)) > 0.001) throw new Error('negative fill start is wrong: ' + JSON.stringify(neg));

  const posSweep = pos.end - pos.start;
  const negSweep = neg.end - neg.start;
  if (Math.abs(posSweep - negSweep) > 0.001) {
    throw new Error('equal-magnitude scores must sweep equal angular distance: ' + posSweep + ' vs ' + negSweep);
  }

  const zero = todayScoreFillAngles(0);
  if (zero.start !== zero.end) throw new Error('zero score must have no fill: ' + JSON.stringify(zero));

  function parseRgb(css) {
    const m = css.match(/rgb\\((\\d+), (\\d+), (\\d+)\\)/);
    if (!m) throw new Error('todayScoreColor did not return an rgb() triplet: ' + css);
    return [Number(m[1]), Number(m[2]), Number(m[3])];
  }
  function dist(a, b) {
    return Math.sqrt(a.reduce((sum, v, i) => sum + (v - b[i]) ** 2, 0));
  }
  const neutral = [143, 163, 188];
  const weakPositive = parseRgb(todayScoreColor(5, 'OK'));
  const strongPositive = parseRgb(todayScoreColor(95, 'OK'));
  if (dist(weakPositive, neutral) >= dist(strongPositive, neutral)) {
    throw new Error('a weak positive score must shade closer to neutral than a strong one');
  }
  const weakNegative = parseRgb(todayScoreColor(-5, 'OK'));
  const strongNegative = parseRgb(todayScoreColor(-95, 'OK'));
  if (dist(weakNegative, neutral) >= dist(strongNegative, neutral)) {
    throw new Error('a weak negative score must shade closer to neutral than a strong one');
  }
  const zeroColor = parseRgb(todayScoreColor(0, 'OK'));
  if (dist(zeroColor, neutral) > 0.5) {
    throw new Error('a zero score must render at the neutral baseline: ' + zeroColor);
  }
  if (parseRgb(todayScoreColor(60, 'OK')).join(',') === parseRgb(todayScoreColor(-60, 'OK')).join(',')) {
    throw new Error('positive and negative scores of equal magnitude must still differ in hue');
  }
`);
"""
    result = subprocess.run(
        [NODE, "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
