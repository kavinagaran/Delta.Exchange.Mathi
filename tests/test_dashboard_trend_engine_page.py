"""The current Trend Engine page reads committed and display-only live scores.

Static checks keep the page read-only, understandable, and independent of the
retired legacy surface.
"""
from pathlib import Path
import shutil
import subprocess

import dashboard
import pytest

TEMPLATE = (Path(dashboard.BASE) / "templates" / "trend_engine.html").read_text(
    encoding="utf-8")
STYLE = (Path(dashboard.BASE) / "static" / "css" / "app.css").read_text(
    encoding="utf-8")
NODE = shutil.which("node")


def test_trend_engine_page_is_registered_and_reads_only():
    assert dashboard._PAGES["trend-engine"] == ("trend_engine.html", "Trend Engine")
    shell = (Path(dashboard.BASE) / "templates" / "base.html").read_text(
        encoding="utf-8")
    assert "('trend-engine', '/trend-engine', 'Trend Engine'" in shell

    assert "BTC TREND ENGINE" in TEMPLATE
    assert "BTC_TREND_ENGINE · TRANSPARENT SCORE" not in TEMPLATE
    assert "Read-only signal" not in TEMPLATE
    assert "te-readonly-notice" not in TEMPLATE
    assert "/trend-engine-legacy" not in TEMPLATE
    assert "jget('/api/engine/snapshot')" in TEMPLATE
    assert "jget('/api/engine/live')" in TEMPLATE
    assert "jget('/api/engine/decision-history')" in TEMPLATE
    assert "jget('/api/engine/status')" in TEMPLATE
    assert "Live preview" in TEMPLATE
    assert "Committed decision" in TEMPLATE
    assert "Legacy vs. engine agreement" not in TEMPLATE
    assert "renderShadowSummary" not in TEMPLATE
    # No write path of any kind on this page.
    assert "jpost(" not in TEMPLATE
    assert "method: 'POST'" not in TEMPLATE
    assert "method: 'DELETE'" not in TEMPLATE
    assert "/api/trend-entry" not in TEMPLATE
    assert "/api/trend-engine'" not in TEMPLATE  # the legacy endpoint, not this page's


def test_score_dials_are_segmented_circular_gauges_with_needles():
    assert TEMPLATE.count('class="te-score-needle"') == 2
    assert TEMPLATE.count('class="te-score-limit is-min"') == 2
    assert TEMPLATE.count('class="te-score-limit is-max"') == 2
    assert "--te-needle-angle" in TEMPLATE
    assert "score * 1.35" in TEMPLATE
    for required in (
        ".te-score-gauge::before",
        "repeating-conic-gradient",
        "conic-gradient(from 225deg",
        ".te-score-needle",
        ".te-score-limit.is-min",
        ".te-score-limit.is-max",
        "aspect-ratio: 1",
        "border-radius: 50%",
    ):
        assert required in STYLE


def test_committed_score_chart_is_a_zone_colored_line_with_every_boundary():
    assert 'id="te-preview-score-chart"' in TEMPLATE
    assert "renderCommittedDecisionChart(decisionHistory)" in TEMPLATE
    assert "drawCommittedDecisionChart" in TEMPLATE
    assert "COMMITTED_SCORE_ZONE_LEVELS" in TEMPLATE
    assert "COMMITTED_ZONE_COLOURS" in TEMPLATE
    assert "normaliseCommittedZone" in TEMPLATE
    assert "const colour = COMMITTED_ZONE_COLOURS[decision.zone]" in TEMPLATE
    assert "ctx.lineTo(decisionX(index), scoreY(decision.score))" in TEMPLATE
    for label in ("BUY CE >+40", "SHORT MOVE +30", "SHORT MOVE −30", "BUY PE <−40"):
        assert label in TEMPLATE
    # Zone separators are deliberately fine; the axes stay strong neutral grey.
    assert "ctx.setLineDash([1, 6])" in TEMPLATE
    assert "ctx.lineWidth = .6" in TEMPLATE
    assert "ctx.strokeStyle = 'rgba(168, 178, 190, .94)'" in TEMPLATE
    assert "ctx.lineWidth = 2" in TEMPLATE
    # 75% of the previous 3x chart height (834px) is 626px.
    assert "height: 626px" in STYLE
    assert "COMMITTED DECISION SCORE" in TEMPLATE
    assert ".te-preview-chart-stage canvas" in STYLE


