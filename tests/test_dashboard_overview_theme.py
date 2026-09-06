"""Overview keeps the same contrast hierarchy as the Performance screen."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_overview_uses_performance_style_cards_and_today_trade_table():
    overview = (ROOT / "templates" / "overview.html").read_text(encoding="utf-8")
    styles = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")

    assert 'class="overview-page"' in overview
    assert 'class="stats today-summary"' in overview
    assert "stat performance-stat accent" in overview
    assert "stat performance-stat neutral" in overview
    assert "setTodaySummaryTone('today-pnl'" in overview
    assert "overview-today-card" in overview
    assert ".today-summary" in styles
    assert "today-trades-card" in overview
    assert ".today-trades-table" in styles
    assert 'id="today-trades-body"' in overview
    assert ".today-protection-metric.tone-sl { color: #ff6172; }" in styles
    assert ".today-protection-metric.tone-tsl-arm { color: #ffc267; }" in styles
    assert ".today-protection-metric.tone-tsl-trail { color: #70b8ff; }" in styles
    assert "<th>Actions</th>" not in overview
    assert ".today-trade-actions" not in styles
    assert ".trade-action-link" not in styles


def test_today_trade_table_inherits_the_selected_theme_palette():
    """Every Today table header follows the shared selected-theme gradient."""
    overview = (ROOT / "templates" / "overview.html").read_text(encoding="utf-8")
    styles = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")

    assert ':root:not([data-theme="dark"]) table thead th' in styles
    assert ':root[data-theme="dark"] table thead th' in styles
    card = overview.split("today-trades-card", 1)[1]
    assert 'id="today-trades-body"' in card
    assert '<table class="today-trades-table"' in card
