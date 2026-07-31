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
        "Today's Trades",
        'id="today-body"',
        "jget('/api/today-trades')",
        "function updateTodaySummary(rows)",
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
        "Manage protection",
        "Payoff at settlement",
        "loadStatusTiles",
        "loadStatsInto",
        "loadSlots",
        "openProtectionDrawer",
        "showPayoff",
        "squareOff",
        "/api/engine/health",
        "/api/tp-monitor",
    ):
        assert removed not in source


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
