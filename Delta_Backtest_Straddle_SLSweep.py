"""
BTC ATM Straddle Seller — SL Sweep  |  2026

Tests SL levels from 10% to no-SL and picks the best configuration.
Fetches data once, runs all variants in-memory.

Same assumptions as Delta_Backtest_Straddle.py:
  Entry   : 23:00 UTC candle (≈ 5 AM IST)
  Exit    : 11:00 UTC candle (≈ 5 PM IST, 12 h later)
  Lots    : 100 × 0.01 BTC
  Pricing : Black-Scholes + 30-day rolling realized vol
"""

import numpy as np
import pandas as pd
from scipy import stats
import requests, time, os
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

# ── CONFIG ───────────────────────────────────────────────────────
BASE_URL         = os.getenv("BASE_URL", "https://api.india.delta.exchange")
PERPETUAL_SYMBOL = os.getenv("PERPETUAL_SYMBOL", "BTCUSD")

LOTS          = 100
CONTRACT_BTC  = 0.01
STRIKE_STEP   = 100
IV_WINDOW_H   = 720        # 30-day rolling vol
ENTRY_UTC_H   = 23
EXIT_UTC_H    = 11
T_TOTAL_H     = 12.0

FETCH_FROM = int(datetime(2025, 12, 1, tzinfo=timezone.utc).timestamp())
FETCH_TO   = int(datetime(2026, 7, 2, 23, 59, 59, tzinfo=timezone.utc).timestamp())
LIVE_FROM  = datetime(2026, 1, 1, tzinfo=timezone.utc)
LIVE_TO    = datetime(2026, 7, 2, tzinfo=timezone.utc)

SL_LEVELS = [0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50, 0.75, None]
# None = no SL (ride to expiry always)

OUTPUT_CSV = r"D:\AI\Delta.Exchange\backtest_straddle_sl_sweep.csv"

