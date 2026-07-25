"""The rebuilt Trend Engine page (UI-3) — reads TrendSnapshot v1.0.0 via
/api/engine/snapshot. Static-content checks in the style of test_topbar_ui.py
and test_dashboard_trend_engine.py: prove the page is registered, read-only,
and wired to the new proxy routes rather than the legacy /api/trend-engine
chain it replaced at this URL.
"""
from pathlib import Path

import dashboard

TEMPLATE = (Path(dashboard.BASE) / "templates" / "trend_engine.html").read_text(
    encoding="utf-8")


def test_trend_engine_page_is_registered_and_reads_only():
    assert dashboard._PAGES["trend-engine"] == ("trend_engine.html", "Trend Engine")
    shell = (Path(dashboard.BASE) / "templates" / "base.html").read_text(
        encoding="utf-8")
    assert "('trend-engine', '/trend-engine', 'Trend Engine'" in shell

    assert "This engine never submits an order" in TEMPLATE
    assert "/trend-engine-legacy" in TEMPLATE
    assert "jget('/api/engine/snapshot')" in TEMPLATE
    assert "jget('/api/engine/status')" in TEMPLATE
    # No write path of any kind on this page.
    assert "jpost(" not in TEMPLATE
    assert "method: 'POST'" not in TEMPLATE
    assert "method: 'DELETE'" not in TEMPLATE
    assert "/api/trend-entry" not in TEMPLATE
    assert "/api/trend-engine'" not in TEMPLATE  # the legacy endpoint, not this page's


def test_trend_engine_page_covers_every_contract_field_family():
    """docs/trend-snapshot-contract.md v1.0.0 — every top-level field family
    the contract defines has a rendering path on this page."""
    for marker in (
        "snapshot.regime", "snapshot.direction", "snapshot.trend_score",
        "snapshot.confidence", "snapshot.data_quality",
        "renderComponents(snapshot.components)",
        "renderTimeframes(snapshot.timeframes)",
        "renderGates(snapshot.gates)",
        "renderReasonCodes(snapshot.reason_codes)",
        "snapshot.forecast_horizon_seconds", "snapshot.expected_return_bps",
        "snapshot.invalidation_price", "snapshot.suggested_stop_bps",
        "snapshot.schema_version", "snapshot.signal_id",
        "snapshot.candle_close_utc",
    ):
        assert marker in TEMPLATE, f"missing: {marker}"


def test_trend_engine_page_never_shows_entry_allowed_as_true_without_ok_quality():
    """Client-substituted degraded snapshots always carry entry_allowed=false
    and data_quality != OK (trend_engine_client.degraded_snapshot) — the page
    must render that distinction, not paper over it with a generic spinner."""
    assert "renderError(snapshot)" in TEMPLATE
    assert "snapshot.data_quality !== 'OK'" in TEMPLATE
    assert "client_detail" in TEMPLATE
