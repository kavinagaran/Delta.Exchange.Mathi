"""Does each score component actually predict anything, at the horizon we hold?

The trend score sums six components with fixed weights (``signals.score``),
but nothing has ever measured what any of them predicts.  Two questions the
weights currently answer by assertion:

1. ``higher_timeframe_trend`` carries 40% of the weight and is built from 4H
   and 1H structure -- while positions are held for hours.  A feature can be
   genuinely informative at its own horizon and worthless at ours.
2. Four of the six components are monotone functions of the same recent price
   path, so the "ensemble" may be one opinion counted four times.  Correlated
   components make a weighted sum overconfident, not diversified.

This module answers both from public candle history alone: no trade records,
no option chain, no credentials.  It imports the *production* feature and
score code rather than reimplementing it, so what it measures is what the
engine actually computes.

Method.  At each decision bar it rebuilds the exact inputs the engine would
have seen, records every component's raw value, and pairs them with forward
returns measured from the next bar's open -- the earliest price an order
could realistically fill at.  Then:

* **IC** (information coefficient) = Spearman rank correlation between a
  component and forward return.  Rank-based because option-relevant moves are
  fat-tailed and Pearson would let a handful of bars set the answer.
* **Component correlation** = how much the components repeat each other.
* **Calibration** = what a given score band actually implied, historically.

Overlap warning, handled explicitly.  Decisions are 5 minutes apart but
horizons run to hours, so consecutive observations share most of their
forward window.  That autocorrelation does not bias IC, but it inflates
apparent significance enormously -- a naive t-stat on 20,000 overlapping
samples is meaningless.  Every significance figure here is therefore computed
on a *non-overlapping* subsample (stride = horizon), and the honest sample
size is reported next to it.

Look-ahead safety mirrors ``backtest.event_replay``: a higher-timeframe
candle becomes visible only once it has fully closed at or before the
decision bar's own close.  The resampler is imported from there rather than
rewritten, because a study that resampled differently from the backtester
would be measuring a different signal.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..backtest.event_replay import ROLES, _resample
from ..features.pipeline import compute_timeframe_features
from ..market_data.messages import Candle
from ..market_data.normalizer import resolution_seconds
from ..signals.producer import TRIGGER
from ..signals.score import V1_WEIGHTS, compute_score

SCORE_KEY = "trend_score"

# Enough history for the slowest feature (4H EMA-50 plus ATR) to be defined.
DEFAULT_FEATURE_WINDOW = 240
DEFAULT_WARMUP = 2_880          # 5m bars: 10 days, so 4H features exist


@dataclass(frozen=True, slots=True)
class Observation:
    """One decision bar: what the engine saw, and what happened next."""

    at: datetime
    components: Mapping[str, float]        # component -> raw value in [-1, 1]
    score: float                           # -100 .. 100
    forward_returns: Mapping[int, float]   # horizon minutes -> return


# ── statistics (stdlib only; no scipy dependency for a research script) ──

def _ranks(values: Sequence[float]) -> list[float]:
    """Average ranks, so ties cannot manufacture correlation."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    position = 0
    while position < len(order):
        end = position
        while (end + 1 < len(order)
               and values[order[end + 1]] == values[order[position]]):
            end += 1
        shared = (position + end) / 2.0 + 1.0
        for index in range(position, end + 1):
            ranks[order[index]] = shared
        position = end + 1
    return ranks


def pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    n = len(xs)
    if n < 3 or n != len(ys):
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    dx = [x - mean_x for x in xs]
    dy = [y - mean_y for y in ys]
    denominator = math.sqrt(sum(a * a for a in dx) * sum(b * b for b in dy))
    if denominator <= 0:
        return None
    return sum(a * b for a, b in zip(dx, dy, strict=True)) / denominator


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    return pearson(_ranks(xs), _ranks(ys))


