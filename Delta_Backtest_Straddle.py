"""
Delta Exchange — BTC ATM Straddle Seller  |  Backtest 2026

Sell 100 lots of ATM straddle (call + put) on BTCUSD daily.

  Entry      : ~5:00 AM IST  = close of 23:00 UTC candle (previous night)
  Forced exit: ~5:00 PM IST  = close of 11:00 UTC candle (same day, 12 h later)
  SL         : Close immediately if combined mark > entry_premium × 1.30
  Monitoring : Checked every 1h candle between entry and exit

Option pricing  : Black-Scholes  (r = 0, no dividends)
Volatility      : 30-day rolling realized vol from 1h log-returns (annualised)
ATM strike      : spot rounded to nearest $100
Contract size   : 0.01 BTC per lot  (100 lots = 1.0 BTC per leg)
P&L             : USD

Assumptions / simplifications:
  - No bid-ask spread, transaction costs, or slippage
  - IV = realized vol (no vol-risk premium, no skew)
  - Settlement price = perpetual close at 11:00 UTC
  - IV held constant throughout each trade (no intraday vol update)
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

LOTS             = 100
CONTRACT_BTC     = 0.01      # BTC per lot
STRIKE_STEP      = 100       # USD — round ATM strike to nearest
SL_PCT           = 0.30      # stop-loss: exit if mark rises 30% above entry premium
IV_WINDOW_H      = 720       # 30 × 24 = 30-day rolling realized vol window

ENTRY_UTC_H      = 23        # 23:00 UTC ≈ 5:00 AM IST (next calendar day)
EXIT_UTC_H       = 11        # 11:00 UTC ≈ 5:00 PM IST
T_TOTAL_H        = 12.0      # hours from entry to forced exit

# Warmup: fetch from Dec 1, 2025 for IV computation
FETCH_FROM = int(datetime(2025, 12, 1, tzinfo=timezone.utc).timestamp())
FETCH_TO   = int(datetime(2026, 7, 2, 23, 59, 59, tzinfo=timezone.utc).timestamp())
LIVE_FROM  = datetime(2026, 1, 1, tzinfo=timezone.utc)
LIVE_TO    = datetime(2026, 7, 2, tzinfo=timezone.utc)

OUTPUT_CSV = r"D:\AI\Delta.Exchange\backtest_straddle_2026.csv"

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
        print(f"  {_dt(cursor)} → {_dt(batch_end)}  ({len(rows)} candles)")
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
    print(f"\n  Total: {len(df):,} candles  (incl. Dec-2025 warmup)")
    return df

# ── OPTION PRICING ───────────────────────────────────────────────
def bs_straddle(S: float, K: float, T_years: float, sigma: float) -> float:
    """Long straddle fair value (call + put) in USD, per unit of BTC exposure."""
    if T_years <= 0:
        return float(abs(S - K))
    sigma = max(sigma, 0.01)
    d1 = (np.log(S / K) + 0.5 * sigma**2 * T_years) / (sigma * np.sqrt(T_years))
    d2 = d1 - sigma * np.sqrt(T_years)
    call = S * stats.norm.cdf(d1) - K * stats.norm.cdf(d2)
    put  = K * stats.norm.cdf(-d2) - S * stats.norm.cdf(-d1)
    return float(call + put)

# ── BACKTEST ENGINE ──────────────────────────────────────────────
def run_backtest(df: pd.DataFrame) -> pd.DataFrame:
    # Rolling realized IV (annualised, 30-day window, floored at 20%)
    log_ret = np.log(df["close"] / df["close"].shift(1))
    df = df.copy()
    df["iv"] = (
        log_ret.rolling(IV_WINDOW_H, min_periods=IV_WINDOW_H // 2).std()
        * np.sqrt(8760)
    ).ffill().clip(lower=0.20)

    trades = []
    day = LIVE_FROM.date()

    while day <= LIVE_TO.date():
        # Entry bar: 23:00 UTC on (day - 1)  ≈ 5 AM IST on day
        entry_dt = (
            datetime(day.year, day.month, day.day, ENTRY_UTC_H, 0, 0, tzinfo=timezone.utc)
            - timedelta(days=1)
        )
        # Forced exit bar: 11:00 UTC on day  ≈ 5 PM IST on day
        exit_dt = datetime(day.year, day.month, day.day, EXIT_UTC_H, 0, 0, tzinfo=timezone.utc)

        if entry_dt not in df.index:
            day += timedelta(days=1)
            continue

        entry_row = df.loc[entry_dt]
        S0  = float(entry_row["close"])
        iv  = float(entry_row["iv"])
        K   = round(S0 / STRIKE_STEP) * STRIKE_STEP
        T0  = T_TOTAL_H / 8760                           # years

        prem0       = bs_straddle(S0, K, T0, iv)         # USD / BTC at entry
        sl_mark     = prem0 * (1 + SL_PCT)               # premium level that triggers SL
        total_prem  = prem0 * CONTRACT_BTC * LOTS        # total USD collected from both legs

        exit_reason = "Expiry"
        exit_time   = exit_dt
        exit_mark   = None

        # ── Scan hourly bars between entry and exit ──────────────
        bar = entry_dt + timedelta(hours=1)
        while bar <= exit_dt:
            if bar not in df.index:
                bar += timedelta(hours=1)
                continue

            S_now   = float(df.loc[bar, "close"])
            t_rem   = max((exit_dt - bar).total_seconds() / 3600 / 8760, 0)
            mark_now = bs_straddle(S_now, K, t_rem, iv)

            if bar < exit_dt and mark_now >= sl_mark:
                exit_reason = "SL"
                exit_time   = bar
                exit_mark   = mark_now
                break

            if bar == exit_dt:
                # Settle at intrinsic at the exit bar
                exit_mark = abs(S_now - K)
                break

            bar += timedelta(hours=1)

        if exit_mark is None:
            # Exit bar missing — fall back to last available bar
            avail = df.index[(df.index > entry_dt) & (df.index <= exit_dt)]
            if avail.empty:
                day += timedelta(days=1)
                continue
            S_last    = float(df.loc[avail[-1], "close"])
            exit_mark = abs(S_last - K)
            exit_time = avail[-1].to_pydatetime()

        exit_pnl   = (prem0 - exit_mark) * CONTRACT_BTC * LOTS
        btc_at_exit = (
            float(df.loc[exit_time, "close"])
            if exit_time in df.index
            else S0
        )
        btc_move_pct = (btc_at_exit - S0) / S0 * 100

        trades.append({
            "date":            str(day),
            "entry_utc":       entry_dt.strftime("%Y-%m-%d %H:%M"),
            "exit_utc":        exit_time.strftime("%Y-%m-%d %H:%M"),
            "btc_entry":       round(S0),
            "atm_strike":      K,
            "iv_pct":          round(iv * 100, 1),
            "prem_per_unit":   round(prem0, 2),
            "prem_per_lot":    round(prem0 * CONTRACT_BTC, 4),
            "total_collected": round(total_prem, 2),
            "be_up":           round(K + prem0),
            "be_down":         round(K - prem0),
            "btc_at_exit":     round(btc_at_exit),
            "btc_move_pct":    round(btc_move_pct, 2),
            "exit_mark":       round(exit_mark, 2),
            "pnl_usd":         round(exit_pnl, 2),
            "exit_reason":     exit_reason,
        })

        day += timedelta(days=1)

    return pd.DataFrame(trades)

# ── REPORT ───────────────────────────────────────────────────────
def print_report(trades: pd.DataFrame) -> None:
    if trades.empty:
        print("No trades generated.")
        return

    n         = len(trades)
    wins      = (trades["pnl_usd"] > 0).sum()
    losses    = n - wins
    sl_count  = (trades["exit_reason"] == "SL").sum()
    exp_count = n - sl_count
    total     = trades["pnl_usd"].sum()
    avg_w     = trades.loc[trades["pnl_usd"] > 0, "pnl_usd"].mean() if wins   else 0.0
    avg_l     = trades.loc[trades["pnl_usd"] < 0, "pnl_usd"].mean() if losses else 0.0

    trades["cum_pnl"] = trades["pnl_usd"].cumsum()
    max_dd = (trades["cum_pnl"] - trades["cum_pnl"].cummax()).min()

    avg_prem  = trades["total_collected"].mean()
    avg_move  = trades["btc_move_pct"].abs().mean()
    avg_iv    = trades["iv_pct"].mean()
    # Straddle breakeven range as % of spot
    be_range_pct = (trades["prem_per_unit"] / trades["btc_entry"] * 100).mean()

    trades["month"] = pd.to_datetime(trades["date"]).dt.to_period("M")
    m_pnl  = trades.groupby("month")["pnl_usd"].sum()
    m_sl   = trades.groupby("month").apply(lambda x: (x["exit_reason"] == "SL").sum())
    m_n    = trades.groupby("month").size()
    m_prem = trades.groupby("month")["total_collected"].sum()

    print("\n" + "=" * 72)
    print("  BTC ATM STRADDLE SELLER  |  DELTA EXCHANGE  |  2026 YTD")
    print("=" * 72)
    print(f"  Instrument     :  {PERPETUAL_SYMBOL}  (proxy for same-day BTC options)")
    print(f"  Lots           :  {LOTS} × {CONTRACT_BTC} BTC = {LOTS*CONTRACT_BTC:.1f} BTC per leg")
    print(f"  Entry          :  ~5:00 AM IST  (23:00 UTC candle close)")
    print(f"  Exit           :  ~5:00 PM IST  (11:00 UTC candle close, 12h later)")
    print(f"  SL             :  Mark rises {SL_PCT*100:.0f}% above collected premium")
    print(f"  Pricing        :  Black-Scholes  |  IV = 30-day rolling realized vol")
    print()
    print(f"  Avg IV         :  {avg_iv:.1f}%  annualised")
    print(f"  Avg premium    :  ${avg_prem:.2f}  per day  (100 lots)")
    print(f"  Avg BE range   :  ±{be_range_pct:.2f}% of spot  (${trades['prem_per_unit'].mean():.0f} per unit)")
    print(f"  Avg BTC move   :  {avg_move:.2f}%  abs  (12h window)")
    print()
    print(f"  Total Days     :  {n}")
    print(f"  SL exits       :  {sl_count}  ({sl_count/n*100:.1f}%)")
    print(f"  Expiry exits   :  {exp_count}  ({exp_count/n*100:.1f}%)")
    print(f"  Win days       :  {wins}  ({wins/n*100:.1f}%)")
    print(f"  Loss days      :  {losses}  ({losses/n*100:.1f}%)")
    print(f"  Total P&L      :  ${total:,.2f}")
    print(f"  Avg Win        :  ${avg_w:,.2f}")
    print(f"  Avg Loss       :  ${avg_l:,.2f}")
    print(f"  Max Drawdown   :  ${max_dd:,.2f}")
    print()
    print(f"  {'Month':<10}  {'Days':>5}  {'SL':>4}  {'Prem Coll':>11}  {'P&L':>10}")
    print("  " + "-" * 48)
    for period in m_pnl.index:
        pnl  = m_pnl[period]
        sl   = int(m_sl[period])
        nn   = int(m_n[period])
        pc   = m_prem[period]
        sign = "+" if pnl >= 0 else ""
        print(f"  {str(period):<10}  {nn:>5}  {sl:>4}  "
              f"${pc:>10,.2f}  {sign}${abs(pnl):>9,.2f}")
    print("=" * 72)

    trades.to_csv(OUTPUT_CSV, index=False)
    print(f"\n  Trade log → {OUTPUT_CSV}")
    print(f"\n  Note: 1 BTC = ~${int(trades['btc_entry'].mean()):,} during this period.")
    print(f"  P&L above is for 100 lots × 0.01 BTC = 1.0 BTC total exposure per leg.")
    print(f"  Scale linearly: 1000 lots ≈ 10× the P&L figures above.")

# ── ENTRY ────────────────────────────────────────────────────────
if __name__ == "__main__":
    df     = fetch_candles()
    trades = run_backtest(df)
    print_report(trades)
