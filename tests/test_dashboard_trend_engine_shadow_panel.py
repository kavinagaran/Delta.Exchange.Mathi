"""Trend Engine page — Phase 8a shadow-comparison panel.

Static-content checks in the style of test_dashboard_trend_engine_page.py:
prove the panel reads the read-only /api/engine/shadow proxy and has no
write path, since a shadow decision is posted only by the trend-auto loop
via trend_engine_client directly, never from a browser.
"""
from pathlib import Path

import dashboard

TEMPLATE = (Path(dashboard.BASE) / "templates" / "trend_engine.html").read_text(
    encoding="utf-8")


def test_the_panel_reads_the_readonly_shadow_proxy():
    assert "jget('/api/engine/shadow')" in TEMPLATE
    assert "renderShadowSummary" in TEMPLATE
    assert "'/shadow/legacy-decision'" not in TEMPLATE


def test_the_panel_shows_coverage_separately_from_agreement_rate():
    """Coverage (did the engine have an opinion) and agreement (did it agree)
    are different facts and must not collapse into one number."""
    for marker in ("te-shadow-coverage", "te-shadow-rate", "te-shadow-total",
                  "te-shadow-eligible", "engine_coverage", "agreement_rate"):
        assert marker in TEMPLATE, f"missing: {marker}"


def test_a_zero_comparable_candles_state_is_not_rendered_as_zero_percent():
    """summary.agreement_rate is None (not 0) with no comparable candles —
    the page must say so, not print a misleading 0.0%."""
    assert "finite(summary.agreement_rate)" in TEMPLATE
    assert "n/a (no comparable candles yet)" in TEMPLATE


def test_every_disagreement_class_has_a_plain_english_label():
    for reason in ("engine_no_snapshot", "engine_data_degraded",
                  "engine_gated_entry", "direction_opposed",
                  "engine_flat_legacy_directional",
                  "engine_directional_legacy_flat"):
        assert reason in TEMPLATE, f"missing label for {reason}"
