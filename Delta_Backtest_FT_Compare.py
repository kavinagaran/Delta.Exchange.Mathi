"""
Flawless Trend — 5-variant parameter comparison

V0  Baseline   : 15m · RSI 50/50 · no min-hold
V1  1h candles : 1h  · RSI 50/50 · no min-hold
V2  RSI 55/45  : 15m · RSI 55/45 · no min-hold
V3  Min hold 4 : 15m · RSI 50/50 · hold ≥ 4 bars before allowing reversal
V4  All three  : 1h  · RSI 55/45 · hold ≥ 4 bars

P&L model (inverse perpetual):
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

BACKTEST_START = int(datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp())
BACKTEST_END   = int(datetime(2026, 7, 2, 23, 59, 59, tzinfo=timezone.utc).timestamp())
WARMUP_BARS    = EMA_PERIOD + 50

OUTPUT_DIR = r"D:\AI\Delta.Exchange"

VARIANTS = [
    {"name": "V0-Baseline",    "res": "15m", "rsi_bull": 50.0, "rsi_bear": 50.0, "min_hold": 0},
    {"name": "V1-1h-Candles",  "res": "1h",  "rsi_bull": 50.0, "rsi_bear": 50.0, "min_hold": 0},
    {"name": "V2-RSI-55/45",   "res": "15m", "rsi_bull": 55.0, "rsi_bear": 45.0, "min_hold": 0},
    {"name": "V3-MinHold-4",   "res": "15m", "rsi_bull": 50.0, "rsi_bear": 50.0, "min_hold": 4},
    {"name": "V4-AllThree",    "res": "1h",  "rsi_bull": 55.0, "rsi_bear": 45.0, "min_hold": 4},
]

RES_SECONDS = {"15m": 900, "1h": 3600}
BATCH_SIZE  = 500

# ─────────────────────────────────────────────
# DATA FETCH
# ─────────────────────────────────────────────
def _dt(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

def fetch_all_candles(resolution: str) -> pd.DataFrame:
    step     = RES_SECONDS[resolution]
    all_rows = []
    cursor   = BACKTEST_START

    print(f"\nFetching {resolution} candles  {_dt(BACKTEST_START)} → {_dt(BACKTEST_END)}")
    print("-" * 56)

    while cursor < BACKTEST_END:
        batch_end = min(cursor + step * BATCH_SIZE, BACKTEST_END)
        try:
            resp = requests.get(
                f"{BASE_URL}/v2/history/candles",
                params={"symbol": PERPETUAL_SYMBOL, "resolution": resolution,
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
        cursor = batch_end + step
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

def compute_signals(df: pd.DataFrame, rsi_bull: float, rsi_bear: float) -> pd.Series:
    close = df["close"]
    ema   = ema_series(close, EMA_PERIOD)
    rsi   = rsi_series(close, RSI_PERIOD)
    st    = supertrend_series(df, SUPERTREND_ATR, SUPERTREND_MULT)

    sig = pd.Series("neutral", index=df.index, dtype=object)
    sig[(st == "bullish") & (close > ema) & (rsi > rsi_bull)] = "bullish"
    sig[(st == "bearish") & (close < ema) & (rsi < rsi_bear)] = "bearish"
    return sig

# ─────────────────────────────────────────────
# BACKTEST ENGINE
# ─────────────────────────────────────────────
def run_variant(df: pd.DataFrame, signals: pd.Series,
                min_hold: int) -> pd.DataFrame:
    trades: list[dict] = []

    direction   = "flat"
    entry_price = 0.0
    entry_time  = None
    last_signal = "neutral"
    bars_held   = 0

    def _close(exit_price: float, exit_time, reason: str) -> None:
        nonlocal direction, entry_price, entry_time, last_signal, bars_held
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

        # Hold if neutral or same signal
        if sig == "neutral" or sig == last_signal:
            continue

        # Signal changed — respect minimum hold
        if direction != "flat" and bars_held < min_hold:
            continue

        # Close current position
        if direction != "flat":
            _close(price, bar_time, "Reversal")

        # Open new position
        direction   = "long" if sig == "bullish" else "short"
        entry_price = price
        entry_time  = bar_time
        last_signal = sig
        bars_held   = 0

    # Close any open position at end of period
    if direction != "flat":
        last_price = float(df["close"].iloc[-1])
        last_time  = datetime.fromtimestamp(int(df["timestamp"].iloc[-1]), tz=timezone.utc)
        _close(last_price, last_time, "End")

    return pd.DataFrame(trades)

# ─────────────────────────────────────────────
# STATS
# ─────────────────────────────────────────────
def summarise(name: str, trades: pd.DataFrame) -> dict:
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

    return {
        "name":     name,
        "trades":   n,
        "win_rate": round(wins / n * 100, 1),
        "total":    round(total, 2),
        "avg_win":  round(avg_w, 2),
        "avg_loss": round(avg_l, 2),
        "rr":       round(rr, 2),
        "max_dd":   round(max_dd, 2),
        "avg_dur_h":round(avg_dur, 1),
    }

def print_comparison(results: list[dict]) -> None:
    print("\n" + "=" * 95)
    print("  FLAWLESS TREND — VARIANT COMPARISON  |  BTCUSD FUTURES  |  2026 YTD  (Jan–Jul)")
    print("=" * 95)
    hdr = (f"  {'Variant':<20} {'Trades':>7} {'Win%':>6} {'Total $':>10} "
           f"{'Avg Win':>8} {'Avg Loss':>9} {'RR':>5} {'MaxDD':>8} {'AvgDur':>8}")
    print(hdr)
    print("  " + "-" * 91)
    for r in results:
        if r.get("trades", 0) == 0:
            print(f"  {r['name']:<20}   no trades")
            continue
        win_flag = "✓" if r["total"] > 0 else " "
        print(
            f"  {r['name']:<20} {r['trades']:>7} {r['win_rate']:>5.1f}% "
            f"{r['total']:>+10.2f} {r['avg_win']:>+8.2f} {r['avg_loss']:>+9.2f} "
            f"{r['rr']:>5.2f}× {r['max_dd']:>+8.2f} {r['avg_dur_h']:>6.1f}h  {win_flag}"
        )
    print("=" * 95)

# ─────────────────────────────────────────────
# ENTRY
# ─────────────────────────────────────────────
if __name__ == "__main__":
    # Fetch once per resolution, reuse across variants
    data_cache: dict[str, pd.DataFrame] = {}

    results = []
    trade_logs: dict[str, pd.DataFrame] = {}

    for v in VARIANTS:
        res = v["res"]
        if res not in data_cache:
            data_cache[res] = fetch_all_candles(res)

        df      = data_cache[res]
        signals = compute_signals(df, v["rsi_bull"], v["rsi_bear"])

        print(f"\nRunning {v['name']} ...", end=" ", flush=True)
        trades = run_variant(df, signals, v["min_hold"])
        print(f"{len(trades)} trades")

        trade_logs[v["name"]] = trades
        results.append(summarise(v["name"], trades))

        safe_name = v["name"].replace("/", "-")
        out = f"{OUTPUT_DIR}\\backtest_{safe_name}.csv"
        trades.to_csv(out, index=False)

    print_comparison(results)

    # Monthly breakdown per variant
    print("\n  MONTHLY P&L BREAKDOWN")
    print("  " + "-" * 91)
    for v, r in zip(VARIANTS, results):
        name   = v["name"]
        trades = trade_logs[name]
        if trades.empty:
            continue
        monthly = (
            pd.to_datetime(trades["entry_time"])
              .dt.to_period("M")
        )
        m_pnl = trades.groupby(monthly)["pnl_usd"].sum().round(2)
        row   = "  ".join(f"{str(p):>8}: {'+' if x >= 0 else ''}{x:>8.2f}"
                          for p, x in m_pnl.items())
        print(f"  {name:<20}  {row}")
