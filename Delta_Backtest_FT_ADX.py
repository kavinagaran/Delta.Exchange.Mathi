"""
Flawless Trend + ADX entry filter — threshold sweep

Base config fixed at V4 winner: 1h candles · RSI 55/45 · min-hold 4 bars

ADX is an ENTRY filter only:
  - Skip new entry if ADX(14) ≤ threshold  (ranging market)
  - Never exit an existing position due to ADX dropping
  - Exit only on signal reversal (unchanged from V4)

Variants:
  A0  No ADX   (V4 baseline, for comparison)
  A1  ADX > 20
  A2  ADX > 25
  A3  ADX > 30

P&L model (BTCUSD inverse perpetual):
  Long  : ORDER_SIZE × (exit − entry) / entry
  Short : ORDER_SIZE × (entry − exit) / entry
"""

import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv
import os

load_dotenv()

BASE_URL         = os.getenv("BASE_URL", "https://api.india.delta.exchange")
PERPETUAL_SYMBOL = os.getenv("PERPETUAL_SYMBOL", "BTCUSD")
EMA_PERIOD       = int(os.getenv("EMA_PERIOD", 200))
RSI_PERIOD       = int(os.getenv("RSI_PERIOD", 14))
SUPERTREND_ATR   = int(os.getenv("SUPERTREND_ATR", 10))
SUPERTREND_MULT  = float(os.getenv("SUPERTREND_MULT", 3.0))
ORDER_SIZE       = int(os.getenv("ORDER_SIZE", 1000))

# V4 fixed settings
RSI_BULL  = 55.0
RSI_BEAR  = 45.0
MIN_HOLD  = 4       # bars
ADX_PERIOD = 14

BACKTEST_START = int(datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp())
BACKTEST_END   = int(datetime(2026, 7, 2, 23, 59, 59, tzinfo=timezone.utc).timestamp())
RESOLUTION     = "1h"
RES_SECONDS    = 3600
BATCH_SIZE     = 500
WARMUP_BARS    = EMA_PERIOD + ADX_PERIOD + 10

OUTPUT_DIR = r"D:\AI\Delta.Exchange"

VARIANTS = [
    {"name": "A0-NoADX",  "adx_thresh": 0},
    {"name": "A1-ADX>20", "adx_thresh": 20},
    {"name": "A2-ADX>25", "adx_thresh": 25},
    {"name": "A3-ADX>30", "adx_thresh": 30},
]

