"""
S4 Strategy Refinement  —  EMA Cross(9/21) + RSI + 4h macro filter
Tests 7 targeted variations of the winning S4 strategy.

Shared config: 50% SL · 2-step ITM · Full BS pricing · Time filter 02–22 UTC

Variants
─────────────────────────────────────────────────────────────
S4a  Baseline   : EMA(9/21) cross + RSI(14) 55/45 + 4h EMA(9/21) cross
S4b  Tight RSI  : same + RSI threshold raised to 60/40
S4c  RSI(9)     : faster RSI period (9 instead of 14), thresholds 55/45
S4d  Volume     : S4a + volume > 1.2× 20-bar rolling average
S4e  Freshness  : S4a + crossover must be ≤ 4 bars old (45 min)
S4f  ST4h       : replace 4h EMA cross with 4h Supertrend direction
S4g  Full combo : RSI(14) 60/40 + ST4h + volume > 1.2× avg + fresh ≤ 4 bars
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
SL_PCT           = float(os.getenv("SL_PCT", 0.50))
STRIKE_STEP      = int(os.getenv("STRIKE_STEP", 1000))
ORDER_SIZE       = int(os.getenv("ORDER_SIZE", 1000))
IV_FLOOR         = 0.60

BACKTEST_START = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
BACKTEST_END   = int(datetime(2026, 6, 20, 23, 59, 59, tzinfo=timezone.utc).timestamp())
RESOLUTION     = "15m"
RES_SECONDS    = 900
BATCH_SIZE     = 500
WARMUP_BARS    = 150
OUTPUT_DIR     = r"D:\LocalGIT\Delta.Exchange"

# ─────────────────────────────────────────────
# DATA FETCH
# ─────────────────────────────────────────────
def _dt(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

def fetch_all_candles(symbol, resolution, start_ts, end_ts):
    all_rows, cursor = [], start_ts
    print(f"\nFetching {resolution} candles  {_dt(start_ts)} → {_dt(end_ts)}")
    print("-" * 56)
    while cursor < end_ts:
        batch_end = min(cursor + RES_SECONDS * BATCH_SIZE, end_ts)
        try:
            resp = requests.get(f"{BASE_URL}/v2/history/candles",
                                params={"symbol": symbol, "resolution": resolution,
                                        "start": cursor, "end": batch_end},
                                timeout=(5, 30))
            resp.raise_for_status()
            rows = resp.json().get("result") or []
        except Exception as e:
            print(f"  Retry ({e})"); time.sleep(5); continue
        all_rows.extend(rows)
        print(f"  {_dt(cursor):19s} → {_dt(batch_end):19s}  ({len(rows):3d} candles)")
        cursor = batch_end + RES_SECONDS
        time.sleep(0.3)
    df = pd.DataFrame(all_rows)
    df.rename(columns={"time": "timestamp"}, inplace=True)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df.sort_values("timestamp", inplace=True)
    df.drop_duplicates("timestamp", inplace=True)
    df.reset_index(drop=True, inplace=True)
    print(f"\n  Total candles : {len(df)}")
    return df

# ─────────────────────────────────────────────
# BLACK-SCHOLES PRICING
# ─────────────────────────────────────────────
def _ncdf(x: float) -> float:
    t    = 1.0 / (1.0 + 0.2316419 * abs(x))
    poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937
           + t * (-1.821255978 + t * 1.330274429))))
    c    = 1.0 - math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi) * poly
    return c if x >= 0 else 1.0 - c

def bs_price(S, K, T, sigma, opt):
    if T <= 1e-8:
        return max(S - K, 0.0) if opt == "call" else max(K - S, 0.0)
    sq = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * sigma ** 2 * T) / sq
    d2 = d1 - sq
    return (S * _ncdf(d1) - K * _ncdf(d2)) if opt == "call" else (K * _ncdf(-d2) - S * _ncdf(-d1))

def itm_strike(spot, opt):
    atm = round(spot / STRIKE_STEP) * STRIKE_STEP
    return (atm - 2 * STRIKE_STEP) if opt == "call" else (atm + 2 * STRIKE_STEP)

def hours_to_eod(ts):
    return max(((ts // 86400) + 1) * 86400 - ts, 0) / 3600.0

# ─────────────────────────────────────────────
# INDICATORS
# ─────────────────────────────────────────────
def _rsi(close, period=14):
    d = close.diff()
    g = d.clip(lower=0).ewm(com=period - 1, min_periods=period).mean()
    l = (-d.clip(upper=0)).ewm(com=period - 1, min_periods=period).mean()
    return 100 - 100 / (1 + g / l)

def _ema(close, period):
    return close.ewm(span=period, adjust=False).mean()

def _realised_vol(close, window=30):
    return np.log(close / close.shift(1)).rolling(window).std() * math.sqrt(96 * 252)

def _supertrend(df, atr_period=10, mult=3.0):
    h, l, c = df["high"].copy(), df["low"].copy(), df["close"].copy()
    tr  = pd.concat([(h - l), (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(com=atr_period - 1, min_periods=atr_period).mean()
    hl2 = (h + l) / 2
    ub, lb = (hl2 + mult * atr).copy(), (hl2 - mult * atr).copy()
    direction = pd.Series("neutral", index=df.index, dtype=str)
    st        = pd.Series(np.nan, index=df.index, dtype=float)
    for i in range(1, len(df)):
        if c.iloc[i-1] <= ub.iloc[i-1]: ub.iloc[i] = min(ub.iloc[i], ub.iloc[i-1])
        if c.iloc[i-1] >= lb.iloc[i-1]: lb.iloc[i] = max(lb.iloc[i], lb.iloc[i-1])
        if i == 1:
            st.iloc[i], direction.iloc[i] = ub.iloc[i], "bearish"
        elif st.iloc[i-1] == ub.iloc[i-1]:
            if c.iloc[i] <= ub.iloc[i]: st.iloc[i], direction.iloc[i] = ub.iloc[i], "bearish"
            else:                        st.iloc[i], direction.iloc[i] = lb.iloc[i], "bullish"
        else:
            if c.iloc[i] >= lb.iloc[i]: st.iloc[i], direction.iloc[i] = lb.iloc[i], "bullish"
            else:                        st.iloc[i], direction.iloc[i] = ub.iloc[i], "bearish"
    return direction

# ─────────────────────────────────────────────
# SHARED PRE-COMPUTATION  (runs once)
# ─────────────────────────────────────────────
def compute_shared(df):
    idx   = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    close = df["close"]
    c_s   = pd.Series(close.values, index=idx)

    print("  Computing shared indicators...")

    # RSI periods
    rsi14 = _rsi(close, 14).values
    rsi9  = _rsi(close, 9).values

    # 15m EMA cross
    e9_15m  = _ema(close, 9).values
    e21_15m = _ema(close, 21).values

    # Detect fresh EMA crossovers (within 4 bars)
    cross_bull_raw = np.zeros(len(close), dtype=np.int8)
    cross_bear_raw = np.zeros(len(close), dtype=np.int8)
    for i in range(1, len(e9_15m)):
        if e9_15m[i] > e21_15m[i] and e9_15m[i-1] <= e21_15m[i-1]:
            cross_bull_raw[i] = 1
        if e9_15m[i] < e21_15m[i] and e9_15m[i-1] >= e21_15m[i-1]:
            cross_bear_raw[i] = 1
    # Propagate: signal stays active for 4 bars after the crossover
    fresh_bull = pd.Series(cross_bull_raw).rolling(4, min_periods=1).max().values.astype(bool)
    fresh_bear = pd.Series(cross_bear_raw).rolling(4, min_periods=1).max().values.astype(bool)

    # 4h EMA cross mapped back to 15m
    e9_4h  = c_s.resample("4h").last().dropna().ewm(span=9,  adjust=False).mean().reindex(idx, method="ffill").values
    e21_4h = c_s.resample("4h").last().dropna().ewm(span=21, adjust=False).mean().reindex(idx, method="ffill").values

    # 4h Supertrend mapped to 15m
    df_4h = pd.DataFrame({
        "high":  pd.Series(df["high"].values,  index=idx).resample("4h").max(),
        "low":   pd.Series(df["low"].values,   index=idx).resample("4h").min(),
        "close": c_s.resample("4h").last(),
    }).dropna()
    st4h_s = _supertrend(df_4h, 10, 3.0)
    st4h   = st4h_s.reindex(idx, method="ffill").fillna("neutral").values
    st4hb  = (pd.Series(st4h) == "bullish").values
    st4hr  = (pd.Series(st4h) == "bearish").values

    # Volume filter: vol > 1.2× 20-bar rolling mean
    vol_ma  = df["volume"].rolling(20).mean()
    vol_ok  = (df["volume"] > vol_ma * 1.2).values

    # Realised vol for BS pricing
    rvol = _realised_vol(close, 30).values

    # Time mask 02:00–22:00 UTC
    hours   = idx.dt.hour.values
    time_ok = (hours >= 2) & (hours < 22)

    return dict(
        rsi14=rsi14, rsi9=rsi9,
        e9_15m=e9_15m, e21_15m=e21_15m,
        fresh_bull=fresh_bull, fresh_bear=fresh_bear,
        e9_4h=e9_4h, e21_4h=e21_4h,
        st4hb=st4hb, st4hr=st4hr,
        vol_ok=vol_ok, rvol=rvol,
        time_ok=time_ok, idx=idx,
        close=close.values,
    )

# ─────────────────────────────────────────────
# SIGNAL ARRAY BUILDER
# ─────────────────────────────────────────────
def _sig_array(bull, bear, n):
    out = np.zeros(n, dtype=np.int8)
    out[bull] = 1
    out[bear] = -1
    return out

# ─────────────────────────────────────────────
# S4 VARIANT SIGNAL FUNCTIONS
# ─────────────────────────────────────────────
def S4a_base(df, sh):
    """Baseline: EMA(9/21) cross + RSI(14) 55/45 + 4h EMA(9/21) cross"""
    e9, e21 = sh["e9_15m"], sh["e21_15m"]
    r       = sh["rsi14"]
    b = (e9 > e21) & (r > 55) & (sh["e9_4h"] > sh["e21_4h"]) & sh["time_ok"]
    r_ = (e9 < e21) & (r < 45) & (sh["e9_4h"] < sh["e21_4h"]) & sh["time_ok"]
    return _sig_array(b, r_, len(df))

def S4b_tight_rsi(df, sh):
    """Tighter RSI thresholds: 60/40 (vs baseline 55/45)"""
    e9, e21 = sh["e9_15m"], sh["e21_15m"]
    r       = sh["rsi14"]
    b = (e9 > e21) & (r > 60) & (sh["e9_4h"] > sh["e21_4h"]) & sh["time_ok"]
    r_ = (e9 < e21) & (r < 40) & (sh["e9_4h"] < sh["e21_4h"]) & sh["time_ok"]
    return _sig_array(b, r_, len(df))

def S4c_rsi9(df, sh):
    """Faster RSI(9) instead of RSI(14), thresholds 55/45"""
    e9, e21 = sh["e9_15m"], sh["e21_15m"]
    r       = sh["rsi9"]
    b = (e9 > e21) & (r > 55) & (sh["e9_4h"] > sh["e21_4h"]) & sh["time_ok"]
    r_ = (e9 < e21) & (r < 45) & (sh["e9_4h"] < sh["e21_4h"]) & sh["time_ok"]
    return _sig_array(b, r_, len(df))

def S4d_volume(df, sh):
    """S4a + volume > 1.2× 20-bar rolling average"""
    e9, e21 = sh["e9_15m"], sh["e21_15m"]
    r       = sh["rsi14"]
    b = (e9 > e21) & (r > 55) & (sh["e9_4h"] > sh["e21_4h"]) & sh["vol_ok"] & sh["time_ok"]
    r_ = (e9 < e21) & (r < 45) & (sh["e9_4h"] < sh["e21_4h"]) & sh["vol_ok"] & sh["time_ok"]
    return _sig_array(b, r_, len(df))

def S4e_fresh(df, sh):
    """S4a + entry only within 4 bars (60 min) of actual EMA crossover"""
    e9, e21 = sh["e9_15m"], sh["e21_15m"]
    r       = sh["rsi14"]
    b = (e9 > e21) & sh["fresh_bull"] & (r > 55) & (sh["e9_4h"] > sh["e21_4h"]) & sh["time_ok"]
    r_ = (e9 < e21) & sh["fresh_bear"] & (r < 45) & (sh["e9_4h"] < sh["e21_4h"]) & sh["time_ok"]
    return _sig_array(b, r_, len(df))

def S4f_st4h(df, sh):
    """Replace 4h EMA cross with 4h Supertrend direction"""
    e9, e21 = sh["e9_15m"], sh["e21_15m"]
    r       = sh["rsi14"]
    b = (e9 > e21) & (r > 55) & sh["st4hb"] & sh["time_ok"]
    r_ = (e9 < e21) & (r < 45) & sh["st4hr"] & sh["time_ok"]
    return _sig_array(b, r_, len(df))

def S4g_full(df, sh):
    """Full combo: RSI(14) 60/40 + ST4h + volume > 1.2× avg + fresh ≤ 4 bars"""
    e9, e21 = sh["e9_15m"], sh["e21_15m"]
    r       = sh["rsi14"]
    b = (e9 > e21) & sh["fresh_bull"] & (r > 60) & sh["st4hb"] & sh["vol_ok"] & sh["time_ok"]
    r_ = (e9 < e21) & sh["fresh_bear"] & (r < 40) & sh["st4hr"] & sh["vol_ok"] & sh["time_ok"]
    return _sig_array(b, r_, len(df))

STRATEGIES = [
    ("S4a  Baseline  : EMA(9/21) + RSI14 55/45 + 4h EMA",   S4a_base),
    ("S4b  Tight RSI : EMA(9/21) + RSI14 60/40 + 4h EMA",   S4b_tight_rsi),
    ("S4c  RSI(9)    : EMA(9/21) + RSI9  55/45 + 4h EMA",   S4c_rsi9),
    ("S4d  Volume    : S4a + vol > 1.2× avg",                S4d_volume),
    ("S4e  Fresh     : S4a + crossover ≤ 4 bars old",        S4e_fresh),
    ("S4f  ST4h      : EMA(9/21) + RSI14 55/45 + ST4h",     S4f_st4h),
    ("S4g  Full combo: RSI14 60/40 + ST4h + vol + fresh",    S4g_full),
]

# ─────────────────────────────────────────────
# BACKTEST ENGINE
# ─────────────────────────────────────────────
def run_backtest(df, signals, sh):
    rvol  = sh["rvol"]
    close = sh["close"]
    tss   = df["timestamp"].values.astype(int)

    trades = []
    in_trade = False
    e_sig = e_prem = e_strike = e_iv = e_spot = sl_lvl = 0.0
    e_opt = ""
    e_time = None

    def _enter(sig, i):
        nonlocal in_trade, e_sig, e_prem, e_strike, e_iv, e_spot, sl_lvl, e_opt, e_time
        opt      = "call" if sig == 1 else "put"
        S        = close[i]
        K        = itm_strike(S, opt)
        rv       = float(rvol[i])
        iv       = max(rv if not np.isnan(rv) else IV_FLOOR, IV_FLOOR)
        T        = hours_to_eod(tss[i]) / 8760
        prem     = bs_price(S, K, T, iv, opt)
        e_sig    = sig; e_prem = prem; e_strike = K; e_iv = iv
        e_spot   = S;   sl_lvl  = prem * (1 - SL_PCT)
        e_opt    = opt; e_time  = datetime.fromtimestamp(tss[i], tz=timezone.utc)
        in_trade = True

    for i in range(WARMUP_BARS, len(df)):
        s      = int(signals[i])
        ts     = tss[i]
        bar_dt = datetime.fromtimestamp(ts, tz=timezone.utc)

        if not in_trade:
            if s != 0:
                _enter(s, i)
            continue

        T_now    = hours_to_eod(ts) / 8760
        cur_prem = max(bs_price(close[i], e_strike, T_now, e_iv, e_opt), 0.0)

        sl_hit  = cur_prem <= sl_lvl
        flipped = (s != 0) and (s != e_sig)
        eod     = (bar_dt.hour == 23 and bar_dt.minute >= 45)

        if sl_hit or flipped or eod:
            trades.append({
                "entry_time":    e_time.strftime("%Y-%m-%d %H:%M"),
                "exit_time":     bar_dt.strftime("%Y-%m-%d %H:%M"),
                "signal":        "bullish" if e_sig == 1 else "bearish",
                "entry_spot":    round(e_spot, 2),
                "exit_spot":     round(close[i], 2),
                "strike":        round(e_strike, 2),
                "entry_premium": round(e_prem, 4),
                "exit_premium":  round(cur_prem, 4),
                "exit_reason":   "SL" if sl_hit else ("EOD" if eod else "Flip"),
                "pnl_usd":       round((cur_prem - e_prem) * ORDER_SIZE, 2),
            })
            in_trade = False
            if not sl_hit and s != 0:
                _enter(s, i)

    return pd.DataFrame(trades)

# ─────────────────────────────────────────────
# STATS
# ─────────────────────────────────────────────
def summarise(name, trades):
    if trades.empty:
        return dict(name=name, trades=0, win_pct=0, rr=0,
                    net_pnl=0, max_dd=0, sl_rate=0, monthly=pd.Series(dtype=float))
    n      = len(trades)
    wins   = (trades["pnl_usd"] > 0).sum()
    losses = n - wins
    total  = trades["pnl_usd"].sum()
    avg_w  = trades.loc[trades["pnl_usd"] > 0, "pnl_usd"].mean() if wins   else 0
    avg_l  = trades.loc[trades["pnl_usd"] < 0, "pnl_usd"].mean() if losses else 0
    rr     = abs(avg_w / avg_l) if avg_l != 0 else float("inf")
    cum    = trades["pnl_usd"].cumsum()
    max_dd = (cum - cum.cummax()).min()
    sl_rt  = (trades["exit_reason"] == "SL").mean() * 100
    _mlist = pd.to_datetime(trades["entry_time"]).dt.strftime("%Y-%m").tolist()
    _plist = trades["pnl_usd"].tolist()
    _macc: dict = {}
    for _m, _p in zip(_mlist, _plist):
        _macc[_m] = _macc.get(_m, 0.0) + _p
    monthly = pd.Series(_macc).sort_index()
    return dict(name=name, trades=n, win_pct=wins / n * 100, rr=rr,
                net_pnl=total, max_dd=max_dd, sl_rate=sl_rt,
                monthly=monthly, avg_win=avg_w, avg_loss=avg_l)

# ─────────────────────────────────────────────
# REPORT
# ─────────────────────────────────────────────
def print_comparison(results):
    ranked = sorted(results, key=lambda x: x["net_pnl"], reverse=True)
    w = 50

    print("\n" + "=" * 112)
    print(f"  {'S4 VARIANT':<{w}}  {'TRADES':>6}  {'WIN%':>5}  {'RR':>5}  "
          f"{'NET P&L':>14}  {'MAX DD':>14}  {'SL%':>5}")
    print("=" * 112)
    for r in ranked:
        sign    = "+" if r["net_pnl"] >= 0 else ""
        dd_str  = f"-${abs(r['max_dd']):>11,.0f}"
        pnl_str = f"{sign}${r['net_pnl']:>11,.0f}"
        print(f"  {r['name']:<{w}}  {r['trades']:>6}  {r['win_pct']:>4.1f}%  "
              f"{r['rr']:>4.2f}x  {pnl_str:>14}  {dd_str:>14}  {r['sl_rate']:>4.1f}%")
    print("=" * 112)

    # Monthly P&L matrix
    months = []
    for r in results:
        if "monthly" in r and not r["monthly"].empty:
            months = sorted(r["monthly"].index.tolist())
            break

    if months:
        labels = [r["name"].split(":")[0].strip() for r in results]
        col_w  = 14
        print(f"\n  MONTHLY P&L  (USD)  —  cols in strategy order\n")
        hdr = f"  {'Month':<9}" + "".join(f"  {lb:>{col_w}}" for lb in labels)
        print(hdr)
        print("  " + "-" * (9 + (col_w + 2) * len(results)))
        for m in months:
            row = f"  {m:<9}"
            for r in results:
                pnl = r["monthly"].get(m, 0)
                tag = f"+${pnl:,.0f}" if pnl >= 0 else f"-${abs(pnl):,.0f}"
                row += f"  {tag:>{col_w}}"
            print(row)
        print()

    best = ranked[0]
    print(f"  BEST VARIANT:  {best['name']}")
    print(f"  Net P&L ${best['net_pnl']:,.0f}  |  "
          f"Win {best['win_pct']:.1f}%  |  RR {best['rr']:.2f}x  |  "
          f"Avg Win ${best['avg_win']:,.0f}  |  Avg Loss ${best['avg_loss']:,.0f}")
    print("=" * 112)

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
if __name__ == "__main__":
    df = fetch_all_candles(PERPETUAL_SYMBOL, RESOLUTION, BACKTEST_START, BACKTEST_END)

    print("\n  Pre-computing shared S4 indicators...")
    sh = compute_shared(df)

    all_results = []
    for name, sig_fn in STRATEGIES:
        print(f"\n  Running {name}...")
        signals = sig_fn(df, sh)
        trades  = run_backtest(df, signals, sh)
        stats   = summarise(name, trades)
        all_results.append(stats)

        out = f"{OUTPUT_DIR}\\s4_refine_{name[:3].strip()}.csv"
        trades.to_csv(out, index=False)
        print(f"    → {stats['trades']} trades  Win {stats['win_pct']:.1f}%  "
              f"Net ${stats['net_pnl']:,.0f}  saved to {out}")

    print_comparison(all_results)
