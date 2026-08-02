from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_today_page_contains_only_same_day_trade_content():
    source = (ROOT / "templates" / "overview.html").read_text(encoding="utf-8")

    for required in (
        'id="today-summary"',
        'id="today-trade-total"',
        'id="today-open-total"',
        'id="today-closed-total"',
        'id="today-pnl"',
        "Latest Trade",
        'id="today-latest-trade"',
        'id="today-latest-note"',
        "jget('/api/today-trades')",
        "function updateTodaySummary(rows)",
        "function latestCompletedTodayTrade(rows)",
        "function renderTodayLatestTrade(trade)",
        "function todayInlineProtectionHtml(protection)",
        "jget('/api/tp-monitor')",
        "jget('/api/engine/snapshot')",
        "jget('/api/engine/live')",
        "function todayOdometerScore(value)",
        "function todayEngineScoreDials(snapshot, liveView, status)",
        "Live preview",
        "Committed decision",
        'aria-label="Live TP, SL and TSL monitor"',
        '>Exit</button>',
        "function closeTodayLiveTrade(index)",
        "'/api/square-off?slot='",
        "target_mode: 'live'",
        'aria-label="Today trading dashboard"',
        'data-metric="pnl"',
        'class="wide decision-primary"',
        'class="today-score-dial"',
        'class="today-latest-detail"',
        'Realized P&amp;L',
    ):
        assert required in source

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
        "Today's Trades",
        ">Close Position</button>",
        ">Protection</button>",
        ">Payoff</button>",
        "openProtectionDrawer",
        "showPayoff",
        "squareOff",
        "/api/engine/health",
        "View Trend Engine",
    ):
        assert removed not in source


def test_today_page_uses_compact_responsive_terminal_layout():
    styles = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")

    for required in (
        "body:has(.overview-page) .main",
        ".today-current-trade-body",
        ".today-decision-panel .decision-primary",
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
        ".today-odometer-window",
        "@keyframes today-odometer-roll",
        "@media (max-width: 920px)",
        "@media (max-width: 560px)",
        "prefers-reduced-motion",
    ):
        assert required in styles


def test_today_summary_cards_use_meaningful_svg_icons_not_empty_boxes():
    source = (ROOT / "templates" / "overview.html").read_text(encoding="utf-8")
    styles = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")
    summary = source.split('id="today-summary"', 1)[1].split(
        'class="card overview-today-card today-latest-trade-card"', 1)[0]

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
