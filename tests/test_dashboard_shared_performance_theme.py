"""The Performance color hierarchy is the common dashboard visual system."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_global_card_stat_and_table_treatments_cover_every_dashboard_page():
    styles = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")
    pages = ("overview.html", "dry_run.html", "positions.html", "config.html",
             "accounts.html", "logs.html", "trades.html", "trend_engine.html")

    for page in pages:
        source = (ROOT / "templates" / page).read_text(encoding="utf-8")
        assert 'class="card' in source or page == "overview.html"

    assert ':root:not([data-theme="dark"]) .card-hd' in styles
    assert ':root[data-theme="dark"] .card-hd' in styles
    assert ':root .stats .stat:not(.performance-stat)::before' in styles
    assert ':root table thead th' in styles
    assert ':root:not([data-theme="dark"]) table thead th' in styles
    assert ':root[data-theme="dark"] table thead th' in styles
    assert ':root .section-title' in styles
