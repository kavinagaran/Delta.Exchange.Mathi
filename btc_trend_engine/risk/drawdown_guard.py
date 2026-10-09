"""Escalate a repeated drawdown breach from a soft, day-scoped block into a
kill-switch trigger recommendation.

``risk_controls.evaluate_entry`` already blocks new entries for the rest of
the trading day on a daily-loss or consecutive-loss breach, but that block
clears automatically at the next trading-day rollover. A kill switch is a
strictly stronger, manually-reset safety net layered on top: these functions
only ever recommend a *trigger name*; firing and persisting the latch is the
caller's job (``risk_manager.py`` composes this with ``kill_switch.py``), so
this module stays pure and does no I/O.
"""

from __future__ import annotations


def check_daily_loss(*, daily_pnl_usd: float, max_daily_loss_usd: float) -> bool:
    """True when the daily loss breach should latch a kill switch, not just
    block today's remaining entries."""
    if max_daily_loss_usd <= 0:
        return False
    return daily_pnl_usd <= -abs(max_daily_loss_usd)


def check_consecutive_losses(*, consecutive_losses: int, max_consecutive_losses: int) -> bool:
    if max_consecutive_losses <= 0:
        return False
    return consecutive_losses >= max_consecutive_losses


def triggers_from_decision(
    *,
    daily_pnl_usd: float,
    consecutive_losses: int,
    max_daily_loss_usd: float,
    max_consecutive_losses: int,
) -> list[str]:
    """Return the kill-switch names (see ``kill_switch.KillSwitchName``) that
    this account state should latch, by value rather than by enum to keep
    this module import-independent of ``kill_switch.py``."""
    triggers: list[str] = []
    if check_daily_loss(daily_pnl_usd=daily_pnl_usd, max_daily_loss_usd=max_daily_loss_usd):
        triggers.append("max_daily_loss")
    if check_consecutive_losses(
        consecutive_losses=consecutive_losses,
        max_consecutive_losses=max_consecutive_losses,
    ):
        triggers.append("consecutive_losses")
    return triggers
