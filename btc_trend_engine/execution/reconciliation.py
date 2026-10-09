"""Pure state-vs-exchange comparator (Trend_Engine.md §15.5, ADR 0003).

This is a new, general-purpose comparator for the engine's own future
execution path. It is intentionally independent of, and does not replace,
``dashboard.py``'s existing exchange-sync reconciliation
(``_sync_states_from_exchange_unlocked`` / ``ownership`` semantics), which is
safety-critical, already covers every legacy strategy slot, and stays
untouched. This module takes plain dicts and performs no I/O, so either
process can call it without crossing the process boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping

# A local-state row is considered "open" for reconciliation purposes at these
# statuses. ENTRY_PENDING has no confirmed exchange position yet, so a bare
# absence on the exchange side is not itself a mismatch for it.
PENDING_STATUSES = {"ENTRY_PENDING"}
OPEN_STATUSES = {"OPEN"}


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    matched: bool
    mismatches: tuple[str, ...] = field(default_factory=tuple)


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def compare(
    bot_state: Mapping[str, Any] | None,
    exchange_position: Mapping[str, Any] | None,
) -> ReconciliationResult:
    """Compare one local strategy state to the exchange's view of that symbol.

    ``exchange_position`` is ``None`` when the exchange reports no open
    position for the symbol. Absence of local state with an exchange position
    present is also a mismatch — that is an unowned, unmanaged exposure.
    """
    bot_state = bot_state or {}
    status = str(bot_state.get("status") or "").upper()
    bot_open = status in OPEN_STATUSES
    bot_pending = status in PENDING_STATUSES

    if not bot_open and not bot_pending:
        if exchange_position is not None:
            return ReconciliationResult(
                False, ("exchange reports an open position with no matching local state",))
        return ReconciliationResult(True)

    if bot_pending and exchange_position is None:
        # A just-submitted order may not have landed on the exchange feed yet;
        # this is expected transient state, not a mismatch.
        return ReconciliationResult(True)

    if exchange_position is None:
        return ReconciliationResult(
            False, ("local state is OPEN but the exchange reports no position",))

    mismatches: list[str] = []
    local_size = _dec(bot_state.get("lots") or bot_state.get("size"))
    exch_size = _dec(exchange_position.get("size") or exchange_position.get("lots"))
    if local_size is not None and exch_size is not None and local_size != abs(exch_size):
        mismatches.append(f"lot size mismatch: local={local_size} exchange={abs(exch_size)}")

    local_side = str(bot_state.get("side") or "long").lower()
    exch_side = str(exchange_position.get("side") or "").lower()
    if not exch_side and exch_size is not None:
        exch_side = "short" if exch_size < 0 else "long"
    if exch_side and local_side != exch_side:
        mismatches.append(f"side mismatch: local={local_side} exchange={exch_side}")

    local_symbol = str(bot_state.get("symbol") or "")
    exch_symbol = str(exchange_position.get("symbol") or "")
    if local_symbol and exch_symbol and local_symbol != exch_symbol:
        mismatches.append(f"symbol mismatch: local={local_symbol} exchange={exch_symbol}")

    return ReconciliationResult(not mismatches, tuple(mismatches))