def test_committed_score_chart_supports_axis_drag_scaling_and_zone_label_lane():
    assert "Move the pointer for score/time crosshairs." in TEMPLATE
    assert "24H · 5M" in TEMPLATE
    assert "previewScoreChartStates" in TEMPLATE
    assert "installPreviewScoreChartInteractions" in TEMPLATE
    assert "canvas.addEventListener('pointerdown'" in TEMPLATE
    assert "canvas.addEventListener('pointermove'" in TEMPLATE
    assert "canvas.addEventListener('wheel'" in TEMPLATE
    assert "canvas.addEventListener('dblclick'" in TEMPLATE
    assert "state.visibleBars" in TEMPLATE
    assert "state.rightOffset" in TEMPLATE
    assert "state.yZoom" in TEMPLATE
    assert "state.yPan" in TEMPLATE
    assert "axis === 'plot'" in TEMPLATE
    assert "Math.min(288, decisions.length)" in TEMPLATE
    assert "const visibleLow" in TEMPLATE
    assert "const visibleHigh" in TEMPLATE
    assert "nicePreviewScoreStep" in TEMPLATE
    assert "position < labelCount" in TEMPLATE
    assert "position * (visibleDecisions.length - 1) / (labelCount - 1)" in TEMPLATE
    assert "const labelStep" not in TEMPLATE
    # Candles render only through plot.right; zone labels begin after it.
    assert "const zoneLabelLane" in TEMPLATE
    assert "const labelX = plot.right + 6" in TEMPLATE
    assert "ctx.rect(plot.left, plot.top, plotWidth, plotHeight)" in TEMPLATE
    assert "touch-action: none" in STYLE


def test_committed_score_chart_is_bounded_and_has_axis_crosshairs():
    assert "function boundedPreviewScore" in TEMPLATE
    assert "function previewScoreViewport" in TEMPLATE
    assert "-100 + scoreSpan / 2" in TEMPLATE
    assert "100 - scoreSpan / 2" in TEMPLATE
    assert "function drawPreviewChartCrosshair" in TEMPLATE
    assert "state.hover" in TEMPLATE
    assert "canvas.addEventListener('pointerleave'" in TEMPLATE
    assert "ctx.lineWidth = .65" in TEMPLATE
    assert "score.toFixed(1)" in TEMPLATE
    assert "formatPreviewChartTime(" in TEMPLATE


@pytest.mark.skipif(NODE is None, reason="Node.js is required for frontend tests")
def test_score_viewport_never_leaves_minus_100_to_plus_100():
    script = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('templates/trend_engine.html', 'utf8');
const start = source.indexOf('function clampPreviewChart');
const end = source.indexOf('function drawPreviewChartCrosshair', start);
if (start < 0 || end <= start) throw new Error('viewport helpers not found');
vm.runInThisContext(source.slice(start, end));

