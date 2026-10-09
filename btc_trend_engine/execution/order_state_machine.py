"""Order-lifecycle state machine as a pure transition *validator* (ADR 0003).

This does not drive anything — ``trend_score_live_execution.py`` is the state
machine that actually runs. This module exists so that, once the engine's
intents are wired to real fills (post-cutover), every observed transition can
be asserted legal before it is trusted, and an illegal one raises loudly
instead of silently corrupting a downstream comparison.
"""

from __future__ import annotations

import enum


class OrderState(enum.StrEnum):
    PROPOSED = "proposed"
    RISK_APPROVED = "risk_approved"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


TERMINAL_STATES = frozenset(
    {OrderState.FILLED, OrderState.REJECTED, OrderState.CANCELLED, OrderState.EXPIRED}
)

# §15.2. A partial fill may still resolve into a full fill, a cancel (the IOC
# remainder is cancelled by the venue) or expiry (TTL elapsed while pending
# reconciliation) — but never back into an earlier, less-informed state.
_ALLOWED: dict[OrderState, frozenset[OrderState]] = {
    OrderState.PROPOSED: frozenset({OrderState.RISK_APPROVED, OrderState.REJECTED, OrderState.EXPIRED}),
    OrderState.RISK_APPROVED: frozenset({OrderState.SUBMITTED, OrderState.REJECTED, OrderState.EXPIRED}),
    OrderState.SUBMITTED: frozenset({
        OrderState.FILLED, OrderState.PARTIALLY_FILLED,
        OrderState.REJECTED, OrderState.CANCELLED,
    }),
    OrderState.PARTIALLY_FILLED: frozenset({
        OrderState.FILLED, OrderState.CANCELLED, OrderState.EXPIRED,
    }),
    OrderState.FILLED: frozenset(),
    OrderState.REJECTED: frozenset(),
    OrderState.CANCELLED: frozenset(),
    OrderState.EXPIRED: frozenset(),
}


class IllegalTransitionError(RuntimeError):
    """An observed transition is not reachable from the current state."""


def transition(current: OrderState, target: OrderState) -> OrderState:
    """Validate ``current -> target`` and return ``target`` unchanged.

    Raises rather than clamping or ignoring: a caller that observes an illegal
    transition has a bug or is looking at corrupted/stale state, and silently
    accepting it would let that state poison whatever reads it next.
    """
    if current in TERMINAL_STATES:
        raise IllegalTransitionError(
            f"{current.value} is terminal; cannot transition to {target.value}")
    if target not in _ALLOWED[current]:
        raise IllegalTransitionError(
            f"{current.value} -> {target.value} is not a legal transition "
            f"(allowed: {sorted(s.value for s in _ALLOWED[current])})")
    return target


def is_terminal(state: OrderState) -> bool:
    return state in TERMINAL_STATES
