"""
BTC ATM Long Straddle — Exit Time Sweep  |  2026

Fixed entry: 5:35 PM IST = 12:00 UTC candle close (just after daily settlement)
Variable exit: 8 PM IST (14:00 UTC) → 6 AM IST next day (00:00 UTC next day)
No SL — hold to each exit time unconditionally.

At entry the option has T=24h to next-day settlement (12:00 UTC).
At each exit bar, T_remaining decreases and mark = BS(S, K, T_rem, 18%).
P&L = (exit_mark − entry_mark) × 0.001 BTC × 100 lots

IST exit → UTC candle:
  8 PM  = 14:00 UTC  (T_rem = 22h, held  2h)
  9 PM  = 15:00 UTC  (T_rem = 21h, held  3h)
  10 PM = 16:00 UTC  (T_rem = 20h, held  4h)
  11 PM = 17:00 UTC  (T_rem = 19h, held  5h)
  12 AM = 18:00 UTC  (T_rem = 18h, held  6h)
   1 AM = 19:00 UTC  (T_rem = 17h, held  7h)
   2 AM = 20:00 UTC  (T_rem = 16h, held  8h)
   3 AM = 21:00 UTC  (T_rem = 15h, held  9h)
   4 AM = 22:00 UTC  (T_rem = 14h, held 10h)
   5 AM = 23:00 UTC  (T_rem = 13h, held 11h)
   6 AM = 00:00 UTC+ (T_rem = 12h, held 12h)

Market IV : 18%  |  Contract: 0.001 BTC/lot  |  Strike step: $200
"""

import numpy as np
import pandas as pd
from scipy import stats
import requests, time, os
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

BASE_URL         = os.getenv("BASE_URL", "https://api.india.delta.exchange")
PERPETUAL_SYMBOL = os.getenv("PERPETUAL_SYMBOL", "BTCUSD")

LOTS         = 100
CONTRACT_BTC = 0.001
STRIKE_STEP  = 200
MARKET_IV    = 0.18
ENTRY_UTC_H  = 12      # 12:00 UTC = 5:30 PM IST
T_ENTRY_H    = 24.0    # hours to next-day expiry at entry

# Exit sweep: UTC candle hours (12+h offset from entry) and IST labels
EXIT_VARIANTS = [
    {"utc_h": 14, "ist_label": " 8 PM", "held_h":  2},
    {"utc_h": 15, "ist_label": " 9 PM", "held_h":  3},
    {"utc_h": 16, "ist_label": "10 PM", "held_h":  4},
    {"utc_h": 17, "ist_label": "11 PM", "held_h":  5},
    {"utc_h": 18, "ist_label": "12 AM", "held_h":  6},
    {"utc_h": 19, "ist_label": " 1 AM", "held_h":  7},
    {"utc_h": 20, "ist_label": " 2 AM", "held_h":  8},
    {"utc_h": 21, "ist_label": " 3 AM", "held_h":  9},
    {"utc_h": 22, "ist_label": " 4 AM", "held_h": 10},
    {"utc_h": 23, "ist_label": " 5 AM", "held_h": 11},
    {"utc_h":  0, "ist_label": " 6 AM", "held_h": 12, "next_day": True},
]

FETCH_FROM = int(datetime(2025, 12, 1, tzinfo=timezone.utc).timestamp())
FETCH_TO   = int(datetime(2026, 7, 3,  1, 0, 0, tzinfo=timezone.utc).timestamp())  # +1h buffer
LIVE_FROM  = datetime(2026, 1, 1, tzinfo=timezone.utc)
LIVE_TO    = datetime(2026, 7, 2, tzinfo=timezone.utc)

OUTPUT_CSV = r"D:\AI\Delta.Exchange\backtest_exit_sweep.csv"

# ── DATA ─────────────────────────────────────────────────────────
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
            print(f"  Retry: {e}")
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
    print(f"  Unique candles: {len(df):,}")
    return df

# ── OPTION PRICING ────────────────────────────────────────────────
def bs_straddle(S: float, K: float, T_years: float, sigma: float) -> float:
    if T_years <= 0:
        return float(abs(S - K))
    sigma = max(sigma, 0.001)
    d1 = (np.log(S / K) + 0.5 * sigma**2 * T_years) / (sigma * np.sqrt(T_years))
    d2 = d1 - sigma * np.sqrt(T_years)
    call = S * stats.norm.cdf(d1)  - K * stats.norm.cdf(d2)
    put  = K * stats.norm.cdf(-d2) - S * stats.norm.cdf(-d1)
    return float(call + put)

