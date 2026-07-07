"""
BTC ATM Long Straddle — 1 AM IST Exit | 1000 Lots | 2026 YTD
  Entry  : 5:35 PM IST = 12:00 UTC
  Exit   : 1 AM IST    = 19:00 UTC  (7h hold)
  IV     : 18%  |  Contract: 0.001 BTC/lot
  Lots   : 1000
  No SL, No TP
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

LOTS         = 1000
CONTRACT_BTC = 0.001
STRIKE_STEP  = 200
MARKET_IV    = 0.18
ENTRY_UTC_H  = 12      # 5:35 PM IST
EXIT_UTC_H   = 19      # 1 AM IST
T_ENTRY_H    = 24.0
T_EXIT_H     = T_ENTRY_H - 7   # 17h remaining at exit

FETCH_FROM = int(datetime(2025, 12, 1, tzinfo=timezone.utc).timestamp())
FETCH_TO   = int(datetime(2026, 7, 3, 1, 0, 0, tzinfo=timezone.utc).timestamp())
LIVE_FROM  = datetime(2026, 1, 1, tzinfo=timezone.utc)
LIVE_TO    = datetime(2026, 7, 2, tzinfo=timezone.utc)

OUTPUT_CSV = r"D:\AI\Delta.Exchange\backtest_1am_1000lots_daywise.csv"

def _dt(ts): return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

def fetch_candles():
    all_rows, cursor, step = [], FETCH_FROM, 3600
    print(f"Fetching 1h candles  {_dt(FETCH_FROM)} → {_dt(FETCH_TO)}")
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
    df["dt"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    df = df.set_index("dt")
    print(f"  Candles: {len(df):,}")
    return df

def bs_straddle(S, K, T_years, sigma):
    if T_years <= 0:
        return float(abs(S - K))
    sigma = max(sigma, 0.001)
    d1 = (np.log(S / K) + 0.5 * sigma**2 * T_years) / (sigma * np.sqrt(T_years))
    d2 = d1 - sigma * np.sqrt(T_years)
    return float(
        S * stats.norm.cdf(d1) - K * stats.norm.cdf(d2) +
        K * stats.norm.cdf(-d2) - S * stats.norm.cdf(-d1)
    )

def run_backtest(df):
    records = []
    day = LIVE_FROM.date()

    while day <= LIVE_TO.date():
        entry_dt  = datetime(day.year, day.month, day.day,
                             ENTRY_UTC_H, 0, 0, tzinfo=timezone.utc)
        expiry_dt = entry_dt + timedelta(hours=T_ENTRY_H)
        exit_dt   = entry_dt + timedelta(hours=7)

        if entry_dt not in df.index or exit_dt not in df.index:
            day += timedelta(days=1)
            continue

        S0    = float(df.loc[entry_dt, "close"])
        S_ex  = float(df.loc[exit_dt,  "close"])
        K     = round(S0 / STRIKE_STEP) * STRIKE_STEP

        prem0     = bs_straddle(S0,  K, T_ENTRY_H / 8760, MARKET_IV)
        t_rem     = max((expiry_dt - exit_dt).total_seconds() / 3600 / 8760, 0)
        mark_exit = bs_straddle(S_ex, K, t_rem, MARKET_IV)

        prem_total = prem0      * CONTRACT_BTC * LOTS
        mark_total = mark_exit  * CONTRACT_BTC * LOTS
        pnl        = mark_total - prem_total

        move_pct   = (S_ex - S0) / S0 * 100
        breakeven  = prem0 / S0 * 100   # % BTC needs to move to break even

        records.append({
            "date":          str(day),
            "day":           day.strftime("%a"),
            "btc_entry":     round(S0),
            "atm_strike":    K,
            "btc_exit":      round(S_ex),
            "move_pct":      round(move_pct, 2),
            "prem_per_btc":  round(prem0, 2),
            "be_pct":        round(breakeven, 3),
            "mark_per_btc":  round(mark_exit, 2),
            "prem_paid":     round(prem_total, 2),
            "mark_value":    round(mark_total, 2),
            "pnl":           round(pnl, 2),
        })
        day += timedelta(days=1)

    return pd.DataFrame(records)

def print_summary(df):
    wins    = (df["pnl"] > 0).sum()
    losses  = (df["pnl"] < 0).sum()
    n       = len(df)
    total   = df["pnl"].sum()
    avg_w   = df.loc[df["pnl"] > 0, "pnl"].mean()
    avg_l   = df.loc[df["pnl"] < 0, "pnl"].mean()
    cum     = df["pnl"].cumsum()
    max_dd  = (cum - cum.cummax()).min()
    max_win = df["pnl"].max()
    max_los = df["pnl"].min()
    avg_be  = df["be_pct"].mean()
    avg_mv  = df["move_pct"].abs().mean()

    print("\n" + "=" * 68)
    print("  BTC LONG STRADDLE  |  1 AM IST EXIT  |  1000 LOTS  |  2026 YTD")
    print("  Entry: 5:35 PM IST (12:00 UTC)  →  Exit: 1 AM IST (19:00 UTC)")
    print("  IV=18%  |  0.001 BTC/lot  |  ATM strike rounded to $200")
    print("=" * 68)
    print(f"  Days traded     : {n}")
    print(f"  Winners         : {wins}  ({wins/n*100:.1f}%)")
    print(f"  Losers          : {losses}  ({losses/n*100:.1f}%)")
    print(f"  Total P&L       : ${total:,.2f}")
    print(f"  Avg win         : ${avg_w:,.2f}")
    print(f"  Avg loss        : ${avg_l:,.2f}")
    print(f"  Win/Loss ratio  : {abs(avg_w/avg_l):.2f}×")
    print(f"  Max single win  : ${max_win:,.2f}")
    print(f"  Max single loss : ${max_los:,.2f}")
    print(f"  Max drawdown    : ${max_dd:,.2f}")
    print(f"  Avg breakeven   : ±{avg_be:.3f}%  (BTC move needed)")
    print(f"  Avg actual move : ±{avg_mv:.2f}%")
    print(f"  Avg prem/trade  : ${df['prem_paid'].mean():,.2f}")
    print(f"  Total prem paid : ${df['prem_paid'].sum():,.2f}")
    print("=" * 68)

def print_monthly(df):
    df["month"] = pd.to_datetime(df["date"]).dt.to_period("M")
    grp = df.groupby("month")
    print("\n  MONTHLY SUMMARY")
    print(f"  {'Month':<10}  {'Days':>5}  {'Win':>4}  {'Win%':>6}  {'Prem Paid':>12}  {'P&L':>12}  {'MaxDD':>10}")
    print("  " + "-" * 70)
    for m, g in grp:
        wins = (g["pnl"] > 0).sum()
        n    = len(g)
        cum  = g["pnl"].cumsum()
        dd   = (cum - cum.cummax()).min()
        sign = "+" if g["pnl"].sum() >= 0 else ""
        print(f"  {str(m):<10}  {n:>5}  {wins:>4}  {wins/n*100:>5.1f}%"
              f"  ${g['prem_paid'].sum():>11,.2f}"
              f"  {sign}${abs(g['pnl'].sum()):>11,.2f}"
              f"  ${dd:>9,.2f}")
    print("  " + "-" * 70)
    sign = "+" if df["pnl"].sum() >= 0 else ""
    cum = df["pnl"].cumsum()
    dd  = (cum - cum.cummax()).min()
    print(f"  {'TOTAL':<10}  {len(df):>5}  {(df['pnl']>0).sum():>4}  "
          f"{(df['pnl']>0).mean()*100:>5.1f}%"
          f"  ${df['prem_paid'].sum():>11,.2f}"
          f"  {sign}${abs(df['pnl'].sum()):>11,.2f}"
          f"  ${dd:>9,.2f}")

def print_daywise(df):
    cum = df["pnl"].cumsum()
    print(f"\n  DAY-WISE P&L  (1000 lots × 0.001 BTC/lot = 1.0 BTC notional per entry)")
    print(f"  {'Date':<12} {'Day':>3}  {'BTC In':>7}  {'Strike':>7}  {'BTC Out':>8}  "
          f"{'Move%':>7}  {'Prem/BTC':>9}  {'Prem Paid':>10}  {'Mark':>9}  {'P&L':>10}  {'Cumul':>11}")
    print("  " + "-" * 118)
    for i, row in df.iterrows():
        c     = cum.iloc[i]
        sign  = "+" if row["pnl"] >= 0 else ""
        csign = "+" if c >= 0 else ""
        marker = " ◄WIN" if row["pnl"] > 0 else ("  ◄LOSS" if row["pnl"] < 0 else "")
        print(
            f"  {row['date']:<12} {row['day']:>3}"
            f"  {row['btc_entry']:>7,}  {row['atm_strike']:>7,}  {row['btc_exit']:>8,}"
            f"  {row['move_pct']:>+6.2f}%"
            f"  ${row['prem_per_btc']:>8.2f}"
            f"  ${row['prem_paid']:>9.2f}"
            f"  ${row['mark_per_btc']:>8.2f}"
            f"  {sign}${abs(row['pnl']):>9.2f}"
            f"  {csign}${abs(c):>10.2f}"
            f"{marker}"
        )
    print("  " + "-" * 118)
    cum_total = df["pnl"].sum()
    sign = "+" if cum_total >= 0 else ""
    print(f"  {'TOTAL':>82}  {sign}${abs(cum_total):>9.2f}")

if __name__ == "__main__":
    df_candles = fetch_candles()
    df_trades  = run_backtest(df_candles)

    print_summary(df_trades)
    print_monthly(df_trades)
    print_daywise(df_trades)

    df_trades.to_csv(OUTPUT_CSV, index=False)
    print(f"\n  Trade log → {OUTPUT_CSV}")
