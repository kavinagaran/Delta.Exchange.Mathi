"""Overview keeps the same contrast hierarchy as the Performance screen."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_overview_uses_performance_style_cards_and_latest_trade_detail():
    overview = (ROOT / "templates" / "overview.html").read_text(encoding="utf-8")
    styles = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")

    assert 'class="overview-page"' in overview
    assert 'class="stats today-summary"' in overview
    assert "stat performance-stat accent" in overview
    assert "stat performance-stat neutral" in overview
    assert "setTodaySummaryTone('today-pnl'" in overview
    assert "overview-today-card" in overview
    assert ".today-summary" in styles
    assert "today-latest-trade-card" in overview
    assert ".today-latest-trade-body" in styles
    assert ".today-latest-detail" in styles
    assert "<th>Actions</th>" not in overview
    assert ".today-trade-actions" not in styles
    assert ".trade-action-link" not in styles


def test_latest_trade_card_inherits_the_selected_theme_palette():
    """The latest-trade treatment is expressed entirely in theme variables."""
    overview = (ROOT / "templates" / "overview.html").read_text(encoding="utf-8")
    styles = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")

    detail_rule = styles.split(".today-latest-detail {", 1)[1].split("}", 1)[0]
    assert "var(--accent-dark)" in detail_rule
    assert "var(--surface)" in detail_rule
    assert "var(--border)" in detail_rule
    card = overview.split("today-latest-trade-card", 1)[1]
    assert 'id="today-latest-trade"' in card
    assert "<table" not in card
