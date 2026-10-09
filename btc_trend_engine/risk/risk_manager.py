"""Single entry point for "may this signal become an OrderIntent?" (ADR 0003).

Composes, in order: kill switches (caller-supplied, latched state wins over
everything) -> signal freshness/entry_allowed on the TrendSnapshot -> sizing
(``position_sizer``, itself capped at 1,000 lots) -> the one account-risk
gate, ``risk_controls.evaluate_entry`` (repo root, never reimplemented here).

This module does no I/O. Kill-switch state and account history are both
caller-supplied or read via ``risk_controls`` (which the caller points at a
real ``data_dir``); nothing here reaches for a file path on its own. That
keeps it callable identically from a unit test, from a future dashboard
integration, or from the engine's own process.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from risk_controls import RiskDecision as AccountRiskDecision  # noqa: E402
from risk_controls import cfg_float, cfg_int  # noqa: E402
from risk_controls import evaluate_entry as _evaluate_account_entry  # noqa: E402

from ..execution.intents import IntentRiskDecision
from ..execution.reconciliation import ReconciliationResult
from . import drawdown_guard
from .kill_switch import KillSwitchName
from .position_sizer import DEFAULT_CUTOVER_CAP, size_position

_TIMESTAMP_FMT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True, slots=True)
class SizingInputs:
    """Mirrors ``dashboard.py::_trend_lot_plan``'s inputs to
    ``risk_based_lots``, minus the engine-specific cutover cap it adds."""

    configured: int
    affordable: int
    liquidity_cap: int
    max_order_lots: int
    risk_budget_usd: float
    stop_loss_usd: float
    premium_per_lot: float
    round_trip_fee_per_lot: float
    slippage_per_lot: float
    short: bool = False


@dataclass(frozen=True, slots=True)
class RiskManagerResult:
    decision: IntentRiskDecision
    account: AccountRiskDecision | None
    kill_switch_triggers: tuple[str, ...] = ()


def _signal_expiry_reason(snapshot: Mapping[str, Any], now: datetime) -> str | None:
    raw_ts = snapshot.get("timestamp")
    try:
        ts = datetime.strptime(str(raw_ts), _TIMESTAMP_FMT).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return f"signal snapshot has no valid timestamp ({raw_ts!r})"
    try:
        ttl = int(snapshot.get("signal_ttl_seconds"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "signal snapshot has no valid signal_ttl_seconds"
    age = (now - ts).total_seconds()
    if age < 0:
        return f"signal timestamp is in the future by {-age:.1f}s (clock skew)"
    if age > ttl:
        return f"signal is expired ({age:.1f}s old, TTL {ttl}s)"
    return None


def _proposed_risk_usd(lots: int, sizing: SizingInputs) -> float:
    if lots <= 0:
        return 0.0
    per_lot = sizing.premium_per_lot + sizing.round_trip_fee_per_lot + sizing.slippage_per_lot
    return max(sizing.stop_loss_usd, lots * per_lot)


def evaluate_entry(
    *,
    data_dir: Path,
    signal_id: str,
    signal_key: str,
    config: Mapping[str, Any],
    sizing: SizingInputs,
    kill_switches_active: Sequence[str] = (),
    snapshot: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    unrealized_pnl_usd: float = 0.0,
    dry_run: bool = False,
    cutover_cap: int = DEFAULT_CUTOVER_CAP,
) -> RiskManagerResult:
    now = now or datetime.now(timezone.utc)

    active = tuple(sorted(set(kill_switches_active)))
    if active:
        return RiskManagerResult(
            decision=IntentRiskDecision(
                allowed=False,
                reason=f"kill switch(es) active: {', '.join(active)}",
                signal_id=signal_id, signal_key=signal_key,
                kill_switches_active=active,
            ),
            account=None,
        )

    if snapshot is not None:
        expired_reason = _signal_expiry_reason(snapshot, now)
        if expired_reason:
            return RiskManagerResult(
                decision=IntentRiskDecision(
                    allowed=False, reason=expired_reason,
                    signal_id=signal_id, signal_key=signal_key,
                    signal_expired=True,
                ),
                account=None,
            )
        if not snapshot.get("entry_allowed", False):
            return RiskManagerResult(
                decision=IntentRiskDecision(
                    allowed=False,
                    reason="signal snapshot does not permit entry (entry_allowed=false)",
                    signal_id=signal_id, signal_key=signal_key,
                ),
                account=None,
            )

    lots = size_position(
        configured=sizing.configured, affordable=sizing.affordable,
        liquidity_cap=sizing.liquidity_cap, max_order_lots=sizing.max_order_lots,
        risk_budget_usd=sizing.risk_budget_usd, stop_loss_usd=sizing.stop_loss_usd,
        premium_per_lot=sizing.premium_per_lot,
        round_trip_fee_per_lot=sizing.round_trip_fee_per_lot,
        slippage_per_lot=sizing.slippage_per_lot, short=sizing.short,
        cutover_cap=cutover_cap,
    )
    if lots < 1:
        return RiskManagerResult(
            decision=IntentRiskDecision(
                allowed=False,
                reason="configured, affordable, risk, premium or order cap permits zero lots",
                signal_id=signal_id, signal_key=signal_key,
            ),
            account=None,
        )

    proposed_risk_usd = _proposed_risk_usd(lots, sizing)
    account_decision = _evaluate_account_entry(
        data_dir, proposed_risk_usd, dict(config),
        now=now, unrealized_pnl_usd=unrealized_pnl_usd, dry_run=dry_run)

    triggers = tuple(drawdown_guard.triggers_from_decision(
        daily_pnl_usd=account_decision.daily_pnl_usd,
        consecutive_losses=account_decision.consecutive_losses,
        max_daily_loss_usd=cfg_float(dict(config), "MAX_DAILY_LOSS_USD", 500.0),
        max_consecutive_losses=cfg_int(dict(config), "MAX_CONSECUTIVE_LOSSES", 3),
    ))

    if not account_decision.allowed:
        return RiskManagerResult(
            decision=IntentRiskDecision(
                allowed=False, reason=account_decision.reason,
                signal_id=signal_id, signal_key=signal_key,
                account_risk_reason=account_decision.reason,
                proposed_risk_usd=proposed_risk_usd,
            ),
            account=account_decision,
            kill_switch_triggers=triggers,
        )

    return RiskManagerResult(
        decision=IntentRiskDecision(
            allowed=True, reason="risk checks passed",
            signal_id=signal_id, signal_key=signal_key,
            proposed_risk_usd=proposed_risk_usd, lots=lots,
        ),
        account=account_decision,
        kill_switch_triggers=triggers,
    )


def reconciliation_trigger(result: ReconciliationResult) -> list[str]:
    """A mismatch is always a manual-attention event — never auto-cleared by
    the next reconciliation pass agreeing, only by an explicit resume."""
    return [] if result.matched else [KillSwitchName.RECONCILIATION_MISMATCH.value]