# ── DATA FETCH ───────────────────────────────────────────────────
def _dt(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

def fetch_candles() -> pd.DataFrame:
    all_rows, cursor, step = [], FETCH_FROM, 3600
    print(f"\nFetching 1h candles  {_dt(FETCH_FROM)} → {_dt(FETCH_TO)}")
    print("-" * 56)
    while cursor < FETCH_TO:
        batch_end = min(cursor + step * 500, FETCH_TO)
        try:
            resp = requests.get(
                f"{BASE_URL}/v2/history/candles",
                params={"symbol": PERPETUAL_SYMBOL, "resolution": "1h",
                        "start": cursor, "end": batch_end},
                timeout=(5, 30),
            )
            resp.raise_for_status()
            rows = resp.json().get("result") or []
        except Exception as e:
            print(f"  Retry ({_dt(cursor)}): {e}")
            time.sleep(5)
            continue
        all_rows.extend(rows)
        cursor = batch_end + step
        time.sleep(0.3)

    df = pd.DataFrame(all_rows)
    df.rename(columns={"time": "timestamp"}, inplace=True)
    for c in ("open", "high", "low", "close"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df.sort_values("timestamp", inplace=True)
    df.drop_duplicates("timestamp", inplace=True)
    df.reset_index(drop=True, inplace=True)
    df["dt"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    df = df.set_index("dt")
    print(f"  Total: {len(df):,} candles")
    return df

# ── OPTION PRICING ───────────────────────────────────────────────
def bs_straddle(S: float, K: float, T_years: float, sigma: float) -> float:
    if T_years <= 0:
        return float(abs(S - K))
    sigma = max(sigma, 0.01)
    d1 = (np.log(S / K) + 0.5 * sigma**2 * T_years) / (sigma * np.sqrt(T_years))
    d2 = d1 - sigma * np.sqrt(T_years)
    call = S * stats.norm.cdf(d1) - K * stats.norm.cdf(d2)
    put  = K * stats.norm.cdf(-d2) - S * stats.norm.cdf(-d1)
    return float(call + put)

# ── CORE SIMULATION (precomputed per-day rows) ───────────────────
def build_day_rows(df: pd.DataFrame) -> list[dict]:
    """
    Precompute per-day bar snapshots so the SL sweep doesn't re-fetch.
    Returns list of dicts, one per live trading day, each containing:
      prem0, K, S0, iv, hourly_bars [(t_rem, mark)] from h+1 to exit
    """
    log_ret = np.log(df["close"] / df["close"].shift(1))
    df = df.copy()
    df["iv"] = (
        log_ret.rolling(IV_WINDOW_H, min_periods=IV_WINDOW_H // 2).std()
        * np.sqrt(8760)
    ).ffill().clip(lower=0.20)

    rows = []
    day = LIVE_FROM.date()
    while day <= LIVE_TO.date():
        entry_dt = (
            datetime(day.year, day.month, day.day, ENTRY_UTC_H, 0, 0, tzinfo=timezone.utc)
            - timedelta(days=1)
        )
        exit_dt = datetime(day.year, day.month, day.day, EXIT_UTC_H, 0, 0, tzinfo=timezone.utc)

        if entry_dt not in df.index:
            day += timedelta(days=1)
            continue

        r     = df.loc[entry_dt]
        S0    = float(r["close"])
        iv    = float(r["iv"])
        K     = round(S0 / STRIKE_STEP) * STRIKE_STEP
        T0    = T_TOTAL_H / 8760
        prem0 = bs_straddle(S0, K, T0, iv)

        # Precompute mark at each monitoring bar
        hourly_bars = []
        bar = entry_dt + timedelta(hours=1)
        while bar <= exit_dt:
            if bar in df.index:
                S_now  = float(df.loc[bar, "close"])
                t_rem  = max((exit_dt - bar).total_seconds() / 3600 / 8760, 0)
                mark   = bs_straddle(S_now, K, t_rem, iv)
                hourly_bars.append({
                    "bar":    bar,
                    "S":      S_now,
                    "mark":   mark,
                    "t_rem":  t_rem,
                    "is_exit": bar == exit_dt,
                })
            bar += timedelta(hours=1)

        if not hourly_bars:
            day += timedelta(days=1)
            continue

        rows.append({
            "date":         str(day),
            "S0":           S0,
            "K":            K,
            "iv":           iv,
            "prem0":        prem0,
            "total_prem":   prem0 * CONTRACT_BTC * LOTS,
            "hourly_bars":  hourly_bars,
        })
        day += timedelta(days=1)
    return rows

# ── SWEEP ────────────────────────────────────────────────────────
def run_sweep(day_rows: list[dict], sl_pct: float | None) -> dict:
    pnls, sl_count = [], 0
    total_prem_sum = 0.0

    for d in day_rows:
        prem0     = d["prem0"]
        sl_mark   = prem0 * (1 + sl_pct) if sl_pct is not None else float("inf")
        total_prem_sum += d["total_prem"]

        exit_mark = None
        triggered_sl = False

        for h in d["hourly_bars"]:
            if not h["is_exit"] and h["mark"] >= sl_mark:
                exit_mark    = h["mark"]
                triggered_sl = True
                sl_count    += 1
                break
            if h["is_exit"]:
                exit_mark = abs(h["S"] - d["K"])  # intrinsic at settlement
                break

        if exit_mark is None:
            # Fallback: last bar intrinsic
            last = d["hourly_bars"][-1]
            exit_mark = abs(last["S"] - d["K"])

        pnl = (prem0 - exit_mark) * CONTRACT_BTC * LOTS
        pnls.append(pnl)

    pnls_s = pd.Series(pnls)
    n      = len(pnls_s)
    wins   = (pnls_s > 0).sum()
    total  = pnls_s.sum()
    avg_w  = pnls_s[pnls_s > 0].mean() if wins > 0 else 0.0
    avg_l  = pnls_s[pnls_s < 0].mean() if wins < n else 0.0
    cum    = pnls_s.cumsum()
    max_dd = (cum - cum.cummax()).min()

    return {
        "sl_pct":       f"{sl_pct*100:.0f}%" if sl_pct is not None else "None",
        "total_pnl":    round(total, 2),
        "win_rate":     round(wins / n * 100, 1),
        "sl_count":     sl_count,
        "sl_rate":      round(sl_count / n * 100, 1),
        "avg_win":      round(avg_w, 2),
        "avg_loss":     round(avg_l, 2),
        "rr":           round(abs(avg_w / avg_l), 2) if avg_l else float("inf"),
        "max_dd":       round(max_dd, 2),
        "prem_kept_pct": round(total / total_prem_sum * 100, 1),
        "n":            n,
    }

# ── DISPLAY ──────────────────────────────────────────────────────
def print_results(results: list[dict]) -> None:
    best = max(results, key=lambda r: r["total_pnl"])

    print("\n" + "=" * 100)
    print("  BTC ATM STRADDLE SELLER — SL SWEEP  |  BTCUSD  |  2026 YTD  (Jan 1 – Jul 2, 183 days)")
    print("=" * 100)
    hdr = (f"  {'SL':>6}  {'Total P&L':>11}  {'Prem%':>7}  {'Win%':>6}  "
           f"{'SL#':>5}  {'SL%':>6}  {'AvgWin':>9}  {'AvgLoss':>9}  {'RR':>5}  {'MaxDD':>10}")
    print(hdr)
    print("  " + "-" * 94)
    for r in results:
        marker = "  ← BEST" if r is best else ""
        print(
            f"  {r['sl_pct']:>6}  ${r['total_pnl']:>10,.2f}  {r['prem_kept_pct']:>6.1f}%"
            f"  {r['win_rate']:>5.1f}%  {r['sl_count']:>5}  {r['sl_rate']:>5.1f}%"
            f"  ${r['avg_win']:>8,.2f}  ${r['avg_loss']:>8,.2f}  {r['rr']:>5.2f}×"
            f"  ${r['max_dd']:>9,.2f}{marker}"
        )
    print("=" * 100)

    # Monthly breakdown for best SL
    print(f"\n  MONTHLY DETAIL — best SL: {best['sl_pct']}")

def monthly_detail(day_rows: list[dict], sl_pct: float | None) -> None:
    records = []
    for d in day_rows:
        prem0   = d["prem0"]
        sl_mark = prem0 * (1 + sl_pct) if sl_pct is not None else float("inf")
        exit_mark, reason = None, "Expiry"
        for h in d["hourly_bars"]:
            if not h["is_exit"] and h["mark"] >= sl_mark:
                exit_mark, reason = h["mark"], "SL"
                break
            if h["is_exit"]:
                exit_mark = abs(h["S"] - d["K"])
                break
        if exit_mark is None:
            exit_mark = abs(d["hourly_bars"][-1]["S"] - d["K"])

        pnl = (prem0 - exit_mark) * CONTRACT_BTC * LOTS
        records.append({"date": d["date"], "pnl_usd": pnl, "reason": reason,
                        "total_collected": d["total_prem"]})

    df  = pd.DataFrame(records)
    df["month"] = pd.to_datetime(df["date"]).dt.to_period("M")
    m_pnl  = df.groupby("month")["pnl_usd"].sum()
    m_prem = df.groupby("month")["total_collected"].sum()
    m_sl   = df.groupby("month").apply(lambda x: (x["reason"] == "SL").sum())
    m_n    = df.groupby("month").size()

    print(f"  {'Month':<10}  {'Days':>5}  {'SL':>4}  {'Prem Coll':>11}  {'P&L':>11}")
    print("  " + "-" * 48)
    for p in m_pnl.index:
        pnl  = m_pnl[p]
        sign = "+" if pnl >= 0 else ""
        print(f"  {str(p):<10}  {int(m_n[p]):>5}  {int(m_sl[p]):>4}"
              f"  ${m_prem[p]:>10,.2f}  {sign}${abs(pnl):>9,.2f}")

    df.to_csv(OUTPUT_CSV, index=False)
    print(f"\n  Trade log (best SL) → {OUTPUT_CSV}")

# ── ENTRY ────────────────────────────────────────────────────────
if __name__ == "__main__":
    df       = fetch_candles()
    day_rows = build_day_rows(df)
    print(f"\n  Precomputed {len(day_rows)} trading days.  Running {len(SL_LEVELS)} SL variants...\n")

    results = [run_sweep(day_rows, sl) for sl in SL_LEVELS]
    print_results(results)

    best_sl = max(results, key=lambda r: r["total_pnl"])
    best_sl_val = SL_LEVELS[results.index(best_sl)]
    monthly_detail(day_rows, best_sl_val)
