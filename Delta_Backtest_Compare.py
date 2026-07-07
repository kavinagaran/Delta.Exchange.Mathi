"""
Delta Exchange BTCUSD — Multi-Strategy Indicator Comparison (2026 YTD)

Runs 7 different indicator combos through the same 2-step ITM backtest engine.
All strategies share:  50% SL · 2-step ITM strikes · Full Black-Scholes pricing
                       4h macro-trend filter · Time filter 02–22 UTC

Strategies tested
─────────────────────────────────────────────────────────────
S1  RSI(70/30)  + Supertrend 15m + Supertrend 4h + EMA1h(20)    [current]
S2  MACD(12/26/9) + Bollinger Bands(20,2) + ST4h + EMA1h
S3  StochRSI(14,14,3) + ADX(14>25) + ST4h + EMA1h
S4  EMA Cross(9/21) + RSI confirm + 4h EMA cross
S5  Williams %R(14) + Supertrend 15m + ST4h + EMA1h
S6  CCI(20) + Supertrend 15m + ST4h + EMA1h
S7  Hybrid: EMA Cross(9/21) + StochRSI + ST4h + EMA1h
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
# INDICATOR LIBRARY
# ─────────────────────────────────────────────
def _rsi(close, period=14):
    d = close.diff()
    g = d.clip(lower=0).ewm(com=period - 1, min_periods=period).mean()
    l = (-d.clip(upper=0)).ewm(com=period - 1, min_periods=period).mean()
    return 100 - 100 / (1 + g / l)

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

def _macd(close, fast=12, slow=26, sig=9):
    ml = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    sl = ml.ewm(span=sig, adjust=False).mean()
    return ml, sl, ml - sl

def _bollinger(close, period=20, dev=2.0):
    mid = close.rolling(period).mean()
    std = close.rolling(period).std()
    return mid + dev * std, mid - dev * std  # upper, lower

def _stochrsi(close, rsi_p=14, stoch_p=14, k_sm=3):
    rsi  = _rsi(close, rsi_p)
    lo   = rsi.rolling(stoch_p).min()
    hi   = rsi.rolling(stoch_p).max()
    k    = ((rsi - lo) / (hi - lo + 1e-10) * 100).rolling(k_sm).mean()
    return k

def _adx(df, period=14):
    h, l, c = df["high"], df["low"], df["close"]
    tr   = pd.concat([(h - l), (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    up   = h - h.shift()
    dn   = l.shift() - l
    pdm  = up.where((up > dn) & (up > 0), 0.0)
    mdm  = dn.where((dn > up) & (dn > 0), 0.0)
    atr  = tr.ewm(com=period - 1, min_periods=period).mean()
    pdi  = 100 * pdm.ewm(com=period - 1, min_periods=period).mean() / atr
    mdi  = 100 * mdm.ewm(com=period - 1, min_periods=period).mean() / atr
    dx   = 100 * (pdi - mdi).abs() / (pdi + mdi + 1e-10)
    return dx.ewm(com=period - 1, min_periods=period).mean(), pdi, mdi

def _williams_r(df, period=14):
    hi = df["high"].rolling(period).max()
    lo = df["low"].rolling(period).min()
    return -100 * (hi - df["close"]) / (hi - lo + 1e-10)

def _cci(df, period=20):
    tp  = (df["high"] + df["low"] + df["close"]) / 3
    ma  = tp.rolling(period).mean()
    md  = tp.rolling(period).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    return (tp - ma) / (0.015 * md + 1e-10)

def _ema(close, period):
    return close.ewm(span=period, adjust=False).mean()

def _realised_vol(close, window=30):
    return np.log(close / close.shift(1)).rolling(window).std() * math.sqrt(96 * 252)

# ─────────────────────────────────────────────
# SHARED PRE-COMPUTATION  (runs once)
# ─────────────────────────────────────────────
def compute_shared(df):
    idx = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    close = df["close"]

    print("  Computing shared indicators (RSI, ST15m, ST4h, EMA1h)...")
    rsi14  = _rsi(close, 14).values
    st15m  = _supertrend(df, 10, 3.0)
    rvol   = _realised_vol(close, 30).values

    # 1h EMA(20) mapped back to 15m
    ema1h_1h = pd.Series(close.values, index=idx).resample("1h").last().dropna() \
                 .ewm(span=20, adjust=False).mean()
    ema1h = ema1h_1h.reindex(idx, method="ffill").values

    # 4h Supertrend mapped back to 15m
    df_4h = pd.DataFrame({
        "high":  pd.Series(df["high"].values,  index=idx).resample("4h").max(),
        "low":   pd.Series(df["low"].values,   index=idx).resample("4h").min(),
        "close": pd.Series(close.values,        index=idx).resample("4h").last(),
    }).dropna()
    st4h_s = _supertrend(df_4h, 10, 3.0)
    st4h   = st4h_s.reindex(idx, method="ffill").fillna("neutral").values

    # Boolean arrays (avoids string compare in tight loops)
    st15b = (st15m == "bullish").values
    st15r = (st15m == "bearish").values
    st4hb = pd.Series(st4h).str.startswith("b").values   # "bullish"
    st4hr = pd.Series(st4h).str.startswith("b").values   # placeholder, fix below
    st4hb = (pd.Series(st4h) == "bullish").values
    st4hr = (pd.Series(st4h) == "bearish").values

    # Time mask  02:00–22:00 UTC
    hours   = idx.dt.hour.values
    time_ok = (hours >= 2) & (hours < 22)

    return dict(rsi14=rsi14, st15b=st15b, st15r=st15r, st4hb=st4hb, st4hr=st4hr,
                ema1h=ema1h, rvol=rvol, time_ok=time_ok, idx=idx,
                close=close.values)

# ─────────────────────────────────────────────
# STRATEGY SIGNAL FUNCTIONS
# Each returns int8 array: 1=bullish, -1=bearish, 0=neutral
# ─────────────────────────────────────────────
def _sig_array(bullish_mask, bearish_mask, n):
    out = np.zeros(n, dtype=np.int8)
    out[bullish_mask] = 1
    out[bearish_mask] = -1
    return out

def S1_rsi_st_ema(df, sh):
    """RSI(70/30) + ST15m + ST4h + EMA1h"""
    c = sh["close"]
    b = (sh["rsi14"] > 70) & sh["st15b"] & sh["st4hb"] & (c > sh["ema1h"]) & sh["time_ok"]
    r = (sh["rsi14"] < 30) & sh["st15r"] & sh["st4hr"] & (c < sh["ema1h"]) & sh["time_ok"]
    return _sig_array(b, r, len(df))

def S2_macd_bb(df, sh):
    """MACD(12/26/9) histogram > 0 + price outside BB(20,2) + ST4h + EMA1h"""
    close = df["close"]
    ml, sl, hist = _macd(close)
    bb_up, bb_lo = _bollinger(close)
    c = sh["close"]
    b = (hist > 0) & (c > bb_up) & sh["st4hb"] & (c > sh["ema1h"]) & sh["time_ok"]
    r = (hist < 0) & (c < bb_lo) & sh["st4hr"] & (c < sh["ema1h"]) & sh["time_ok"]
    return _sig_array(b.values, r.values, len(df))

def S3_stochrsi_adx(df, sh):
    """StochRSI-K > 80 / < 20 + ADX > 25 (trending) + ST4h + EMA1h"""
    close = df["close"]
    k     = _stochrsi(close)
    adx, pdi, mdi = _adx(df)
    c = sh["close"]
    b = (k > 80) & (adx > 25) & (pdi > mdi) & sh["st4hb"] & (c > sh["ema1h"]) & sh["time_ok"]
    r = (k < 20) & (adx > 25) & (mdi > pdi) & sh["st4hr"] & (c < sh["ema1h"]) & sh["time_ok"]
    return _sig_array(b.values, r.values, len(df))

def S4_ema_cross_rsi(df, sh):
    """EMA(9) × EMA(21) + RSI confirm + 4h EMA cross"""
    close  = df["close"]
    e9     = _ema(close, 9).values
    e21    = _ema(close, 21).values
    rsi14  = sh["rsi14"]
    idx    = sh["idx"]
    # 4h EMA cross
    c1h    = pd.Series(close.values, index=idx)
    e9_4h  = c1h.resample("4h").last().dropna().ewm(span=9,  adjust=False).mean().reindex(idx, method="ffill").values
    e21_4h = c1h.resample("4h").last().dropna().ewm(span=21, adjust=False).mean().reindex(idx, method="ffill").values
    b = (e9 > e21) & (rsi14 > 55) & (e9_4h > e21_4h) & sh["time_ok"]
    r = (e9 < e21) & (rsi14 < 45) & (e9_4h < e21_4h) & sh["time_ok"]
    return _sig_array(b, r, len(df))

def S5_williams_st(df, sh):
    """Williams %R(14) > -20 (bull) / < -80 (bear) + ST15m + ST4h + EMA1h"""
    wr = _williams_r(df).values
    c  = sh["close"]
    b  = (wr > -20) & sh["st15b"] & sh["st4hb"] & (c > sh["ema1h"]) & sh["time_ok"]
    r  = (wr < -80) & sh["st15r"] & sh["st4hr"] & (c < sh["ema1h"]) & sh["time_ok"]
    return _sig_array(b, r, len(df))

def S6_cci_st(df, sh):
    """CCI(20) > 100 / < -100 + ST15m + ST4h + EMA1h"""
    cci = _cci(df).values
    c   = sh["close"]
    b   = (cci > 100)  & sh["st15b"] & sh["st4hb"] & (c > sh["ema1h"]) & sh["time_ok"]
    r   = (cci < -100) & sh["st15r"] & sh["st4hr"] & (c < sh["ema1h"]) & sh["time_ok"]
    return _sig_array(b, r, len(df))

def S7_hybrid(df, sh):
    """EMA(9/21) cross + StochRSI > 60 / < 40 + ST4h + EMA1h"""
    close = df["close"]
    e9    = _ema(close, 9).values
    e21   = _ema(close, 21).values
    k     = _stochrsi(close).values
    c     = sh["close"]
    b = (e9 > e21) & (k > 60) & sh["st4hb"] & (c > sh["ema1h"]) & sh["time_ok"]
    r = (e9 < e21) & (k < 40) & sh["st4hr"] & (c < sh["ema1h"]) & sh["time_ok"]
    return _sig_array(b, r, len(df))

STRATEGIES = [
    ("S1  RSI(70/30) + ST15m + ST4h + EMA1h",        S1_rsi_st_ema),
    ("S2  MACD + Bollinger Bands + ST4h + EMA1h",     S2_macd_bb),
    ("S3  StochRSI + ADX(>25) + ST4h + EMA1h",       S3_stochrsi_adx),
    ("S4  EMA Cross(9/21) + RSI + 4h EMA cross",      S4_ema_cross_rsi),
    ("S5  Williams %R + ST15m + ST4h + EMA1h",        S5_williams_st),
    ("S6  CCI(20) + ST15m + ST4h + EMA1h",            S6_cci_st),
    ("S7  Hybrid: EMA Cross + StochRSI + ST4h + EMA1h", S7_hybrid),
]

# ─────────────────────────────────────────────
# BACKTEST ENGINE  (signal-array driven)
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
            if flipped:
                _enter(s, i)

    return pd.DataFrame(trades)

# ─────────────────────────────────────────────
# STATS SUMMARY
# ─────────────────────────────────────────────
def summarise(name, trades):
    if trades.empty:
        return {"name": name, "trades": 0, "win_pct": 0, "rr": 0,
                "net_pnl": 0, "max_dd": 0, "sl_rate": 0}
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
    w = 42

    print("\n" + "=" * 110)
    print(f"  {'STRATEGY':<{w}}  {'TRADES':>6}  {'WIN%':>5}  {'RR':>5}  "
          f"{'NET P&L':>14}  {'MAX DD':>14}  {'SL%':>5}")
    print("=" * 110)
    for r in ranked:
        sign   = "+" if r["net_pnl"] >= 0 else ""
        dd_str = f"-${abs(r['max_dd']):>11,.0f}"
        pnl_str = f"{sign}${r['net_pnl']:>11,.0f}"
        print(f"  {r['name']:<{w}}  {r['trades']:>6}  {r['win_pct']:>4.1f}%  "
              f"{r['rr']:>4.2f}x  {pnl_str:>14}  {dd_str:>14}  {r['sl_rate']:>4.1f}%")
    print("=" * 110)

    # Monthly matrix
    months = []
    for r in results:
        if "monthly" in r and not r["monthly"].empty:
            months = sorted(r["monthly"].index.tolist())
            break
    print(f"\n  MONTHLY P&L MATRIX  (rows=months, cols=strategies)\n")
    header = "  " + f"{'Month':<9}" + "".join(f"  {'S'+str(i+1):>12}" for i in range(len(results)))
    print(header)
    print("  " + "-" * (9 + 14 * len(results)))
    for m in months:
        row = f"  {m:<9}"
        for r in results:
            if "monthly" not in r:
                row += f"  {'N/A':>12}"; continue
            pnl = r["monthly"].get(m, 0)
            tag = f"+${pnl:,.0f}" if pnl >= 0 else f"-${abs(pnl):,.0f}"
            row += f"  {tag:>12}"
        print(row)
    print()

    # Best strategy summary
    best = ranked[0]
    print(f"  BEST:  {best['name']}")
    print(f"         Net P&L ${best['net_pnl']:,.0f}  |  "
          f"Win {best['win_pct']:.1f}%  |  RR {best['rr']:.2f}x  |  "
          f"Max DD ${best['max_dd']:,.0f}")
    print("=" * 110)

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
if __name__ == "__main__":
    df = fetch_all_candles(PERPETUAL_SYMBOL, RESOLUTION, BACKTEST_START, BACKTEST_END)

    print("\n  Pre-computing shared indicators...")
    sh = compute_shared(df)

    all_results = []
    for name, sig_fn in STRATEGIES:
        print(f"\n  Running {name}...")
        signals = sig_fn(df, sh)
        trades  = run_backtest(df, signals, sh)
        stats   = summarise(name, trades)
        all_results.append(stats)

        out = f"{OUTPUT_DIR}\\backtest_{name[:2].strip()}.csv"
        trades.to_csv(out, index=False)
        print(f"    → {stats['trades']} trades  Win {stats['win_pct']:.1f}%  "
              f"Net ${stats['net_pnl']:,.0f}  saved to {out}")

    print_comparison(all_results)
