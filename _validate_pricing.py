import requests
import numpy as np
from scipy import stats, optimize
from datetime import datetime, timezone

BASE = "https://api.india.delta.exchange"

def fetch(sym, start_ts, end_ts, res="1h"):
    resp = requests.get(f"{BASE}/v2/history/candles",
        params={"symbol": sym, "resolution": res, "start": start_ts, "end": end_ts},
        timeout=20)
    rows = resp.json().get("result") or []
    return sorted(rows, key=lambda x: x["time"])

def bs_call(S, K, T, sigma):
    if T <= 0: return max(S - K, 0.0)
    d1 = (np.log(S / K) + 0.5 * sigma**2 * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    return float(S * stats.norm.cdf(d1) - K * stats.norm.cdf(d2))

def bs_put(S, K, T, sigma):
    if T <= 0: return max(K - S, 0.0)
    d1 = (np.log(S / K) + 0.5 * sigma**2 * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    return float(K * stats.norm.cdf(-d2) - S * stats.norm.cdf(-d1))

def impl_vol(mkt, S, K, T, otype):
    if T <= 0 or mkt <= 0: return float("nan")
    intr = max(S - K, 0) if otype == "call" else max(K - S, 0)
    if mkt <= intr: return float("nan")
    try:
        fn = (lambda s: bs_call(S, K, T, s) - mkt) if otype == "call" else \
             (lambda s: bs_put(S, K, T, s) - mkt)
        return optimize.brentq(fn, 0.001, 10.0, xtol=1e-6)
    except Exception:
        return float("nan")

# Jul 4 window: entry 23:00 UTC Jul 3 → expiry 12:00 UTC Jul 4
entry_ts  = int(datetime(2026, 7, 3, 23, 0, 0, tzinfo=timezone.utc).timestamp())
expiry_ts = int(datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc).timestamp())
K = 62600
RV = 0.446  # 44.6% realized IV from backtest

perp  = fetch("BTCUSD",              entry_ts, expiry_ts, "1h")
calls = fetch("C-BTC-62600-040726", entry_ts, expiry_ts, "1h")
puts  = fetch("P-BTC-62600-040726", entry_ts, expiry_ts, "1h")

print(f"BTCUSD 1h bars   : {len(perp)}")
print(f"C-BTC-62600 bars : {len(calls)}")
print(f"P-BTC-62600 bars : {len(puts)}")

# Settlement prices from product API
resp2 = requests.get(f"{BASE}/v2/products",
    params={"contract_types": "call_options,put_options", "page_size": 200, "state": "expired"},
    timeout=20)
prods = {r["symbol"]: r for r in resp2.json().get("result", [])}
cp = prods.get("C-BTC-62600-040726", {})
pp = prods.get("P-BTC-62600-040726", {})
print(f"Call settlement_price : {cp.get('settlement_price')}")
print(f"Put  settlement_price : {pp.get('settlement_price')}")
print(f"contract_value        : {cp.get('contract_value')} BTC")
print()

perp_by_ts = {r["time"]: float(r["close"]) for r in perp}
call_by_ts = {r["time"]: float(r["close"]) for r in calls}
put_by_ts  = {r["time"]: float(r["close"]) for r in puts}

all_ts = sorted(set(perp_by_ts) | set(call_by_ts))
hdr = f"{'Time UTC':<16} {'BTC':>7} {'C_mkt':>7} {'P_mkt':>7} {'Strad_mkt':>10} {'BS_strad':>9} {'C_IV':>7} {'P_IV':>7} {'T_h':>5}"
print(hdr)
print("-" * 80)
for ts in all_ts:
    dt   = datetime.fromtimestamp(ts, tz=timezone.utc)
    S    = perp_by_ts.get(ts)
    Cm   = call_by_ts.get(ts)
    Pm   = put_by_ts.get(ts)
    if S is None:
        continue
    T = max((expiry_ts - ts) / 3600 / 8760, 0)
    bsc = bs_call(S, K, T, RV)
    bsp = bs_put(S,  K, T, RV)
    bss = bsc + bsp
    mkt = (Cm + Pm) if (Cm and Pm) else None
    civ = impl_vol(Cm, S, K, T, "call") if (Cm and T > 0) else float("nan")
    piv = impl_vol(Pm, S, K, T, "put")  if (Pm and T > 0) else float("nan")
    row = (
        f"{dt.strftime('%m-%d %H:%M'):<16}"
        f"{S:>7.0f}"
        f"{Cm:>7.1f}" if Cm else f"{'  -':>7}"
    )
    Cs  = f"{Cm:>7.1f}" if Cm else f"{'  -':>7}"
    Ps  = f"{Pm:>7.1f}" if Pm else f"{'  -':>7}"
    Ms  = f"{mkt:>10.1f}" if mkt else f"{'  -':>10}"
    civs = f"{civ*100:>6.1f}%" if not np.isnan(civ) else f"{'  n/a':>7}"
    pivs = f"{piv*100:>6.1f}%" if not np.isnan(piv) else f"{'  n/a':>7}"
    Th   = T * 8760
    print(f"{dt.strftime('%m-%d %H:%M'):<16} {S:>7.0f} {Cs} {Ps} {Ms} {bss:>9.1f} {civs} {pivs} {Th:>5.1f}")
