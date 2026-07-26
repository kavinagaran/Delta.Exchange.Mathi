"""Overview keeps the same contrast hierarchy as the Performance screen."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_overview_uses_performance_style_cards_and_table_header():
    overview = (ROOT / "templates" / "overview.html").read_text(encoding="utf-8")
    styles = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")
    stats = (ROOT / "static" / "js" / "stats.js").read_text(encoding="utf-8")

    assert 'class="overview-page"' in overview
    assert 'class="greeting overview-hero"' in overview
    assert "stat performance-stat neutral" in overview
    assert "setTone('tile-day-pnl'" in overview
    assert "overview-today-card" in overview
    assert ".overview-hero" in styles
    assert ".overview-page .next-action" in styles
    assert ".overview-page .overview-today-card thead th" in styles
    assert "stat performance-stat ${tone(+s.total_pnl)}" in stats
