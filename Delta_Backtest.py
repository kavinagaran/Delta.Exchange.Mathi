"""
Delta Exchange BTCUSD — RSI + Supertrend Backtest (2026)

Fetches all available 15-minute BTCUSD perpetual candles from Jan 1 – Jun 20, 2026
and replays the live-bot signal logic bar by bar.

Option premium estimation (same-day ATM):
    premium ≈ S × max(realised_vol, IV_FLOOR) × √(T_hours / 8760) / √(2π)
    Current option value tracks underlying Δ with delta ≈ 0.5 (ATM approximation).
"""

import math
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv
import os

load_dotenv()

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
BASE_URL         = os.getenv("BASE_URL", "https://api.india.delta.exchange")
PERPETUAL_SYMBOL = os.getenv("PERPETUAL_SYMBOL", "BTCUSD")
RSI_PERIOD       = int(os.getenv("RSI_PERIOD", 14))
RSI_BULLISH      = float(os.getenv("RSI_BULLISH", 65))
RSI_BEARISH      = float(os.getenv("RSI_BEARISH", 35))
SUPERTREND_ATR   = int(os.getenv("SUPERTREND_ATR", 10))
SUPERTREND_MULT  = float(os.getenv("SUPERTREND_MULT", 3.0))
SL_PCT           = float(os.getenv("SL_PCT", 0.50))
EMA_PERIOD       = int(os.getenv("EMA_PERIOD", 20))
STRIKE_STEP      = int(os.getenv("STRIKE_STEP", 1000))
ORDER_SIZE       = int(os.getenv("ORDER_SIZE", 1000))

BACKTEST_START    = int(datetime(2026, 1, 1,  0,  0,  0, tzinfo=timezone.utc).timestamp())
BACKTEST_END      = int(datetime(2026, 6, 20, 23, 59, 59, tzinfo=timezone.utc).timestamp())
RESOLUTION        = "15m"
RES_SECONDS       = 900
BATCH_SIZE        = 500          # candles per API call

IV_FLOOR          = 0.60         # 60 % annualised IV floor for same-day premium
HOURS_TO_EXPIRY   = 4.0          # assumed average hours left when entry fires
WARMUP_BARS       = 100          # bars discarded before first eligible signal

OUTPUT_CSV        = r"D:\LocalGIT\Delta.Exchange\backtest_2026.csv"