# ── PRECOMPUTE DAY SNAPSHOTS ──────────────────────────────────────
def build_day_rows(df: pd.DataFrame) -> list[dict]:
    """
    For each day: entry at 12:00 UTC, then capture marks at every hour
    from 13:00 UTC through 00:00 UTC next day (6 AM IST).
    T_remaining at each bar = T_ENTRY_H − hours_elapsed.
    """
    rows = []
    day = LIVE_FROM.date()

    while day <= LIVE_TO.date():
        entry_dt  = datetime(day.year, day.month, day.day,
                             ENTRY_UTC_H, 0, 0, tzinfo=timezone.utc)
        expiry_dt = entry_dt + timedelta(hours=T_ENTRY_H)  # next-day 12:00 UTC

        if entry_dt not in df.index:
            day += timedelta(days=1)
            continue

        S0    = float(df.loc[entry_dt, "close"])
        K     = round(S0 / STRIKE_STEP) * STRIKE_STEP
        T0    = T_ENTRY_H / 8760
        prem0 = bs_straddle(S0, K, T0, MARKET_IV)

        # Collect every bar from 13:00 UTC today through 00:00 UTC tomorrow
        bar_marks = {}      # held_h → {S, mark, t_rem_h}
        for v in EXIT_VARIANTS:
            if v.get("next_day"):
                bar_dt = datetime(day.year, day.month, day.day,
                                  0, 0, 0, tzinfo=timezone.utc) + timedelta(days=1)
            else:
                bar_dt = datetime(day.year, day.month, day.day,
                                  v["utc_h"], 0, 0, tzinfo=timezone.utc)

            if bar_dt in df.index:
                S_now  = float(df.loc[bar_dt, "close"])
                t_rem  = max((expiry_dt - bar_dt).total_seconds() / 3600 / 8760, 0)
                mark   = bs_straddle(S_now, K, t_rem, MARKET_IV)
                bar_marks[v["held_h"]] = {
                    "S":      S_now,
                    "mark":   mark,
                    "t_rem_h": t_rem * 8760,
                }

        if not bar_marks:
            day += timedelta(days=1)
            continue

        rows.append({
            "date":      str(day),
            "S0":        S0,
            "K":         K,
            "prem0":     prem0,
            "bar_marks": bar_marks,
        })
        day += timedelta(days=1)

    return rows

# ── SWEEP BY EXIT TIME ────────────────────────────────────────────
def run_exit_sweep(day_rows: list[dict]) -> list[dict]:
    results = []

    for v in EXIT_VARIANTS:
        held_h    = v["held_h"]
        t_rem_h   = T_ENTRY_H - held_h      # hours remaining at exit
        theta_pct = (1 - np.sqrt(t_rem_h / T_ENTRY_H)) * 100  # pure theta decay %

        pnls, moves = [], []
        n_missing = 0

        for d in day_rows:
            bm = d["bar_marks"].get(held_h)
            if bm is None:
                n_missing += 1
                continue
            pnl   = (bm["mark"] - d["prem0"]) * CONTRACT_BTC * LOTS
            move  = abs(bm["S"] - d["S0"]) / d["S0"] * 100
            pnls.append(pnl)
            moves.append(move)

        if not pnls:
            continue

        s      = pd.Series(pnls)
        n      = len(s)
        wins   = (s > 0).sum()
        total  = s.sum()
        avg_w  = s[s > 0].mean() if wins > 0 else 0.0
        avg_l  = s[s < 0].mean() if wins < n else 0.0
        cum    = s.cumsum()
        max_dd = (cum - cum.cummax()).min()
        avg_move = np.mean(moves)

        total_prem = sum(d["prem0"] * CONTRACT_BTC * LOTS for d in day_rows if d["bar_marks"].get(held_h))

        results.append({
            "ist_label":  v["ist_label"],
            "held_h":     held_h,
            "t_rem_h":    t_rem_h,
            "theta_pct":  round(theta_pct, 1),
            "avg_move":   round(avg_move, 2),
            "total_pnl":  round(total, 4),
            "win_rate":   round(wins / n * 100, 1),
            "avg_win":    round(avg_w, 4),
            "avg_loss":   round(avg_l, 4),
            "rr":         round(abs(avg_w / avg_l), 2) if avg_l else float("inf"),
            "max_dd":     round(max_dd, 4),
            "cost_recov": round(total / total_prem * 100, 1),
            "n":          n,
        })

    return results

