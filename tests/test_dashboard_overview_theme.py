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
    assert "stat performance-stat ${tone(+s.total_pnl)}" in stats


def test_overview_inherits_the_shared_table_header_instead_of_restating_it():
    """Same hierarchy as Performance, now by sharing rather than duplication.

    Overview used to carry its own `.overview-page .overview-today-card thead
    th` block copying the Performance header. That per-page override is gone:
    a single `:root table thead th` rule gives every table on every page the
    same graded header, so Overview inherits the treatment. Pinning the shared
    rule keeps the original intent; pinning the deleted duplicate only
    asserted how it used to be achieved.
    """
    overview = (ROOT / "templates" / "overview.html").read_text(encoding="utf-8")
    styles = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")

    assert ":root table thead th" in styles
    # The rule only reaches Overview if the card holds a real <table><thead>.
    card = overview.split('class="card overview-today-card"', 1)[1]
    assert "<table>" in card and "<thead>" in card
