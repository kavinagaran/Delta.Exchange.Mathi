"""
Delta Exchange BTC Futures — Flawless Trend Backtest 2026  (canonical)

Mirrors Delta_Main.py signal logic bar-by-bar.  Winning configuration
derived from Delta_Backtest_FT_Compare.py (V4) + Delta_Backtest_FT_ADX.py.

  Signal (1h BTCUSD):
    Bullish : Supertrend(10, 3.0)=bullish  AND  close > EMA(200)
              AND  RSI(14) > 55  AND  ADX(14) > 30
    Bearish : Supertrend(10, 3.0)=bearish  AND  close < EMA(200)
              AND  RSI(14) < 45  AND  ADX(14) > 30
    Neutral : hold current position (no entry, no exit)

  Exit      : Signal reversal only (no stop-loss)
  Min-hold  : 4 bars before a reversal exit is allowed
  Both sides: Long AND Short

P&L model (BTCUSD inverse perpetual):
  Long  : pnl_usd = ORDER_SIZE × (exit_price − entry_price) / entry_price
  Short : pnl_usd = ORDER_SIZE × (entry_price − exit_price) / entry_price
"""

import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv
import os

load_dotenv()

# ─────────────────────────────────────────────
# CONFIG  (mirrors Delta_Main.py .env defaults)
# ─────────────────────────────────────────────
BASE_URL         = os.getenv("BASE_URL", "https://api.india.delta.exchange")
PERPETUAL_SYMBOL = os.getenv("PERPETUAL_SYMBOL", "BTCUSD")
EMA_PERIOD       = int(os.getenv("EMA_PERIOD", 200))
RSI_PERIOD       = int(os.getenv("RSI_PERIOD", 14))
RSI_BULL         = float(os.getenv("RSI_BULL", 55))
RSI_BEAR         = float(os.getenv("RSI_BEAR", 45))
SUPERTREND_ATR   = int(os.getenv("SUPERTREND_ATR", 10))
SUPERTREND_MULT  = float(os.getenv("SUPERTREND_MULT", 3.0))
ADX_PERIOD       = int(os.getenv("ADX_PERIOD", 14))
ADX_THRESHOLD    = float(os.getenv("ADX_THRESHOLD", 30))
ORDER_SIZE       = int(os.getenv("ORDER_SIZE", 1000))
MIN_HOLD         = int(os.getenv("MIN_HOLD", 4))   # bars before reversal exit allowed

BACKTEST_START = int(datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp())
BACKTEST_END   = int(datetime(2026, 7, 2, 23, 59, 59, tzinfo=timezone.utc).timestamp())
RESOLUTION     = "1h"
RES_SECONDS    = 3600
BATCH_SIZE     = 500
WARMUP_BARS    = EMA_PERIOD + ADX_PERIOD + 10

OUTPUT_CSV = r"D:\AI\Delta.Exchange\backtest_FT_2026_final.csv"

