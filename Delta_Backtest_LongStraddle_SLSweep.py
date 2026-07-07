"""
BTC ATM Long Straddle — SL Sweep  |  2026

Buy 100 lots of ATM straddle (call + put) on BTCUSD daily.

  Entry      : ~5:00 AM IST  = close of 23:00 UTC candle (prev night)
  Forced exit: ~5:00 PM IST  = close of 11:00 UTC candle (12h later)
  SL         : Exit if combined mark DROPS below entry_premium × (1 − SL_PCT)
               i.e. exit when you've lost X% of what you paid

  Strategy logic (long gamma):
    - We pay premium upfront; profit when BTC makes a big move either way
    - Breakeven at expiry: BTC must move ≥ straddle_premium from the strike
    - If BTC moves more than the premium implied, we profit

Calibrated inputs (from real Delta Exchange option data, 4-Jul-2026):
  IV             : 18% annualised  (market IV, NOT 30-day realized vol)
  Contract size  : 0.001 BTC per lot  (actual Delta Exchange spec)
  Strike step    : $200 (nearest 200 to spot)
  100 lots       : 0.10 BTC total exposure per leg

Why 18% not 44.6%:
  Real option prices on 4-Jul showed market IV ≈ 18%.
  Rolling realized vol (44.6%) overstates actual option premiums by ~2.5×.
  Using 18% IV gives backtest premiums that match observed market prices.
"""

import numpy as np
import pandas as pd
from scipy import stats, optimize
import requests, time, os
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

# ── CONFIG ───────────────────────────────────────────────────────
BASE_URL         = os.getenv("BASE_URL", "https://api.india.delta.exchange")
PERPETUAL_SYMBOL = os.getenv("PERPETUAL_SYMBOL", "BTCUSD")

LOTS          = 100
CONTRACT_BTC  = 0.001      # actual Delta Exchange BTC option contract size
STRIKE_STEP   = 200        # round ATM strike to nearest $200
MARKET_IV     = 0.18       # 18% annualised — calibrated from real option prices
IV_WINDOW_H   = 720        # 30-day window (used only for realized-vol reference column)

ENTRY_UTC_H   = 23
EXIT_UTC_H    = 11
T_TOTAL_H     = 12.0       # hours from entry to forced exit

# Dec 2025 warmup for realized-vol reference
FETCH_FROM = int(datetime(2025, 12, 1, tzinfo=timezone.utc).timestamp())
FETCH_TO   = int(datetime(2026, 7, 2, 23, 59, 59, tzinfo=timezone.utc).timestamp())
LIVE_FROM  = datetime(2026, 1, 1, tzinfo=timezone.utc)
LIVE_TO    = datetime(2026, 7, 2, tzinfo=timezone.utc)

# SL levels: fraction of premium you're willing to lose before exiting
# e.g. 0.30 → exit if mark < 0.70 × entry premium  (−30%)
# None → hold to forced exit always (pure long-gamma, no stop)
SL_LEVELS = [0.20, 0.30, 0.40, 0.50, 0.60, 0.70, None]

OUTPUT_CSV = r"D:\AI\Delta.Exchange\backtest_long_straddle_sl_sweep.csv"

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
    print(f"  Fetched {len(all_rows):,} raw rows")

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
    """Long straddle fair value (call + put) — USD per unit of BTC exposure."""
    if T_years <= 0:
        return float(abs(S - K))
    sigma = max(sigma, 0.001)
    d1 = (np.log(S / K) + 0.5 * sigma**2 * T_years) / (sigma * np.sqrt(T_years))
    d2 = d1 - sigma * np.sqrt(T_years)
    call = S * stats.norm.cdf(d1)  - K * stats.norm.cdf(d2)
    put  = K * stats.norm.cdf(-d2) - S * stats.norm.cdf(-d1)
    return float(call + put)

