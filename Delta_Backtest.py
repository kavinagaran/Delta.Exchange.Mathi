"""Production-faithful backtest for the BTC multi-timeframe Trend strategy.

The live strategy enters a liquid ITM call when the completed 5-minute and
15-minute candles and the 1-hour candle all point UP, and an ITM put when all
three point DOWN. Each timeframe uses EMA(9), EMA(21), price versus EMA(21),
Wilder RSI(14), and the configured production filters. With historical option
quotes, contract selection prefers the configured delta; the synthetic-only
fallback retains the historical two-step-ITM approximation.

Important data-quality note
---------------------------
Real option bid/ask history can be supplied with ``--options-csv``.  When it is
not supplied (or a quote is missing), pricing uses an explicitly labelled
Black-Scholes fallback with configurable synthetic spread and slippage.  That
fallback is useful for screening, but it is not evidence of executable option
performance.

The module remains importable and ``run_backtest(df)`` remains the primary API.
Running it without arguments still fetches BTCUSD candles from Delta; CSV input
is available so research and tests can be completely offline.
"""

from __future__ import annotations

import argparse
import math
import os
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------------------------
# Configuration and backwards-compatible module constants
# ---------------------------------------------------------------------------
BASE_URL = os.getenv("BASE_URL", "https://api.india.delta.exchange")
PERPETUAL_SYMBOL = os.getenv("PERPETUAL_SYMBOL", "BTCUSD")
BACKTEST_START = int(
    os.getenv(
        "BACKTEST_START",
        int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()),
    )
)
BACKTEST_END = int(
    os.getenv(
        "BACKTEST_END",
        int(datetime(2026, 6, 20, 23, 59, 59, tzinfo=timezone.utc).timestamp()),
    )
)
RESOLUTION = "5m"
RES_SECONDS = 300
BATCH_SIZE = 500
OUTPUT_CSV = os.getenv(
    "BACKTEST_OUTPUT_CSV", str(Path(__file__).with_name("backtest_2026.csv"))
)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class BacktestConfig:
    """All assumptions that can materially change the backtest result."""

    ema_fast: int = 9
    ema_slow: int = 21
    rsi_period: int = 14
    rsi_up: float = 55.0
    rsi_down: float = 45.0
    minimum_indicator_bars: int = 40
    ema_gap_pct: float = 0.05
    trend_15m_slope_bars: int = 3
    trend_15m_min_slope_pct: float = 0.0
    trend_adx_period: int = 14
    trend_adx_min: float = 18.0
    hourly_confirm_samples: int = 2
    hourly_mode: str = "live"  # "live" approximates the in-progress 1H close.

    strike_step: float = 1_000.0
    itm_steps: int = 2
    order_size: int = 1_000
    contract_value: float = 0.001
    min_time_to_expiry_hours: float = 4.0
    target_delta: float = 0.65
    max_option_spread_pct: float = 12.0
    min_option_depth_lots: float = 10.0
    allow_missing_option_book: bool = False

    take_profit_usd: float = 100.0
    stop_loss_usd: float = 50.0
    tsl_arm_usd: float = 50.0
    tsl_trail_usd: float = 50.0

    iv_floor: float = 0.60
    realised_vol_window: int = 288  # One day of 5-minute crypto candles.
    synthetic_spread_pct: float = 0.02
    slippage_pct: float = 0.0025
    option_fee_rate: float = 0.0001
    option_fee_cap_pct: float = 0.035
    max_option_quote_age_seconds: int = 900
    force_close_at_end: bool = True

    walk_forward_folds: int = 3
    walk_forward_train_fraction: float = 0.50
    validation_min_trades: int = 100
    validation_min_profit_factor: float = 1.20
    validation_max_drawdown_usd: float = 500.0

    @classmethod
    def from_env(cls) -> "BacktestConfig":
        legacy_tsl = float(os.getenv("TREND_TSL_USD", "50"))
        cfg = cls(
            ema_fast=int(os.getenv("TREND_EMA_FAST", "9")),
            ema_slow=int(os.getenv("TREND_EMA_SLOW", "21")),
            rsi_period=int(os.getenv("TREND_RSI_PERIOD", "14")),
            rsi_up=float(os.getenv("TREND_RSI_UP", "55")),
            rsi_down=float(os.getenv("TREND_RSI_DOWN", "45")),
            minimum_indicator_bars=int(os.getenv("TREND_MIN_INDICATOR_BARS", "40")),
            ema_gap_pct=float(os.getenv("TREND_EMA_GAP_PCT", "0.05")),
            trend_15m_slope_bars=int(os.getenv("TREND_15M_SLOPE_BARS", "3")),
            trend_15m_min_slope_pct=float(os.getenv("TREND_MIN_15M_SLOPE_PCT", "0")),
            trend_adx_period=int(os.getenv("TREND_ADX_PERIOD", "14")),
            trend_adx_min=float(os.getenv("TREND_ADX_MIN", "18")),
            hourly_confirm_samples=int(os.getenv("TREND_1H_CONFIRM_SAMPLES", "2")),
            hourly_mode=os.getenv("BACKTEST_1H_MODE", "live").strip().lower(),
            strike_step=float(os.getenv("STRIKE_STEP", "1000")),
            itm_steps=int(os.getenv("TREND_ITM_STEPS", "2")),
            order_size=int(os.getenv("TREND_LOTS", os.getenv("ORDER_SIZE", "1000"))),
            contract_value=float(os.getenv("OPTION_CONTRACT_VALUE", "0.001")),
            min_time_to_expiry_hours=float(os.getenv("TREND_MIN_TTE_HOURS", "4")),
            target_delta=float(os.getenv("TREND_TARGET_DELTA", "0.65")),
            max_option_spread_pct=float(os.getenv("TREND_MAX_SPREAD_PCT", "12")),
            min_option_depth_lots=float(os.getenv("TREND_MIN_BOOK_DEPTH_LOTS", "10")),
            allow_missing_option_book=_env_bool("TREND_ALLOW_MISSING_BOOK", False),
            take_profit_usd=float(os.getenv("TREND_TP_USD", "100")),
            stop_loss_usd=float(os.getenv("TREND_SL_USD", "50")),
            tsl_arm_usd=float(os.getenv("TREND_TSL_ARM_USD", str(legacy_tsl))),
            tsl_trail_usd=float(os.getenv("TREND_TSL_TRAIL_USD", str(legacy_tsl))),
            iv_floor=float(os.getenv("BACKTEST_IV_FLOOR", "0.60")),
            realised_vol_window=int(os.getenv("BACKTEST_RVOL_WINDOW", "288")),
            synthetic_spread_pct=float(os.getenv("BACKTEST_SPREAD_PCT", "0.02")),
            slippage_pct=float(os.getenv("BACKTEST_SLIPPAGE_PCT", "0.0025")),
            option_fee_rate=float(os.getenv("OPTION_FEE_RATE", "0.0001")),
            option_fee_cap_pct=float(os.getenv("OPTION_FEE_CAP_PCT", "0.035")),
            max_option_quote_age_seconds=int(os.getenv("BACKTEST_QUOTE_MAX_AGE", "900")),
            force_close_at_end=_env_bool("BACKTEST_FORCE_CLOSE_END", True),
            walk_forward_folds=int(os.getenv("BACKTEST_WF_FOLDS", "3")),
            walk_forward_train_fraction=float(os.getenv("BACKTEST_WF_TRAIN_FRACTION", "0.50")),
            validation_min_trades=int(os.getenv("BACKTEST_MIN_TRADES", "100")),
            validation_min_profit_factor=float(os.getenv("BACKTEST_MIN_PROFIT_FACTOR", "1.20")),
            validation_max_drawdown_usd=float(os.getenv("BACKTEST_MAX_DRAWDOWN_USD", "500")),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.hourly_mode not in {"live", "closed"}:
            raise ValueError("hourly_mode must be 'live' or 'closed'")
        if min(self.ema_fast, self.ema_slow, self.rsi_period, self.order_size) <= 0:
            raise ValueError("indicator periods and order_size must be positive")
        if self.ema_fast >= self.ema_slow:
            raise ValueError("ema_fast must be smaller than ema_slow")
        if self.trend_15m_slope_bars <= 0 or self.trend_adx_period <= 0:
            raise ValueError("slope and ADX periods must be positive")
        if self.hourly_confirm_samples <= 0:
            raise ValueError("hourly_confirm_samples must be positive")
        if not 0 < self.target_delta < 1:
            raise ValueError("target_delta must be between 0 and 1")
        if self.contract_value <= 0 or self.strike_step <= 0:
            raise ValueError("contract_value and strike_step must be positive")
        if not 0 <= self.synthetic_spread_pct < 1 or not 0 <= self.slippage_pct < 1:
            raise ValueError("spread and slippage percentages must be in [0, 1)")
        if not 0 < self.walk_forward_train_fraction < 1:
            raise ValueError("walk_forward_train_fraction must be between 0 and 1")


# Legacy names kept for notebooks importing the old module.
RSI_PERIOD = 14
RSI_BULLISH = 50.0
RSI_BEARISH = 50.0
EMA_PERIOD = 21
STRIKE_STEP = int(os.getenv("STRIKE_STEP", "1000"))
ORDER_SIZE = int(os.getenv("ORDER_SIZE", "1000"))
SL_PCT = float(os.getenv("SL_PCT", "0.50"))
IV_FLOOR = float(os.getenv("BACKTEST_IV_FLOOR", "0.60"))
WARMUP_BARS = 40


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def _dt(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _resolution_seconds(resolution: str) -> int:
    unit = resolution[-1].lower()
    amount = int(resolution[:-1])
    return amount * {"m": 60, "h": 3600, "d": 86400}[unit]


def fetch_all_candles(
    symbol: str, resolution: str, start_ts: int, end_ts: int
) -> pd.DataFrame:
    """Fetch candles from Delta. Tests and offline research should pass a CSV."""
    all_rows: list[dict[str, Any]] = []
    cursor = start_ts
    resolution_seconds = _resolution_seconds(resolution)

    print(f"\nFetching {resolution} candles  {_dt(start_ts)} -> {_dt(end_ts)}")
    print("-" * 56)
    while cursor < end_ts:
        batch_end = min(cursor + resolution_seconds * BATCH_SIZE, end_ts)
        try:
            response = requests.get(
                f"{BASE_URL}/v2/history/candles",
                params={
                    "symbol": symbol,
                    "resolution": resolution,
                    "start": cursor,
                    "end": batch_end,
                },
                timeout=(5, 30),
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            print(f"  Request error ({_dt(cursor)}): {exc} - retrying in 5s")
            time.sleep(5)
            continue

        rows = payload.get("result") or []
        all_rows.extend(rows)
        print(f"  {_dt(cursor):19s} -> {_dt(batch_end):19s}  ({len(rows):3d} candles)")
        cursor = batch_end + resolution_seconds
        time.sleep(0.3)

    if not all_rows:
        raise RuntimeError("No candle data returned. Check API endpoint or date range.")
    return normalise_candles(pd.DataFrame(all_rows))


def _timestamps_to_seconds(values: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    result = numeric.copy()
    non_numeric = numeric.isna() & values.notna()
    if non_numeric.any():
        parsed = pd.to_datetime(values[non_numeric], utc=True, errors="coerce")
        # Do not assume pandas' internal datetime unit (it may be ns, us, or s
        # depending on the input and pandas version).
        result.loc[non_numeric] = parsed.map(
            lambda value: value.timestamp() if pd.notna(value) else np.nan
        )
    # Support millisecond and microsecond Unix timestamps.
    result = result.where(result.abs() < 10_000_000_000, result / 1_000)
    result = result.where(result.abs() < 10_000_000_000, result / 1_000)
    result = result.where(result.abs() < 10_000_000_000, result / 1_000)
    return result


def normalise_candles(df: pd.DataFrame) -> pd.DataFrame:
    """Return sorted numeric OHLCV candles with Unix-second timestamps."""
    data = df.copy()
    if "timestamp" not in data and "time" in data:
        data.rename(columns={"time": "timestamp"}, inplace=True)
    required = {"timestamp", "close"}
    missing = required - set(data.columns)
    if missing:
        raise ValueError(f"Candle data is missing required columns: {sorted(missing)}")
    data["timestamp"] = _timestamps_to_seconds(data["timestamp"])
    for name in ("open", "high", "low", "close", "volume"):
        if name not in data:
            data[name] = data["close"] if name != "volume" else 0.0
        data[name] = pd.to_numeric(data[name], errors="coerce")
    data.dropna(subset=["timestamp", "close"], inplace=True)
    data["timestamp"] = data["timestamp"].astype("int64")
    data.sort_values("timestamp", inplace=True)
    data.drop_duplicates("timestamp", keep="last", inplace=True)
    data.reset_index(drop=True, inplace=True)
    if len(data) > 1 and data["timestamp"].diff().dropna().median() > RES_SECONDS:
        raise ValueError(
            "Trend backtesting requires 5-minute source candles; 15-minute data "
            "cannot reconstruct the production 5-minute signal."
        )
    return data


# ---------------------------------------------------------------------------
# Indicators: deliberately mirror dashboard._ema and dashboard._rsi
# ---------------------------------------------------------------------------
def ema_series(close: pd.Series, period: int) -> pd.Series:
    values = pd.to_numeric(close, errors="coerce").to_numpy(dtype=float)
    out = np.full(len(values), np.nan, dtype=float)
    if len(values) < period:
        return pd.Series(out, index=close.index)
    out[period - 1] = np.mean(values[:period])
    alpha = 2.0 / (period + 1)
    for i in range(period, len(values)):
        out[i] = values[i] * alpha + out[i - 1] * (1.0 - alpha)
    return pd.Series(out, index=close.index)


def _wilder_rsi_components(close: pd.Series, period: int) -> tuple[pd.Series, pd.Series]:
    values = pd.to_numeric(close, errors="coerce").to_numpy(dtype=float)
    avg_gain = np.full(len(values), np.nan, dtype=float)
    avg_loss = np.full(len(values), np.nan, dtype=float)
    if len(values) <= period:
        return pd.Series(avg_gain, index=close.index), pd.Series(avg_loss, index=close.index)
    changes = np.diff(values)
    avg_gain[period] = np.maximum(changes[:period], 0.0).mean()
    avg_loss[period] = np.maximum(-changes[:period], 0.0).mean()
    for i in range(period + 1, len(values)):
        delta = values[i] - values[i - 1]
        avg_gain[i] = (avg_gain[i - 1] * (period - 1) + max(delta, 0.0)) / period
        avg_loss[i] = (avg_loss[i - 1] * (period - 1) + max(-delta, 0.0)) / period
    return pd.Series(avg_gain, index=close.index), pd.Series(avg_loss, index=close.index)


def rsi_series(close: pd.Series, period: int) -> pd.Series:
    """Wilder RSI with the same seed and zero-loss handling as the live app."""
    avg_gain, avg_loss = _wilder_rsi_components(close, period)
    rs = avg_gain / avg_loss
    result = 100.0 - 100.0 / (1.0 + rs)
    result = result.mask(avg_loss == 0, 100.0)
    return result


def adx_series(
    highs: pd.Series, lows: pd.Series, closes: pd.Series, period: int = 14
) -> pd.Series:
    """Wilder ADX with the same initialization as dashboard._adx."""
    high = pd.to_numeric(highs, errors="coerce").to_numpy(dtype=float)
    low = pd.to_numeric(lows, errors="coerce").to_numpy(dtype=float)
    close = pd.to_numeric(closes, errors="coerce").to_numpy(dtype=float)
    out = np.zeros(len(close), dtype=float)
    if len(close) < period * 2 + 1:
        return pd.Series(out, index=closes.index)

    true_range: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for i in range(1, len(close)):
        up = high[i] - high[i - 1]
        down = low[i - 1] - low[i]
        true_range.append(
            max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
        )
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)

    atr = sum(true_range[:period])
    plus = sum(plus_dm[:period])
    minus = sum(minus_dm[:period])
    dx: list[float] = []
    dx_close_indices: list[int] = []
    for i in range(period - 1, len(true_range)):
        if i >= period:
            atr = atr - atr / period + true_range[i]
            plus = plus - plus / period + plus_dm[i]
            minus = minus - minus / period + minus_dm[i]
        pdi = 100.0 * plus / atr if atr else 0.0
        mdi = 100.0 * minus / atr if atr else 0.0
        denominator = pdi + mdi
        dx.append(100.0 * abs(pdi - mdi) / denominator if denominator else 0.0)
        dx_close_indices.append(i + 1)

    # The live function requires 2*n+1 closes. At that first eligible close it
    # seeds from n DX values and then applies the newest DX value once.
    if len(dx) > period:
        running = sum(dx[:period]) / period
        for position in range(period, len(dx)):
            running = (running * (period - 1) + dx[position]) / period
            out[dx_close_indices[position]] = running
    return pd.Series(out, index=closes.index)


def _trend_metric_frame(
    closes: pd.Series,
    config: BacktestConfig,
    highs: pd.Series | None = None,
    lows: pd.Series | None = None,
) -> pd.DataFrame:
    fast = ema_series(closes, config.ema_fast)
    slow = ema_series(closes, config.ema_slow)
    rsi = rsi_series(closes, config.rsi_period)
    gap = (fast - slow).abs() / closes.replace(0, np.nan) * 100.0
    adx = (
        adx_series(highs, lows, closes, config.trend_adx_period)
        if highs is not None and lows is not None
        else pd.Series(np.nan, index=closes.index)
    )
    trend = np.where(
        (fast > slow)
        & (closes > slow)
        & (rsi >= config.rsi_up)
        & (gap >= config.ema_gap_pct),
        "up",
        np.where(
            (fast < slow)
            & (closes < slow)
            & (rsi <= config.rsi_down)
            & (gap >= config.ema_gap_pct),
            "down",
            "neutral",
        ),
    )
    frame = pd.DataFrame(
        {
            "close": closes,
            "ema9": fast,
            "ema21": slow,
            "rsi": rsi,
            "ema_gap_pct": gap,
            "adx": adx,
            "trend": trend,
        },
        index=closes.index,
    )
    if config.minimum_indicator_bars > 1:
        trend_column = frame.columns.get_loc("trend")
        frame.iloc[: config.minimum_indicator_bars - 1, trend_column] = "neutral"
    return frame


def _resample_candles(data: pd.DataFrame, seconds: int) -> pd.DataFrame:
    index = pd.to_datetime(data["timestamp"], unit="s", utc=True)
    indexed = data.set_axis(index)
    rule = f"{seconds}s"
    result = indexed.resample(rule, label="left", closed="left").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        child_count=("close", "count"),
    )
    result["timestamp"] = result.index.as_unit("ns").asi8 // 1_000_000_000
    result.reset_index(drop=True, inplace=True)
    return result


def _lookup_metric(
    metrics: pd.DataFrame, metric_timestamps: pd.Series, targets: np.ndarray
) -> pd.DataFrame:
    keyed = metrics.copy()
    keyed.index = metric_timestamps.astype("int64")
    return keyed.reindex(targets).reset_index(drop=True)


def _live_hour_metrics(
    data: pd.DataFrame, hourly: pd.DataFrame, config: BacktestConfig
) -> pd.DataFrame:
    """Approximate live 1H indicators at each completed 5M observation.

    Completed hourly closes establish the Wilder/EMA state.  The latest 5M
    close is then used as the in-progress hourly close without contaminating
    the state used by later observations in that same hour.
    """
    complete = hourly.loc[hourly["child_count"] == 12].copy().reset_index(drop=True)
    closes = complete["close"]
    fast = ema_series(closes, config.ema_fast).to_numpy()
    slow = ema_series(closes, config.ema_slow).to_numpy()
    avg_gain, avg_loss = _wilder_rsi_components(closes, config.rsi_period)
    avg_gain_values = avg_gain.to_numpy()
    avg_loss_values = avg_loss.to_numpy()
    complete_starts = complete["timestamp"].to_numpy(dtype=np.int64)

    source_starts = data["timestamp"].to_numpy(dtype=np.int64)
    hour_starts = (source_starts // 3600) * 3600
    prior_indices = np.searchsorted(complete_starts, hour_starts, side="left") - 1
    spots = data["close"].to_numpy(dtype=float)
    out_fast = np.full(len(data), np.nan)
    out_slow = np.full(len(data), np.nan)
    out_rsi = np.full(len(data), np.nan)

    for row, prior in enumerate(prior_indices):
        # Live dashboard requires at least 40 hourly candles including current.
        if prior < 0 or prior + 2 < config.minimum_indicator_bars:
            continue
        previous_close = float(closes.iloc[prior])
        current = spots[row]
        out_fast[row] = current * (2.0 / (config.ema_fast + 1)) + fast[prior] * (
            1.0 - 2.0 / (config.ema_fast + 1)
        )
        out_slow[row] = current * (2.0 / (config.ema_slow + 1)) + slow[prior] * (
            1.0 - 2.0 / (config.ema_slow + 1)
        )
        gain = max(current - previous_close, 0.0)
        loss = max(previous_close - current, 0.0)
        ag = (avg_gain_values[prior] * (config.rsi_period - 1) + gain) / config.rsi_period
        al = (avg_loss_values[prior] * (config.rsi_period - 1) + loss) / config.rsi_period
        out_rsi[row] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)

    gap = np.abs(out_fast - out_slow) / np.where(spots == 0, np.nan, spots) * 100.0
    trend = np.where(
        (out_fast > out_slow)
        & (spots > out_slow)
        & (out_rsi >= config.rsi_up)
        & (gap >= config.ema_gap_pct),
        "up",
        np.where(
            (out_fast < out_slow)
            & (spots < out_slow)
            & (out_rsi <= config.rsi_down)
            & (gap >= config.ema_gap_pct),
            "down",
            "neutral",
        ),
    )
    return pd.DataFrame(
        {
            "close": spots,
            "ema9": out_fast,
            "ema21": out_slow,
            "rsi": out_rsi,
            "ema_gap_pct": gap,
            "trend": trend,
            "candle_timestamp": hour_starts,
        }
    )


def _debounce_trend(raw: pd.Series, required_samples: int) -> pd.Series:
    """Historical equivalent of the live hourly direction debounce.

    A 5-minute dataset observes the in-progress hour once per source bar, so
    this is intentionally more conservative than the live 15-second sampler.
    """
    candidate = "neutral"
    count = 0
    confirmed = "neutral"
    output: list[str] = []
    for value in raw.fillna("neutral"):
        if value not in {"up", "down"}:
            candidate, count, confirmed = "neutral", 0, "neutral"
        elif value == confirmed:
            candidate, count = value, max(required_samples, 1)
        else:
            if value != candidate:
                candidate, count = value, 1
            else:
                count += 1
            if count >= required_samples:
                confirmed = value
        output.append(confirmed if value == confirmed else "neutral")
    return pd.Series(output, index=raw.index)


def prepare_trend_signals(
    df: pd.DataFrame, config: BacktestConfig | None = None
) -> pd.DataFrame:
    """Create production-equivalent 5M/15M/1H signal snapshots.

    Source timestamps are treated as candle *start* times, matching Delta's
    history endpoint.  A decision is therefore made five minutes later.  The
    5M and 15M lookups only expose buckets whose end is at or before that
    decision.  The 1H lookup is either similarly closed or uses the latest 5M
    close as a live-hour approximation.
    """
    cfg = config or BacktestConfig.from_env()
    cfg.validate()
    data = normalise_candles(df)
    if data.empty:
        return pd.DataFrame()

    five = _trend_metric_frame(data["close"], cfg, data["high"], data["low"])
    fifteen_candles = _resample_candles(data, 900)
    fifteen = _trend_metric_frame(
        fifteen_candles["close"], cfg, fifteen_candles["high"], fifteen_candles["low"]
    )
    previous_ema21 = fifteen["ema21"].shift(cfg.trend_15m_slope_bars)
    fifteen["ema21_slope_pct"] = (
        (fifteen["ema21"] - previous_ema21) / previous_ema21.replace(0, np.nan) * 100.0
    )
    reject_up = (fifteen["trend"] == "up") & (
        fifteen["ema21_slope_pct"] < cfg.trend_15m_min_slope_pct
    )
    reject_down = (fifteen["trend"] == "down") & (
        fifteen["ema21_slope_pct"] > -cfg.trend_15m_min_slope_pct
    )
    reject_adx = fifteen["trend"].isin(["up", "down"]) & (
        fifteen["adx"] < cfg.trend_adx_min
    )
    fifteen.loc[reject_up | reject_down | reject_adx, "trend"] = "neutral"
    fifteen.loc[fifteen_candles["child_count"] != 3, "trend"] = "neutral"
    hourly_candles = _resample_candles(data, 3600)

    decisions = data["timestamp"].to_numpy(dtype=np.int64) + RES_SECONDS
    target_15m = ((decisions - 900) // 900) * 900
    m15 = _lookup_metric(fifteen, fifteen_candles["timestamp"], target_15m)

    if cfg.hourly_mode == "live":
        hour = _live_hour_metrics(data, hourly_candles, cfg)
    else:
        closed_hour = _trend_metric_frame(
            hourly_candles["close"], cfg, hourly_candles["high"], hourly_candles["low"]
        )
        closed_hour.loc[hourly_candles["child_count"] != 12, "trend"] = "neutral"
        target_1h = ((decisions - 3600) // 3600) * 3600
        hour = _lookup_metric(closed_hour, hourly_candles["timestamp"], target_1h)
        hour["candle_timestamp"] = target_1h

    hour["raw_trend"] = hour["trend"].fillna("neutral")
    hour["trend"] = _debounce_trend(hour["raw_trend"], cfg.hourly_confirm_samples)

    signals = pd.DataFrame(
        {
            "timestamp": data["timestamp"].to_numpy(dtype=np.int64),
            "decision_timestamp": decisions,
            "spot": data["close"].to_numpy(dtype=float),
            "trend_5m": five["trend"].to_numpy(),
            "trend_15m": m15["trend"].fillna("neutral").to_numpy(),
            "trend_1h": hour["trend"].fillna("neutral").to_numpy(),
            "trend_1h_raw": hour["raw_trend"].fillna("neutral").to_numpy(),
            "ema9_5m": five["ema9"].to_numpy(),
            "ema21_5m": five["ema21"].to_numpy(),
            "rsi_5m": five["rsi"].to_numpy(),
            "ema9_15m": m15["ema9"].to_numpy(),
            "ema21_15m": m15["ema21"].to_numpy(),
            "rsi_15m": m15["rsi"].to_numpy(),
            "ema_gap_15m_pct": m15["ema_gap_pct"].to_numpy(),
            "ema21_slope_15m_pct": m15["ema21_slope_pct"].to_numpy(),
            "adx_15m": m15["adx"].to_numpy(),
            "ema9_1h": hour["ema9"].to_numpy(),
            "ema21_1h": hour["ema21"].to_numpy(),
            "rsi_1h": hour["rsi"].to_numpy(),
            "ema_gap_1h_pct": hour["ema_gap_pct"].to_numpy(),
            "candle_5m_timestamp": data["timestamp"].to_numpy(dtype=np.int64),
            "candle_15m_timestamp": target_15m,
            "candle_1h_timestamp": hour["candle_timestamp"].to_numpy(dtype=np.int64),
        }
    )
    aligned_up = (
        (signals["trend_5m"] == "up")
        & (signals["trend_15m"] == "up")
        & (signals["trend_1h"] == "up")
    )
    aligned_down = (
        (signals["trend_5m"] == "down")
        & (signals["trend_15m"] == "down")
        & (signals["trend_1h"] == "down")
    )
    signals["signal"] = np.where(aligned_up, "up", np.where(aligned_down, "down", "neutral"))
    signals["hourly_mode"] = cfg.hourly_mode
    return signals


# ---------------------------------------------------------------------------
# Option pricing and execution assumptions
# ---------------------------------------------------------------------------
def _norm_cdf(x: float) -> float:
    t = 1.0 / (1.0 + 0.2316419 * abs(x))
    poly = t * (
        0.319381530
        + t
        * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429)))
    )
    cdf = 1.0 - math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi) * poly
    return cdf if x >= 0 else 1.0 - cdf