# ─────────────────────────────────────────────
# DATA FETCH
# ─────────────────────────────────────────────
def _dt(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

def fetch_all_candles(symbol: str, resolution: str,
                      start_ts: int, end_ts: int) -> pd.DataFrame:
    all_rows, cursor = [], start_ts
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
            rows = resp.json().get("result") or []
        except Exception as e:
            print(f"  Request error ({_dt(cursor)}): {e} — retrying in 5 s")
            time.sleep(5)
            continue

        all_rows.extend(rows)
        print(f"  {_dt(cursor):19s} → {_dt(batch_end):19s}  ({len(rows):3d} candles)")
        cursor = batch_end + RES_SECONDS
        time.sleep(0.3)

    if not all_rows:
        raise RuntimeError("No candle data returned.")

    df = pd.DataFrame(all_rows)
    df.rename(columns={"time": "timestamp"}, inplace=True)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df.sort_values("timestamp", inplace=True)
    df.drop_duplicates("timestamp", inplace=True)
    df.reset_index(drop=True, inplace=True)
    print(f"\n  Total candles: {len(df):,}")
    return df

# ─────────────────────────────────────────────
# INDICATORS  (vectorised)
# ─────────────────────────────────────────────
def ema_series(close: pd.Series, period: int) -> pd.Series:
    return close.ewm(span=period, adjust=False).mean()

def rsi_series(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_g = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_l = loss.ewm(com=period - 1, min_periods=period).mean()
    rs    = avg_g / avg_l
    return 100 - (100 / (1 + rs))

def supertrend_series(df: pd.DataFrame,
                      atr_period: int, mult: float) -> pd.Series:
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

    direction = pd.Series("neutral", index=df.index, dtype=object)
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

def adx_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high  = df["high"]
    low   = df["low"]
    close = df["close"]

    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)

    up   = high.diff()
    down = -low.diff()
    plus_dm  = pd.Series(
        np.where((up > down) & (up > 0), up, 0.0), index=df.index, dtype=float
    )
    minus_dm = pd.Series(
        np.where((down > up) & (down > 0), down, 0.0), index=df.index, dtype=float
    )

    atr14     = tr.ewm(com=period - 1, min_periods=period, adjust=False).mean()
    plus_di   = 100 * plus_dm.ewm(com=period - 1, min_periods=period, adjust=False).mean() / atr14
    minus_di  = 100 * minus_dm.ewm(com=period - 1, min_periods=period, adjust=False).mean() / atr14
    dx        = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(com=period - 1, min_periods=period, adjust=False).mean()

# ─────────────────────────────────────────────
# SIGNAL  (mirrors get_signal() in Delta_Main.py)
# ─────────────────────────────────────────────
def compute_signals(df: pd.DataFrame) -> pd.Series:
    """Returns per-bar signal: 'bullish' | 'bearish' | 'neutral'."""
    close    = df["close"]
    ema      = ema_series(close, EMA_PERIOD)
    rsi      = rsi_series(close, RSI_PERIOD)
    st       = supertrend_series(df, SUPERTREND_ATR, SUPERTREND_MULT)
    adx      = adx_series(df, ADX_PERIOD)
    trend_ok = adx > ADX_THRESHOLD

    sig = pd.Series("neutral", index=df.index, dtype=object)
    sig[(st == "bullish") & (close > ema) & (rsi > RSI_BULL) & trend_ok] = "bullish"
    sig[(st == "bearish") & (close < ema) & (rsi < RSI_BEAR) & trend_ok] = "bearish"
    return sig

# ─────────────────────────────────────────────
# BACKTEST ENGINE
# ─────────────────────────────────────────────
def run_backtest(df: pd.DataFrame) -> pd.DataFrame:
    signals = compute_signals(df)

    trades: list[dict] = []

    direction   = "flat"
    entry_price = 0.0
    entry_time  = None
    last_signal = "neutral"
    bars_held   = 0

    def _close_trade(exit_price: float, exit_time, reason: str) -> None:
        nonlocal direction, entry_price, entry_time, last_signal, bars_held
        if direction == "flat":
            return
        pnl = (ORDER_SIZE * (exit_price - entry_price) / entry_price
               if direction == "long"
               else ORDER_SIZE * (entry_price - exit_price) / entry_price)
        dur = (exit_time - entry_time).total_seconds() / 3600
        trades.append({
            "entry_time":  entry_time.strftime("%Y-%m-%d %H:%M"),
            "exit_time":   exit_time.strftime("%Y-%m-%d %H:%M"),
            "direction":   direction,
            "entry_price": round(entry_price, 2),
            "exit_price":  round(exit_price, 2),
            "pct_move":    round((exit_price - entry_price) / entry_price * 100
                                 * (1 if direction == "long" else -1), 3),
            "duration_h":  round(dur, 2),
            "pnl_usd":     round(pnl, 2),
            "exit_reason": reason,
        })
        direction   = "flat"
        entry_price = 0.0
        entry_time  = None
        last_signal = "neutral"
        bars_held   = 0

    for i in range(WARMUP_BARS, len(df)):
        sig      = signals.iloc[i]
        price    = float(df["close"].iloc[i])
        bar_time = datetime.fromtimestamp(int(df["timestamp"].iloc[i]), tz=timezone.utc)

        if direction != "flat":
            bars_held += 1

        if sig == "neutral" or sig == last_signal:
            continue

        # Signal changed — respect minimum hold
        if direction != "flat" and bars_held < MIN_HOLD:
            continue

        if direction != "flat":
            _close_trade(price, bar_time, "Reversal")

        direction   = "long" if sig == "bullish" else "short"
        entry_price = price
        entry_time  = bar_time
        last_signal = sig
        bars_held   = 0

    if direction != "flat":
        last_price = float(df["close"].iloc[-1])
        last_time  = datetime.fromtimestamp(int(df["timestamp"].iloc[-1]), tz=timezone.utc)
        _close_trade(last_price, last_time, "End")

    return pd.DataFrame(trades)

# ─────────────────────────────────────────────
# REPORT
# ─────────────────────────────────────────────
def print_report(trades: pd.DataFrame) -> None:
    if trades.empty:
        print("\nNo trades generated.")
        return

    n      = len(trades)
    wins   = (trades["pnl_usd"] > 0).sum()
    losses = n - wins
    total  = trades["pnl_usd"].sum()
    avg_w  = trades.loc[trades["pnl_usd"] > 0, "pnl_usd"].mean() if wins   else 0.0
    avg_l  = trades.loc[trades["pnl_usd"] < 0, "pnl_usd"].mean() if losses else 0.0
    rr     = abs(avg_w / avg_l) if avg_l else float("inf")
    avg_dur = trades["duration_h"].mean()

    trades["cum_pnl"] = trades["pnl_usd"].cumsum()
    peak   = trades["cum_pnl"].cummax()
    max_dd = (trades["cum_pnl"] - peak).min()

    monthly = (
        pd.to_datetime(trades["entry_time"])
          .dt.to_period("M")
          .rename("month")
    )
    m_pnl = trades.groupby(monthly)["pnl_usd"].sum()

    by_dir = trades.groupby("direction")["pnl_usd"].agg(
        count="count", total="sum", avg="mean", wins=lambda x: (x > 0).sum()
    ).round(2)

    print("\n" + "=" * 66)
    print("  FLAWLESS TREND BACKTEST  |  BTCUSD FUTURES  |  2026 YTD")
    print("=" * 66)
    print(f"  Period        :  Jan 1 – Jul 2, 2026")
    print(f"  Candle res    :  {RESOLUTION}")
    print(f"  Order size    :  {ORDER_SIZE:,} contracts  ($1 each)")
    print(f"  Signal        :  ST({SUPERTREND_ATR},{SUPERTREND_MULT})"
          f" + EMA({EMA_PERIOD}) + RSI({RSI_PERIOD}) {RSI_BULL:.0f}/{RSI_BEAR:.0f}"
          f" + ADX({ADX_PERIOD})>{ADX_THRESHOLD:.0f}")
    print(f"  Min-hold      :  {MIN_HOLD} bars before reversal exit")
    print(f"  Exit          :  Signal reversal only  (no stop-loss)")
    print()
    print(f"  Total Trades  :  {n}")
    print(f"  Win / Loss    :  {wins}W / {losses}L  ({wins/n*100:.1f}% win rate)")
    print(f"  Total P&L     :  ${total:,.2f}")
    print(f"  Avg Win       :  ${avg_w:,.2f}")
    print(f"  Avg Loss      :  ${avg_l:,.2f}")
    print(f"  Reward/Risk   :  {rr:.2f}×")
    print(f"  Max Drawdown  :  ${max_dd:,.2f}")
    print(f"  Avg Duration  :  {avg_dur:.1f}h")
    print()

    print("  Monthly P&L:")
    max_abs = m_pnl.abs().max() or 1
    for period, pnl in m_pnl.items():
        bar  = "█" * max(int(abs(pnl) / max_abs * 24), 1)
        sign = "+" if pnl >= 0 else "-"
        print(f"    {str(period):8s}  {sign}${abs(pnl):>10,.2f}  {bar}")

    print()
    print("  By Direction:")
    print(by_dir.to_string())
    print("=" * 66)

    trades.to_csv(OUTPUT_CSV, index=False)
    print(f"\n  Trade log → {OUTPUT_CSV}")

# ─────────────────────────────────────────────
# ENTRY
# ─────────────────────────────────────────────
if __name__ == "__main__":
    df     = fetch_all_candles(PERPETUAL_SYMBOL, RESOLUTION, BACKTEST_START, BACKTEST_END)
    trades = run_backtest(df)
    print_report(trades)