# ── PRINT TABLE ───────────────────────────────────────────────────
def print_sweep(results: list[dict]) -> None:
    best = max(results, key=lambda r: r["total_pnl"])

    print("\n" + "=" * 112)
    print("  BTC ATM LONG STRADDLE — EXIT TIME SWEEP  |  Entry: 5:35 PM IST (12:00 UTC)  |  2026 YTD (183 days)")
    print(f"  Market IV={MARKET_IV*100:.0f}%  |  {CONTRACT_BTC} BTC/lot  |  100 lots  |  No SL")
    print("=" * 112)
    print(f"  {'Exit IST':>8}  {'Held':>5}  {'T_rem':>6}  {'θ decay':>8}  {'AvgMove':>8}  "
          f"{'Total P&L':>11}  {'Win%':>6}  {'AvgWin':>9}  {'AvgLoss':>9}  {'RR':>5}  {'MaxDD':>10}")
    print("  " + "-" * 104)
    for r in results:
        marker = "  ← BEST" if r is best else ""
        sign   = "+" if r["total_pnl"] >= 0 else ""
        print(
            f"  {r['ist_label']:>8}  {r['held_h']:>4}h  {r['t_rem_h']:>5.0f}h"
            f"  {r['theta_pct']:>7.1f}%  {r['avg_move']:>7.2f}%"
            f"  {sign}${abs(r['total_pnl']):>9,.4f}"
            f"  {r['win_rate']:>5.1f}%  ${r['avg_win']:>8,.4f}"
            f"  ${r['avg_loss']:>8,.4f}  {r['rr']:>5.2f}×"
            f"  ${r['max_dd']:>9,.4f}{marker}"
        )
    print("=" * 112)

# ── MONTHLY DETAIL FOR BEST EXIT ──────────────────────────────────
def monthly_detail(day_rows: list[dict], best: dict) -> None:
    held_h = best["held_h"]
    records = []

    for d in day_rows:
        bm = d["bar_marks"].get(held_h)
        if bm is None:
            continue
        pnl  = (bm["mark"] - d["prem0"]) * CONTRACT_BTC * LOTS
        move = (bm["S"] - d["S0"]) / d["S0"] * 100
        records.append({
            "date":          d["date"],
            "btc_entry":     round(d["S0"]),
            "atm_strike":    d["K"],
            "prem_per_unit": round(d["prem0"], 2),
            "total_cost":    round(d["prem0"] * CONTRACT_BTC * LOTS, 4),
            "btc_exit":      round(bm["S"]),
            "btc_move_pct":  round(move, 2),
            "exit_mark":     round(bm["mark"], 2),
            "t_rem_h":       round(bm["t_rem_h"], 1),
            "pnl_usd":       round(pnl, 4),
        })

    df = pd.DataFrame(records)
    df["month"] = pd.to_datetime(df["date"]).dt.to_period("M")
    m_pnl  = df.groupby("month")["pnl_usd"].sum()
    m_cost = df.groupby("month")["total_cost"].sum()
    m_n    = df.groupby("month").size()
    m_move = df.groupby("month")["btc_move_pct"].apply(lambda x: x.abs().mean())
    m_win  = df.groupby("month").apply(lambda x: (x["pnl_usd"] > 0).mean() * 100)

    print(f"\n  MONTHLY DETAIL — Best exit: {best['ist_label'].strip()} IST  "
          f"(held {held_h}h, T_rem={best['t_rem_h']:.0f}h)")
    print(f"  {'Month':<10}  {'Days':>5}  {'Win%':>6}  {'Avg|Move|':>10}  "
          f"{'Prem Paid':>12}  {'P&L':>12}")
    print("  " + "-" * 64)
    for p in m_pnl.index:
        pnl  = m_pnl[p]
        sign = "+" if pnl >= 0 else ""
        print(f"  {str(p):<10}  {int(m_n[p]):>5}  {m_win[p]:>5.1f}%"
              f"  {m_move[p]:>9.2f}%"
              f"  ${m_cost[p]:>11,.4f}"
              f"  {sign}${abs(pnl):>11,.4f}")

    df.to_csv(OUTPUT_CSV, index=False)
    print(f"\n  Trade log → {OUTPUT_CSV}")

# ── ENTRY ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    df       = fetch_candles()
    day_rows = build_day_rows(df)
    avg_prem = np.mean([d["prem0"] * CONTRACT_BTC * LOTS for d in day_rows])
    print(f"\n  {len(day_rows)} trading days  |  Avg entry premium: ${avg_prem:.4f}/trade  "
          f"|  Breakeven: ±{np.mean([d['prem0']/d['S0']*100 for d in day_rows]):.2f}% of spot")

    results = run_exit_sweep(day_rows)
    print_sweep(results)

    best = max(results, key=lambda r: r["total_pnl"])
    monthly_detail(day_rows, best)