# ─────────────────────────────────────────────
# DATA FETCH
# ─────────────────────────────────────────────
def _dt(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

def fetch_all_candles() -> pd.DataFrame:
    all_rows, cursor = [], BACKTEST_START
    print(f"\nFetching {RESOLUTION} candles  {_dt(BACKTEST_START)} → {_dt(BACKTEST_END)}")
    print("-" * 56)

    while cursor < BACKTEST_END:
        batch_end = min(cursor + RES_SECONDS * BATCH_SIZE, BACKTEST_END)
        try:
            resp = requests.get(
                f"{BASE_URL}/v2/history/candles",
                params={"symbol": PERPETUAL_SYMBOL, "resolution": RESOLUTION,
                        "start": cursor, "end": batch_end},
                timeout=(5, 30),
            )
            resp.raise_for_status()
            rows = resp.json().get("result") or []
        except Exception as e:
            print(f"  Error ({_dt(cursor)}): {e} — retrying in 5 s")
            time.sleep(5)
            continue

        all_rows.extend(rows)
        print(f"  {_dt(cursor):19s} → {_dt(batch_end):19s}  ({len(rows):3d} candles)")
        cursor = batch_end + RES_SECONDS
        time.sleep(0.3)

    df = pd.DataFrame(all_rows)
    df.rename(columns={"time": "timestamp"}, inplace=True)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df.sort_values("timestamp", inplace=True)
    df.drop_duplicates("timestamp", inplace=True)
    df.reset_index(drop=True, inplace=True)
    print(f"\n  Total: {len(df):,} candles")
    return df

# ─────────────────────────────────────────────
# INDICATORS
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

def supertrend_series(df: pd.DataFrame, atr_period: int, mult: float) -> pd.Series:
    high  = df["high"].copy()
    low   = df["low"].copy()
    close = df["close"].copy()

    tr = pd.concat([
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

    # Wilder smoothing via EWM (com = period - 1)
    atr14      = tr.ewm(com=period - 1, min_periods=period, adjust=False).mean()
    plus_di14  = 100 * plus_dm.ewm(com=period - 1, min_periods=period, adjust=False).mean() / atr14
    minus_di14 = 100 * minus_dm.ewm(com=period - 1, min_periods=period, adjust=False).mean() / atr14

    dx  = 100 * (plus_di14 - minus_di14).abs() / (plus_di14 + minus_di14).replace(0, np.nan)
    adx = dx.ewm(com=period - 1, min_periods=period, adjust=False).mean()
    return adx

def compute_signals(df: pd.DataFrame, adx_thresh: float) -> tuple[pd.Series, pd.Series]:
    """Returns (signal_series, adx_series). Signal already has ADX gate applied."""
    close = df["close"]
    ema   = ema_series(close, EMA_PERIOD)
    rsi   = rsi_series(close, RSI_PERIOD)
    st    = supertrend_series(df, SUPERTREND_ATR, SUPERTREND_MULT)
    adx   = adx_series(df, ADX_PERIOD)

    trend_ok = (adx > adx_thresh) if adx_thresh > 0 else pd.Series(True, index=df.index)

    sig = pd.Series("neutral", index=df.index, dtype=object)
    sig[(st == "bullish") & (close > ema) & (rsi > RSI_BULL) & trend_ok] = "bullish"
    sig[(st == "bearish") & (close < ema) & (rsi < RSI_BEAR) & trend_ok] = "bearish"
    return sig, adx

# ─────────────────────────────────────────────
# BACKTEST ENGINE
# ─────────────────────────────────────────────
def run_variant(df: pd.DataFrame, signals: pd.Series) -> pd.DataFrame:
    trades: list[dict] = []

    direction   = "flat"
    entry_price = 0.0
    entry_time  = None
    last_signal = "neutral"
    bars_held   = 0

    def _close(exit_price: float, exit_time) -> None:
        nonlocal direction, entry_price, entry_time, last_signal, bars_held
        pnl = (ORDER_SIZE * (exit_price - entry_price) / entry_price
               if direction == "long"
               else ORDER_SIZE * (entry_price - exit_price) / entry_price)
        trades.append({
            "entry_time":  entry_time.strftime("%Y-%m-%d %H:%M"),
            "exit_time":   exit_time.strftime("%Y-%m-%d %H:%M"),
            "direction":   direction,
            "entry_price": round(entry_price, 2),
            "exit_price":  round(exit_price, 2),
            "pct_move":    round((exit_price - entry_price) / entry_price * 100
                                 * (1 if direction == "long" else -1), 3),
            "duration_h":  round((exit_time - entry_time).total_seconds() / 3600, 2),
            "pnl_usd":     round(pnl, 2),
        })
        direction = "flat"; entry_price = 0.0
        entry_time = None; last_signal = "neutral"; bars_held = 0

    for i in range(WARMUP_BARS, len(df)):
        sig      = signals.iloc[i]
        price    = float(df["close"].iloc[i])
        bar_time = datetime.fromtimestamp(int(df["timestamp"].iloc[i]), tz=timezone.utc)

        if direction != "flat":
            bars_held += 1

        if sig == "neutral" or sig == last_signal:
            continue

        # Reversal signal — respect min hold before allowing exit
        if direction != "flat" and bars_held < MIN_HOLD:
            continue

        if direction != "flat":
            _close(price, bar_time)

        direction   = "long" if sig == "bullish" else "short"
        entry_price = price
        entry_time  = bar_time
        last_signal = sig
        bars_held   = 0

    if direction != "flat":
        _close(float(df["close"].iloc[-1]),
               datetime.fromtimestamp(int(df["timestamp"].iloc[-1]), tz=timezone.utc))

    return pd.DataFrame(trades)

# ─────────────────────────────────────────────
# STATS + REPORT
# ─────────────────────────────────────────────
def summarise(name: str, trades: pd.DataFrame, adx_vals: pd.Series,
              adx_thresh: float) -> dict:
    if trades.empty:
        return {"name": name, "trades": 0}

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

    # How many candles were in trending regime (ADX > threshold)?
    if adx_thresh > 0:
        pct_trending = (adx_vals.iloc[WARMUP_BARS:] > adx_thresh).mean() * 100
    else:
        pct_trending = 100.0

    return {
        "name":         name,
        "trades":       n,
        "win_rate":     round(wins / n * 100, 1),
        "total":        round(total, 2),
        "avg_win":      round(avg_w, 2),
        "avg_loss":     round(avg_l, 2),
        "rr":           round(rr, 2),
        "max_dd":       round(max_dd, 2),
        "avg_dur_h":    round(avg_dur, 1),
        "pct_trending": round(pct_trending, 1),
    }

def print_comparison(results: list[dict], trade_logs: dict) -> None:
    print("\n" + "=" * 105)
    print("  FLAWLESS TREND + ADX FILTER  |  BTCUSD 1h FUTURES  |  2026 YTD  (Jan–Jul 2)")
    print("  Base: 1h · RSI 55/45 · Min-hold 4 bars · Exit on signal reversal")
    print("=" * 105)
    hdr = (f"  {'Variant':<14} {'Trades':>7} {'Win%':>6} {'Total $':>10} "
           f"{'Avg Win':>8} {'Avg Loss':>9} {'RR':>5} {'MaxDD':>8} {'AvgDur':>8} {'Trending%':>10}")
    print(hdr)
    print("  " + "-" * 99)
    for r in results:
        flag = "✓" if r.get("total", 0) > 0 else " "
        print(
            f"  {r['name']:<14} {r['trades']:>7} {r['win_rate']:>5.1f}% "
            f"{r['total']:>+10.2f} {r['avg_win']:>+8.2f} {r['avg_loss']:>+9.2f} "
            f"{r['rr']:>5.2f}× {r['max_dd']:>+8.2f} {r['avg_dur_h']:>6.1f}h  "
            f"{r['pct_trending']:>8.1f}%  {flag}"
        )
    print("=" * 105)

    print("\n  MONTHLY P&L")
    print("  " + "-" * 99)
    for name, trades in trade_logs.items():
        if trades.empty:
            continue
        monthly = pd.to_datetime(trades["entry_time"]).dt.to_period("M")
        m_pnl   = trades.groupby(monthly)["pnl_usd"].sum().round(2)
        row = "  ".join(
            f"{str(p)}: {'+' if x >= 0 else ''}{x:>7.2f}" for p, x in m_pnl.items()
        )
        print(f"  {name:<14}  {row}")

    print("\n  ADX DISTRIBUTION (1h bars, post-warmup)")
    print("  " + "-" * 60)
    # Print once — same ADX values for all variants (ADX doesn't change between variants)
    # We'll print this after we have the adx values

# ─────────────────────────────────────────────
# ENTRY
# ─────────────────────────────────────────────
if __name__ == "__main__":
    df = fetch_all_candles()

    results    = []
    trade_logs = {}
    adx_stored = None

    for v in VARIANTS:
        print(f"\nRunning {v['name']} ...", end=" ", flush=True)
        signals, adx = compute_signals(df, v["adx_thresh"])

        if adx_stored is None:
            adx_stored = adx

        trades = run_variant(df, signals)
        print(f"{len(trades)} trades")

        trade_logs[v["name"]] = trades
        results.append(summarise(v["name"], trades, adx, v["adx_thresh"]))

        safe = v["name"].replace(">", "gt")
        trades.to_csv(f"{OUTPUT_DIR}\\backtest_{safe}.csv", index=False)

    print_comparison(results, trade_logs)

    # ADX distribution summary
    adx_post = adx_stored.iloc[WARMUP_BARS:].dropna()
    print(f"\n  ADX(14) on 1h BTCUSD — Jan–Jul 2026")
    print(f"  Mean : {adx_post.mean():.1f}")
    print(f"  > 20 : {(adx_post > 20).mean()*100:.1f}% of bars")
    print(f"  > 25 : {(adx_post > 25).mean()*100:.1f}% of bars")
    print(f"  > 30 : {(adx_post > 30).mean()*100:.1f}% of bars")
