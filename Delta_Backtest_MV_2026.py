"""
Delta_Backtest_MV_2026.py
BTC MOVE Options (MV-BTC) Long Straddle — 2026 Backtest

Strategy
  Entry : 5:35 PM IST = 12:05 UTC  -> use the 12:00 UTC 1H bar (first available)
  Exit  : 1:00 AM IST = 19:30 UTC  -> use the 19:00 UTC 1H bar open
  Hold  : ~7.5 hours

  At entry the MV contract has ~24 hours to expiry (settles next 12:00 UTC).
  At exit  the MV contract has ~16.5 hours to expiry.

Parameters
  Lots        : 1000
  Contract BTC: 0.001 BTC / lot
  Market IV   : 18 %  (calibrated from real Delta Exchange data)
  Strike step : $200 (ATM rounded to nearest $200)
"""

import csv
import math
import os
import time
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

import requests

# ─────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────
BASE_URL     = "https://api.india.delta.exchange"
RESOLUTION   = "1h"
SYMBOL       = "BTCUSD"

LOTS         = 1000
CONTRACT_BTC = 0.001         # BTC per lot
MARKET_IV    = 0.18          # 18 % annual vol
STRIKE_STEP  = 200           # round strike to nearest $200

# UTC bar hours used as proxies for 12:05 and 19:30 entry/exit
ENTRY_UTC_H  = 12            # 12:00 UTC bar open ≈ 5:35 PM IST entry
EXIT_UTC_H   = 19            # 19:00 UTC bar open ≈ 1:00 AM IST exit (19:30)

# Time-to-expiry at each event
# MV contract settles at 12:00 UTC next day = 24 h from ENTRY_UTC_H
T_ENTRY_H    = 24.0          # hours remaining at entry
T_EXIT_H     = 24.0 - 7.5   # hours remaining at exit  (16.5 h)

START_DATE   = date(2026, 1,  1)
END_DATE     = date(2026, 7,  3)   # last complete day before today

OUT_CSV      = Path(__file__).parent / "backtest_mv_2026_daywise.csv"

# ─────────────────────────────────────────────────────────────
# BLACK-SCHOLES STRADDLE (call + put ATM)
# Abramowitz & Stegun rational approximation (max err 7.5e-8)
# ─────────────────────────────────────────────────────────────
def _ncdf(x: float) -> float:
    z    = abs(x)
    t    = 1.0 / (1.0 + 0.2316419 * z)
    poly = t * (0.319381530 + t * (-0.356563782
               + t * (1.781477937 + t * (-1.821255978
               + t * 1.330274429))))
    pdf  = math.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)
    n    = 1.0 - pdf * poly
    return n if x >= 0 else 1.0 - n

def bs_straddle(S: float, K: float, T_h: float, sigma: float) -> float:
    """ATM straddle mark price in USD per BTC."""
    if T_h <= 0:
        return abs(S - K)
    T    = T_h / 8760.0          # hours -> years (365 * 24)
    sig  = max(sigma, 1e-6)
    sqT  = math.sqrt(T)
    d1   = (math.log(S / K) + 0.5 * sig * sig * T) / (sig * sqT)
    d2   = d1 - sig * sqT
    call = S * _ncdf( d1) - K * _ncdf( d2)
    put  = K * _ncdf(-d2) - S * _ncdf(-d1)
    return call + put

# ─────────────────────────────────────────────────────────────
# DATA FETCH
# ─────────────────────────────────────────────────────────────
def fetch_candles(symbol: str, resolution: str,
                  start_ts: int, end_ts: int) -> list[dict]:
    """Fetch OHLCV candles from Delta Exchange in one page."""
    params = {
        "symbol":     symbol,
        "resolution": resolution,
        "start":      start_ts,
        "end":        end_ts,
    }
    for attempt in range(4):
        try:
            r = requests.get(f"{BASE_URL}/v2/history/candles",
                             params=params, timeout=(5, 30))
            r.raise_for_status()
            raw = r.json().get("result", [])
            return raw
        except Exception as exc:
            if attempt == 3:
                raise
            wait = 2 ** attempt
            print(f"  Retry {attempt+1}/3 after {wait}s — {exc}")
            time.sleep(wait)
    return []

def build_hourly_index(candles: list[dict]) -> dict[int, float]:
    """Map UTC timestamp → open price (int seconds)."""
    idx = {}
    for c in candles:
        ts   = int(c.get("time") or c.get("timestamp") or 0)
        open_ = float(c.get("open", 0))
        if ts > 0 and open_ > 0:
            idx[ts] = open_
    return idx

def get_hour_price(idx: dict[int, float], dt: datetime) -> float | None:
    """Return open price for the 1H bar starting at dt (exact UTC hour)."""
    ts = int(dt.replace(minute=0, second=0, microsecond=0).timestamp())
    return idx.get(ts)

# ─────────────────────────────────────────────────────────────
# ATM STRIKE
# ─────────────────────────────────────────────────────────────
def atm_strike(btc_price: float, step: int) -> float:
    return round(btc_price / step) * step

