"""Pure pre-trade slippage/spread check (Trend_Engine.md §15, ADR 0003).

Mirrors the guard already enforced inline in ``trend_score_live_execution.py``
(max slippage vs a reference price) as a standalone, unit-testable function so
the engine's own sizing/preview path can apply the identical rule without
importing execution code.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class SlippageCheck:
    ok: bool
    reason: str
    slippage_pct: Decimal


def check_slippage(
    *,
    reference_price: Decimal,
    fill_price: Decimal,
    max_slippage_pct: Decimal,
    side: str,
) -> SlippageCheck:
    """``side`` is the position side ("long" buys, "short" sells). Adverse
    slippage is the exchange filling worse than the reference in the
    direction that costs the account money."""
    if reference_price <= 0:
        return SlippageCheck(False, "reference price must be positive", Decimal(0))
    if fill_price <= 0:
        return SlippageCheck(False, "fill price must be positive", Decimal(0))
    if side == "long":
        adverse = fill_price - reference_price
    elif side == "short":
        adverse = reference_price - fill_price
    else:
        return SlippageCheck(False, f"unknown side {side!r}", Decimal(0))
    slippage_pct = (adverse / reference_price) * 100
    if slippage_pct > max_slippage_pct:
        return SlippageCheck(
            False,
            f"fill slipped {slippage_pct:.4f}% against reference "
            f"(cap {max_slippage_pct:.4f}%)",
            slippage_pct,
        )
    return SlippageCheck(True, "within slippage cap", slippage_pct)


def check_quote_age(*, quote_age_seconds: float, max_quote_age_seconds: float) -> SlippageCheck:
    if quote_age_seconds > max_quote_age_seconds:
        return SlippageCheck(
            False,
            f"quote is {quote_age_seconds:.1f}s old (cap {max_quote_age_seconds:.1f}s)",
            Decimal(0),
        )
    return SlippageCheck(True, "quote is fresh enough", Decimal(0))
