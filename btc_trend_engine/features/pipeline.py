"""Per-timeframe feature computation (§9.1–§9.3, §9.5, §9.6).

One entry point — :func:`compute_timeframe_features` — turns a closed-candle
series into a flat ``dict[str, float]``.  Missing features are *absent from
the dict* and listed in ``missing``; a consumer that treats absence as zero
has a bug the property tests catch (§3.2).

Order-flow features (§9.4) are deliberately not computed here: they carry
weight 0 in v1 (ADR 0004) and will be added once recorded microstructure
history exists to validate them against.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ..market_data.messages import Candle
from . import indicators as ind

FEATURE_SET_VERSION = "v1.1.0"


@dataclass(frozen=True, slots=True)
class TimeframeFeatures:
    timeframe: str
    features: dict[str, float]
    missing: list[str]

    def get(self, name: str) -> float | None:
        return self.features.get(name)


def compute_timeframe_features(
    timeframe: str,
    candles: Sequence[Candle],
    *,
    ema_fast: int = 20,
    ema_slow: int = 50,
    atr_period: int = 14,
    donchian_period: int = 20,
    efficiency_lookback: int = 20,
    vol_short: int = 12,
    vol_long: int = 48,
    vwap_period: int = 48,
    swing_strength: int = 2,
) -> TimeframeFeatures:
    features: dict[str, float] = {}
    missing: list[str] = []

    def put(name: str, value: float | None) -> None:
        if value is None or value != value:  # NaN guard
            missing.append(name)
        else:
            features[name] = float(value)

    closes = ind.closes(candles)
    atr_value = ind.atr(candles, atr_period)
    put("atr", atr_value)

    # ── trend (§9.1) ─────────────────────────────────────────────────────
    fast = ind.ema(closes, ema_fast)
    slow = ind.ema(closes, ema_slow)
    if fast and slow and atr_value:
        put("ema_distance_atr", (fast[-1] - slow[-1]) / atr_value)
        slope_bars = min(5, len(fast) - 1)
        if slope_bars >= 1:
            put("ema_slope_atr", (fast[-1] - fast[-1 - slope_bars]) / atr_value)
        else:
            missing.append("ema_slope_atr")
    else:
        missing.extend(["ema_distance_atr", "ema_slope_atr"])
    put("directional_efficiency",
        ind.directional_efficiency(closes, efficiency_lookback))
    put("donchian_position", ind.donchian_position(candles, donchian_period))
    vwap = ind.rolling_vwap(candles, vwap_period)
    if vwap is not None and atr_value and closes:
        put("vwap_distance_atr", (closes[-1] - vwap) / atr_value)
    else:
        missing.append("vwap_distance_atr")
    put("rsi", ind.rsi(closes))
    put("adx", ind.adx(candles))

    # ── market structure (§9.1/§9.2) ─────────────────────────────────────
    _structure_features(candles, atr_value, put, swing_strength)

    # ── breakout quality (§9.2) ──────────────────────────────────────────
    _breakout_features(candles, atr_value, donchian_period, put)

    # ── volatility & jump (§9.6) ─────────────────────────────────────────
    vol_short_value = ind.realized_volatility(closes, vol_short)
    vol_long_value = ind.realized_volatility(closes, vol_long)
    put("realized_vol_short", vol_short_value)
    put("realized_vol_long", vol_long_value)
    if vol_short_value is not None and vol_long_value:
        put("vol_ratio", vol_short_value / vol_long_value)
    else:
        missing.append("vol_ratio")
    _jump_feature(candles, closes, put)

    return TimeframeFeatures(timeframe=timeframe, features=features,
                             missing=sorted(set(missing)))


def _structure_features(candles: Sequence[Candle], atr_value: float | None,
                        put: Any, swing_strength: int) -> None:
    highs_idx, lows_idx = ind.swing_points(candles, swing_strength)
    if len(highs_idx) < 2 or len(lows_idx) < 2:
        put("structure_bias", None)
        put("last_swing_low", None)
        put("last_swing_high", None)
        return
    h1, h2 = (float(candles[i].high) for i in highs_idx[-2:])
    l1, l2 = (float(candles[i].low) for i in lows_idx[-2:])
    higher_highs = h2 > h1
    higher_lows = l2 > l1
    lower_highs = h2 < h1
    lower_lows = l2 < l1
    if higher_highs and higher_lows:
        bias = 1.0
    elif lower_highs and lower_lows:
        bias = -1.0
    else:
        bias = 0.0
    put("structure_bias", bias)
    put("last_swing_high", h2)
    put("last_swing_low", l2)


def _breakout_features(candles: Sequence[Candle], atr_value: float | None,
                       donchian_period: int, put: Any) -> None:
    if len(candles) < donchian_period + 1 or not atr_value:
        put("breakout_direction", None)
        put("breakout_body_atr", None)
        put("close_location", None)
        put("volume_ratio", None)
        return
    *history, last = candles[-(donchian_period + 1):]
    prior_high = max(float(c.high) for c in history)
    prior_low = min(float(c.low) for c in history)
    close = float(last.close)
    direction = 1.0 if close > prior_high else -1.0 if close < prior_low else 0.0
    put("breakout_direction", direction)
    body = abs(close - float(last.open))
    put("breakout_body_atr", body / atr_value)
    high, low = float(last.high), float(last.low)
    put("close_location", 0.5 if high == low else (close - low) / (high - low))
    volumes = [float(c.volume) for c in history]
    median_volume = sorted(volumes)[len(volumes) // 2]
    put("volume_ratio",
        float(last.volume) / median_volume if median_volume > 0 else None)


def _jump_feature(candles: Sequence[Candle], closes: Sequence[float],
                  put: Any) -> None:
    """Last return standardised by recent return scale — a cheap jump score."""
    vol = ind.realized_volatility(closes, 48)
    if vol is None or vol == 0 or len(closes) < 2 or closes[-2] == 0:
        put("jump_score", None)
        return
    last_return = (closes[-1] - closes[-2]) / closes[-2]
    put("jump_score", abs(last_return) / vol)


def derivatives_features(ticker: Mapping[str, Any],
                         funding_history: Sequence[float] = ()) -> dict[str, float]:
    """§9.5 context from the live v2/ticker payload (verified fields, A7)."""
    out: dict[str, float] = {}

    def number(key: str) -> float | None:
        value = ticker.get(key)
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        return result if result == result else None

    mark, spot = number("mark_price"), number("spot_price")
    if mark is not None and spot not in (None, 0.0):
        out["mark_spot_basis_pct"] = (mark - spot) / spot * 100.0
    funding = number("funding_rate")
    if funding is not None:
        out["funding_rate"] = funding
        rank = ind.percentile_rank(list(funding_history), funding)
        if rank is not None:
            out["funding_percentile"] = rank
    oi_change = number("oi_change_usd_6h")
    oi_value = number("oi_value_usd")
    if oi_change is not None and oi_value not in (None, 0.0):
        out["oi_change_6h_pct"] = oi_change / oi_value * 100.0
    return out
