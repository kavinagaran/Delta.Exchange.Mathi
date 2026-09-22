"""Performance metrics with honest uncertainty (plan Phase 5).

Point estimates from a few dozen trades are close to meaningless, so every
headline number that gates a decision is reported with a bootstrap confidence
interval. A strategy whose expectancy CI straddles zero has not been shown to
work, however good its mean looks.

Pure stdlib — no numpy/scipy dependency for the engine venv.
"""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from .event_replay import BacktestTrade

BOOTSTRAP_SAMPLES = 2000
BOOTSTRAP_SEED = 20260725  # fixed: the same trades must give the same CI


@dataclass(frozen=True, slots=True)
class ConfidenceInterval:
    low: float
    high: float

    @property
    def excludes_zero(self) -> bool:
        return (self.low > 0 and self.high > 0) or (self.low < 0 and self.high < 0)


@dataclass(frozen=True, slots=True)
class Performance:
    trades: int
    wins: int
    losses: int
    win_rate: float
    total_pnl: float
    expectancy: float
    expectancy_ci: ConfidenceInterval | None
    max_drawdown: float
    profit_factor: float | None
    sharpe: float | None
    gross_profit: float
    gross_loss: float
    total_fees: float

    @property
    def is_significant(self) -> bool:
        """True only when the expectancy CI is entirely on one side of zero."""
        return self.expectancy_ci is not None and self.expectancy_ci.excludes_zero


def _f(value: Decimal | float) -> float:
    return float(value)


def bootstrap_mean_ci(values: Sequence[float], *, samples: int = BOOTSTRAP_SAMPLES,
                      confidence: float = 0.95,
                      seed: int = BOOTSTRAP_SEED) -> ConfidenceInterval | None:
    """Percentile bootstrap of the mean. None below 5 observations — a CI
    from fewer is arithmetic theatre, not evidence."""
    if len(values) < 5:
        return None
    rng = random.Random(seed)
    n = len(values)
    means = []
    for _ in range(samples):
        means.append(sum(values[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    tail = (1.0 - confidence) / 2.0
    low = means[int(tail * samples)]
    high = means[min(int((1.0 - tail) * samples), samples - 1)]
    return ConfidenceInterval(low, high)


def max_drawdown(pnls: Sequence[float]) -> float:
    """Largest peak-to-trough decline of the cumulative curve (<= 0)."""
    peak = 0.0
    cumulative = 0.0
    worst = 0.0
    for pnl in pnls:
        cumulative += pnl
        peak = max(peak, cumulative)
        worst = min(worst, cumulative - peak)
    return worst


def evaluate(trades: Sequence[BacktestTrade]) -> Performance:
    if not trades:
        return Performance(0, 0, 0, 0.0, 0.0, 0.0, None, 0.0, None, None,
                           0.0, 0.0, 0.0)
    pnls = [_f(t.pnl) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))

    sharpe: float | None = None
    if len(pnls) >= 2:
        stdev = statistics.stdev(pnls)
        if stdev > 0:
            # Per-trade Sharpe. Deliberately NOT annualised: trade frequency
            # here is irregular, and annualising it would invent precision.
            sharpe = statistics.fmean(pnls) / stdev * math.sqrt(len(pnls))

    return Performance(
        trades=len(trades),
        wins=len(wins),
        losses=len(losses),
        win_rate=len(wins) / len(trades),
        total_pnl=sum(pnls),
        expectancy=statistics.fmean(pnls),
        expectancy_ci=bootstrap_mean_ci(pnls),
        max_drawdown=max_drawdown(pnls),
        profit_factor=(gross_profit / gross_loss) if gross_loss > 0 else None,
        sharpe=sharpe,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        total_fees=sum(_f(t.fees) for t in trades),
    )


def by_regime(trades: Sequence[BacktestTrade]) -> dict[str, Performance]:
    grouped: dict[str, list[BacktestTrade]] = {}
    for trade in trades:
        grouped.setdefault(trade.regime_at_entry, []).append(trade)
    return {regime: evaluate(rows) for regime, rows in sorted(grouped.items())}