# ─────────────────────────────────────────────────────────────
# BACKTEST CORE
# ─────────────────────────────────────────────────────────────
def run_backtest():
    print("=" * 64)
    print("MV-BTC Long Straddle  |  Full 2026 Backtest")
    print(f"  Entry  : {ENTRY_UTC_H:02d}:00 UTC (≈ 5:35 PM IST)  T={T_ENTRY_H}h to expiry")
    print(f"  Exit   : {EXIT_UTC_H:02d}:00 UTC (≈ 1:00 AM IST)  T={T_EXIT_H}h to expiry")
    print(f"  Lots   : {LOTS}  |  Contract: {CONTRACT_BTC} BTC  |  IV: {MARKET_IV*100:.0f}%")
    print(f"  Period : {START_DATE}  →  {END_DATE}")
    print("=" * 64)

    # Fetch all candles in one API call (Jan 1 → Jul 3 2026)
    start_ts = int(datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc).timestamp())
    end_ts   = int(datetime(2026, 7, 4, 0, 0, tzinfo=timezone.utc).timestamp())

    print("Fetching BTCUSD 1H candles from Delta Exchange …")
    candles  = fetch_candles(SYMBOL, RESOLUTION, start_ts, end_ts)
    print(f"  Received {len(candles)} bars.")
    if not candles:
        print("ERROR: No candle data returned. Check API connectivity.")
        return

    idx = build_hourly_index(candles)

    # Day-wise loop
    rows       = []
    total_pnl  = 0.0
    wins       = 0
    losses     = 0
    max_dd     = 0.0
    peak       = 0.0
    skipped    = 0
    current_day = START_DATE

    while current_day <= END_DATE:
        entry_dt = datetime(current_day.year, current_day.month, current_day.day,
                            ENTRY_UTC_H, 0, tzinfo=timezone.utc)
        exit_dt  = datetime(current_day.year, current_day.month, current_day.day,
                            EXIT_UTC_H,  0, tzinfo=timezone.utc)

        s0 = get_hour_price(idx, entry_dt)
        s1 = get_hour_price(idx, exit_dt)

        if s0 is None or s1 is None:
            # Weekend / missing bar
            skipped += 1
            current_day += timedelta(days=1)
            continue

        k        = atm_strike(s0, STRIKE_STEP)
        prem0    = bs_straddle(s0, k, T_ENTRY_H, MARKET_IV)
        prem1    = bs_straddle(s1, k, T_EXIT_H,  MARKET_IV)

        pnl_per_btc = prem1 - prem0
        pnl_usd     = pnl_per_btc * CONTRACT_BTC * LOTS
        cost_usd    = prem0       * CONTRACT_BTC * LOTS

        btc_move = (s1 - s0) / s0 * 100

        total_pnl += pnl_usd
        if pnl_usd >= 0:
            wins  += 1
        else:
            losses += 1

        # Drawdown
        if total_pnl > peak:
            peak = total_pnl
        dd = total_pnl - peak
        if dd < max_dd:
            max_dd = dd

        rows.append({
            "date":         current_day.isoformat(),
            "btc_entry":    round(s0,    2),
            "btc_exit":     round(s1,    2),
            "btc_move_pct": round(btc_move, 3),
            "strike":       int(k),
            "prem_entry":   round(prem0, 4),
            "prem_exit":    round(prem1, 4),
            "pnl_usd":      round(pnl_usd, 2),
            "cost_usd":     round(cost_usd, 2),
            "cum_pnl":      round(total_pnl, 2),
        })
        current_day += timedelta(days=1)

    # ─── RESULTS ──────────────────────────────────────────────
    total_days = wins + losses
    win_rate   = wins / total_days * 100 if total_days else 0
    avg_win    = (sum(r["pnl_usd"] for r in rows if r["pnl_usd"] >= 0) / wins
                  if wins else 0)
    avg_loss   = (sum(r["pnl_usd"] for r in rows if r["pnl_usd"] < 0)  / losses
                  if losses else 0)
    rr         = abs(avg_win / avg_loss) if avg_loss else float("inf")

    print()
    print("─" * 64)
    print(f"  Days traded     : {total_days}  (skipped {skipped} — weekend/no data)")
    print(f"  Win / Loss      : {wins} / {losses}  ({win_rate:.1f}% win rate)")
    print(f"  Avg win         : ${avg_win:>10,.2f}")
    print(f"  Avg loss        : ${avg_loss:>10,.2f}")
    print(f"  Reward/Risk     : {rr:.2f}×")
    print(f"  Max Drawdown    : ${max_dd:>10,.2f}")
    print(f"  TOTAL P&L       : ${total_pnl:>10,.2f}")
    print("─" * 64)

    # ─── MONTHLY SUMMARY ──────────────────────────────────────
    print()
    print("Monthly P&L:")
    monthly = {}
    for r in rows:
        m = r["date"][:7]
        monthly[m] = monthly.get(m, 0) + r["pnl_usd"]
    for m, pnl in sorted(monthly.items()):
        bar = "▓" * max(0, int(pnl / 500))
        print(f"  {m}  {pnl:>+10,.2f}  {bar}")

    # ─── CSV ──────────────────────────────────────────────────
    with open(OUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print()
    print(f"Day-wise P&L saved → {OUT_CSV.name}")
    print()

    # ─── SAMPLE ROWS ──────────────────────────────────────────
    print("Sample trades:")
    print(f"{'Date':<12} {'BTC Entry':>10} {'BTC Exit':>10} {'Move%':>7} "
          f"{'Strike':>7} {'Premium':>9} {'PnL':>10} {'Cum PnL':>12}")
    print("-" * 78)
    sample = rows[:5] + rows[-5:]
    for r in sample:
        print(f"{r['date']:<12} ${r['btc_entry']:>9,.0f} ${r['btc_exit']:>9,.0f} "
              f"{r['btc_move_pct']:>+6.2f}% ${r['strike']:>6,} "
              f"${r['prem_entry']:>7.2f} ${r['pnl_usd']:>+9.2f} ${r['cum_pnl']:>11,.2f}")

    return rows, total_pnl, win_rate, max_dd


if __name__ == "__main__":
    run_backtest()