# ── PRECOMPUTE PER-DAY SNAPSHOTS ─────────────────────────────────
def build_day_rows(df: pd.DataFrame) -> list[dict]:
    """
    Build per-day list with hourly mark snapshots for fast SL sweep.
    Uses MARKET_IV (18%) for all pricing — matches observed option prices.
    """
    # Realized vol (for reference only — not used in pricing)
    log_ret = np.log(df["close"] / df["close"].shift(1))
    rv = (
        log_ret.rolling(IV_WINDOW_H, min_periods=IV_WINDOW_H // 2).std()
        * np.sqrt(8760)
    ).ffill().clip(lower=0.10)

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

        r    = df.loc[entry_dt]
        S0   = float(r["close"])
        rv0  = float(rv.loc[entry_dt])
        K    = round(S0 / STRIKE_STEP) * STRIKE_STEP
        T0   = T_TOTAL_H / 8760

        prem0 = bs_straddle(S0, K, T0, MARKET_IV)  # USD/BTC at entry
        total_cost = prem0 * CONTRACT_BTC * LOTS    # total USD paid

        # Hourly marks during the trade
        hourly_bars = []
        bar = entry_dt + timedelta(hours=1)
        while bar <= exit_dt:
            if bar in df.index:
                S_now  = float(df.loc[bar, "close"])
                t_rem  = max((exit_dt - bar).total_seconds() / 3600 / 8760, 0)
                mark   = bs_straddle(S_now, K, t_rem, MARKET_IV)
                hourly_bars.append({
                    "bar":     bar,
                    "S":       S_now,
                    "mark":    mark,
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
            "rv_pct":      round(rv0 * 100, 1),
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
        prem0      = d["prem0"]
        total_cost_sum += d["total_cost"]
        # SL fires when mark drops below (1 - sl_pct) × prem0
        sl_floor   = prem0 * (1 - sl_pct) if sl_pct is not None else -float("inf")

        exit_mark    = None
        triggered_sl = False

        for h in d["hourly_bars"]:
            if not h["is_exit"] and h["mark"] <= sl_floor:
                exit_mark    = h["mark"]
                triggered_sl = True
                sl_count    += 1
                break
            if h["is_exit"]:
                # At forced exit: settle at intrinsic (BTC move vs strike)
                exit_mark = abs(h["S"] - d["K"])
                break

        if exit_mark is None:
            last = d["hourly_bars"][-1]
            exit_mark = abs(last["S"] - d["K"])

        pnl = (exit_mark - prem0) * CONTRACT_BTC * LOTS
        pnls.append(pnl)

    s = pd.Series(pnls)
    n      = len(s)
    wins   = (s > 0).sum()
    total  = s.sum()
    avg_w  = s[s > 0].mean() if wins > 0     else 0.0
    avg_l  = s[s < 0].mean() if wins < n     else 0.0
    cum    = s.cumsum()
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
        "cost_recov":   round(total / total_cost_sum * 100, 1),
        "n":            n,
    }

# ── PRINT SWEEP TABLE ────────────────────────────────────────────
def print_sweep(results: list[dict]) -> None:
    best = max(results, key=lambda r: r["total_pnl"])
    print("\n" + "=" * 108)
    print("  BTC ATM LONG STRADDLE — SL SWEEP  |  BTCUSD  |  2026 YTD  (Jan 1 – Jul 2, 183 days)")
    print(f"  Market IV = {MARKET_IV*100:.0f}%  |  Contract = {CONTRACT_BTC} BTC/lot  |  100 lots = {CONTRACT_BTC*LOTS} BTC per leg")
    print("=" * 108)
    hdr = (f"  {'SL':>6}  {'Total P&L':>11}  {'Cost%':>7}  {'Win%':>6}  "
           f"{'SL#':>5}  {'SL%':>6}  {'AvgWin':>9}  {'AvgLoss':>9}  {'RR':>5}  {'MaxDD':>11}")
    print(hdr)
    print("  " + "-" * 100)
    for r in results:
        marker = "  ← BEST" if r is best else ""
        sign   = "+" if r["total_pnl"] >= 0 else ""
        print(
            f"  {r['sl_pct']:>6}  {sign}${abs(r['total_pnl']):>9,.2f}  "
            f"{r['cost_recov']:>6.1f}%  {r['win_rate']:>5.1f}%"
            f"  {r['sl_count']:>5}  {r['sl_rate']:>5.1f}%"
            f"  ${r['avg_win']:>8,.2f}  ${r['avg_loss']:>8,.2f}"
            f"  {r['rr']:>5.2f}×  ${r['max_dd']:>10,.2f}{marker}"
        )
    print("=" * 108)

# ── MONTHLY DETAIL FOR BEST SL ────────────────────────────────────
def monthly_detail(day_rows: list[dict], sl_pct: float | None) -> None:
    records = []
    for d in day_rows:
        prem0    = d["prem0"]
        sl_floor = prem0 * (1 - sl_pct) if sl_pct is not None else -float("inf")
        exit_mark, reason = None, "Expiry"
        for h in d["hourly_bars"]:
            if not h["is_exit"] and h["mark"] <= sl_floor:
                exit_mark, reason = h["mark"], "SL"
                break
            if h["is_exit"]:
                exit_mark = abs(h["S"] - d["K"])
                break
        if exit_mark is None:
            exit_mark = abs(d["hourly_bars"][-1]["S"] - d["K"])

        pnl = (exit_mark - prem0) * CONTRACT_BTC * LOTS
        records.append({
            "date":         d["date"],
            "btc_entry":    round(d["S0"]),
            "atm_strike":   d["K"],
            "iv_pct":       MARKET_IV * 100,
            "rv_pct":       d["rv_pct"],
            "prem_per_unit": round(d["prem0"], 2),
            "total_cost":   round(d["total_cost"], 4),
            "btc_move_pct": d["btc_move_pct"],
            "exit_mark":    round(exit_mark, 2),
            "pnl_usd":      round(pnl, 4),
            "exit_reason":  reason,
        })

    df  = pd.DataFrame(records)
    df["month"] = pd.to_datetime(df["date"]).dt.to_period("M")
    m_pnl  = df.groupby("month")["pnl_usd"].sum()
    m_cost = df.groupby("month")["total_cost"].sum()
    m_sl   = df.groupby("month").apply(lambda x: (x["exit_reason"] == "SL").sum())
    m_n    = df.groupby("month").size()
    m_move = df.groupby("month")["btc_move_pct"].apply(lambda x: x.abs().mean())

    print(f"\n  MONTHLY DETAIL — SL = {f'{sl_pct*100:.0f}%' if sl_pct else 'None'}")
    print(f"  {'Month':<10}  {'Days':>5}  {'SL':>4}  {'Avg|Move|':>10}  "
          f"{'Premium Paid':>13}  {'P&L':>11}")
    print("  " + "-" * 60)
    for p in m_pnl.index:
        pnl  = m_pnl[p]
        sign = "+" if pnl >= 0 else ""
        print(f"  {str(p):<10}  {int(m_n[p]):>5}  {int(m_sl[p]):>4}"
              f"  {m_move[p]:>9.2f}%"
              f"  ${m_cost[p]:>12,.4f}"
              f"  {sign}${abs(pnl):>10,.4f}")

    df.to_csv(OUTPUT_CSV, index=False)
    print(f"\n  Trade log → {OUTPUT_CSV}")

# ── SUMMARY STATS ────────────────────────────────────────────────
def print_summary(day_rows: list[dict]) -> None:
    avg_prem  = np.mean([d["prem0"] for d in day_rows])
    avg_cost  = np.mean([d["total_cost"] for d in day_rows])
    avg_move  = np.mean([abs(d["btc_move_pct"]) for d in day_rows])
    avg_btc   = np.mean([d["S0"] for d in day_rows])
    be_pct    = avg_prem / avg_btc * 100  # breakeven move needed (% of spot)
    print(f"\n  Market context  (183 days, 18% IV):")
    print(f"  Avg BTC price          : ${avg_btc:,.0f}")
    print(f"  Avg straddle premium   : ${avg_prem:,.2f}/unit  →  ${avg_cost:,.4f}/100-lot trade")
    print(f"  Avg breakeven move     : ±{be_pct:.2f}% of spot")
    print(f"  Avg actual 12h move    : {avg_move:.2f}% abs  "
          f"({'> BE — favours LONG' if avg_move > be_pct else '< BE — favours SHORT'})")

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
