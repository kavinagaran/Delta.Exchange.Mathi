"""
BTC ATM Long Straddle — Exit Time Sweep + TP 50%  |  2026

Same as Delta_Backtest_ExitSweep.py but adds a Take Profit trigger:
exit immediately if mark >= entry_premium × 1.50 at any hourly bar
before the scheduled exit time.

  Entry  : 5:35 PM IST = 12:00 UTC
  TP     : Exit if mark rises to 150% of entry premium (+50%)
  Exits  : 8 PM → 6 AM IST (same sweep as before)
  No SL  : hold to scheduled exit unless TP fires first

Comparison columns show side-by-side: no-TP vs TP-50% for each exit time.
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
ENTRY_UTC_H  = 12
T_ENTRY_H    = 24.0
TP_PCT       = 0.50      # take profit at +50% of entry premium

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
FETCH_TO   = int(datetime(2026, 7, 3,  1, 0, 0, tzinfo=timezone.utc).timestamp())
LIVE_FROM  = datetime(2026, 1, 1, tzinfo=timezone.utc)
LIVE_TO    = datetime(2026, 7, 2, tzinfo=timezone.utc)

OUTPUT_CSV = r"D:\AI\Delta.Exchange\backtest_exit_sweep_tp50.csv"

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
    return float(
        S * stats.norm.cdf(d1) - K * stats.norm.cdf(d2) +
        K * stats.norm.cdf(-d2) - S * stats.norm.cdf(-d1)
    )

# ── PRECOMPUTE DAY ROWS ───────────────────────────────────────────
def build_day_rows(df: pd.DataFrame) -> list[dict]:
    """
    Per day: entry at 12:00 UTC, then ordered list of hourly bars
    from 13:00 UTC through 00:00 UTC next day (12 bars total).
    """
    rows = []
    day = LIVE_FROM.date()

    while day <= LIVE_TO.date():
        entry_dt  = datetime(day.year, day.month, day.day,
                             ENTRY_UTC_H, 0, 0, tzinfo=timezone.utc)
        expiry_dt = entry_dt + timedelta(hours=T_ENTRY_H)

        if entry_dt not in df.index:
            day += timedelta(days=1)
            continue

        S0    = float(df.loc[entry_dt, "close"])
        K     = round(S0 / STRIKE_STEP) * STRIKE_STEP
        prem0 = bs_straddle(S0, K, T_ENTRY_H / 8760, MARKET_IV)

        # Build ordered bar list: held_h 1 → 12
        ordered_bars = []
        for v in EXIT_VARIANTS:
            if v.get("next_day"):
                bar_dt = datetime(day.year, day.month, day.day,
                                  0, 0, 0, tzinfo=timezone.utc) + timedelta(days=1)
            else:
                bar_dt = datetime(day.year, day.month, day.day,
                                  v["utc_h"], 0, 0, tzinfo=timezone.utc)

            if bar_dt in df.index:
                S_now = float(df.loc[bar_dt, "close"])
                t_rem = max((expiry_dt - bar_dt).total_seconds() / 3600 / 8760, 0)
                mark  = bs_straddle(S_now, K, t_rem, MARKET_IV)
                ordered_bars.append({
                    "held_h":  v["held_h"],
                    "S":       S_now,
                    "mark":    mark,
                    "t_rem_h": t_rem * 8760,
                })

        if not ordered_bars:
            day += timedelta(days=1)
            continue

        rows.append({
            "date":         str(day),
            "S0":           S0,
            "K":            K,
            "prem0":        prem0,
            "total_cost":   prem0 * CONTRACT_BTC * LOTS,
            "ordered_bars": ordered_bars,   # sorted by held_h ascending
        })
        day += timedelta(days=1)

    return rows

# ── SIMULATE ONE DAY with TP ──────────────────────────────────────
def simulate_day(d: dict, max_held_h: int, tp_pct: float | None):
    """
    Scan bars up to max_held_h. Return (pnl, exit_reason, exit_held_h).
    tp_pct=None → no TP, hold to scheduled exit.
    """
    prem0   = d["prem0"]
    tp_mark = prem0 * (1 + tp_pct) if tp_pct is not None else float("inf")

    for bar in d["ordered_bars"]:
        if bar["held_h"] > max_held_h:
            break
        # TP check (every bar up to and including exit)
        if tp_pct is not None and bar["mark"] >= tp_mark:
            pnl = (bar["mark"] - prem0) * CONTRACT_BTC * LOTS
            return pnl, "TP", bar["held_h"]
        # At scheduled exit
        if bar["held_h"] == max_held_h:
            pnl = (bar["mark"] - prem0) * CONTRACT_BTC * LOTS
            return pnl, "Exit", bar["held_h"]

    # Fallback: use last available bar
    last = next((b for b in reversed(d["ordered_bars"]) if b["held_h"] <= max_held_h), None)
    if last:
        pnl = (last["mark"] - prem0) * CONTRACT_BTC * LOTS
        return pnl, "Exit", last["held_h"]
    return 0.0, "NoData", 0

# ── SWEEP ────────────────────────────────────────────────────────
def run_sweep(day_rows: list[dict]) -> list[dict]:
    results = []

    for v in EXIT_VARIANTS:
        max_h     = v["held_h"]
        t_rem_h   = T_ENTRY_H - max_h
        theta_pct = (1 - np.sqrt(t_rem_h / T_ENTRY_H)) * 100

        # No TP
        pnls_base, moves = [], []
        for d in day_rows:
            pnl, _, _ = simulate_day(d, max_h, tp_pct=None)
            pnls_base.append(pnl)
            bar = next((b for b in d["ordered_bars"] if b["held_h"] == max_h), None)
            if bar:
                moves.append(abs(bar["S"] - d["S0"]) / d["S0"] * 100)

        # With TP 50%
        pnls_tp, tp_count, tp_avg_held = [], 0, []
        for d in day_rows:
            pnl, reason, exit_h = simulate_day(d, max_h, tp_pct=TP_PCT)
            pnls_tp.append(pnl)
            if reason == "TP":
                tp_count    += 1
                tp_avg_held.append(exit_h)

        def stats_of(pnls):
            s      = pd.Series(pnls)
            n      = len(s)
            wins   = (s > 0).sum()
            total  = s.sum()
            avg_w  = s[s > 0].mean() if wins > 0 else 0.0
            avg_l  = s[s < 0].mean() if wins < n else 0.0
            cum    = s.cumsum()
            max_dd = (cum - cum.cummax()).min()
            return dict(n=n, wins=wins, total=round(total,4),
                        win_rate=round(wins/n*100,1),
                        avg_win=round(avg_w,4), avg_loss=round(avg_l,4),
                        rr=round(abs(avg_w/avg_l),2) if avg_l else float("inf"),
                        max_dd=round(max_dd,4))

        sb = stats_of(pnls_base)
        st = stats_of(pnls_tp)

        results.append({
            "ist_label":   v["ist_label"],
            "held_h":      max_h,
            "t_rem_h":     t_rem_h,
            "theta_pct":   round(theta_pct, 1),
            "avg_move":    round(np.mean(moves), 2) if moves else 0,
            # no-TP
            "base_pnl":    sb["total"],
            "base_win":    sb["win_rate"],
            "base_dd":     sb["max_dd"],
            "base_rr":     sb["rr"],
            # TP 50%
            "tp_pnl":      st["total"],
            "tp_win":      st["win_rate"],
            "tp_dd":       st["max_dd"],
            "tp_rr":       st["rr"],
            "tp_count":    tp_count,
            "tp_rate":     round(tp_count / sb["n"] * 100, 1),
            "tp_avg_h":    round(np.mean(tp_avg_held), 1) if tp_avg_held else 0,
            "delta_pnl":   round(st["total"] - sb["total"], 4),
        })

    return results

# ── PRINT TABLE ───────────────────────────────────────────────────
def print_results(results: list[dict]) -> None:
    best_base = max(results, key=lambda r: r["base_pnl"])
    best_tp   = max(results, key=lambda r: r["tp_pnl"])

    print("\n" + "=" * 122)
    print("  BTC LONG STRADDLE — EXIT SWEEP + TP 50%  |  Entry: 5:35 PM IST (12:00 UTC)  |  2026 YTD (183 days)")
    print(f"  IV=18%  |  0.001 BTC/lot  |  100 lots  |  TP fires when mark ≥ 150% of entry premium")
    print("=" * 122)
    print(f"  {'Exit':>8}  {'Held':>5}  {'θ':>6}  {'Move':>6}  "
          f"{'── No TP ──':^32}  "
          f"{'──── TP 50% ────':^48}  {'ΔPNL':>10}")
    print(f"  {'IST':>8}  {'':>5}  {'dec%':>6}  {'abs%':>6}  "
          f"{'P&L':>10}  {'Win%':>5}  {'RR':>5}  {'MaxDD':>9}  "
          f"{'P&L':>10}  {'Win%':>5}  {'RR':>5}  {'MaxDD':>9}  {'TP#':>4}  {'TP%':>5}  "
          f"{'ΔP&L':>10}")
    print("  " + "-" * 114)

    for r in results:
        b_mark = " ◄B" if r is best_base else "   "
        t_mark = " ◄T" if r is best_tp   else "   "
        b_sign = "+" if r["base_pnl"] >= 0 else ""
        t_sign = "+" if r["tp_pnl"]   >= 0 else ""
        d_sign = "+" if r["delta_pnl"] >= 0 else ""
        print(
            f"  {r['ist_label']:>8}  {r['held_h']:>4}h  {r['theta_pct']:>5.1f}%  {r['avg_move']:>5.2f}%"
            f"  {b_sign}${abs(r['base_pnl']):>8,.2f}{b_mark}"
            f"  {r['base_win']:>4.1f}%  {r['base_rr']:>4.1f}×  ${r['base_dd']:>8,.2f}"
            f"  {t_sign}${abs(r['tp_pnl']):>8,.2f}{t_mark}"
            f"  {r['tp_win']:>4.1f}%  {r['tp_rr']:>4.1f}×  ${r['tp_dd']:>8,.2f}"
            f"  {r['tp_count']:>4}  {r['tp_rate']:>4.1f}%"
            f"  {d_sign}${abs(r['delta_pnl']):>8,.2f}"
        )
    print("=" * 122)
    print(f"  ◄B = best no-TP exit  |  ◄T = best TP-50% exit  |  ΔPNL = TP minus no-TP at same exit")

# ── MONTHLY DETAIL FOR BEST TP EXIT ──────────────────────────────
def monthly_detail(day_rows: list[dict], results: list[dict]) -> None:
    best = max(results, key=lambda r: r["tp_pnl"])
    max_h = best["held_h"]

    records = []
    for d in day_rows:
        pnl, reason, exit_h = simulate_day(d, max_h, tp_pct=TP_PCT)
        bar = next((b for b in d["ordered_bars"] if b["held_h"] == exit_h), None)
        S_exit = bar["S"] if bar else d["S0"]
        records.append({
            "date":          d["date"],
            "btc_entry":     round(d["S0"]),
            "atm_strike":    d["K"],
            "prem_per_unit": round(d["prem0"], 2),
            "total_cost":    round(d["total_cost"], 4),
            "btc_exit":      round(S_exit),
            "btc_move_pct":  round((S_exit - d["S0"]) / d["S0"] * 100, 2),
            "exit_h":        exit_h,
            "exit_reason":   reason,
            "pnl_usd":       round(pnl, 4),
        })

    df = pd.DataFrame(records)
    df["month"] = pd.to_datetime(df["date"]).dt.to_period("M")
    m_pnl   = df.groupby("month")["pnl_usd"].sum()
    m_cost  = df.groupby("month")["total_cost"].sum()
    m_n     = df.groupby("month").size()
    m_tp    = df.groupby("month").apply(lambda x: (x["exit_reason"] == "TP").sum())
    m_win   = df.groupby("month").apply(lambda x: (x["pnl_usd"] > 0).mean() * 100)
    m_move  = df.groupby("month")["btc_move_pct"].apply(lambda x: x.abs().mean())

    print(f"\n  MONTHLY DETAIL — Best TP exit: {best['ist_label'].strip()} IST  "
          f"(max hold {max_h}h, TP fires at +{TP_PCT*100:.0f}%)")
    print(f"  {'Month':<10}  {'Days':>5}  {'TPs':>5}  {'Win%':>6}  {'AvgMove':>8}  "
          f"{'Prem Paid':>12}  {'P&L':>12}")
    print("  " + "-" * 68)
    for p in m_pnl.index:
        pnl  = m_pnl[p]
        sign = "+" if pnl >= 0 else ""
        print(f"  {str(p):<10}  {int(m_n[p]):>5}  {int(m_tp[p]):>5}  {m_win[p]:>5.1f}%"
              f"  {m_move[p]:>7.2f}%"
              f"  ${m_cost[p]:>11,.4f}"
              f"  {sign}${abs(pnl):>11,.4f}")

    df.to_csv(OUTPUT_CSV, index=False)
    print(f"\n  Trade log → {OUTPUT_CSV}")

# ── ENTRY ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    df       = fetch_candles()
    day_rows = build_day_rows(df)
    avg_prem = np.mean([d["total_cost"] for d in day_rows])
    tp_level = np.mean([d["prem0"] * (1 + TP_PCT) for d in day_rows])
    print(f"\n  {len(day_rows)} days  |  Avg premium paid: ${avg_prem:.4f}/trade"
          f"  |  TP triggers at mark ≥ ${tp_level:.2f}/unit (+{TP_PCT*100:.0f}%)")

    results = run_sweep(day_rows)
    print_results(results)
    monthly_detail(day_rows, results)
