"""Data-only order intent and its accompanying risk record (ADR 0003, §5.4/§5.5).

``OrderIntent`` is everything the engine is allowed to produce: a proposal,
never a placed order. ``IntentRiskDecision`` is the immutable audit record of
*why* — it is deliberately a different shape from ``risk_controls.RiskDecision``
(the account-limit evaluation risk_manager.py wraps), because it also captures
the engine-specific checks risk_controls has no concept of: kill switches and
signal freshness. The dashboard consumes an ``OrderIntent``; it does not trust
this package to have decided correctly and re-derives nothing from it that
matters to money — the intent only carries `lots`, price ceiling and the
signal it traces to (criterion 19).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

VALID_SIDES = ("long", "short")
VALID_ORDER_TYPES = ("limit_ioc",)


class InvalidOrderIntent(ValueError):
    """The requested intent violates an invariant; refuse to construct it."""


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True, slots=True)
class IntentRiskDecision:
    """Immutable snapshot of the checks that justified (or refused) an intent."""

    allowed: bool
    reason: str
    signal_id: str
    signal_key: str
    kill_switches_active: tuple[str, ...] = ()
    signal_expired: bool = False
    account_risk_reason: str | None = None
    proposed_risk_usd: float = 0.0
    lots: int = 0

    def __post_init__(self) -> None:
        if self.allowed and self.lots <= 0:
            raise InvalidOrderIntent("an allowed decision must size at least one lot")
        if self.allowed and (self.kill_switches_active or self.signal_expired):
            raise InvalidOrderIntent(
                "a decision cannot be allowed while a kill switch is active "
                "or the signal is expired")


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """A proposal only. No code path in this package can turn this into a
    live order — that remains ``trend_score_live_execution.py``'s job."""

    symbol: str
    side: str
    lots: int
    max_price: Decimal
    signal_id: str
    signal_key: str
    reason: str
    risk: IntentRiskDecision
    created_at_utc: str
    expires_at_utc: str
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.symbol:
            raise InvalidOrderIntent("symbol is required")
        if self.side not in VALID_SIDES:
            raise InvalidOrderIntent(f"side must be one of {VALID_SIDES}, got {self.side!r}")
        if self.lots <= 0:
            raise InvalidOrderIntent(f"lots must be positive, got {self.lots}")
        if self.max_price <= 0:
            raise InvalidOrderIntent(f"max_price must be positive, got {self.max_price}")
        if not self.risk.allowed:
            raise InvalidOrderIntent(
                "an OrderIntent cannot be built from a refused risk decision "
                f"({self.risk.reason!r}); the caller must check risk.allowed first")
        if self.expires_at_utc <= self.created_at_utc:
            raise InvalidOrderIntent("expires_at_utc must be after created_at_utc")

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "lots": self.lots,
            "max_price": str(self.max_price),
            "signal_id": self.signal_id,
            "signal_key": self.signal_key,
            "reason": self.reason,
            "created_at_utc": self.created_at_utc,
            "expires_at_utc": self.expires_at_utc,
            "extra": dict(self.extra),
        }


def build_order_intent(
    *,
    symbol: str,
    side: str,
    max_price: Decimal,
    reason: str,
    risk: IntentRiskDecision,
    now: datetime,
    ttl_seconds: int,
    extra: dict[str, Any] | None = None,
) -> OrderIntent | None:
    """Return ``None`` rather than raise when the decision refuses the trade —
    "no intent" is the normal, expected outcome of a refused risk check, not
    an error condition for the caller to catch."""
    if not risk.allowed:
        return None
    created = now.astimezone(timezone.utc)
    return OrderIntent(
        symbol=symbol,
        side=side,
        lots=risk.lots,
        max_price=max_price,
        signal_id=risk.signal_id,
        signal_key=risk.signal_key,
        reason=reason,
        risk=risk,
        created_at_utc=_iso(created),
        expires_at_utc=_iso(created + timedelta(seconds=max(int(ttl_seconds), 0))),
        extra=dict(extra or {}),
    )
