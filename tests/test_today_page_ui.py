from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_today_page_uses_current_trade_copy_without_schedule_panel():
    source = (ROOT / "templates" / "overview.html").read_text(encoding="utf-8")

    assert "Your Trades, at a glance!" in source
    assert "Trend Engine-driven MOVE and CE/PE positions, at a glance." not in source
    assert "Next scheduled action" not in source
    assert 'id="next-actions"' not in source
    assert "loadNextActions" not in source
    assert "setNextAction" not in source


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
