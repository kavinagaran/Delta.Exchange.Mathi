"""
Delta_Straddle_Live.py
BTC MOVE Options (MV-BTC) Long Straddle — Delta Exchange India

Schedule (IST):
  Entry : 5:35 PM IST  = 12:05 UTC
  Exit  : 1:00 AM IST  = 19:30 UTC  (next morning IST, same UTC day)

Guarantees:
  - Exactly ONE order per calendar day (UTC date guard + fired flag)
  - Hard cap of 1000 lots per order — refuses to place if lots != 1000
  - Crash-safe: persists state to straddle_state.json and resumes on restart
"""

import hashlib
import hmac
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv

# Force IPv4: Delta's IP whitelist holds our stable IPv4; Windows rotates
# IPv6 privacy addresses hourly, which gets requests rejected.
import socket
import urllib3.util.connection as _u3c
_u3c.allowed_gai_family = lambda: socket.AF_INET

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
load_dotenv()

BASE_URL         = os.getenv("BASE_URL",         "https://api.india.delta.exchange")
API_KEY          = os.getenv("API_KEY",          "")
API_SECRET       = os.getenv("API_SECRET",       "")
PERPETUAL_SYMBOL = os.getenv("PERPETUAL_SYMBOL", "BTCUSD")

# Capped at 1000 — env can only decrease, never increase
_env_lots = int(os.getenv("STRADDLE_LOTS", 1000))
LOTS      = min(_env_lots, 1000)            # safety cap: never exceed 1000
assert 1 <= LOTS <= 1000, f"LOTS must be between 1 and 1000, got {LOTS}"

# Timing in UTC — configurable via .env / dashboard (defaults: 12:05 entry, 19:30 exit)
ENTRY_H_UTC = int(os.getenv("ENTRY_H_UTC", 12))
ENTRY_M_UTC = int(os.getenv("ENTRY_M_UTC", 5))
EXIT_H_UTC  = int(os.getenv("EXIT_H_UTC",  19))
EXIT_M_UTC  = int(os.getenv("EXIT_M_UTC",  30))
assert 0 <= ENTRY_H_UTC <= 23 and 0 <= ENTRY_M_UTC <= 59, "invalid entry time"
assert 0 <= EXIT_H_UTC  <= 23 and 0 <= EXIT_M_UTC  <= 59, "invalid exit time"

# Execution windows (minutes) — fires once per day inside these ranges
ENTRY_WIN_START = ENTRY_M_UTC
ENTRY_WIN_END   = min(ENTRY_M_UTC + 10, 60)  # 10-min window, capped at hour end
EXIT_WIN_START  = EXIT_M_UTC
EXIT_WIN_END    = min(EXIT_M_UTC  + 10, 60)  # 10-min window, capped at hour end

DRY_RUN  = os.getenv("DRY_RUN", "false").lower() in ("1", "true", "yes")
POLL_SEC = 30

# Morning trade — buys TODAY's contract (settles 12:00 UTC same day)
MORNING_ENABLED = os.getenv("MORNING_ENABLED", "true").lower() in ("1", "true", "yes")
MORNING_H_UTC   = int(os.getenv("MORNING_H_UTC", 0))     # 00:15 UTC = 5:45 AM IST
MORNING_M_UTC   = int(os.getenv("MORNING_M_UTC", 15))
_morning_lots   = int(os.getenv("MORNING_LOTS", 2000))
MAX_ORDER_LOTS  = int(os.getenv("MAX_ORDER_LOTS", 5000))  # hard per-order cap
MORNING_LOTS    = min(_morning_lots, MAX_ORDER_LOTS)
# Dynamic sizing: buy max(configured, affordable-with-balance) at entry time
DYNAMIC_LOTS    = os.getenv("DYNAMIC_LOTS", "true").lower() in ("1", "true", "yes")
assert 0 <= MORNING_H_UTC <= 23 and 0 <= MORNING_M_UTC <= 59, "invalid morning time"
MORNING_WIN_START = MORNING_M_UTC
MORNING_WIN_END   = min(MORNING_M_UTC + 10, 60)

# Morning exit — default 11:30 UTC (5:00 PM IST), 30 min before settlement.
# MORNING_EXIT_ENABLED=false disables the scheduled exit entirely — the
# morning position then closes only via TP monitor, square-off, or settlement.
MORNING_EXIT_ENABLED = os.getenv("MORNING_EXIT_ENABLED", "true").lower() in ("1", "true", "yes")
MORNING_EXIT_H_UTC = int(os.getenv("MORNING_EXIT_H_UTC", 11))
MORNING_EXIT_M_UTC = int(os.getenv("MORNING_EXIT_M_UTC", 30))
assert 0 <= MORNING_EXIT_H_UTC <= 23 and 0 <= MORNING_EXIT_M_UTC <= 59, "invalid morning exit time"
MORNING_EXIT_WIN_START = MORNING_EXIT_M_UTC
MORNING_EXIT_WIN_END   = min(MORNING_EXIT_M_UTC + 10, 60)

