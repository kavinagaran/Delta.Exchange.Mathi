"""Portfolio-level exposure checks beyond a single ``evaluate_entry`` call.

``risk_controls.evaluate_entry`` already enforces the open-risk cap for one
proposed trade against one account's persisted state. These functions add the
checks that are about the *shape* of the portfolio rather than one entry's
risk budget: how many positions are concurrently open, and how much premium
is committed in aggregate. Pure and stateless — callers supply the current
portfolio figures; nothing here reads a file.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ExposureCheck:
    passed: bool
    reason: str


def check_open_risk_cap(
    *, open_risk_usd: float, proposed_risk_usd: float, max_open_risk_usd: float
) -> ExposureCheck:
    if max_open_risk_usd <= 0:
        return ExposureCheck(True, "no open-risk cap configured")
    total = open_risk_usd + proposed_risk_usd
    if total > max_open_risk_usd:
        return ExposureCheck(
            False,
            f"portfolio open-risk cap exceeded (${total:.2f} > ${max_open_risk_usd:.2f})")
    return ExposureCheck(True, "within open-risk cap")


def check_premium_at_risk_cap(
    *, current_premium_usd: float, incremental_premium_usd: float, max_premium_usd: float
) -> ExposureCheck:
    if max_premium_usd <= 0:
        return ExposureCheck(True, "no premium cap configured")
    total = current_premium_usd + incremental_premium_usd
    if total > max_premium_usd:
        return ExposureCheck(
            False,
            f"account premium-at-risk cap exceeded (${total:.2f} > ${max_premium_usd:.2f})")
    return ExposureCheck(True, "within premium-at-risk cap")


def check_max_concurrent_positions(
    *, open_count: int, max_concurrent: int
) -> ExposureCheck:
    if max_concurrent <= 0:
        return ExposureCheck(True, "no concurrency cap configured")
    if open_count >= max_concurrent:
        return ExposureCheck(
            False,
            f"max concurrent positions reached ({open_count}/{max_concurrent})")
    return ExposureCheck(True, "within concurrency cap")
