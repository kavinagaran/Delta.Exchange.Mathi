"""The obsolete shadow-comparison panel stays removed after engine cutover.

The engine became the sole score source, so comparing the dashboard decision
back to that same engine measured self-agreement. Historical shadow data stays
available through the API, but the active Trend Engine page must show the
provisional-versus-committed score instead of presenting a misleading rate.
"""
from pathlib import Path

import dashboard

TEMPLATE = (Path(dashboard.BASE) / "templates" / "trend_engine.html").read_text(
    encoding="utf-8")


def test_shadow_comparison_is_not_rendered_as_current_evidence():
    assert "jget('/api/engine/shadow')" not in TEMPLATE
    assert "renderShadowSummary" not in TEMPLATE
    assert "Legacy vs. engine agreement" not in TEMPLATE


def test_page_reads_both_live_preview_and_committed_decision():
    assert "jget('/api/engine/live')" in TEMPLATE
    assert "jget('/api/engine/snapshot')" in TEMPLATE
    assert "Live preview" in TEMPLATE
    assert "Committed decision" in TEMPLATE
    assert 'id="te-trade-decision"' in TEMPLATE
    assert "Preview decision" not in TEMPLATE


def test_live_preview_cannot_supply_an_order_identity_or_permission():
    assert "liveView.live_score" in TEMPLATE
    assert "liveView.signal_id" not in TEMPLATE
    assert "liveView.entry_allowed" not in TEMPLATE
    assert "liveView.zone_action_allowed" not in TEMPLATE
