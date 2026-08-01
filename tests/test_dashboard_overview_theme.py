"""Overview keeps the same contrast hierarchy as the Performance screen."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_overview_uses_performance_style_cards_and_table_header():
    overview = (ROOT / "templates" / "overview.html").read_text(encoding="utf-8")
    styles = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")

    assert 'class="overview-page"' in overview
    assert 'class="stats today-summary"' in overview
    assert "stat performance-stat accent" in overview
    assert "stat performance-stat neutral" in overview
    assert "setTodaySummaryTone('today-pnl'" in overview
    assert "overview-today-card" in overview
    assert ".today-summary" in styles
    assert ".today-trades-table td.numeric" in styles
    assert "<th>Actions</th>" not in overview
    assert ".today-trade-actions" not in styles
    assert ".trade-action-link" not in styles


def test_overview_inherits_the_selected_theme_table_header():
    """The shared header follows Red/Blue without a per-page override.

    Overview used to carry its own `.overview-page .overview-today-card thead
    th` block copying the Performance header. That per-page override is gone:
    the shared structural rule and two theme-specific palettes give every
    table the selected treatment.
    """
    overview = (ROOT / "templates" / "overview.html").read_text(encoding="utf-8")
    styles = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")

    assert ":root table thead th" in styles
    assert ':root:not([data-theme="dark"]) table thead th' in styles
    assert ':root[data-theme="dark"] table thead th' in styles
    assert ":root table thead th:nth-child" not in styles
    red_header = styles.split(
        ':root:not([data-theme="dark"]) table thead th', 1)[1].split("}", 1)[0]
    blue_header = styles.split(
        ':root[data-theme="dark"] table thead th', 1)[1].split("}", 1)[0]
    assert "#7c2032" in red_header
    assert "#0f5688" in blue_header
    assert red_header != blue_header
    # The rule only reaches Overview if the card holds a real <table><thead>.
    card = overview.split('class="card overview-today-card"', 1)[1]
    assert "<table" in card and "<thead>" in card
