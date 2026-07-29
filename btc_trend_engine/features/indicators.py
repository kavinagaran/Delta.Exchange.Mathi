"""Base indicators over closed candles (Trend_Engine.md §9).

Pure functions on ``Sequence[Candle]`` (oldest-first, closed only).  Floats are
used for *derived statistics* — ratios, slopes, normalized distances — never
for prices that go back to the exchange; that boundary is what keeps Decimal
discipline where it matters (money) without dragging Decimal through math it
was never needed for (indicator ratios).

Every function returns ``None`` when the window is insufficient; callers treat
``None`` as a missing feature, never as zero (§3.2).
"""

from __future__ import annotations

from typing import Sequence

from ..market_data.messages import Candle


def closes(candles: Sequence[Candle]) -> list[float]:
    return [float(c.close) for c in candles]


def ema(values: Sequence[float], period: int) -> list[float] | None:
    if period <= 0 or len(values) < period:
        return None
    weight = 2.0 / (period + 1)
    out = [sum(values[:period]) / period]
    for value in values[period:]:
        out.append(out[-1] + weight * (value - out[-1]))
    return out


def atr(candles: Sequence[Candle], period: int = 14) -> float | None:
    """Wilder-smoothed average true range."""
    if len(candles) < period + 1:
        return None
    true_ranges = []
    for previous, current in zip(candles, candles[1:]):
        high, low = float(current.high), float(current.low)
        previous_close = float(previous.close)
        true_ranges.append(max(high - low, abs(high - previous_close),
                               abs(low - previous_close)))
    value = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        value = (value * (period - 1) + tr) / period
    return value if value > 0 else None


def rsi(values: Sequence[float], period: int = 14) -> float | None:
    if len(values) < period + 1:
        return None
    gains = losses = 0.0
    for previous, current in zip(values[:period], values[1:period + 1]):
        change = current - previous
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    avg_gain, avg_loss = gains / period, losses / period
    for previous, current in zip(values[period:-1], values[period + 1:]):
        change = current - previous
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
    if avg_loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def adx(candles: Sequence[Candle], period: int = 14) -> float | None:
    """Wilder Average Directional Index, returned on its usual 0..100 scale.

    ADX measures trend *strength*, not direction.  Direction remains the job
    of price structure and RSI; the regime classifier treats ADX below 35 as
    a calm/sideways market.  ``None`` is returned until a complete Wilder
    warm-up exists rather than inventing a weak-trend reading.
    """
    if period <= 0 or len(candles) < 2 * period + 1:
        return None

    true_ranges: list[float] = []
    plus_moves: list[float] = []
    minus_moves: list[float] = []
    for previous, current in zip(candles, candles[1:]):
        high, low = float(current.high), float(current.low)
        previous_high, previous_low = float(previous.high), float(previous.low)
        previous_close = float(previous.close)
        up_move = high - previous_high
        down_move = previous_low - low
        plus_moves.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_moves.append(down_move if down_move > up_move and down_move > 0 else 0.0)
        true_ranges.append(max(high - low, abs(high - previous_close),
                               abs(low - previous_close)))

    smooth_tr = sum(true_ranges[:period])
    smooth_plus = sum(plus_moves[:period])
    smooth_minus = sum(minus_moves[:period])
    dx_values: list[float] = []

    def append_dx() -> None:
        if smooth_tr <= 0:
            return
        plus_di = 100.0 * smooth_plus / smooth_tr
        minus_di = 100.0 * smooth_minus / smooth_tr
        denominator = plus_di + minus_di
        if denominator > 0:
            dx_values.append(100.0 * abs(plus_di - minus_di) / denominator)

    append_dx()
    for index in range(period, len(true_ranges)):
        smooth_tr = smooth_tr - smooth_tr / period + true_ranges[index]
        smooth_plus = smooth_plus - smooth_plus / period + plus_moves[index]
        smooth_minus = smooth_minus - smooth_minus / period + minus_moves[index]
        append_dx()

    if len(dx_values) < period:
        return None
    value = sum(dx_values[:period]) / period
    for dx_value in dx_values[period:]:
        value = (value * (period - 1) + dx_value) / period
    return value


def directional_efficiency(values: Sequence[float], lookback: int) -> float | None:
    """Net displacement over path length, signed: +1 straight up, -1 straight
    down, ~0 churn.  (Kaufman efficiency ratio with direction retained.)"""
    if lookback < 2 or len(values) < lookback:
        return None
    window = values[-lookback:]
    net = window[-1] - window[0]
    path = sum(abs(b - a) for a, b in zip(window, window[1:]))
    if path == 0:
        return 0.0
    return net / path


def donchian_position(candles: Sequence[Candle], period: int = 20) -> float | None:
    """Close's position inside the period's high-low channel, 0..1."""
    if len(candles) < period:
        return None
    window = candles[-period:]
    highest = max(float(c.high) for c in window)
    lowest = min(float(c.low) for c in window)
    if highest == lowest:
        return 0.5
    return (float(window[-1].close) - lowest) / (highest - lowest)


def rolling_vwap(candles: Sequence[Candle], period: int) -> float | None:
    if len(candles) < period:
        return None
    window = candles[-period:]
    volume_sum = sum(float(c.volume) for c in window)
    if volume_sum <= 0:
        return None
    typical = [(float(c.high) + float(c.low) + float(c.close)) / 3 for c in window]
    return sum(t * float(c.volume) for t, c in zip(typical, window)) / volume_sum


def realized_volatility(values: Sequence[float], lookback: int) -> float | None:
    """Stdev of simple returns over the lookback (per-bar, not annualised)."""
    if lookback < 2 or len(values) < lookback + 1:
        return None
    window = values[-(lookback + 1):]
    returns = [(b - a) / a for a, b in zip(window, window[1:]) if a != 0]
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return variance ** 0.5


def percentile_rank(history: Sequence[float], value: float) -> float | None:
    """Fraction of history strictly below value, 0..1."""
    if len(history) < 8:
        return None
    below = sum(1 for h in history if h < value)
    return below / len(history)


def swing_points(candles: Sequence[Candle], strength: int = 2
                 ) -> tuple[list[int], list[int]]:
    """Indices of swing highs/lows: bars whose high/low dominates ``strength``
    neighbours on both sides.  The last ``strength`` bars can never confirm a
    swing — no future data (§3.5).

    Ties are broken toward the earlier bar: a swing must be *at least* as
    extreme as everything on its left and *strictly* more extreme than
    everything on its right.  Without this, a flat top emits one swing per
    tied bar, and two equal "swings" make a clean uptrend read as no
    structure at all.
    """
    highs: list[int] = []
    lows: list[int] = []
    for i in range(strength, len(candles) - strength):
        left = candles[i - strength: i]
        right = candles[i + 1: i + strength + 1]
        bar_high, bar_low = float(candles[i].high), float(candles[i].low)
        if (bar_high >= max(float(c.high) for c in left)
                and bar_high > max(float(c.high) for c in right)):
            highs.append(i)
        if (bar_low <= min(float(c.low) for c in left)
                and bar_low < min(float(c.low) for c in right)):
            lows.append(i)
    return highs, lows
