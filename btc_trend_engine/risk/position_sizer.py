"""Sizing at cutover: `min(risk_based_lots, 1000)` (ADR 0003 "Sizing at cutover").

The risk manager may only ever *reduce* size relative to today's fixed
1,000-lot policy. ``risk_based_lots`` itself is never reimplemented here —
imported directly from the repo-root ``risk_controls`` module, which has no
Flask/exchange dependencies and is safe to import from either process
(pyproject.toml: both processes run with the repo root on ``sys.path``).
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from risk_controls import risk_based_lots  # noqa: E402

DEFAULT_CUTOVER_CAP = 1000


def size_position(
    *,
    configured: int,
    affordable: int,
    liquidity_cap: int,
    max_order_lots: int,
    risk_budget_usd: float,
    stop_loss_usd: float,
    premium_per_lot: float,
    round_trip_fee_per_lot: float,
    slippage_per_lot: float,
    short: bool = False,
    cutover_cap: int = DEFAULT_CUTOVER_CAP,
) -> int:
    """Every independent cap, then the cutover ceiling — never the other way
    round: the ceiling caps the *result*, it does not relax any input cap."""
    lots = risk_based_lots(
        configured=configured,
        affordable=affordable,
        liquidity_cap=liquidity_cap,
        max_order_lots=max_order_lots,
        risk_budget_usd=risk_budget_usd,
        stop_loss_usd=stop_loss_usd,
        premium_per_lot=premium_per_lot,
        round_trip_fee_per_lot=round_trip_fee_per_lot,
        slippage_per_lot=slippage_per_lot,
        short=short,
    )
    if lots <= 0:
        return 0
    return min(lots, max(int(cutover_cap), 0))
