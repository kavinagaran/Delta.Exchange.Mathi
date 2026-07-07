"""
BTC ATM Long Straddle — Evening Window  |  Backtest 2026

Buy 100 lots of ATM straddle right after daily settlement,
hold for 6 hours into the night session.

  Entry  : ~5:35 PM IST  = 12:00 UTC candle close  (just after daily settlement)
  Exit   : ~12:00 AM IST = 18:00 UTC candle close  (midnight IST, 6h later)
  SL     : Exit if combined mark drops below entry_premium × (1 − SL_PCT)

Option lifecycle:
  Delta Exchange daily BTC options settle at 12:00 UTC (5:30 PM IST).
  New next-day options are listed immediately after.
  At entry (12:00 UTC day D), the option expires at 12:00 UTC day D+1:
    T_entry = 24h
    T_exit  = 18h  (6h of theta has elapsed at midnight exit)
  Exit is mid-life — P&L = (BS_mark_at_18h − prem0) × lots.

Market IV : 18% (calibrated from real Delta Exchange option prices 4-Jul-2026)
Contract  : 0.001 BTC per lot  |  Strike step: $200
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
CONTRACT_BTC  = 0.001
STRIKE_STEP   = 200
MARKET_IV     = 0.18

ENTRY_UTC_H   = 12    # 12:00 UTC = ~5:30 PM IST (just after settlement)
EXIT_UTC_H    = 18    # 18:00 UTC = ~11:30 PM IST (~midnight IST)
T_ENTRY_H     = 24.0  # hours to next-day 12:00 UTC settlement at entry
T_EXIT_H      = 18.0  # hours remaining at exit

FETCH_FROM = int(datetime(2025, 12, 1, tzinfo=timezone.utc).timestamp())
FETCH_TO   = int(datetime(2026, 7, 2, 23, 59, 59, tzinfo=timezone.utc).timestamp())
LIVE_FROM  = datetime(2026, 1, 1, tzinfo=timezone.utc)
LIVE_TO    = datetime(2026, 7, 2, tzinfo=timezone.utc)

SL_LEVELS = [0.20, 0.30, 0.40, 0.50, 0.60, 0.70, None]

OUTPUT_CSV = r"D:\AI\Delta.Exchange\backtest_long_straddle_evening.csv"

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

# ── OPTION PRICING ───────────────────────────────────────────────
def bs_straddle(S: float, K: float, T_years: float, sigma: float) -> float:
    """Long straddle (call + put) fair value in USD/unit."""
    if T_years <= 0:
        return float(abs(S - K))
    sigma = max(sigma, 0.001)
    d1 = (np.log(S / K) + 0.5 * sigma**2 * T_years) / (sigma * np.sqrt(T_years))
    d2 = d1 - sigma * np.sqrt(T_years)
    call = S * stats.norm.cdf(d1)  - K * stats.norm.cdf(d2)
    put  = K * stats.norm.cdf(-d2) - S * stats.norm.cdf(-d1)
    return float(call + put)

# ── PRECOMPUTE DAY ROWS ───────────────────────────────────────────
def build_day_rows(df: pd.DataFrame) -> list[dict]:
    """
    For each live trading day:
      entry bar  : ENTRY_UTC_H (12:00 UTC) on day D
      exit bar   : EXIT_UTC_H  (18:00 UTC) on day D
      monitoring : 13:00–18:00 UTC (6 bars)
    T at entry = 24h, T decreases each hour to 18h at exit.
    At each bar: mark = BS(S_now, K, T_remaining, MARKET_IV)
    """
    rows = []
    day = LIVE_FROM.date()
    while day <= LIVE_TO.date():
        entry_dt = datetime(day.year, day.month, day.day,
                            ENTRY_UTC_H, 0, 0, tzinfo=timezone.utc)
        exit_dt  = datetime(day.year, day.month, day.day,
                            EXIT_UTC_H,  0, 0, tzinfo=timezone.utc)
        # Next-day expiry at 12:00 UTC
        expiry_dt = datetime(day.year, day.month, day.day,
                             ENTRY_UTC_H, 0, 0, tzinfo=timezone.utc) + timedelta(hours=T_ENTRY_H)

        if entry_dt not in df.index:
            day += timedelta(days=1)
            continue

        S0    = float(df.loc[entry_dt, "close"])
        K     = round(S0 / STRIKE_STEP) * STRIKE_STEP
        T0    = T_ENTRY_H / 8760       # years at entry

        prem0      = bs_straddle(S0, K, T0, MARKET_IV)
        total_cost = prem0 * CONTRACT_BTC * LOTS

        hourly_bars = []
        bar = entry_dt + timedelta(hours=1)
        while bar <= exit_dt:
            if bar in df.index:
                S_now = float(df.loc[bar, "close"])
                # T remaining to next-day expiry
                t_rem = max((expiry_dt - bar).total_seconds() / 3600 / 8760, 0)
                mark  = bs_straddle(S_now, K, t_rem, MARKET_IV)
                hourly_bars.append({
                    "bar":     bar,
                    "S":       S_now,
                    "mark":    mark,
                    "t_rem_h": t_rem * 8760,
                    "is_exit": bar == exit_dt,
                })
            bar += timedelta(hours=1)

        if not hourly_bars:
            day += timedelta(days=1)
            continue

        S_exit = hourly_bars[-1]["S"]
        rows.append({
            "date":        str(day),
            "S0":          S0,
            "K":           K,
            "prem0":       prem0,
            "total_cost":  total_cost,
            "hourly_bars": hourly_bars,
            "S_exit":      S_exit,
            "btc_move_pct": round((S_exit - S0) / S0 * 100, 2),
        })
        day += timedelta(days=1)
    return rows

# ── SL SWEEP ─────────────────────────────────────────────────────
def run_sweep(day_rows: list[dict], sl_pct: float | None) -> dict:
    pnls, sl_count = [], 0
    total_cost_sum = 0.0

    for d in day_rows:
        prem0          = d["prem0"]
        total_cost_sum += d["total_cost"]
        sl_floor       = prem0 * (1 - sl_pct) if sl_pct is not None else -float("inf")

        exit_mark, triggered_sl = None, False

        for h in d["hourly_bars"]:
            if not h["is_exit"] and h["mark"] <= sl_floor:
                exit_mark    = h["mark"]
                triggered_sl = True
                sl_count    += 1
                break
            if h["is_exit"]:
                exit_mark = h["mark"]   # BS mark at 18h remaining (NOT intrinsic)
                break

        if exit_mark is None:
            exit_mark = d["hourly_bars"][-1]["mark"]

        pnl = (exit_mark - prem0) * CONTRACT_BTC * LOTS
        pnls.append(pnl)

    s = pd.Series(pnls)
    n      = len(s)
    wins   = (s > 0).sum()
    total  = s.sum()
    avg_w  = s[s > 0].mean() if wins > 0 else 0.0
    avg_l  = s[s < 0].mean() if wins < n else 0.0
    cum    = s.cumsum()
    max_dd = (cum - cum.cummax()).min()

    return {
        "sl_pct":     f"{sl_pct*100:.0f}%" if sl_pct is not None else "None",
        "total_pnl":  round(total, 4),
        "win_rate":   round(wins / n * 100, 1),
        "sl_count":   sl_count,
        "sl_rate":    round(sl_count / n * 100, 1),
        "avg_win":    round(avg_w, 4),
        "avg_loss":   round(avg_l, 4),
        "rr":         round(abs(avg_w / avg_l), 2) if avg_l else float("inf"),
        "max_dd":     round(max_dd, 4),
        "cost_recov": round(total / total_cost_sum * 100, 1),
        "n":          n,
    }

# ── PRINT SWEEP TABLE ────────────────────────────────────────────
def print_sweep(results: list[dict]) -> None:
    best = max(results, key=lambda r: r["total_pnl"])

    print("\n" + "=" * 110)
    print("  BTC ATM LONG STRADDLE — EVENING SESSION  |  Entry 5:35 PM IST → Exit Midnight IST  |  2026")
    print(f"  Market IV={MARKET_IV*100:.0f}%  |  {CONTRACT_BTC} BTC/lot  |  {LOTS} lots = {CONTRACT_BTC*LOTS} BTC/leg  "
          f"|  T_entry=24h  T_exit=18h  (6h window)")
    print("=" * 110)
    print(f"  {'SL':>6}  {'Total P&L':>11}  {'Cost%':>7}  {'Win%':>6}  "
          f"{'SL#':>5}  {'SL%':>6}  {'AvgWin':>10}  {'AvgLoss':>10}  {'RR':>5}  {'MaxDD':>11}")
    print("  " + "-" * 100)
    for r in results:
        marker = "  ← BEST" if r is best else ""
        sign   = "+" if r["total_pnl"] >= 0 else ""
        print(
            f"  {r['sl_pct']:>6}  {sign}${abs(r['total_pnl']):>9,.4f}  "
            f"{r['cost_recov']:>6.1f}%  {r['win_rate']:>5.1f}%"
            f"  {r['sl_count']:>5}  {r['sl_rate']:>5.1f}%"
            f"  ${r['avg_win']:>9,.4f}  ${r['avg_loss']:>9,.4f}"
            f"  {r['rr']:>5.2f}×  ${r['max_dd']:>10,.4f}{marker}"
        )
    print("=" * 110)

# ── MONTHLY DETAIL FOR BEST SL ────────────────────────────────────
def monthly_detail(day_rows: list[dict], sl_pct: float | None) -> None:
    records = []
    for d in day_rows:
        prem0    = d["prem0"]
        sl_floor = prem0 * (1 - sl_pct) if sl_pct is not None else -float("inf")
        exit_mark, reason = None, "MidLife"

        for h in d["hourly_bars"]:
            if not h["is_exit"] and h["mark"] <= sl_floor:
                exit_mark, reason = h["mark"], "SL"
                break
            if h["is_exit"]:
                exit_mark = h["mark"]
                break
        if exit_mark is None:
            exit_mark = d["hourly_bars"][-1]["mark"]

        pnl = (exit_mark - prem0) * CONTRACT_BTC * LOTS
        records.append({
            "date":          d["date"],
            "btc_entry":     round(d["S0"]),
            "atm_strike":    d["K"],
            "prem_per_unit": round(d["prem0"], 2),
            "total_cost":    round(d["total_cost"], 4),
            "btc_exit":      round(d["S_exit"]),
            "btc_move_pct":  d["btc_move_pct"],
            "exit_mark":     round(exit_mark, 2),
            "pnl_usd":       round(pnl, 4),
            "exit_reason":   reason,
        })

    df  = pd.DataFrame(records)
    df["month"] = pd.to_datetime(df["date"]).dt.to_period("M")
    m_pnl  = df.groupby("month")["pnl_usd"].sum()
    m_cost = df.groupby("month")["total_cost"].sum()
    m_sl   = df.groupby("month").apply(lambda x: (x["exit_reason"] == "SL").sum())
    m_n    = df.groupby("month").size()
    m_move = df.groupby("month")["btc_move_pct"].apply(lambda x: x.abs().mean())

    sl_label = f"{sl_pct*100:.0f}%" if sl_pct is not None else "None"
    print(f"\n  MONTHLY DETAIL — SL = {sl_label}")
    print(f"  {'Month':<10}  {'Days':>5}  {'SL':>4}  {'Avg|Move|':>10}  "
          f"{'Prem Paid':>12}  {'P&L':>12}")
    print("  " + "-" * 62)
    for p in m_pnl.index:
        pnl  = m_pnl[p]
        sign = "+" if pnl >= 0 else ""
        print(f"  {str(p):<10}  {int(m_n[p]):>5}  {int(m_sl[p]):>4}"
              f"  {m_move[p]:>9.2f}%"
              f"  ${m_cost[p]:>11,.4f}"
              f"  {sign}${abs(pnl):>11,.4f}")

    df.to_csv(OUTPUT_CSV, index=False)
    print(f"\n  Trade log → {OUTPUT_CSV}")

# ── SUMMARY STATS ────────────────────────────────────────────────
def print_summary(day_rows: list[dict]) -> None:
    avg_prem   = np.mean([d["prem0"]       for d in day_rows])
    avg_cost   = np.mean([d["total_cost"]  for d in day_rows])
    avg_btc    = np.mean([d["S0"]          for d in day_rows])
    avg_move   = np.mean([abs(d["btc_move_pct"]) for d in day_rows])

    # Theta decay over 6h at entry (without BTC move)
    theta_loss_pct = (1 - np.sqrt(T_EXIT_H / T_ENTRY_H)) * 100

    be_pct = avg_prem / avg_btc * 100

    print(f"\n  Market context  ({len(day_rows)} days, {MARKET_IV*100:.0f}% IV, 6h evening window):")
    print(f"  Avg BTC price          : ${avg_btc:,.0f}")
    print(f"  Avg straddle premium   : ${avg_prem:,.2f}/unit  →  ${avg_cost:,.4f}/100-lot trade")
    print(f"  Avg breakeven move     : ±{be_pct:.2f}% of spot  at ENTRY (T=24h)")
    print(f"  Theta decay in 6h      : ~{theta_loss_pct:.1f}% of premium (time alone, no BTC move)")
    print(f"  Avg actual 6h move     : {avg_move:.2f}% abs")
    edge = "FAVOURS LONG" if avg_move * np.sqrt(T_EXIT_H / T_ENTRY_H) > be_pct * np.sqrt(T_EXIT_H / T_ENTRY_H) else "unclear"
    print(f"  Note: exit is mid-life (T=18h remaining) — P&L driven by delta/gamma, not intrinsic")

# ── ENTRY ────────────────────────────────────────────────────────
if __name__ == "__main__":
    df       = fetch_candles()
    day_rows = build_day_rows(df)
    print(f"\n  Precomputed {len(day_rows)} trading days.")
    print_summary(day_rows)

    print(f"\n  Running {len(SL_LEVELS)} SL variants...")
    results = [run_sweep(day_rows, sl) for sl in SL_LEVELS]
    print_sweep(results)

    best_val = max(results, key=lambda r: r["total_pnl"])
    best_sl  = SL_LEVELS[results.index(best_val)]
    monthly_detail(day_rows, best_sl)
