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
        'class="cockpit-option-choice is-wide" data-action="buy_move"',
        'class="cockpit-option-choice is-wide" data-action="sell_move"',
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
        "Wallet-affordable lots",
        "DRY RUN dashboard",
        "Open DRY RUN Trade",
        "Protected premium-selling strategies",
    ):
        assert required in source

    for removed in (
        'id="cockpit-bot-toggle"',
        "function cockpitToggleBot(checked)",
        "BOT = automatic · COCKPIT = manual",
        "cockpitBotMode === 'disabled'",
    ):
        assert removed not in source


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
        "position: fixed; top: 50%; left: 50%; right: auto; bottom: auto",
        "transform: translate(-50%, -50%)",
        ".cockpit-safety-strip",
        "@media (max-width: 920px)",
        "@media (max-width: 640px)",
    ):
        assert required in styles


def test_cockpit_typography_stays_legible_at_desktop_density():
    styles = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")

    # Regression guard: this terminal previously compressed important setup
    # and strategy copy into 6.7--10 px text, which was unreadable at normal
    # dashboard scale.
    for required in (
        '--cockpit-secondary: color-mix(in srgb, var(--muted) 72%, var(--text))',
        '.cockpit-setup-choice strong { overflow: hidden; color: var(--text); font-size: 12.5px',
        '.cockpit-setup-choice small { overflow: hidden; color: var(--cockpit-secondary); font-size: 11px',
        '.cockpit-setup-choice > b { color: var(--setup-tone); font-size: 9.5px',
        '.cockpit-option-choice strong { color: var(--text); font-size: 13px',
        '.cockpit-option-choice small { overflow: hidden; color: var(--cockpit-secondary); font-size: 11px',
        '.cockpit-option-choice.is-disabled { cursor: not-allowed; filter: grayscale(.6); opacity: .58; }',
    ):
        assert required in styles