const cases = [
  [[-500, 500], 1, 0],
  [[92, 97], 8, 200],
  [[-97, -92], .35, -200],
  [[12, 14, 15], 1, 0],
];
for (const [scores, zoom, pan] of cases) {
  const view = previewScoreViewport(scores, zoom, pan);
  if (view.scoreMinimum < -100 - 1e-9 || view.scoreMaximum > 100 + 1e-9) {
    throw new Error('viewport escaped score bounds: ' + JSON.stringify(view));
  }
  if (!(view.scoreMinimum < view.scoreMaximum)) {
    throw new Error('viewport collapsed: ' + JSON.stringify(view));
  }
}
if (boundedPreviewScore(-500) !== -100 || boundedPreviewScore(500) !== 100) {
  throw new Error('historical outliers were not bounded');
}
const fitted = previewScoreViewport([12, 14, 15], 1, 0);
if (fitted.scoreMinimum <= -100 || fitted.scoreMaximum >= 100) {
  throw new Error('ordinary data did not use a tight min/max range');
}
"""
    result = subprocess.run(
        [NODE, "-e", script],
        cwd=Path(dashboard.BASE),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_committed_line_marks_trade_lifecycle_with_large_signed_dots():
    assert "history?.trade_markers" in TEMPLATE
    assert "drawCommittedTradeMarker" in TEMPLATE
    for code in ("CE", "PE", "MV"):
        assert f"{code}:" in TEMPLATE
    assert "CE: { fill: '#20d991'" in TEMPLATE
    assert "PE: { fill: '#ff4f72'" in TEMPLATE
    assert "MV: { fill: '#a9b3bf'" in TEMPLATE
    assert "const radius = 12" in TEMPLATE
    assert "ctx.arc(markerX, markerY, radius" in TEMPLATE
    assert "action === 'EXIT' ? '×' : '+'" in TEMPLATE
    assert "raw?.action" in TEMPLATE
    assert "timeMillis" in TEMPLATE
    assert "nearestVisibleDecision(marker.timeMillis)" in TEMPLATE
    assert "markerStacks.get(match.index)" in TEMPLATE
    assert "order_id" not in TEMPLATE
    assert "fill_id" not in TEMPLATE


@pytest.mark.skipif(NODE is None, reason="Node.js is required for frontend tests")
def test_trend_engine_script_is_valid_javascript():
    script = r"""
