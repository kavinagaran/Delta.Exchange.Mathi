from pathlib import Path

import dashboard


ROOT = Path(__file__).resolve().parents[1]


def test_cockpit_is_a_dedicated_dashboard_page():
    assert dashboard._PAGES["cockpit"] == ("cockpit.html", "Cockpit")

    base = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
    assert "('cockpit',   '/cockpit',   'Cockpit'" in base


def test_cockpit_page_contains_every_supported_manual_strategy():
    source = (ROOT / "templates" / "cockpit.html").read_text(encoding="utf-8")

    for required in (
        'aria-label="Manual option strategy cockpit"',
        'id="directional-strategies-title"',
        'id="volatility-strategies-title"',
        'id="cockpit-buy-ce"',
        'id="cockpit-buy-pe"',
        'id="cockpit-buy-move"',
        'id="cockpit-sell-move"',
        "cockpitEnter('buy_ce')",
        "cockpitEnter('buy_pe')",
        "cockpitEnter('buy_move')",
        "cockpitEnter('sell_move')",
        "'/api/cockpit/preview'",
        "'/api/cockpit/enter'",
        'id="cockpit-preview-dialog"',
        "function cockpitConfirmEntry()",
        'id="cockpit-setup-lock-reset"',
        "function cockpitResetZoneLock()",
        'id="cockpit-bot-toggle"',
        "function cockpitToggleBot(checked)",
        "TREND_ENGINE_SCORE_AUTO_MODE: checked ? 'live' : 'disabled'",
        "A confirmed fill turns the automated bot off",
        "Wallet-affordable lots",
    ):
        assert required in source


def test_cockpit_page_has_responsive_strategy_terminal_styles():
    styles = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")

    for required in (
        ".cockpit-page",
        ".cockpit-command-bar",
        ".cockpit-state-grid",
        ".cockpit-strategy-grid",
        ".cockpit-strategy-card.is-ce",
        ".cockpit-strategy-card.is-pe",
        ".cockpit-strategy-card.is-move-buy",
        ".cockpit-strategy-card.is-move-sell",
        ".cockpit-preview-dialog",
        ".cockpit-safety-strip",
        "@media (max-width: 920px)",
        "@media (max-width: 640px)",
    ):
        assert required in styles