def ic_t_stat(ic: float, n: int) -> float | None:
    """t = IC * sqrt((n-2)/(1-IC^2)). Only meaningful on independent samples.

    A perfect |IC| = 1 is clamped rather than rejected: the t-statistic
    diverges there, and returning None would report the most extreme possible
    relationship as "not significant". In real data a perfect IC means a
    look-ahead leak, so it should scream, not go quiet.
    """
    if n < 4:
        return None
    bounded = max(-0.999_999_999, min(0.999_999_999, ic))
    return bounded * math.sqrt((n - 2) / (1.0 - bounded * bounded))


@dataclass(frozen=True, slots=True)
class ICResult:
    component: str
    horizon_minutes: int
    ic: float | None                # all (overlapping) observations
    n_overlapping: int
    ic_independent: float | None    # non-overlapping subsample
    n_independent: int
    t_stat: float | None            # from the independent subsample only

    @property
    def significant(self) -> bool:
        """|t| > 2 on independent samples -- a screen, not a proof."""
        return self.t_stat is not None and abs(self.t_stat) > 2.0


# ── building observations from candles ──────────────────────────────────

def collect_observations(
    base_candles: Sequence[Candle],
    *,
    horizons_minutes: Sequence[int],
    warmup: int = DEFAULT_WARMUP,
    feature_window: int = DEFAULT_FEATURE_WINDOW,
    derivatives: Mapping[str, float] | None = None,
    symbol: str = "BTCUSD",
) -> list[Observation]:
    """Replay the engine's own scoring over history, pairing it with outcomes.

    ``base_candles`` must be the 5-minute series, ascending. Forward returns
    are measured open-to-open starting at the bar *after* the decision, which
    is the first price an order placed on that signal could have touched.
    """
    ordered = sorted(base_candles, key=lambda c: c.start)
    if len(ordered) <= warmup + 2:
        return []

    step_seconds = resolution_seconds(TRIGGER)
    step_minutes = step_seconds // 60
    for horizon in horizons_minutes:
        if horizon <= 0 or horizon % step_minutes:
            raise ValueError(
                f"horizon {horizon}m must be a positive multiple of {step_minutes}m")

    full_series = {role: _resample(ordered, role, symbol) for role in ROLES}
    series_starts = {role: [c.start for c in candles]
                     for role, candles in full_series.items()}
    opens = [float(c.open) for c in ordered]
    max_offset = max(horizon // step_minutes for horizon in horizons_minutes)

    observations: list[Observation] = []
    # Stop early enough that the longest horizon still has a real price.
    for index in range(warmup, len(ordered) - 1 - max_offset):
        decision_close = ordered[index].start + timedelta(seconds=step_seconds)

        window: dict[str, Sequence[Candle]] = {}
        for role in ROLES:
            cutoff = decision_close - timedelta(seconds=resolution_seconds(role))
            end = bisect_right(series_starts[role], cutoff)
            window[role] = full_series[role][max(0, end - feature_window):end]

        features = {role: compute_timeframe_features(role, window[role])
                    for role in ROLES}
        result = compute_score(
            structural=features[ROLES[0]], primary=features[ROLES[1]],
            setup=features[ROLES[2]], trigger=features[ROLES[3]],
            derivatives=dict(derivatives or {}),
        )
        if result.trend_score is None:
            continue

        components = {c.name: (c.score / 100.0) for c in result.components
                      if c.score is not None}
        if not components:
            continue

        entry = opens[index + 1]
        if entry <= 0:
            continue
        forward = {}
        for horizon in horizons_minutes:
            target = index + 1 + horizon // step_minutes
            forward[horizon] = opens[target] / entry - 1.0

        observations.append(Observation(
            at=ordered[index].start, components=components,
            score=float(result.trend_score), forward_returns=forward))
    return observations


# ── the three questions ─────────────────────────────────────────────────

def _series(observations: Sequence[Observation], key: str,
            horizon: int) -> tuple[list[float], list[float]]:
    xs: list[float] = []
    ys: list[float] = []
    for obs in observations:
        value = (obs.score if key == SCORE_KEY else obs.components.get(key))
        outcome = obs.forward_returns.get(horizon)
        if value is None or outcome is None:
            continue
        xs.append(float(value))
        ys.append(float(outcome))
    return xs, ys


def information_coefficient(observations: Sequence[Observation], key: str,
                            horizon_minutes: int,
                            *, bar_minutes: int = 5) -> ICResult:
    """IC for one component at one horizon, with an honest significance test."""
    xs, ys = _series(observations, key, horizon_minutes)
    ic = spearman(xs, ys)

    # Independent subsample: one observation per forward window.
    stride = max(1, horizon_minutes // bar_minutes)
    xs_i, ys_i = xs[::stride], ys[::stride]
    ic_independent = spearman(xs_i, ys_i)
    t_stat = (ic_t_stat(ic_independent, len(xs_i))
              if ic_independent is not None else None)

    return ICResult(component=key, horizon_minutes=horizon_minutes, ic=ic,
                    n_overlapping=len(xs), ic_independent=ic_independent,
                    n_independent=len(xs_i), t_stat=t_stat)


def information_coefficients(
    observations: Sequence[Observation],
    horizons_minutes: Sequence[int],
    *, keys: Sequence[str] | None = None, bar_minutes: int = 5,
) -> list[ICResult]:
    names = list(keys) if keys else [*V1_WEIGHTS.keys(), SCORE_KEY]
    return [information_coefficient(observations, key, horizon,
                                    bar_minutes=bar_minutes)
            for key in names for horizon in horizons_minutes]


def component_correlation(
    observations: Sequence[Observation],
    *, keys: Sequence[str] | None = None,
) -> dict[tuple[str, str], float]:
    """Pairwise Spearman between components.

    High values mean the weighted sum is one opinion counted several times:
    the weights then describe emphasis, not independent evidence.
    """
    names = [k for k in (keys or V1_WEIGHTS.keys())
             if any(k in obs.components for obs in observations)]
    matrix: dict[tuple[str, str], float] = {}
    for i, left in enumerate(names):
        for right in names[i + 1:]:
            xs: list[float] = []
            ys: list[float] = []
            for obs in observations:
                a, b = obs.components.get(left), obs.components.get(right)
                if a is None or b is None:
                    continue
                xs.append(a)
                ys.append(b)
            value = spearman(xs, ys)
            if value is not None:
                matrix[(left, right)] = value
    return matrix


@dataclass(frozen=True, slots=True)
class CalibrationBucket:
    low: float
    high: float
    n: int
    mean_return: float
    median_return: float
    hit_rate: float          # share with a favourable move, signed by the band

    @property
    def label(self) -> str:
        return f"{self.low:+.0f}..{self.high:+.0f}"


def score_calibration(
    observations: Sequence[Observation], horizon_minutes: int,
    *, edges: Sequence[float] = (-100, -35, -15, 15, 35, 100),
) -> list[CalibrationBucket]:
    """What each score band actually implied, historically.

    ``hit_rate`` is signed by the band: for a positive band it is the share of
    positive forward returns, for a negative band the share of negative ones,
    so 0.5 always means "no better than a coin toss" regardless of direction.
    The default edges are the live zone boundaries, so the output says
    directly whether the thresholds in use separate anything.
    """
    buckets: list[CalibrationBucket] = []
    for low, high in zip(edges, edges[1:], strict=False):
        returns = [obs.forward_returns[horizon_minutes] for obs in observations
                   if low <= obs.score < high
                   and horizon_minutes in obs.forward_returns]
        if not returns:
            buckets.append(CalibrationBucket(low, high, 0, 0.0, 0.0, 0.0))
            continue
        ordered = sorted(returns)
        middle = len(ordered) // 2
        median = (ordered[middle] if len(ordered) % 2
                  else (ordered[middle - 1] + ordered[middle]) / 2.0)
        favourable = (sum(1 for r in returns if r > 0) if high > 0
                      else sum(1 for r in returns if r < 0))
        buckets.append(CalibrationBucket(
            low=low, high=high, n=len(returns),
            mean_return=sum(returns) / len(returns), median_return=median,
            hit_rate=favourable / len(returns)))
    return buckets