const fs = require('fs');
const source = fs.readFileSync('templates/trend_engine.html', 'utf8');
const start = source.indexOf('<script>') + '<script>'.length;
const end = source.lastIndexOf('</script>');
if (start < '<script>'.length || end <= start) {
  throw new Error('script block not found');
}
new Function(source.slice(start, end));
"""
    result = subprocess.run(
        [NODE, "-e", script],
        cwd=Path(dashboard.BASE),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_committed_score_chart_normalises_decision_points_not_preview_ohlc():
    assert "history?.decisions" in TEMPLATE
    assert "raw?.committed_score" in TEMPLATE
    assert "const decisionsByTime = new Map()" in TEMPLATE
    assert "zone: normaliseCommittedZone(raw?.zone, score)" in TEMPLATE
    assert "const isDoji" not in TEMPLATE
    assert "candlesByStart" not in TEMPLATE


@pytest.mark.skipif(NODE is None, reason="Node.js is required for frontend tests")
def test_committed_zone_normalisation_prefers_the_engine_zone():
    script = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('templates/trend_engine.html', 'utf8');
const start = source.indexOf('function normaliseCommittedZone');
const end = source.indexOf('function formatPreviewChartTime', start);
if (start < 0 || end <= start) throw new Error('zone normaliser not found');
vm.runInThisContext(source.slice(start, end));

const cases = [
  ['CE_2_ITM', 0, 'CE'],
  ['PE_2_ITM', 0, 'PE'],
  ['SHORT_MOVE', -45, 'MV'],
  ['HOLD', 90, 'HOLD'],
  ['', 40.1, 'CE'],
  ['', 30, 'MV'],
  ['', -30, 'MV'],
  ['', -40.1, 'PE'],
];
for (const [zone, score, expected] of cases) {
  const actual = normaliseCommittedZone(zone, score);
  if (actual !== expected) {
    throw new Error(`${zone || '(score fallback)'} ${score}: ${actual} != ${expected}`);
  }
}
"""
    result = subprocess.run(
        [NODE, "-e", script],
        cwd=Path(dashboard.BASE),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_live_and_committed_scores_have_separate_colored_circles():
    assert 'id="te-live-score-gauge"' in TEMPLATE
    assert 'id="te-committed-score-gauge"' in TEMPLATE
    assert 'id="te-live-score"' in TEMPLATE
    assert 'id="te-committed-score"' in TEMPLATE
    assert "updateScoreGauge(" in TEMPLATE
    assert "scoreColor(" in TEMPLATE
    assert "--te-score-tone" in TEMPLATE
    assert "width: 170px" in STYLE


def test_trade_decisions_are_colored_capsules_below_the_circles():
    assert 'id="te-live-decision"' in TEMPLATE
    assert 'id="te-committed-decision"' in TEMPLATE
    assert "tradeDecisionMeta(" in TEMPLATE
    assert "renderTradeDecision(" in TEMPLATE
    assert "BUY 2-STEP ITM CE" in TEMPLATE
    assert "BUY 2-STEP ITM PE" in TEMPLATE
    assert "SELL ATM MOVE" in TEMPLATE
    assert "HOLD — NO NEW TRADE" in TEMPLATE
    assert "WAIT — 5M ADX NOT CALM" in TEMPLATE
    assert "WAIT — 30-MIN CONFIRMATION" not in TEMPLATE
    assert "six completed five-minute candles" not in TEMPLATE
    assert "g.label || formatCode(g.name)" in TEMPLATE
    assert "BEARISH / PE −100" not in TEMPLATE
    assert "BULLISH / CE +100" not in TEMPLATE
    for tone in ("is-ce", "is-pe", "is-move", "is-hold", "is-blocked"):
        assert f".te-decision-capsule.{tone}" in STYLE


def test_time_and_data_quality_have_clear_display_paths():
    assert "Asia/Kolkata" in TEMPLATE
    assert "formatIstTimestamp(snapshot.timestamp)" in TEMPLATE
    assert "formatIstTimestamp(snapshot.candle_close_utc)" in TEMPLATE
    assert "Candle close (IST)" in TEMPLATE
    assert "DATA_QUALITY_COPY" in TEMPLATE
    assert "Clock sync needed" not in TEMPLATE
    assert "Exchange time mismatch" in TEMPLATE
    assert "Building history" in TEMPLATE
    assert "Live price and order-book data passed all freshness checks." in TEMPLATE


@pytest.mark.skipif(NODE is None, reason="Node.js is required for frontend tests")
def test_ist_formatter_and_quality_copy_execute_with_exact_user_facing_values():
    script = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('templates/trend_engine.html', 'utf8');
const start = source.indexOf('const hasValue');
const end = source.indexOf('function freshnessState', start);
if (start < 0 || end <= start) throw new Error('display helpers not found');
vm.runInThisContext(source.slice(start, end) + `
  const stamp = formatIstTimestamp('2026-07-26T06:54:56Z');
  if (stamp !== '2026-07-26:12:24 PM IST') {
    throw new Error('unexpected IST timestamp: ' + stamp);
  }
  const clock = dataQualityCopy('CLOCK_DRIFT');
  if (clock.label !== 'Exchange time mismatch' || !clock.detail.includes('five seconds')) {
    throw new Error('clock-drift copy is not understandable');
  }
  if (dataQualityCopy('OK').label !== 'Good') {
    throw new Error('healthy data copy is not understandable');
  }
  if (scoreColor(70, 'OK') === scoreColor(-70, 'OK')) {
    throw new Error('positive and negative scores do not have distinct colors');
  }
  if (scoreColor(70, 'CLOCK_DRIFT') !== 'var(--neg)') {
    throw new Error('degraded data is not fail-closed red');
  }
  const regimes = {
    TREND_UP: 'Bullish Trend',
    TREND_DOWN: 'Bearish Trend',
    RANGE: 'Sideways Market',
    BREAKOUT_UP: 'Bullish Breakout',
    BREAKOUT_DOWN: 'Bearish Breakout',
    HIGH_VOL_SHOCK: 'Volatility Spike',
    LOW_LIQUIDITY: 'Low Liquidity',
    DEGRADED: 'Waiting for Reliable Data',
  };
  for (const [raw, friendly] of Object.entries(regimes)) {
    if (regimeLabel(raw) !== friendly) {
      throw new Error(raw + ' was not mapped to ' + friendly);
    }
  }
  if (tradeDecisionMeta('CE_2_ITM').label !== 'BUY 2-STEP ITM CE') {
    throw new Error('CE decision capsule is wrong');
  }
  if (tradeDecisionMeta('PE_2_ITM').tone !== 'is-pe') {
    throw new Error('PE decision color is wrong');
  }
  if (tradeDecisionMeta('SHORT_MOVE').label !== 'SELL ATM MOVE') {
    throw new Error('MOVE decision capsule is wrong');
  }
  if (tradeDecisionMeta('SHORT_MOVE', false).label !== 'WAIT — CHECKS BLOCKED') {
    throw new Error('blocked committed decision is not fail-closed');
  }
  const calmBlock = tradeDecisionMeta(
    'SHORT_MOVE', false,
    'ADX is not below 25; calm-market confirmation is required before selling MOVE',
  );
  if (calmBlock.label !== 'WAIT — 5M ADX NOT CALM' ||
      !calmBlock.detail.includes('ADX is not below 25')) {
    throw new Error('ADX blocker is mislabeled: ' + JSON.stringify(calmBlock));
  }
`);
"""
    result = subprocess.run(
        [NODE, "-e", script],
        cwd=Path(dashboard.BASE),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_trend_engine_page_covers_every_contract_field_family():
    """docs/trend-snapshot-contract.md v1.0.0 — every top-level field family
    the contract defines has a rendering path on this page."""
    for marker in (
        "snapshot.regime", "snapshot.direction", "snapshot.trend_score",
        "liveView.live_score",
        "snapshot.confidence", "snapshot.data_quality",
        "renderComponents(snapshot.components)",
        "renderTimeframes(snapshot.timeframes)",
        "renderGates(snapshot.gates)",
        "renderReasonCodes(snapshot.reason_codes)",
        "snapshot.forecast_horizon_seconds", "snapshot.expected_return_bps",
        "snapshot.invalidation_price", "snapshot.suggested_stop_bps",
        "snapshot.schema_version", "snapshot.signal_id",
        "snapshot.candle_close_utc",
    ):
        assert marker in TEMPLATE, f"missing: {marker}"


def test_trend_engine_page_never_shows_entry_allowed_as_true_without_ok_quality():
    """Client-substituted degraded snapshots always carry entry_allowed=false
    and data_quality != OK (trend_engine_client.degraded_snapshot) — the page
    must render that distinction, not paper over it with a generic spinner."""
    assert "renderError(snapshot)" in TEMPLATE
    assert "snapshot.data_quality !== 'OK'" in TEMPLATE
    assert "client_detail" in TEMPLATE


def test_clock_drift_stays_fail_closed_without_a_redundant_page_alert():
    assert "snapshot.data_quality !== 'CLOCK_DRIFT'" in TEMPLATE
    assert "const showAlert = isDegraded" in TEMPLATE
    assert "box.hidden = !showAlert" in TEMPLATE
    assert "dataQualityCopy(snapshot.data_quality)" in TEMPLATE


@pytest.mark.skipif(NODE is None, reason="Node.js is required for frontend tests")
def test_clock_drift_alert_is_hidden_but_other_degradation_alerts_remain():
    script = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('templates/trend_engine.html', 'utf8');
const start = source.indexOf('function renderError');
const end = source.indexOf('async function renderEngineHealth', start);
if (start < 0 || end <= start) throw new Error('renderError not found');

const alertBox = { hidden: false };
const alertCopy = { textContent: '', title: '' };
global.document = {
  getElementById(id) {
    if (id === 'te-error') return alertBox;
    if (id === 'te-error-copy') return alertCopy;
    throw new Error('unexpected element: ' + id);
  },
};
global.dataQualityCopy = code => ({
  detail: code === 'CLOCK_DRIFT' ? 'clock' : 'feed problem',
});
vm.runInThisContext(source.slice(start, end));

renderError({data_quality: 'CLOCK_DRIFT', client_detail: 'offset'});
if (alertBox.hidden !== true) throw new Error('CLOCK_DRIFT banner remained visible');

renderError({data_quality: 'STALE_L1', client_detail: 'stale trade feed'});
if (alertBox.hidden !== false || alertCopy.textContent !== 'feed problem') {
  throw new Error('a genuine feed-degradation banner was suppressed');
}
"""
    result = subprocess.run(
        [NODE, "-e", script],
        cwd=Path(dashboard.BASE),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