# ─────────────────────────────────────────────
# DATA FETCH
# ─────────────────────────────────────────────
def fetch_all_candles(symbol: str, resolution: str, start_ts: int, end_ts: int) -> pd.DataFrame:
    all_rows = []
    cursor   = start_ts

    print(f"\nFetching {resolution} candles  {_dt(start_ts)} → {_dt(end_ts)}")
    print("-" * 56)

    while cursor < end_ts:
        batch_end = min(cursor + RES_SECONDS * BATCH_SIZE, end_ts)
        try:
            resp = requests.get(
                f"{BASE_URL}/v2/history/candles",
                params={"symbol": symbol, "resolution": resolution,
                        "start": cursor, "end": batch_end},
                timeout=(5, 30),
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  Request error ({_dt(cursor)}): {e}  — retrying in 5s")
            time.sleep(5)
            continue

        rows = data.get("result") or []
        all_rows.extend(rows)
        print(f"  {_dt(cursor):19s} → {_dt(batch_end):19s}  ({len(rows):3d} candles)")
        cursor = batch_end + RES_SECONDS
        time.sleep(0.3)

    if not all_rows:
        raise RuntimeError("No candle data returned. Check API endpoint or date range.")

    df = pd.DataFrame(all_rows)
    df.rename(columns={"time": "timestamp"}, inplace=True)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df.sort_values("timestamp", inplace=True)
    df.drop_duplicates("timestamp", inplace=True)
    df.reset_index(drop=True, inplace=True)
    print(f"\n  Total candles : {len(df)}")
    return df

def _dt(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

# ─────────────────────────────────────────────
# INDICATORS  (vectorised)
# ─────────────────────────────────────────────
def rsi_series(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_g = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_l = loss.ewm(com=period - 1, min_periods=period).mean()
    rs    = avg_g / avg_l
    return 100 - (100 / (1 + rs))

def supertrend_series(df: pd.DataFrame, atr_period: int, mult: float) -> pd.Series:
    high  = df["high"].copy()
    low   = df["low"].copy()
    close = df["close"].copy()

    tr  = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)

    atr = tr.ewm(com=atr_period - 1, min_periods=atr_period).mean()
    hl2 = (high + low) / 2
    ub  = (hl2 + mult * atr).copy()
    lb  = (hl2 - mult * atr).copy()

    direction = pd.Series("neutral", index=df.index, dtype=str)
    st        = pd.Series(np.nan,    index=df.index, dtype=float)

    for i in range(1, len(df)):
        if close.iloc[i - 1] <= ub.iloc[i - 1]:
            ub.iloc[i] = min(ub.iloc[i], ub.iloc[i - 1])
        if close.iloc[i - 1] >= lb.iloc[i - 1]:
            lb.iloc[i] = max(lb.iloc[i], lb.iloc[i - 1])

        if i == 1:
            st.iloc[i]        = ub.iloc[i]
            direction.iloc[i] = "bearish"
        elif st.iloc[i - 1] == ub.iloc[i - 1]:
            if close.iloc[i] <= ub.iloc[i]:
                st.iloc[i]        = ub.iloc[i]
                direction.iloc[i] = "bearish"
            else:
                st.iloc[i]        = lb.iloc[i]
                direction.iloc[i] = "bullish"
        else:
            if close.iloc[i] >= lb.iloc[i]:
                st.iloc[i]        = lb.iloc[i]
                direction.iloc[i] = "bullish"
            else:
                st.iloc[i]        = ub.iloc[i]
                direction.iloc[i] = "bearish"

    return direction

def realised_vol_series(close: pd.Series, window: int = 30) -> pd.Series:
    log_ret = np.log(close / close.shift(1))
    # 96 bars/day on 15 m × 252 trading days
    return log_ret.rolling(window).std() * math.sqrt(96 * 252)

def supertrend_4h_series(df: pd.DataFrame, atr_period: int, mult: float) -> np.ndarray:
    """Compute 4h Supertrend direction and forward-fill back to the 15m index."""
    idx = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    df_4h = pd.DataFrame({
        "high":  pd.Series(df["high"].values,  index=idx).resample("4h").max(),
        "low":   pd.Series(df["low"].values,   index=idx).resample("4h").min(),
        "close": pd.Series(df["close"].values,  index=idx).resample("4h").last(),
    }).dropna()
    st_4h = supertrend_series(df_4h, atr_period, mult)
    return st_4h.reindex(idx, method="ffill").fillna("neutral").values

def ema_1h_series(df: pd.DataFrame, period: int) -> pd.Series:
    """
    Resample 15m data to 1h, compute EMA, then forward-fill back onto
    the 15m index so each bar knows the last confirmed 1h EMA value.
    """
    idx    = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    closes = pd.Series(df["close"].values, index=idx)
    ema_1h = closes.resample("1h").last().dropna().ewm(span=period, adjust=False).mean()
    # reindex + ffill onto original 15m timestamps
    return ema_1h.reindex(idx, method="ffill").values

# ─────────────────────────────────────────────
# OPTION PRICING  (Black-Scholes, r=0)
# ─────────────────────────────────────────────
def _norm_cdf(x: float) -> float:
    """Abramowitz & Stegun polynomial approximation (error < 7.5e-8)."""
    t    = 1.0 / (1.0 + 0.2316419 * abs(x))
    poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937
           + t * (-1.821255978 + t * 1.330274429))))
    cdf  = 1.0 - math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi) * poly
    return cdf if x >= 0 else 1.0 - cdf

def bs_price(S: float, K: float, T: float, sigma: float, opt: str) -> float:
    """Full BS price (r=0). opt = 'call' | 'put'."""
    if T <= 1e-8:
        return max(S - K, 0.0) if opt == "call" else max(K - S, 0.0)
    sq = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * sigma ** 2 * T) / sq
    d2 = d1 - sq
    if opt == "call":
        return S * _norm_cdf(d1) - K * _norm_cdf(d2)
    return K * _norm_cdf(-d2) - S * _norm_cdf(-d1)

def itm_strike(spot: float, opt: str) -> float:
    """2-step ITM strike: calls below spot, puts above spot."""
    atm = round(spot / STRIKE_STEP) * STRIKE_STEP
    return (atm - 2 * STRIKE_STEP) if opt == "call" else (atm + 2 * STRIKE_STEP)