# Telegram alerts
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHATID = os.getenv("TELEGRAM_CHAT_ID",   "")
TELEGRAM_ON     = os.getenv("TELEGRAM_ALERTS", "true").lower() in ("1", "true", "yes")

STATE_FILE         = Path(__file__).parent / "straddle_state.json"
MORNING_STATE_FILE = Path(__file__).parent / "morning_state.json"
HISTORY_FILE       = Path(__file__).parent / "trade_history.json"
ENV_FILE           = Path(__file__).parent / ".env"

# ─────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

_fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s",
                         datefmt="%Y-%m-%d %H:%M:%S")
_fh  = TimedRotatingFileHandler(LOG_DIR / "straddle.log",
                                 when="midnight", backupCount=30, utc=True)
_fh.setFormatter(_fmt)
_ch  = logging.StreamHandler(sys.stdout)
_ch.setFormatter(_fmt)

log = logging.getLogger("straddle")
log.setLevel(logging.INFO)
log.addHandler(_fh)
log.addHandler(_ch)

if DRY_RUN:
    log.info("*** DRY-RUN MODE — no real orders will be placed ***")

if not API_KEY or not API_SECRET:
    log.critical("API_KEY / API_SECRET missing in .env — aborting.")
    sys.exit(1)

# ─────────────────────────────────────────────────────────────
# AUTHENTICATION
# ─────────────────────────────────────────────────────────────
def _sign(method: str, path: str, query: str = "", body: str = "") -> dict:
    ts  = str(int(time.time()))
    msg = method + ts + path + query + body
    sig = hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return {
        "api-key":      API_KEY,
        "timestamp":    ts,
        "signature":    sig,
        "Content-Type": "application/json",
        "User-Agent":   "delta-straddle-bot/2.0",
    }

# ─────────────────────────────────────────────────────────────
# API HELPERS
# ─────────────────────────────────────────────────────────────
def _get(path: str, params: dict = None, auth: bool = False) -> dict:
    qs   = ("?" + urlencode(params)) if params else ""
    hdrs = _sign("GET", path, qs) if auth else {"User-Agent": "delta-straddle-bot/2.0"}
    resp = requests.get(BASE_URL + path, params=params, headers=hdrs, timeout=(5, 30))
    resp.raise_for_status()
    return resp.json()

def _post(path: str, payload: dict) -> dict:
    body = json.dumps(payload, separators=(",", ":"))
    hdrs = _sign("POST", path, "", body)
    resp = requests.post(BASE_URL + path, data=body, headers=hdrs, timeout=(5, 30))
    if not resp.ok:
        log.error("POST %s  %d  %s", path, resp.status_code, resp.text[:400])
        resp.raise_for_status()
    return resp.json()

def _retry(fn, *args, retries=3, delay=3, **kwargs):
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            log.warning("Attempt %d/%d — %s: %s", attempt + 1, retries, fn.__name__, exc)
            if attempt < retries - 1:
                time.sleep(delay)
    raise RuntimeError(f"All {retries} retries exhausted: {fn.__name__}")

# ─────────────────────────────────────────────────────────────
# STATE PERSISTENCE
# ─────────────────────────────────────────────────────────────
def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))
    log.info("State saved → %s", STATE_FILE.name)

def load_state() -> dict | None:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return None

def clear_state():
    STATE_FILE.unlink(missing_ok=True)

def save_morning_state(state: dict):
    MORNING_STATE_FILE.write_text(json.dumps(state, indent=2))
    log.info("State saved → %s", MORNING_STATE_FILE.name)

def load_morning_state() -> dict | None:
    if MORNING_STATE_FILE.exists():
        try:
            return json.loads(MORNING_STATE_FILE.read_text())
        except Exception:
            pass
    return None

def log_trade(state: dict):
    """Append closed trade to trade_history.json for the dashboard."""
    record = {
        "date":         state.get("entry_date", ""),
        "symbol":       state.get("symbol", ""),
        "strike":       state.get("strike", 0),
        "lots":         state.get("lots", 0),
        "entry_mark":   state.get("entry_mark", 0),
        "exit_mark":    state.get("exit_mark", 0),
        "btc_entry":    state.get("btc_at_entry", 0),
        "btc_exit":     state.get("btc_at_exit", 0),
        "btc_move_pct": state.get("btc_move_pct", 0),
        "pnl_usd":      state.get("pnl_usd", 0),
        "cost_usd":     state.get("total_cost_usd", 0),
        "entry_time":   state.get("entry_time_utc", ""),
        "exit_time":    state.get("exit_time_utc", ""),
    }
    history = []
    if HISTORY_FILE.exists():
        try:
            history = json.loads(HISTORY_FILE.read_text())
        except Exception:
            history = []
    # Dedupe on date+symbol+entry_time so multiple trades per day are kept
    history = [r for r in history
               if not (r.get("date") == record["date"]
                       and r.get("symbol") == record["symbol"]
                       and r.get("entry_time") == record["entry_time"])]
    history.append(record)
    history.sort(key=lambda r: (r.get("date", ""), r.get("entry_time", "")))
    HISTORY_FILE.write_text(json.dumps(history, indent=2))
    log.info("Trade logged → %s  P&L=$%.2f", record["date"], record.get("pnl_usd", 0))

