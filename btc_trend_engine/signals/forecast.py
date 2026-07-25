"""Magnitude/volatility forecast, transparent baseline (§10.3, §30).

Not a model in the ML sense: realized volatility projected to the horizon by
√time, expected absolute move from the same projection, jump probability as
the *empirical frequency* of past standardized bar moves beyond a threshold.
Every number is a description of recent history, stated as such — calibrated
probabilistic models are Phase 6, and nothing here pretends otherwise.

Expected *return* (directional drift) is deliberately None in v1: publishing a
drift estimate a walk-forward has never validated would hand the MOVE policy a
fake edge number (§16 uses drift_used = drift × confidence). Absent is honest;
zero would be a claim.
"""

from __future__ import annotations

import math
from typing import Sequence

from ..features.indicators import realized_volatility


def forecast_from_closes(
    closes: Sequence[float],
    *,
    bar_seconds: int = 300,
    horizon_seconds: int = 900,
    vol_lookback: int = 96,
    jump_threshold: float = 3.0,
) -> dict[str, float | None]:
    volatility = realized_volatility(closes, vol_lookback)
    if volatility is None or volatility <= 0:
        return {"expected_return_bps": None,
                "expected_absolute_move_bps": None,
                "forecast_volatility_bps": None,
                "jump_probability": None}
    bars = max(horizon_seconds / bar_seconds, 1.0)
    horizon_vol = volatility * math.sqrt(bars)
    horizon_vol_bps = horizon_vol * 10_000.0
    # E|X| for a centred normal is σ·√(2/π); stated as an approximation of
    # recent behaviour, not a distributional claim about the future.
    expected_abs_bps = horizon_vol_bps * math.sqrt(2.0 / math.pi)

    window = closes[-(vol_lookback + 1):]
    returns = [(b - a) / a for a, b in zip(window, window[1:]) if a != 0]
    jumps = sum(1 for r in returns if abs(r) > jump_threshold * volatility)
    jump_probability = jumps / len(returns) if returns else None

    return {
        "expected_return_bps": None,  # v1: no validated drift model
        "expected_absolute_move_bps": round(expected_abs_bps, 1),
        "forecast_volatility_bps": round(horizon_vol_bps, 1),
        "jump_probability": (round(jump_probability, 4)
                             if jump_probability is not None else None),
    }