def bs_price(spot: float, strike: float, years: float, sigma: float, opt: str) -> float:
    """Black-Scholes fallback price (r=0); ``opt`` is call or put."""
    if years <= 1e-8:
        return max(spot - strike, 0.0) if opt == "call" else max(strike - spot, 0.0)
    sigma_root_t = sigma * math.sqrt(years)
    if sigma_root_t <= 0 or spot <= 0 or strike <= 0:
        return 0.0
    d1 = (math.log(spot / strike) + 0.5 * sigma**2 * years) / sigma_root_t
    d2 = d1 - sigma_root_t
    if opt == "call":
        return spot * _norm_cdf(d1) - strike * _norm_cdf(d2)
    return strike * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def itm_strike(
    spot: float, opt: str, strike_step: float | None = None, itm_steps: int = 2
) -> float:
    step = float(strike_step or STRIKE_STEP)
    atm = round(spot / step) * step
    return atm - itm_steps * step if opt == "call" else atm + itm_steps * step


def hours_to_eod(timestamp: int) -> float:
    next_midnight = ((timestamp // 86400) + 1) * 86400
    return max(next_midnight - timestamp, 0) / 3600.0


def realised_vol_series(close: pd.Series, window: int = 288) -> pd.Series:
    log_return = np.log(close / close.shift(1))
    return log_return.rolling(window).std() * math.sqrt(365 * 24 * 12)


def _normalise_option_type(value: Any) -> str | None:
    text = str(value).strip().lower()
    if text in {"call", "ce", "c"} or text.startswith("c-"):
        return "call"
    if text in {"put", "pe", "p"} or text.startswith("p-"):
        return "put"
    return None


def _first_column(data: pd.DataFrame, names: tuple[str, ...], default: Any = np.nan) -> pd.Series:
    for name in names:
        if name in data:
            return data[name]
    return pd.Series(default, index=data.index)


def normalise_option_data(option_data: pd.DataFrame) -> pd.DataFrame:
    """Normalise historical option quotes.

    Supported aliases include ``time/timestamp``, ``strike/strike_price``,
    ``bid/best_bid``, ``ask/best_ask``, ``mark/close/price``, and
    ``expiry/settlement_time``.  ``option_type`` may be call/put, CE/PE, or it
    may be inferred from a Delta-style C-/P- symbol.
    """
    source = option_data.copy()
    timestamp = _first_column(source, ("timestamp", "time"))
    symbol = _first_column(source, ("symbol", "product_symbol"), "")
    option_type = _first_column(source, ("option_type", "type"), "")
    inferred_type = option_type.where(option_type.astype(str).str.len() > 0, symbol)
    expiry_raw = _first_column(source, ("expiry_timestamp", "expiry", "settlement_time"))
    expiry = _timestamps_to_seconds(expiry_raw)
    result = pd.DataFrame(
        {
            "timestamp": _timestamps_to_seconds(timestamp),
            "symbol": symbol.fillna("").astype(str),
            "option_type": inferred_type.map(_normalise_option_type),
            "strike": pd.to_numeric(_first_column(source, ("strike", "strike_price")), errors="coerce"),
            "expiry_timestamp": expiry,
            "bid": pd.to_numeric(_first_column(source, ("bid", "best_bid", "bid_price")), errors="coerce"),
            "ask": pd.to_numeric(_first_column(source, ("ask", "best_ask", "ask_price")), errors="coerce"),
            "mark": pd.to_numeric(_first_column(source, ("mark", "mark_price", "close", "price")), errors="coerce"),
            "bid_size": pd.to_numeric(
                _first_column(source, ("bid_size", "best_bid_size", "bid_depth")), errors="coerce"
            ),
            "ask_size": pd.to_numeric(
                _first_column(source, ("ask_size", "best_ask_size", "ask_depth")), errors="coerce"
            ),
            "delta": pd.to_numeric(_first_column(source, ("delta", "option_delta")), errors="coerce"),
            "mark_iv": pd.to_numeric(
                _first_column(source, ("mark_iv", "iv", "implied_volatility")), errors="coerce"
            ),
        }
    )
    midpoint = (result["bid"] + result["ask"]) / 2.0
    result["mark"] = result["mark"].fillna(midpoint)
    result.dropna(subset=["timestamp", "option_type", "strike", "mark"], inplace=True)
    result = result.loc[result["mark"] > 0].copy()
    result["timestamp"] = result["timestamp"].astype("int64")
    result.sort_values(["timestamp", "symbol", "strike"], inplace=True)
    result.reset_index(drop=True, inplace=True)
    return result


@dataclass
class OptionPrice:
    mark: float
    bid: float
    ask: float
    source: str
    strike: float
    option_type: str
    symbol: str = ""
    expiry_timestamp: int | None = None
    delta: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None
    mark_iv: float | None = None


class OptionQuoteBook:
    def __init__(self, data: pd.DataFrame, config: BacktestConfig):
        self.data = normalise_option_data(data)
        self.config = config

    def _recent(self, timestamp: int) -> pd.DataFrame:
        return self.data.loc[
            (self.data["timestamp"] <= timestamp)
            & (self.data["timestamp"] >= timestamp - self.config.max_option_quote_age_seconds)
        ]

    def entry_quote(self, timestamp: int, spot: float, option_type: str) -> OptionPrice | None:
        candidates = self._recent(timestamp)
        candidates = candidates.loc[candidates["option_type"] == option_type].copy()
        min_expiry = timestamp + int(self.config.min_time_to_expiry_hours * 3600)
        candidates = candidates.loc[
            candidates["expiry_timestamp"].isna()
            | (candidates["expiry_timestamp"] > min_expiry)
        ]
        candidates = candidates.loc[
            ((candidates["strike"] < spot) if option_type == "call" else (candidates["strike"] > spot))
        ]
        if candidates.empty:
            return None
        # Keep the newest quote for each contract before selecting the live-like
        # nearest expiry and two-step ITM strike from the actual strike ladder.
        contract_keys = ["symbol"] if (candidates["symbol"] != "").any() else [
            "option_type",
            "strike",
            "expiry_timestamp",
        ]
        candidates.sort_values("timestamp", inplace=True)
        candidates = candidates.groupby(contract_keys, dropna=False).tail(1)
        with_expiry = candidates.loc[candidates["expiry_timestamp"].notna()]
        if not with_expiry.empty:
            nearest_expiry = with_expiry["expiry_timestamp"].min()
            candidates = with_expiry.loc[with_expiry["expiry_timestamp"] == nearest_expiry]
        if not self.config.allow_missing_option_book:
            candidates = candidates.loc[
                candidates["bid"].notna()
                & candidates["ask"].notna()
                & (candidates["bid"] > 0)
                & (candidates["ask"] > 0)
                & candidates["ask_size"].notna()
                & (candidates["ask_size"] >= self.config.min_option_depth_lots)
            ]
        if candidates.empty:
            return None
        midpoint = candidates["mark"].where(candidates["mark"] > 0, (candidates["bid"] + candidates["ask"]) / 2)
        candidates["spread_pct"] = (candidates["ask"] - candidates["bid"]) / midpoint * 100.0
        if self.config.max_option_spread_pct > 0:
            candidates = candidates.loc[
                candidates["spread_pct"].isna()
                | (candidates["spread_pct"] <= self.config.max_option_spread_pct)
            ]
        if candidates.empty:
            return None
        has_delta = candidates["delta"].notna() & (candidates["delta"].abs() > 0)
        candidates["selection_no_delta"] = (~has_delta).astype(int)
        candidates["selection_delta_distance"] = np.where(
            has_delta, (candidates["delta"].abs() - self.config.target_delta).abs(), 99.0
        )
        candidates["selection_spread"] = candidates["spread_pct"].fillna(999.0)
        candidates["selection_moneyness"] = (candidates["strike"] - spot).abs()
        candidates.sort_values(
            [
                "selection_no_delta",
                "selection_delta_distance",
                "selection_spread",
                "selection_moneyness",
            ],
            inplace=True,
        )
        row = candidates.iloc[0]
        return self._to_price(row)

    def quote_for_contract(self, timestamp: int, contract: OptionPrice) -> OptionPrice | None:
        candidates = self._recent(timestamp)
        if contract.symbol:
            candidates = candidates.loc[candidates["symbol"] == contract.symbol]
        else:
            candidates = candidates.loc[
                (candidates["option_type"] == contract.option_type)
                & np.isclose(candidates["strike"], contract.strike)
            ]
            if contract.expiry_timestamp is not None:
                candidates = candidates.loc[
                    candidates["expiry_timestamp"].isna()
                    | np.isclose(candidates["expiry_timestamp"], contract.expiry_timestamp)
                ]
        if candidates.empty:
            return None
        return self._to_price(candidates.iloc[-1])

    def _to_price(self, row: pd.Series) -> OptionPrice:
        mark = float(row["mark"])
        half_spread = self.config.synthetic_spread_pct / 2.0
        bid = float(row["bid"]) if pd.notna(row["bid"]) and row["bid"] > 0 else mark * (1 - half_spread)
        ask = float(row["ask"]) if pd.notna(row["ask"]) and row["ask"] > 0 else mark * (1 + half_spread)
        expiry = int(row["expiry_timestamp"]) if pd.notna(row["expiry_timestamp"]) else None
        return OptionPrice(
            mark=mark,
            bid=bid,
            ask=ask,
            source="option_data",
            strike=float(row["strike"]),
            option_type=str(row["option_type"]),
            symbol=str(row["symbol"]),
            expiry_timestamp=expiry,
            delta=float(row["delta"]) if pd.notna(row["delta"]) else None,
            bid_size=float(row["bid_size"]) if pd.notna(row["bid_size"]) else None,
            ask_size=float(row["ask_size"]) if pd.notna(row["ask_size"]) else None,
            mark_iv=float(row["mark_iv"]) if pd.notna(row["mark_iv"]) else None,
        )


def _synthetic_price(
    timestamp: int,
    spot: float,
    strike: float,
    option_type: str,
    iv: float,
    expiry_timestamp: int,
    config: BacktestConfig,
) -> OptionPrice:
    years = max(expiry_timestamp - timestamp, 0) / (365.0 * 24 * 3600)
    mark = max(bs_price(spot, strike, years, iv, option_type), 0.0001)
    half_spread = config.synthetic_spread_pct / 2.0
    return OptionPrice(
        mark=mark,
        bid=max(mark * (1 - half_spread), 0.0001),
        ask=mark * (1 + half_spread),
        source="black_scholes_fallback",
        strike=strike,
        option_type=option_type,
        expiry_timestamp=expiry_timestamp,
    )


def calculate_option_fee(
    spot: float, premium: float, lots: int, config: BacktestConfig
) -> float:
    """Configurable Delta-style option fee, capped as a share of premium."""
    notional_fee = abs(spot) * config.contract_value * lots * config.option_fee_rate
    premium_cap = abs(premium) * config.contract_value * lots * config.option_fee_cap_pct
    return min(notional_fee, premium_cap)


# ---------------------------------------------------------------------------
# Backtest engine
# ---------------------------------------------------------------------------
def run_backtest(
    df: pd.DataFrame,
    config: BacktestConfig | None = None,
    option_data: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Replay the production Trend entry and TP/SL/TSL protection logic.

    ``pnl_usd`` is net of entry/exit fees and execution spread/slippage.  A
    Black-Scholes fallback is always disclosed in ``pricing_source``.
    """
    cfg = config or BacktestConfig.from_env()
    cfg.validate()
    candles = normalise_candles(df)
    signals = prepare_trend_signals(candles, cfg)
    quote_book = OptionQuoteBook(option_data, cfg) if option_data is not None and not option_data.empty else None
    rvol = realised_vol_series(candles["close"], cfg.realised_vol_window)

    trades: list[dict[str, Any]] = []
    position: dict[str, Any] | None = None
    last_entry_key: tuple[Any, ...] | None = None

    def price_for_position(row_index: int, state: dict[str, Any]) -> OptionPrice:
        timestamp = int(signals.iloc[row_index]["decision_timestamp"])
        spot = float(signals.iloc[row_index]["spot"])
        if quote_book is not None and state["entry_quote"].source == "option_data":
            quote = quote_book.quote_for_contract(timestamp, state["entry_quote"])
            if quote is not None:
                return quote
            state["used_gap_fallback"] = True
        return _synthetic_price(
            timestamp,
            spot,
            state["strike"],
            state["option_type"],
            state["iv"],
            state["expiry_timestamp"],
            cfg,
        )

    def close_position(row_index: int, state: dict[str, Any], reason: str) -> None:
        quote = price_for_position(row_index, state)
        row = signals.iloc[row_index]
        exit_fill = max(quote.bid * (1.0 - cfg.slippage_pct), 0.0)
        gross = (exit_fill - state["entry_fill"]) * cfg.contract_value * cfg.order_size
        exit_fee = calculate_option_fee(float(row["spot"]), exit_fill, cfg.order_size, cfg)
        source = state["entry_quote"].source
        if state["used_gap_fallback"]:
            source = "option_data_with_bs_gap_fallback"
        entry_ts = int(state["entry_timestamp"])
        exit_ts = int(row["decision_timestamp"])
        trades.append(
            {
                "entry_time": _dt(entry_ts),
                "exit_time": _dt(exit_ts),
                "entry_timestamp": entry_ts,
                "exit_timestamp": exit_ts,
                "signal": state["direction"],
                "option_type": "CE" if state["option_type"] == "call" else "PE",
                "symbol": state["entry_quote"].symbol,
                "entry_spot": round(state["entry_spot"], 2),
                "exit_spot": round(float(row["spot"]), 2),
                "strike": round(state["strike"], 2),
                "entry_premium": round(state["entry_fill"], 6),
                "exit_premium": round(exit_fill, 6),
                "entry_mark": round(state["entry_quote"].mark, 6),
                "entry_bid": round(state["entry_quote"].bid, 6),
                "entry_ask": round(state["entry_quote"].ask, 6),
                "entry_spread_pct": round(
                    (state["entry_quote"].ask - state["entry_quote"].bid)
                    / state["entry_quote"].mark
                    * 100.0,
                    4,
                ) if state["entry_quote"].mark else None,
                "exit_mark": round(quote.mark, 6),
                "option_delta": state["entry_quote"].delta,
                "mark_iv": state["entry_quote"].mark_iv,
                "expiry_timestamp": state["expiry_timestamp"],
                "entry_tte_hours": round(
                    (state["expiry_timestamp"] - entry_ts) / 3600.0, 2
                ),
                "lots": cfg.order_size,
                "contract_value": cfg.contract_value,
                "gross_pnl_usd": round(gross, 2),
                "entry_fee_usd": round(state["entry_fee"], 4),
                "exit_fee_usd": round(exit_fee, 4),
                "fees_usd": round(state["entry_fee"] + exit_fee, 4),
                "pnl_usd": round(gross - state["entry_fee"] - exit_fee, 2),
                "peak_trigger_pnl_usd": round(state["peak_pnl"], 2),
                "exit_reason": reason,
                "holding_minutes": round((exit_ts - entry_ts) / 60.0, 1),
                "pricing_source": source,
                "hourly_mode": cfg.hourly_mode,
                "signal_key": state["signal_key"],
                **state["signal_snapshot"],
            }
        )

    for i, row in signals.iterrows():
        if position is not None:
            quote = price_for_position(i, position)
            trigger_pnl = (
                quote.mark - position["entry_fill"]
            ) * cfg.contract_value * cfg.order_size
            position["peak_pnl"] = max(position["peak_pnl"], trigger_pnl)
            if cfg.tsl_arm_usd > 0 and position["peak_pnl"] >= cfg.tsl_arm_usd:
                position["tsl_armed"] = True
            tsl_floor = position["peak_pnl"] - cfg.tsl_trail_usd
            expired = int(row["decision_timestamp"]) >= position["expiry_timestamp"]
            reason = None
            if expired:
                reason = "EXPIRY"
            elif cfg.stop_loss_usd > 0 and trigger_pnl <= -cfg.stop_loss_usd:
                reason = "SL"
            elif cfg.take_profit_usd > 0 and trigger_pnl >= cfg.take_profit_usd:
                reason = "TP"
            elif position["tsl_armed"] and trigger_pnl <= tsl_floor:
                reason = "TSL"
            if reason:
                close_position(i, position, reason)
                position = None

        if position is not None or row["signal"] not in {"up", "down"}:
            continue
        signal_key = (
            row["signal"],
            int(row["candle_5m_timestamp"]),
            int(row["candle_15m_timestamp"]),
            int(row["candle_1h_timestamp"]),
        )
        if signal_key == last_entry_key:
            continue
        timestamp = int(row["decision_timestamp"])
        spot = float(row["spot"])
        option_type = "call" if row["signal"] == "up" else "put"
        quote = quote_book.entry_quote(timestamp, spot, option_type) if quote_book else None
        if quote is None:
            expiry_timestamp = ((timestamp // 86400) + 1) * 86400
            if expiry_timestamp - timestamp < cfg.min_time_to_expiry_hours * 3600:
                continue
            strike = itm_strike(spot, option_type, cfg.strike_step, cfg.itm_steps)
            realised = float(rvol.iloc[i]) if i < len(rvol) else float("nan")
            iv = max(realised if math.isfinite(realised) else cfg.iv_floor, cfg.iv_floor)
            quote = _synthetic_price(
                timestamp, spot, strike, option_type, iv, expiry_timestamp, cfg
            )
        else:
            strike = quote.strike
            expiry_timestamp = quote.expiry_timestamp or ((timestamp // 86400) + 1) * 86400
            realised = float(rvol.iloc[i]) if i < len(rvol) else float("nan")
            observed_iv = quote.mark_iv if quote.mark_iv is not None and quote.mark_iv > 0 else realised
            iv = max(observed_iv if math.isfinite(observed_iv) else cfg.iv_floor, cfg.iv_floor)

        entry_fill = quote.ask * (1.0 + cfg.slippage_pct)
        entry_fee = calculate_option_fee(spot, entry_fill, cfg.order_size, cfg)
        position = {
            "direction": row["signal"],
            "option_type": option_type,
            "strike": strike,
            "expiry_timestamp": int(expiry_timestamp),
            "iv": iv,
            "entry_timestamp": timestamp,
            "entry_spot": spot,
            "entry_quote": quote,
            "entry_fill": entry_fill,
            "entry_fee": entry_fee,
            "peak_pnl": 0.0,
            "tsl_armed": False,
            "used_gap_fallback": False,
            "signal_key": "|".join(str(value) for value in signal_key),
            "signal_snapshot": {
                key: row[key]
                for key in (
                    "ema9_5m",
                    "ema21_5m",
                    "rsi_5m",
                    "ema9_15m",
                    "ema21_15m",
                    "rsi_15m",
                    "ema_gap_15m_pct",
                    "ema21_slope_15m_pct",
                    "adx_15m",
                    "ema9_1h",
                    "ema21_1h",
                    "rsi_1h",
                    "ema_gap_1h_pct",
                )
            },
        }
        last_entry_key = signal_key

    if position is not None and cfg.force_close_at_end and not signals.empty:
        close_position(len(signals) - 1, position, "END_OF_DATA")

    result = pd.DataFrame(trades)
    result.attrs["config"] = cfg
    result.attrs["start_timestamp"] = int(candles["timestamp"].min()) if not candles.empty else None
    result.attrs["end_timestamp"] = int(candles["timestamp"].max() + RES_SECONDS) if not candles.empty else None
    result.attrs["pricing_warning"] = (
        "Black-Scholes prices are a screening fallback, not executable quotes."
    )
    return result


# ---------------------------------------------------------------------------
# Reporting, walk-forward analysis, and validation gates
# ---------------------------------------------------------------------------
def performance_metrics(trades: pd.DataFrame) -> dict[str, float | int]:
    if trades.empty or "pnl_usd" not in trades:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate_pct": 0.0,
            "net_pnl_usd": 0.0,
            "expectancy_usd": 0.0,
            "profit_factor": 0.0,
            "max_drawdown_usd": 0.0,
            "fees_usd": 0.0,
        }
    pnl = pd.to_numeric(trades["pnl_usd"], errors="coerce").fillna(0.0)
    wins = int((pnl > 0).sum())
    losses = int((pnl < 0).sum())
    gross_profit = float(pnl.loc[pnl > 0].sum())
    gross_loss = abs(float(pnl.loc[pnl < 0].sum()))
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = float("inf")
    else:
        profit_factor = 0.0
    equity = pd.Series(np.r_[0.0, pnl.cumsum().to_numpy()])
    drawdown = equity.cummax() - equity
    fees = float(pd.to_numeric(trades.get("fees_usd", 0.0), errors="coerce").fillna(0).sum()) if "fees_usd" in trades else 0.0
    return {
        "trades": int(len(pnl)),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": wins / len(pnl) * 100.0 if len(pnl) else 0.0,
        "net_pnl_usd": float(pnl.sum()),
        "expectancy_usd": float(pnl.mean()) if len(pnl) else 0.0,
        "profit_factor": float(profit_factor),
        "max_drawdown_usd": float(drawdown.max()),
        "fees_usd": fees,
    }


def per_direction_metrics(trades: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for direction in ("up", "down"):
        subset = trades.loc[trades["signal"] == direction] if "signal" in trades else trades.iloc[0:0]
        rows.append({"direction": direction, **performance_metrics(subset)})
    return pd.DataFrame(rows).set_index("direction")


def walk_forward_analysis(
    trades: pd.DataFrame,
    start_timestamp: int,
    end_timestamp: int,
    config: BacktestConfig,
) -> pd.DataFrame:
    """Expanding-window train/OOS report with non-overlapping test windows."""
    if config.walk_forward_folds <= 0 or end_timestamp <= start_timestamp:
        return pd.DataFrame()
    initial_test_start = start_timestamp + int(
        (end_timestamp - start_timestamp) * config.walk_forward_train_fraction
    )
    boundaries = np.linspace(
        initial_test_start, end_timestamp, config.walk_forward_folds + 1, dtype=np.int64
    )
    entry_times = pd.to_numeric(trades.get("entry_timestamp", pd.Series(dtype=float)), errors="coerce")
    rows: list[dict[str, Any]] = []
    for fold in range(config.walk_forward_folds):
        test_start, test_end = int(boundaries[fold]), int(boundaries[fold + 1])
        train = trades.loc[entry_times < test_start]
        test = trades.loc[(entry_times >= test_start) & (entry_times < test_end if fold < config.walk_forward_folds - 1 else entry_times <= test_end)]
        train_metrics = performance_metrics(train)
        test_metrics = performance_metrics(test)
        rows.append(
            {
                "fold": fold + 1,
                "train_end": _dt(test_start),
                "test_end": _dt(test_end),
                **{f"train_{key}": value for key, value in train_metrics.items()},
                **{f"oos_{key}": value for key, value in test_metrics.items()},
            }
        )
    return pd.DataFrame(rows).set_index("fold")


def evaluate_validation_gates(
    metrics: dict[str, float | int], config: BacktestConfig
) -> pd.DataFrame:
    checks = [
        ("minimum_trades", float(metrics["trades"]), float(config.validation_min_trades), ">="),
        (
            "profit_factor",
            float(metrics["profit_factor"]),
            float(config.validation_min_profit_factor),
            ">=",
        ),
        ("positive_expectancy", float(metrics["expectancy_usd"]), 0.0, ">"),
        (
            "maximum_drawdown",
            float(metrics["max_drawdown_usd"]),
            float(config.validation_max_drawdown_usd),
            "<=",
        ),
    ]
    rows = []
    for name, actual, threshold, operator in checks:
        passed = actual >= threshold if operator == ">=" else actual > threshold if operator == ">" else actual <= threshold
        rows.append(
            {
                "gate": name,
                "actual": actual,
                "operator": operator,
                "threshold": threshold,
                "passed": bool(passed),
            }
        )
    return pd.DataFrame(rows).set_index("gate")


def analyze_backtest(
    trades: pd.DataFrame, config: BacktestConfig | None = None
) -> dict[str, Any]:
    cfg = config or trades.attrs.get("config") or BacktestConfig.from_env()
    start = trades.attrs.get("start_timestamp")
    end = trades.attrs.get("end_timestamp")
    if start is None or end is None:
        timestamps = pd.to_numeric(trades.get("entry_timestamp", pd.Series(dtype=float)), errors="coerce")
        start = int(timestamps.min()) if not timestamps.empty else 0
        end = int(timestamps.max()) + 1 if not timestamps.empty else 1
    walk_forward = walk_forward_analysis(trades, int(start), int(end), cfg)
    oos_start = int(start + (end - start) * cfg.walk_forward_train_fraction)
    if "entry_timestamp" in trades and cfg.walk_forward_folds > 0:
        oos = trades.loc[pd.to_numeric(trades["entry_timestamp"], errors="coerce") >= oos_start]
        evaluation_scope = "out_of_sample"
    else:
        oos = trades
        evaluation_scope = "full_period"
    evaluation_metrics = performance_metrics(oos)
    return {
        "overall": performance_metrics(trades),
        "per_direction": per_direction_metrics(trades),
        "walk_forward": walk_forward,
        "evaluation_scope": evaluation_scope,
        "evaluation": evaluation_metrics,
        "gates": evaluate_validation_gates(evaluation_metrics, cfg),
    }


def print_report(
    trades: pd.DataFrame,
    config: BacktestConfig | None = None,
    output_csv: str | None = OUTPUT_CSV,
) -> None:
    cfg = config or trades.attrs.get("config") or BacktestConfig.from_env()
    analysis = analyze_backtest(trades, cfg)
    metrics = analysis["overall"]

    print("\n" + "=" * 76)
    print("  BACKTEST | BTC MULTI-TIMEFRAME TREND | AFTER-COST RESULTS")
    print("=" * 76)
    print("  Signal          : EMA9/EMA21 + price/EMA21 + Wilder RSI14")
    print(f"  Candle policy   : closed 5M, closed 15M, {cfg.hourly_mode.upper()} 1H")
    print(
        f"  Trend filters   : RSI {cfg.rsi_up:g}/{cfg.rsi_down:g}, EMA gap "
        f"{cfg.ema_gap_pct:g}%, 15M ADX >= {cfg.trend_adx_min:g}, "
        f"1H samples {cfg.hourly_confirm_samples}"
    )
    print(
        f"  Entry           : target |delta| {cfg.target_delta:g} when quotes exist; "
        f"{cfg.itm_steps}-step ITM BS fallback, {cfg.order_size:,} lots"
    )
    print(
        f"  Protection      : TP ${cfg.take_profit_usd:g} | SL ${cfg.stop_loss_usd:g} | "
        f"TSL arm/trail ${cfg.tsl_arm_usd:g}/${cfg.tsl_trail_usd:g}"
    )
    print(
        f"  Costs           : fee {cfg.option_fee_rate:.4%}, spread "
        f"{cfg.synthetic_spread_pct:.2%}, slippage {cfg.slippage_pct:.2%}"
    )
    if not trades.empty and "pricing_source" in trades:
        sources = ", ".join(
            f"{name}={count}" for name, count in trades["pricing_source"].value_counts().items()
        )
        print(f"  Pricing sources : {sources}")
    print("  WARNING         : Black-Scholes fallback is not executable market evidence.")
    print()
    print(f"  Total trades    : {metrics['trades']}")
    print(f"  Win rate        : {metrics['win_rate_pct']:.1f}%")
    print(f"  Net P&L         : ${metrics['net_pnl_usd']:,.2f}")
    print(f"  Fees            : ${metrics['fees_usd']:,.2f}")
    print(f"  Expectancy      : ${metrics['expectancy_usd']:,.2f} / trade")
    print(f"  Profit factor   : {metrics['profit_factor']:.2f}")
    print(f"  Max drawdown    : ${metrics['max_drawdown_usd']:,.2f}")
    print("\n  Per direction:")
    print(
        analysis["per_direction"][["trades", "win_rate_pct", "net_pnl_usd", "expectancy_usd", "profit_factor", "max_drawdown_usd"]]
        .round(2)
        .to_string()
    )
    if not analysis["walk_forward"].empty:
        print("\n  Walk-forward folds (expanding train, non-overlapping OOS):")
        columns = ["train_trades", "oos_trades", "oos_net_pnl_usd", "oos_expectancy_usd", "oos_profit_factor", "oos_max_drawdown_usd"]
        print(analysis["walk_forward"][columns].round(2).to_string())
    print(f"\n  Validation scope: {analysis['evaluation_scope']}")
    print(analysis["gates"].to_string())
    passed = bool(analysis["gates"]["passed"].all())
    print(f"\n  VALIDATION RESULT: {'PASS' if passed else 'FAIL - DO NOT SCALE/AUTO-ENABLE'}")
    print("=" * 76)

    if output_csv:
        Path(output_csv).parent.mkdir(parents=True, exist_ok=True)
        trades.to_csv(output_csv, index=False)
        print(f"\n  Trade log saved -> {output_csv}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candles-csv", help="Offline 5-minute BTC OHLCV CSV")
    parser.add_argument("--options-csv", help="Optional historical option bid/ask CSV")
    parser.add_argument("--output", default=OUTPUT_CSV, help="Trade-log CSV destination")
    parser.add_argument("--hourly-mode", choices=("live", "closed"), help="1H signal approximation")
    parser.add_argument("--start", type=int, default=BACKTEST_START, help="Fetch start Unix timestamp")
    parser.add_argument("--end", type=int, default=BACKTEST_END, help="Fetch end Unix timestamp")
    parser.add_argument(
        "--no-network",
        action="store_true",
        help="Fail unless --candles-csv is supplied",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    config = BacktestConfig.from_env()
    if args.hourly_mode:
        config = replace(config, hourly_mode=args.hourly_mode)
    if args.candles_csv:
        candles = normalise_candles(pd.read_csv(args.candles_csv))
    elif args.no_network:
        raise SystemExit("--no-network requires --candles-csv")
    else:
        candles = fetch_all_candles(PERPETUAL_SYMBOL, RESOLUTION, args.start, args.end)
    option_data = pd.read_csv(args.options_csv) if args.options_csv else None
    trades = run_backtest(candles, config, option_data)
    print_report(trades, config, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