# ─────────────────────────────────────────────────────────────
# TELEGRAM ALERTS
# ─────────────────────────────────────────────────────────────
def send_telegram(text: str):
    """Fire-and-forget Telegram message. Silently skips if not configured."""
    if not TELEGRAM_ON or not TELEGRAM_TOKEN or not TELEGRAM_CHATID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHATID, "text": text, "parse_mode": "HTML"},
            timeout=8,
        )
        log.info("Telegram alert sent.")
    except Exception as exc:
        log.warning("Telegram alert failed: %s", exc)

# ─────────────────────────────────────────────────────────────
# ONE-ORDER-PER-DAY GUARD
# ─────────────────────────────────────────────────────────────
# Default 3: morning scheduled + evening scheduled + one manual
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", 3))


def already_traded_today() -> bool:
    """
    Returns True if we may NOT enter another trade today:
      - a position is still open on the exchange, or
      - today's trade count has reached MAX_TRADES_PER_DAY.
    A stale OPEN state (contract settled or closed externally) is
    detected, marked CLOSED, and still counts toward the daily cap.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    state = load_state() or {}

    if state.get("status") == "OPEN":
        # Position may have settled or been closed outside the bot
        settled = False
        try:
            st = datetime.fromisoformat(
                state.get("settlement", "").replace("Z", "+00:00"))
            settled = datetime.now(timezone.utc) >= st
        except ValueError:
            pass
        if not settled and not DRY_RUN:
            pos = get_mv_position(state.get("product_id", 0))
            settled = not pos or int(float(pos.get("size", 0))) == 0
        if not settled:
            log.info("Position still OPEN — cannot enter another trade.")
            return True
        log.info("Stale OPEN state — position settled/closed externally. Marking CLOSED.")
        state["status"]       = "CLOSED"
        state["exit_trigger"] = "settlement_or_external"
        save_state(state)

    # Count today's completed trades (history uses 'date' from the bot,
    # 'entry_date' from dashboard square-offs)
    trades = []
    if HISTORY_FILE.exists():
        try:
            trades = json.loads(HISTORY_FILE.read_text())
        except Exception:
            trades = []
    count = sum(1 for t in trades
                if (t.get("entry_date") or t.get("date", "")) == today)

    # Include state's trade if it closed today but never reached history
    # (e.g. a manual position that settled on the exchange)
    if state.get("entry_date") == today and state.get("status") != "OPEN":
        in_history = any(
            (t.get("entry_date") or t.get("date", "")) == today
            and t.get("symbol") == state.get("symbol")
            for t in trades)
        if not in_history:
            count += 1

    if count >= MAX_TRADES_PER_DAY:
        log.info("Daily trade cap reached (%d/%d). Skipping entry.",
                 count, MAX_TRADES_PER_DAY)
        return True
    log.info("Trades today: %d/%d — entry allowed.", count, MAX_TRADES_PER_DAY)
    return False

# ─────────────────────────────────────────────────────────────
# MV CONTRACT LOOKUP
# ─────────────────────────────────────────────────────────────
def get_mv_contract(expiry_date_str: str) -> dict | None:
    """Fetch live BTC MOVE contract for a given UTC expiry date (YYYY-MM-DD)."""
    params = {
        "contract_types": "move_options",
        "states":         "live",
        "expiry":         expiry_date_str,
        "page_size":      50,
    }
    data = _retry(_get, "/v2/products", params=params)
    for p in data.get("result", []):
        if (p.get("underlying_asset", {}).get("symbol") == "BTC"
                and p.get("trading_status") == "operational"
                and p.get("settlement_time", "").startswith(expiry_date_str)):
            return p
    return None

def find_active_mv_contract() -> dict:
    """
    At 12:05 UTC, today's MV (settling at 12:00 UTC) has already expired.
    We buy the contract settling at 12:00 UTC TOMORROW.
    Falls back to today's contract if entry is before 12:00 UTC.
    """
    now      = datetime.now(timezone.utc)
    tmrw_str = (now.date() + timedelta(days=1)).strftime("%Y-%m-%d")
    today_str = now.date().strftime("%Y-%m-%d")

    contract = get_mv_contract(tmrw_str)
    if contract:
        log.info("MV contract (tomorrow): %s  id=%s  strike=%s  settles=%s",
                 contract["symbol"], contract["id"],
                 contract.get("strike_price"), contract.get("settlement_time"))
        return contract

    contract = get_mv_contract(today_str)
    if contract:
        log.warning("Falling back to today's MV contract: %s", contract["symbol"])
        return contract

    raise LookupError(
        f"No live BTC MV contract found for {tmrw_str} or {today_str}."
    )

# ─────────────────────────────────────────────────────────────
# MARKET DATA
# ─────────────────────────────────────────────────────────────
def get_btc_price() -> float:
    data  = _retry(_get, f"/v2/tickers/{PERPETUAL_SYMBOL}")
    price = float(data.get("result", {}).get("mark_price") or 0)
    if price <= 0:
        raise ValueError(f"Invalid BTC price: {data}")
    return price

def get_mv_mark(symbol: str) -> float:
    try:
        data = _get(f"/v2/tickers/{symbol}")
        return float(data.get("result", {}).get("mark_price") or 0)
    except Exception as e:
        log.warning("Could not fetch mark for %s: %s", symbol, e)
        return 0.0

# ─────────────────────────────────────────────────────────────
# ORDER MANAGEMENT
# ─────────────────────────────────────────────────────────────
def place_market_order(product_id: int, symbol: str, side: str, size: int) -> dict:
    # Hard safety check — never exceed the configured per-order cap
    if size > MAX_ORDER_LOTS:
        raise ValueError(f"Order size {size} exceeds hard cap of {MAX_ORDER_LOTS} lots. Aborting.")

    if DRY_RUN:
        log.info("[DRY-RUN] %s %d lots  %s  id=%d", side.upper(), size, symbol, product_id)
        return {"result": {"id": 0, "state": "dry_run"}}

    log.info("ORDER: %s  %d lots  %s  (product_id=%d)", side.upper(), size, symbol, product_id)
    payload = {
        "product_id": product_id,
        "size":        size,
        "side":        side,
        "order_type": "market_order",
    }
    resp  = _retry(_post, "/v2/orders", payload)
    order = resp.get("result", {})
    log.info("  Filled: order_id=%s  state=%s  avg_price=%s",
             order.get("id"), order.get("state"),
             order.get("average_fill_price", "pending"))
    return resp

def get_mv_position(product_id: int) -> dict | None:
    data = _retry(_get, "/v2/positions/margined", auth=True)
    for pos in data.get("result", []):
        if pos.get("product_id") == product_id and float(pos.get("size", 0)) != 0:
            return pos
    return None

def get_available_usd() -> float:
    data = _retry(_get, "/v2/wallet/balances", auth=True)
    for w in data.get("result", []):
        if w.get("asset_symbol") == "USD":
            return float(w.get("available_balance") or 0)
    return 0.0

def _effective_lots(configured: int, mark: float, contract_val: float, label: str) -> int:
    """Dynamic lot sizing: how many lots the available balance can buy at the
    current mark (with a 2% fee/slippage buffer). Per spec we buy whichever is
    HIGHER — configured or affordable — capped at MAX_ORDER_LOTS. Falls back
    to the configured size if the balance check fails."""
    if not DYNAMIC_LOTS:
        return configured
    try:
        bal  = get_available_usd()
        cost = mark * contract_val
        afford = int((bal * 0.98) / cost) if cost > 0 else 0
    except Exception as e:
        log.warning("%s: balance check failed (%s) — using configured %d lots.",
                    label, e, configured)
        return configured
    lots = min(max(configured, afford), MAX_ORDER_LOTS)
    log.info("%s lot sizing: configured=%d  affordable=%d  (bal $%.2f, $%.4f/lot)  -> using %d",
             label, configured, afford, bal, cost, lots)
    if afford < configured:
        log.warning("%s: balance only covers %d of the configured %d lots — "
                    "order may be rejected for insufficient margin.",
                    label, afford, configured)
    return max(lots, 1)

# ─────────────────────────────────────────────────────────────
# API / IP WATCHDOG — detects whitelisted-IP lockout
# ─────────────────────────────────────────────────────────────
_ip_alert_at   = 0.0
_ip_was_broken = False

def check_api_access():
    """Probe an authenticated endpoint each heartbeat. If the API key
    rejects this machine's IP (ISP rotated the address), alert on
    Telegram with the new IPs to whitelist; alert again on recovery."""
    global _ip_alert_at, _ip_was_broken
    try:
        resp = requests.get(
            BASE_URL + "/v2/positions/margined",
            headers=_sign("GET", "/v2/positions/margined"),
            timeout=(5, 15),
        )
        data = resp.json()
    except Exception as exc:
        log.warning("API watchdog probe failed: %s", exc)
        return

    if data.get("success"):
        if _ip_was_broken:
            _ip_was_broken = False
            _ip_alert_at   = 0.0
            log.info("API access restored — orders can be placed again.")
            send_telegram("✅ <b>API ACCESS RESTORED — MATHI</b>\nOrders can be placed again.")
        return

    code = str(((data.get("error") or {}).get("code")) or "")
    if code != "ip_not_whitelisted_for_api_key":
        log.warning("API watchdog: auth probe failed with %s", code or data)
        return

    _ip_was_broken = True
    if time.time() - _ip_alert_at < 3600:   # alert at most once per hour
        return
    _ip_alert_at = time.time()

    ip4 = ip6 = "?"
    try:
        ip4 = requests.get("https://api.ipify.org", timeout=5).text.strip()
    except Exception:
        pass
    try:
        ip6 = requests.get("https://api64.ipify.org", timeout=5).text.strip()
    except Exception:
        pass

    log.error("API KEY BLOCKED — IP not whitelisted. IPv4=%s  IPv6=%s", ip4, ip6)
    send_telegram(
        "🚨 <b>API KEY BLOCKED — IP CHANGED (MATHI)</b>\n"
        "Delta is rejecting authenticated calls: IP not whitelisted.\n"
        "Whitelist these in Delta → Account → API Keys:\n"
        f"IPv4 » <code>{ip4}</code>\n"
        f"IPv6 » <code>{ip6}</code>\n"
        "⚠️ TP monitor, exit and entry orders will FAIL until fixed."
    )

# ─────────────────────────────────────────────────────────────
# MORNING ENTRY JOB  —  00:15 UTC  (5:45 AM IST)
# Buys TODAY's contract (settles 12:00 UTC same day).
# ─────────────────────────────────────────────────────────────
def morning_entry_job():
    log.info("=" * 64)
    log.info("MORNING ENTRY  %s UTC  (%s)",
             datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
             _ist_label(MORNING_H_UTC, MORNING_M_UTC))

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Once-per-day guard for the morning slot
    ms = load_morning_state()
    if ms and ms.get("entry_date") == today:
        log.info("Morning trade already recorded today (status=%s). Skipping.",
                 ms.get("status", ""))
        return

    contract = get_mv_contract(today)
    if not contract:
        raise LookupError(f"No live BTC MV contract found for {today}.")
    product_id   = contract["id"]
    symbol       = contract["symbol"]
    strike       = float(contract.get("strike_price",    0))
    contract_val = float(contract.get("contract_value", 0.001))
    settlement   = contract.get("settlement_time", "")

    btc_price  = get_btc_price()
    entry_mark = get_mv_mark(symbol)
    lots       = _effective_lots(MORNING_LOTS, entry_mark, contract_val, "MORNING")
    total_cost = entry_mark * contract_val * lots

    log.info("Symbol      : %s", symbol)
    log.info("Strike      : $%.0f  |  BTC mark: $%.2f", strike, btc_price)
    log.info("Settlement  : %s", settlement)
    log.info("Entry mark  : $%.4f/BTC", entry_mark)
    log.info("Lots        : %d  |  Total premium: $%.2f", lots, total_cost)

    order = place_market_order(product_id, symbol, "buy", lots)
    fill  = float(order.get("result", {}).get("average_fill_price") or entry_mark)

    now = datetime.now(timezone.utc)
    save_morning_state({
        "slot":           "morning",
        "status":         "OPEN",
        "entry_date":     today,
        "entry_time_utc": now.strftime("%H:%M:%S"),
        "symbol":         symbol,
        "product_id":     product_id,
        "strike":         strike,
        "settlement":     settlement,
        "contract_value": contract_val,
        "lots":           lots,
        "entry_mark":     round(fill, 4),
        "btc_at_entry":   round(btc_price, 2),
        "total_cost_usd": round(fill * contract_val * lots, 2),
        "order_id":       order.get("result", {}).get("id"),
        "entry_trigger":  "morning_scheduled",
    })

    send_telegram(
        f"🌅 <b>MORNING STRADDLE OPENED — MATHI</b>\n"
        f"<code>{'━' * 24}</code>\n"
        f"Symbol  » <code>{symbol}</code>\n"
        f"Strike  » <code>${strike:,.0f}</code>\n"
        f"Lots    » <code>{lots:,}</code>\n"
        f"Entry   » <code>${fill:.4f} / BTC</code>\n"
        f"Cost    » <code>${fill * contract_val * lots:,.2f}</code>\n"
        f"BTC     » <code>${btc_price:,.2f}</code>\n"
        f"Settles » <code>{settlement.replace('T', ' ').replace('Z', ' UTC')}</code>\n"
        f"Mode    » <code>{'DRY-RUN ⚠' if DRY_RUN else 'LIVE ●'}</code>"
    )
    log.info("Morning straddle opened: %d lots %s @ $%.4f", lots, symbol, fill)


# ─────────────────────────────────────────────────────────────
# ENTRY JOB  —  12:05 UTC  (5:35 PM IST)
# ─────────────────────────────────────────────────────────────
def entry_job():
    log.info("=" * 64)
    log.info("ENTRY  %s UTC  (5:35 PM IST)",
             datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"))

    # ONE-ORDER-PER-DAY guard — double check before doing anything
    if already_traded_today():
        return

    # Find MV contract
    contract     = find_active_mv_contract()
    product_id   = contract["id"]
    symbol       = contract["symbol"]
    strike       = float(contract.get("strike_price",    0))
    contract_val = float(contract.get("contract_value", 0.001))
    settlement   = contract.get("settlement_time", "")

    # Market snapshot
    btc_price  = get_btc_price()
    entry_mark = get_mv_mark(symbol)
    lots       = _effective_lots(LOTS, entry_mark, contract_val, "EVENING")
    total_cost = entry_mark * contract_val * lots

    log.info("Symbol      : %s", symbol)
    log.info("Product ID  : %d", product_id)
    log.info("Strike      : $%.0f  |  BTC mark: $%.2f", strike, btc_price)
    log.info("Settlement  : %s", settlement)
    log.info("Entry mark  : $%.4f/BTC  ($%.4f/lot)", entry_mark, entry_mark * contract_val)
    log.info("Lots        : %d  |  Total premium: $%.2f", lots, total_cost)

    # Place the single buy order
    order = place_market_order(product_id, symbol, "buy", lots)

    # Persist — this is what prevents any second order today
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    save_state({
        "status":         "OPEN",
        "entry_date":     today,
        "entry_time_utc": datetime.now(timezone.utc).strftime("%H:%M:%S"),
        "symbol":         symbol,
        "product_id":     product_id,
        "strike":         strike,
        "settlement":     settlement,
        "contract_value": contract_val,
        "lots":           lots,
        "entry_mark":     entry_mark,
        "btc_at_entry":   btc_price,
        "total_cost_usd": round(total_cost, 4),
        "order_id":       order.get("result", {}).get("id"),
    })
    log.info("Straddle OPEN. Waiting for exit at %02d:%02d UTC.",
             EXIT_H_UTC, EXIT_M_UTC)

    send_telegram(
        f"🔺 <b>ENTRY CONFIRMED</b>\n"
        f"<code>{'━' * 24}</code>\n"
        f"Symbol  » <code>{symbol}</code>\n"
        f"Strike  » <code>${strike:,.0f}</code>\n"
        f"Lots    » <code>{lots:,}</code>\n"
        f"Premium » <code>${entry_mark:.4f} / BTC</code>\n"
        f"Cost    » <code>${total_cost:.2f}</code>\n"
        f"BTC     » <code>${btc_price:,.2f}</code>\n"
        f"Time    » <code>{datetime.now(timezone.utc).strftime('%H:%M UTC')}  (IST +5:30)</code>\n"
        f"Mode    » <code>{'DRY-RUN ⚠' if DRY_RUN else 'LIVE ●'}</code>"
    )

# ─────────────────────────────────────────────────────────────
# EXIT JOBS — shared close logic for both slots
# ─────────────────────────────────────────────────────────────
def exit_job():
    """Evening exit — configured via EXIT_H_UTC / EXIT_M_UTC."""
    log.info("=" * 64)
    log.info("EXIT   %s UTC  (%s)",
             datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
             _ist_label(EXIT_H_UTC, EXIT_M_UTC))
    state = load_state()
    if not state or state.get("status") != "OPEN":
        log.info("No open evening position — nothing to close.")
        return
    _close_position_job(state, save_state, "EVENING")


def morning_exit_job():
    """Morning exit — configured via MORNING_EXIT_H_UTC / MORNING_EXIT_M_UTC."""
    log.info("=" * 64)
    log.info("MORNING EXIT  %s UTC  (%s)",
             datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
             _ist_label(MORNING_EXIT_H_UTC, MORNING_EXIT_M_UTC))
    state = load_morning_state()
    if not state or state.get("status") != "OPEN":
        log.info("No open morning position — nothing to close.")
        return
    _close_position_job(state, save_morning_state, "MORNING")


def _close_position_job(state: dict, save_fn, label: str):
    product_id   = state["product_id"]
    symbol       = state["symbol"]
    entry_mark   = state.get("entry_mark",    0)
    contract_val = state.get("contract_value", 0.001)
    lots         = state["lots"]

    # Verify position on exchange (skip in DRY RUN — no real position exists)
    if DRY_RUN:
        actual_size = lots
        log.info("[DRY-RUN] Assuming %d lots of %s (no exchange check)", actual_size, symbol)
    else:
        position    = get_mv_position(product_id)
        actual_size = int(float(position["size"])) if position else 0
        log.info("Exchange position: %d lots of %s", actual_size, symbol)
        if actual_size == 0:
            log.warning("No open position found on exchange for %s — may have been closed manually.", symbol)
        elif abs(actual_size) != lots:
            log.warning("Position mismatch: expected %d lots, found %d. Using exchange figure.", lots, actual_size)

    # Long positions have positive exchange size, shorts negative
    is_short   = state.get("side") == "short" or actual_size < 0
    pnl_sign   = -1 if is_short else 1
    close_side = "buy" if is_short else "sell"
    close_size = abs(actual_size)

    # Snapshot exit mark
    exit_mark  = get_mv_mark(symbol)
    btc_exit   = get_btc_price()
    btc_entry  = state.get("btc_at_entry", 0)
    btc_move   = (btc_exit - btc_entry) / btc_entry * 100 if btc_entry else 0

    if entry_mark > 0 and exit_mark > 0:
        pnl_per_btc = (exit_mark - entry_mark) * pnl_sign
        pnl_usd     = pnl_per_btc * contract_val * close_size
        ret_pct     = pnl_per_btc / entry_mark * 100
        log.info("Entry mark  : $%.4f/BTC  |  Exit mark: $%.4f/BTC  (%s)",
                 entry_mark, exit_mark, "SHORT" if is_short else "LONG")
        log.info("BTC move    : %.2f%%  ($%.0f -> $%.0f)", btc_move, btc_entry, btc_exit)
        log.info("P&L         : $%.2f  (%+.1f%%)", pnl_usd, ret_pct)
    else:
        pnl_usd   = 0.0
        exit_mark = 0.0

    # Close position
    if close_size > 0:
        place_market_order(product_id, symbol, close_side, close_size)
    else:
        log.warning("Size is 0 — no close order placed.")

    # Update state
    state.update({
        "status":         "CLOSED",
        "exit_time_utc":  datetime.now(timezone.utc).strftime("%H:%M:%S"),
        "btc_at_exit":    round(btc_exit,  2),
        "btc_move_pct":   round(btc_move,  4),
        "exit_mark":      exit_mark,
        "pnl_usd":        round(pnl_usd,   4),
        "exit_trigger":   f"scheduled_exit_{label.lower()}",
    })
    save_fn(state)
    log_trade(state)
    log.info("%s straddle CLOSED. P&L: $%.2f", label, pnl_usd)

    _win   = pnl_usd >= 0
    _icon  = "✅" if _win else "❌"
    _label = "WIN" if _win else "LOSS"
    _sign  = "+" if _win else ""
    _arrow = "▲" if btc_move >= 0 else "▼"
    _slot_icon = "🌅" if label == "MORNING" else "🌇"
    send_telegram(
        f"{_icon} <b>{_slot_icon} {label} EXIT — {_label}  {_sign}${abs(pnl_usd):.2f}</b>\n"
        f"<code>{'━' * 24}</code>\n"
        f"Symbol  » <code>{symbol}</code>\n"
        f"Entry   » <code>${entry_mark:.4f} / BTC</code>\n"
        f"Exit    » <code>${exit_mark:.4f} / BTC</code>\n"
        f"BTC Δ   » <code>{_arrow}{abs(btc_move):.2f}%  "
        f"(${btc_entry:,.0f} → ${state.get('btc_at_exit', 0):,.0f})</code>\n"
        f"PnL     » <b>{_sign}${abs(pnl_usd):.2f}</b>\n"
        f"Lots    » <code>{lots:,}</code>\n"
        f"Time    » <code>{datetime.now(timezone.utc).strftime('%H:%M UTC')}  (IST +5:30)</code>"
    )

# ─────────────────────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────────────────────
def _ist_label(h_utc: int, m_utc: int) -> str:
    t = (h_utc * 60 + m_utc + 330) % 1440
    h, m = divmod(t, 60)
    ampm = "AM" if h < 12 else "PM"
    h12  = h % 12 or 12
    return f"{h12}:{m:02d} {ampm} IST"


def main():
    log.info("=" * 64)
    log.info("Delta MV Straddle Bot")
    log.info("  Morning: %02d:%02d UTC (%s)  lots=%d  enabled=%s",
             MORNING_H_UTC, MORNING_M_UTC,
             _ist_label(MORNING_H_UTC, MORNING_M_UTC), MORNING_LOTS, MORNING_ENABLED)
    log.info("  M-Exit : %s",
             ("%02d:%02d UTC (%s)" % (MORNING_EXIT_H_UTC, MORNING_EXIT_M_UTC,
                                       _ist_label(MORNING_EXIT_H_UTC, MORNING_EXIT_M_UTC)))
             if MORNING_EXIT_ENABLED else "DISABLED (TP/settlement only)")
    log.info("  Entry  : %02d:%02d UTC (%s)", ENTRY_H_UTC, ENTRY_M_UTC,
             _ist_label(ENTRY_H_UTC, ENTRY_M_UTC))
    log.info("  Exit   : %02d:%02d UTC (%s)", EXIT_H_UTC, EXIT_M_UTC,
             _ist_label(EXIT_H_UTC, EXIT_M_UTC))
    log.info("  Lots   : %d  |  DRY-RUN: %s", LOTS, DRY_RUN)
    log.info("=" * 64)

    # On restart, show any open positions
    state = load_state()
    if state and state.get("status") == "OPEN":
        log.info("Resuming open EVENING position: %s  (entered %s UTC, lots=%d)",
                 state.get("symbol"), state.get("entry_time_utc"), state.get("lots"))
    m_state = load_morning_state()
    if m_state and m_state.get("status") == "OPEN":
        log.info("Resuming open MORNING position: %s  (entered %s UTC, lots=%d)",
                 m_state.get("symbol"), m_state.get("entry_time_utc"), m_state.get("lots"))

    fired_entry        = False
    fired_exit         = False
    fired_morning      = False
    fired_morning_exit = False
    last_day           = None
    env_mtime          = ENV_FILE.stat().st_mtime if ENV_FILE.exists() else 0

    while True:
        try:
            now     = datetime.now(timezone.utc)
            h, m    = now.hour, now.minute
            today   = now.strftime("%Y-%m-%d")

            # Reset daily flags on new UTC day
            if today != last_day:
                fired_entry        = False
                fired_exit         = False
                fired_morning      = False
                fired_morning_exit = False
                last_day           = today
                log.info("New UTC day: %s — daily flags reset.", today)

            # MORNING ENTRY TRIGGER  00:15–00:24 UTC (5:45 AM IST)
            in_morning_window = (MORNING_ENABLED
                                 and h == MORNING_H_UTC
                                 and MORNING_WIN_START <= m < MORNING_WIN_END)
            if in_morning_window and not fired_morning:
                fired_morning = True
                try:
                    morning_entry_job()
                except Exception as exc:
                    log.exception("Morning entry job failed")
                    send_telegram(f"⚠️ <b>MORNING ENTRY FAILED — MATHI</b>\n<code>{exc}</code>")

            # MORNING EXIT TRIGGER (skipped when MORNING_EXIT_ENABLED=false)
            in_morning_exit = (MORNING_ENABLED
                               and MORNING_EXIT_ENABLED
                               and h == MORNING_EXIT_H_UTC
                               and MORNING_EXIT_WIN_START <= m < MORNING_EXIT_WIN_END)
            if in_morning_exit and not fired_morning_exit:
                fired_morning_exit = True
                try:
                    morning_exit_job()
                except Exception as exc:
                    log.exception("Morning exit job failed")
                    send_telegram(f"⚠️ <b>MORNING EXIT FAILED — MATHI</b>\n<code>{exc}</code>")

            # ENTRY TRIGGER  12:05–12:14 UTC
            in_entry_window = (h == ENTRY_H_UTC
                               and ENTRY_WIN_START <= m < ENTRY_WIN_END)
            if in_entry_window and not fired_entry:
                fired_entry = True
                try:
                    entry_job()
                except Exception as exc:
                    log.exception("Entry job failed")
                    send_telegram(f"⚠️ <b>ENTRY FAILED</b>\n<code>{exc}</code>")

            # EXIT TRIGGER  19:30–19:39 UTC
            in_exit_window = (h == EXIT_H_UTC
                              and EXIT_WIN_START <= m < EXIT_WIN_END)
            if in_exit_window and not fired_exit:
                fired_exit = True
                try:
                    exit_job()
                except Exception as exc:
                    log.exception("Exit job failed")
                    send_telegram(f"⚠️ <b>EXIT FAILED</b>\n<code>{exc}</code>")

            # CONFIG WATCH — when .env changes (dashboard/app save, manual
            # edit), reload the whole process so new settings apply without
            # anyone having to restart the service by hand. os.execv replaces
            # this process in place; daily-fire guards are state-file backed,
            # so a reload inside a trigger window can't double-fire orders.
            if ENV_FILE.exists():
                env_mt = ENV_FILE.stat().st_mtime
                if env_mt != env_mtime and time.time() - env_mt > 2:
                    log.info("Config change detected in .env — reloading bot with new settings.")
                    send_telegram("🔄 <b>CONFIG CHANGED — BOT RELOADED (MATHI)</b>\n"
                                  "New settings are now in effect.")
                    os.execv(sys.executable, [sys.executable] + sys.argv)

            # Heartbeat every 10 min — reports both slots
            if m % 10 == 0 and now.second < POLL_SEC:
                def _slot_desc(s):
                    if not s or not s.get("status") or s.get("status") == "IDLE":
                        return "idle"
                    return f"{s.get('status', '?')} {s.get('symbol', '?')} x{s.get('lots', '?')}"
                log.info("Heartbeat %s UTC  morning[%s]  evening[%s]",
                         now.strftime("%H:%M"),
                         _slot_desc(load_morning_state()), _slot_desc(load_state()))
                check_api_access()

        except Exception:
            log.exception("Main loop error")

        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
