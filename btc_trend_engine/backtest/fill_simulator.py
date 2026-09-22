"""Marketable-IOC fill simulation (§15, plan Phase 5).

Rules this enforces, each corresponding to a way backtests overstate results:

* **No candle-close fills.** An order decided at a candle close cannot be
  filled at that close — the price is only known once the candle is over. The
  earliest realistic fill is the next candle, and the reference is its open.
* **Bid/ask, never mid.** A buy lifts the ask, a sell hits the bid.
* **Slippage beyond the limit means no fill**, not a worse fill. That is what
  a marketable *limit* IOC actually does.
* **IOC cancels the remainder.** Unfilled size does not rest on the book and
  is not carried into the next bar.
* **Fees are charged on both sides** and are never netted out of a "gross"
  headline number.

With candle-only history there is no reconstructed book, so the spread is a
configured input rather than an observation. That is stated in the report,
not hidden — see the package docstring.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class FillConfig:
    spread_bps: Decimal = Decimal("2.0")
    """Assumed round-trip spread. Halved to each side of the reference."""

    max_slippage_bps: Decimal = Decimal("15.0")
    """Beyond this, the IOC does not fill at all."""

    taker_fee_bps: Decimal = Decimal("5.0")
    available_lots: int | None = None
    """Displayed depth cap. None means depth is not modelled — every order
    fills in full if it fills at all, which is optimistic and is reported."""


@dataclass(frozen=True, slots=True)
class Fill:
    filled_lots: int
    price: Decimal
    fee: Decimal
    rejected_reason: str | None = None

    @property
    def filled(self) -> bool:
        return self.filled_lots > 0


def _half_spread(reference: Decimal, config: FillConfig) -> Decimal:
    return reference * config.spread_bps / Decimal(20_000)


def simulate_fill(
    *,
    side: str,
    lots: int,
    reference_price: Decimal,
    limit_price: Decimal,
    config: FillConfig,
) -> Fill:
    """Simulate one marketable IOC against ``reference_price``.

    ``reference_price`` must be a price the order could actually have traded
    at — in practice the *next* candle's open, never the close of the candle
    that produced the signal.
    """
    if lots <= 0:
        return Fill(0, Decimal(0), Decimal(0), "no lots requested")
    if reference_price <= 0 or limit_price <= 0:
        return Fill(0, Decimal(0), Decimal(0), "invalid price")
    if side not in ("long", "short"):
        return Fill(0, Decimal(0), Decimal(0), f"unknown side {side!r}")

    half = _half_spread(reference_price, config)
    touch = reference_price + half if side == "long" else reference_price - half

    # A marketable limit only fills if the touch is within the limit.
    if side == "long" and touch > limit_price:
        return Fill(0, Decimal(0), Decimal(0),
                    f"ask {touch} above limit {limit_price}; IOC cancelled")
    if side == "short" and touch < limit_price:
        return Fill(0, Decimal(0), Decimal(0),
                    f"bid {touch} below limit {limit_price}; IOC cancelled")

    slippage_bps = abs(touch - reference_price) / reference_price * Decimal(10_000)
    if slippage_bps > config.max_slippage_bps:
        return Fill(0, Decimal(0), Decimal(0),
                    f"slippage {slippage_bps:.2f}bps exceeds cap "
                    f"{config.max_slippage_bps}bps; IOC cancelled")

    fillable = lots if config.available_lots is None else min(lots, config.available_lots)
    if fillable <= 0:
        return Fill(0, Decimal(0), Decimal(0), "no displayed depth")

    fee = touch * Decimal(fillable) * config.taker_fee_bps / Decimal(10_000)
    return Fill(fillable, touch, fee)
