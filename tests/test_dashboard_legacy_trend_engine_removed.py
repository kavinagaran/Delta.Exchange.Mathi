"""The retired Trend Engine surface must not remain reachable or navigable."""

from pathlib import Path

import dashboard


BASE = Path(dashboard.BASE)


def test_legacy_page_template_and_routes_are_removed():
    rules = {rule.rule for rule in dashboard.app.url_map.iter_rules()}

    assert "trend-engine-legacy" not in dashboard._PAGES
    assert "/trend-engine-legacy" not in rules
    assert "/api/trend-engine" not in rules
    assert not hasattr(dashboard, "api_trend_engine")
    assert not (BASE / "templates" / "trend_engine_legacy.html").exists()


def test_legacy_links_are_removed_from_web_and_android_navigation():
    sources = [
        BASE / "templates" / "base.html",
        BASE / "templates" / "trend_engine.html",
        BASE / "templates" / "dry_run.html",
        BASE / "mv_btc_bot" / "lib" / "main.dart",
    ]

    for path in sources:
        source = path.read_text(encoding="utf-8")
        assert "/trend-engine-legacy" not in source, path
        assert "Trend Engine (Legacy)" not in source, path
