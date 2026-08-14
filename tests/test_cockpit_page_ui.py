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
        'class="cockpit-workbench"',
        'id="cockpit-setup-groups"',
        'id="cockpit-option-panel"',
        'data-action="buy_ce"',
        'data-action="buy_pe"',
        'data-action="buy_move"',
        'data-action="sell_ce"',
        'data-action="sell_pe"',
        'data-action="sell_move"',
        "buy_ce: 'Buy 2-Step ITM Call'",
        "buy_pe: 'Buy 2-Step ITM Put'",
        "buy_move: 'Buy ATM MOVE'",
        "sell_ce: 'Sell ATM Call'",
        "sell_pe: 'Sell ATM Put'",
        "sell_move: 'Sell ATM MOVE Straddle'",
        "'/api/cockpit/setups'",
        "'/api/cockpit/preview'",
        "'/api/cockpit/enter'",
        'id="cockpit-preview-dialog"',
        "function cockpitConfirmEntry()",
        'id="cockpit-setup-lock-reset"',
        "function cockpitResetZoneLock()",
        'id="cockpit-bot-toggle"',
        "function cockpitToggleBot(checked)",
        "TREND_ENGINE_SCORE_AUTO_MODE: checked ? 'live' : 'disabled'",
        "Manual fills leave automation OFF",
        "Wallet-affordable lots",
    ):
        assert required in source


def test_cockpit_requires_an_eligible_setup_before_enabling_a_trade():
    source = (ROOT / "templates" / "cockpit.html").read_text(encoding="utf-8")

    for required in (
        "const COCKPIT_SETUP_GROUPS",
        "function cockpitDeriveSetups(snapshot)",
        "function cockpitSelectSetup(id)",
        "function cockpitClearSetup()",
        "setup?.eligible",
        "setup.actions.includes(action)",
        "Order-flow feed unavailable",
        "No fresh flip confirmation",
        "calm_range",
        "volatility_expansion",
        "rsi_bullish",
        "rsi_bearish",
        "support_bounce",
        "resistance_rejection",
    ):
        assert required in source


def test_cockpit_page_has_responsive_strategy_terminal_styles():
    styles = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")

    for required in (
        ".cockpit-page",
        ".cockpit-command-bar",
        ".cockpit-state-grid",
        ".cockpit-workbench",
        ".cockpit-setup-panel",
        ".cockpit-option-panel",
        ".cockpit-setup-choice.is-eligible",
        ".cockpit-setup-choice.is-ineligible",
        ".cockpit-option-group.is-buy",
        ".cockpit-option-group.is-sell",
        ".cockpit-option-choice.is-enabled",
        ".cockpit-preview-dialog",
        ".cockpit-safety-strip",
        "@media (max-width: 920px)",
        "@media (max-width: 640px)",
    ):
        assert required in styles