def hours_to_eod(bar_ts: int) -> float:
    """Hours remaining until midnight UTC (same-day expiry)."""
    next_midnight = ((bar_ts // 86400) + 1) * 86400
    return max(next_midnight - bar_ts, 0) / 3600.0

# ─────────────────────────────────────────────
# BACKTEST ENGINE
# ─────────────────────────────────────────────
def run_backtest(df: pd.DataFrame) -> pd.DataFrame:
    rsi   = rsi_series(df["close"], RSI_PERIOD)
    stdir = supertrend_series(df, SUPERTREND_ATR, SUPERTREND_MULT)
    st4h  = supertrend_4h_series(df, SUPERTREND_ATR, SUPERTREND_MULT)
    rvol  = realised_vol_series(df["close"], 30)
    ema1h = ema_1h_series(df, EMA_PERIOD)

    def sig(i: int) -> str:
        bar_ts = int(df["timestamp"].iloc[i])
        bar_dt = datetime.fromtimestamp(bar_ts, tz=timezone.utc)
        # Time filter: skip 00-01 UTC and 22-23 UTC
        if bar_dt.hour < 2 or bar_dt.hour >= 22:
            return "neutral"
        close = float(df["close"].iloc[i])
        ema   = ema1h[i]
        if (rsi.iloc[i] > RSI_BULLISH and stdir.iloc[i] == "bullish"
                and st4h[i] == "bullish" and close > ema):
            return "bullish"
        if (rsi.iloc[i] < RSI_BEARISH and stdir.iloc[i] == "bearish"
                and st4h[i] == "bearish" and close < ema):
            return "bearish"
        return "neutral"

    trades: list[dict] = []

    # Trade state
    in_trade      = False
    entry_signal  = "neutral"
    entry_premium = 0.0
    entry_strike  = 0.0
    entry_opt     = ""
    entry_iv      = 0.0
    entry_spot    = 0.0
    entry_time    = None
    sl_level      = 0.0

    def _open_trade(s: str, spot: float, bar_ts: int, bar_dt) -> None:
        nonlocal in_trade, entry_signal, entry_premium, entry_strike
        nonlocal entry_opt, entry_iv, entry_spot, entry_time, sl_level
        opt            = "call" if s == "bullish" else "put"
        K              = itm_strike(spot, opt)
        rv             = float(rvol.iloc[i])
        iv             = max(rv if not np.isnan(rv) else IV_FLOOR, IV_FLOOR)
        T              = hours_to_eod(bar_ts) / 8760
        premium        = bs_price(spot, K, T, iv, opt)
        entry_signal   = s
        entry_premium  = premium
        entry_strike   = K
        entry_opt      = opt
        entry_iv       = iv
        entry_spot     = spot
        entry_time     = bar_dt
        sl_level       = premium * (1 - SL_PCT)
        in_trade       = True

    for i in range(WARMUP_BARS, len(df)):
        s      = sig(i)
        spot   = float(df["close"].iloc[i])
        bar_ts = int(df["timestamp"].iloc[i])
        bar_dt = datetime.fromtimestamp(bar_ts, tz=timezone.utc)

        if not in_trade:
            if s in ("bullish", "bearish"):
                _open_trade(s, spot, bar_ts, bar_dt)
            continue

        # ── Reprice option using full BS with dynamic time to expiry ──
        T_now           = hours_to_eod(bar_ts) / 8760
        current_premium = max(bs_price(spot, entry_strike, T_now, entry_iv, entry_opt), 0.0)

        sl_hit  = current_premium <= sl_level
        flipped = s not in ("neutral", entry_signal)
        eod     = (bar_dt.hour == 23 and bar_dt.minute >= 45)

        if sl_hit or flipped or eod:
            reason  = "SL" if sl_hit else ("EOD" if eod else "Flip")
            pnl_usd = (current_premium - entry_premium) * ORDER_SIZE

            trades.append({
                "entry_time":    entry_time.strftime("%Y-%m-%d %H:%M"),
                "exit_time":     bar_dt.strftime("%Y-%m-%d %H:%M"),
                "signal":        entry_signal,
                "entry_spot":    round(entry_spot, 2),
                "exit_spot":     round(spot, 2),
                "strike":        round(entry_strike, 2),
                "entry_premium": round(entry_premium, 4),
                "exit_premium":  round(current_premium, 4),
                "sl_level":      round(sl_level, 4),
                "exit_reason":   reason,
                "pnl_usd":       round(pnl_usd, 2),
            })
            in_trade = False

            if flipped and s in ("bullish", "bearish"):
                _open_trade(s, spot, bar_ts, bar_dt)

    return pd.DataFrame(trades)

# ─────────────────────────────────────────────
# REPORT
# ─────────────────────────────────────────────
def print_report(trades: pd.DataFrame) -> None:
    if trades.empty:
        print("\nNo trades generated in the backtest period.")
        return

    n        = len(trades)
    wins     = (trades["pnl_usd"] > 0).sum()
    losses   = n - wins
    total    = trades["pnl_usd"].sum()
    avg_w    = trades.loc[trades["pnl_usd"] > 0, "pnl_usd"].mean() if wins  else 0
    avg_l    = trades.loc[trades["pnl_usd"] < 0, "pnl_usd"].mean() if losses else 0
    rr       = abs(avg_w / avg_l) if avg_l != 0 else float("inf")

    trades["cum_pnl"] = trades["pnl_usd"].cumsum()
    peak     = trades["cum_pnl"].cummax()
    max_dd   = (trades["cum_pnl"] - peak).min()

    monthly  = (
        pd.to_datetime(trades["entry_time"])
          .dt.to_period("M")
          .rename("month")
    )
    m_pnl    = trades.groupby(monthly)["pnl_usd"].sum()

    by_reason = trades.groupby("exit_reason")["pnl_usd"].agg(
        count="count", total_pnl="sum", avg_pnl="mean"
    ).round(2)
    by_signal = trades.groupby("signal")["pnl_usd"].agg(
        count="count", total_pnl="sum", avg_pnl="mean"
    ).round(2)

    print("\n" + "=" * 60)
    print("  BACKTEST  |  BTCUSD RSI+Supertrend  |  2026 YTD")
    print("=" * 60)
    print(f"  Period          :  Jan 1 – Jun 20, 2026")
    print(f"  Candle res      :  15 m")
    print(f"  Order size      :  {ORDER_SIZE:,} lots")
    print(f"  Strategy        :  RSI({RSI_PERIOD},{RSI_BULLISH}/{RSI_BEARISH}) + ST15m + ST4h + EMA1h({EMA_PERIOD})")
    print(f"  Entry           :  2-step ITM  (strike step ${STRIKE_STEP:,})")
    print(f"  Stop-loss       :  {int(SL_PCT*100)}% of entry premium")
    print(f"  Time filter     :  02:00 – 22:00 UTC only")
    print(f"  Premium model   :  Full Black-Scholes (r=0, dynamic T)")
    print()
    print(f"  Total Trades    :  {n}")
    print(f"  Win / Loss      :  {wins}W  /  {losses}L  ({wins/n*100:.1f}% win rate)")
    print(f"  Total P&L       :  ${total:,.2f}")
    print(f"  Avg Win         :  ${avg_w:,.2f}")
    print(f"  Avg Loss        :  ${avg_l:,.2f}")
    print(f"  Reward/Risk     :  {rr:.2f}x")
    print(f"  Max Drawdown    :  ${max_dd:,.2f}")
    print()
    print("  Monthly P&L:")
    for period, pnl in m_pnl.items():
        bar = "█" * max(int(abs(pnl) / max(m_pnl.abs().max(), 1) * 20), 1)
        sign = "+" if pnl >= 0 else "-"
        print(f"    {str(period):8s}  {sign}${abs(pnl):>10,.2f}  {bar}")
    print()
    print("  Exit Breakdown:")
    print(by_reason.to_string())
    print()
    print("  By Signal (CE / PE):")
    print(by_signal.to_string())
    print("=" * 60)

    trades.to_csv(OUTPUT_CSV, index=False)
    print(f"\n  Trade log saved → {OUTPUT_CSV}")

# ─────────────────────────────────────────────
# ENTRY
# ─────────────────────────────────────────────
if __name__ == "__main__":
    df     = fetch_all_candles(PERPETUAL_SYMBOL, RESOLUTION, BACKTEST_START, BACKTEST_END)
    trades = run_backtest(df)
    print_report(trades)
