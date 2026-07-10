"""
tp_monitor.py — Take-profit monitor for MV-BTC positions.
Usage:  python tp_monitor.py [--slot morning|evening] [--user <username>]

Watches one user's slot state file (users/<username>/) and market price;
closes the position at the configured profit target using THAT user's own
Delta API keys (users/<username>/account.json, .env keys as fallback).
Slot config (.env):
  evening: TP_TARGET_PNL, TP_POLL_SECS
  morning: TP_TARGET_PNL_MORNING, TP_POLL_SECS_MORNING
"""
import os, sys, time, hmac, hashlib, json, logging, requests
from pathlib import Path
from dotenv import load_dotenv

# Force IPv4 — Delta's whitelist holds our IPv4; IPv6 rotates and gets rejected
import socket
import urllib3.util.connection as _u3c
_u3c.allowed_gai_family = lambda: socket.AF_INET

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")


def _arg(flag: str, default: str) -> str:
    if flag in sys.argv:
        try:
            return sys.argv[sys.argv.index(flag) + 1].strip().lower()
        except IndexError:
            pass
    return default


SLOT = _arg("--slot", "evening")
if SLOT not in ("morning", "evening"):
    print(f"Invalid slot: {SLOT}")
    sys.exit(1)

USER     = _arg("--user", os.getenv("BOT_USER", os.getenv("DASH_USER", "mathi")))
USER_DIR = BASE_DIR / "users" / USER

# The monitored account's own credentials; .env keys as fallback
try:
    _acct = json.loads((USER_DIR / "account.json").read_text(encoding="utf-8"))
except Exception:
    _acct = {}
API_KEY    = _acct.get("api_key")    or os.getenv("API_KEY", "")
API_SECRET = _acct.get("api_secret") or os.getenv("API_SECRET", "")
BASE_URL   = os.getenv("BASE_URL", "https://api.india.delta.exchange")
TG_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT    = os.getenv("TELEGRAM_CHAT_ID", "")

if SLOT == "morning":
    STATE_FILE = USER_DIR / "morning_state.json"
    TARGET_PNL = float(os.getenv("TP_TARGET_PNL_MORNING") or 300)
    POLL_SECS  = int(float(os.getenv("TP_POLL_SECS_MORNING") or 30))
else:
    STATE_FILE = USER_DIR / "straddle_state.json"
    TARGET_PNL = float(os.getenv("TP_TARGET_PNL") or 105)
    POLL_SECS  = int(float(os.getenv("TP_POLL_SECS") or 30))
LOG_NAME = f"tp_{USER}_{SLOT}.log"

HISTORY_FILE = USER_DIR / "trade_history.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(BASE_DIR / "logs" / LOG_NAME, encoding="utf-8"),
    ],
)
log = logging.getLogger(f"tp_monitor[{USER}/{SLOT}]")


def _sign(method, path, query="", body=""):
    ts  = str(int(time.time()))
    msg = method + ts + path + query + body
    sig = hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return {"api-key": API_KEY, "timestamp": ts, "signature": sig,
            "Content-Type": "application/json", "User-Agent": "tp-monitor/2.0"}


def get_mark(symbol):
    r = requests.get(f"{BASE_URL}/v2/tickers/{symbol}", timeout=8)
    return float(r.json().get("result", {}).get("mark_price") or 0)


def get_exchange_size(product_id):
    """Actual open size on the exchange; 0 if none (or None if check failed)."""
    try:
        hdrs = _sign("GET", "/v2/positions/margined")
        r = requests.get(f"{BASE_URL}/v2/positions/margined", headers=hdrs, timeout=8)
        data = r.json()
        if not data.get("success"):
            log.warning("Position check failed: %s", data.get("error"))
            return None
        for pos in data.get("result", []):
            if pos.get("product_id") == product_id:
                return int(float(pos.get("size", 0)))
        return 0
    except Exception as e:
        log.warning("Position check error: %s", e)
        return None


def place_order(product_id, symbol, side, size):
    payload = {"product_id": product_id, "size": size, "side": side, "order_type": "market_order"}
    body    = json.dumps(payload, separators=(",", ":"))
    hdrs    = _sign("POST", "/v2/orders", "", body)
    r       = requests.post(f"{BASE_URL}/v2/orders", data=body, headers=hdrs, timeout=15)
    return r.json()


def send_telegram(text):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"},
            timeout=8,
        )
    except Exception as e:
        log.warning("Telegram failed: %s", e)


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def append_history(state):
    """Record the closed trade so dashboard history and daily caps see it."""
    try:
        hist = json.loads(HISTORY_FILE.read_text()) if HISTORY_FILE.exists() else []
        rec = {
            "date":         state.get("entry_date", ""),
            "entry_date":   state.get("entry_date", ""),
            "symbol":       state.get("symbol", ""),
            "strike":       state.get("strike", 0),
            "lots":         state.get("lots", 0),
            "entry_mark":   state.get("entry_mark", 0),
            "exit_mark":    state.get("exit_mark", 0),
            "pnl_usd":      state.get("pnl_usd", 0),
            "cost_usd":     state.get("total_cost_usd", 0),
            "entry_time":   state.get("entry_time_utc", ""),
            "exit_time":    state.get("exit_time_utc", ""),
            "exit_trigger": state.get("exit_trigger", ""),
            "slot":         SLOT,
        }
        dup = any(r.get("symbol") == rec["symbol"]
                  and (r.get("entry_date") or r.get("date")) == rec["date"]
                  and r.get("entry_time") == rec["entry_time"]
                  for r in hist)
        if not dup:
            hist.append(rec)
            HISTORY_FILE.write_text(json.dumps(hist, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("History append failed: %s", e)


def close_position(state, mark, pnl):
    product_id = state["product_id"]
    symbol     = state["symbol"]
    lots       = state["lots"]
    is_short   = state.get("side") == "short"
    close_side = "buy" if is_short else "sell"

    # Never close blind — verify the position still exists on the exchange.
    live_size = get_exchange_size(product_id)
    if live_size is None:
        log.warning("Cannot verify position — skipping close this cycle.")
        return False
    if live_size == 0:
        log.info("Position already closed on exchange — marking state CLOSED, monitor done.")
        state.update({
            "status":        "CLOSED",
            "exit_time_utc": time.strftime("%H:%M:%S", time.gmtime()),
            "exit_trigger":  "closed_externally",
        })
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
        return True
    lots = abs(live_size)

    log.info("TP HIT — P&L $%.2f  mark $%.4f  %sing %d lots to close...",
             pnl, mark, close_side, lots)
    result = place_order(product_id, symbol, close_side, lots)
    if result.get("success"):
        o    = result.get("result", {})
        fill = float(o.get("average_fill_price") or mark)
        cv   = float(state.get("contract_value", 0.001))
        sign = -1 if is_short else 1
        real = round((fill - float(state["entry_mark"])) * cv * lots * sign, 2)
        state.update({
            "status":        "CLOSED",
            "exit_time_utc": time.strftime("%H:%M:%S", time.gmtime()),
            "exit_mark":     fill,
            "pnl_usd":       real,
            "exit_trigger":  f"take_profit_{SLOT}",
        })
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
        append_history(state)

        label = "🌅 MORNING" if SLOT == "morning" else "🌇 EVENING"
        send_telegram(
            f"✅ <b>TAKE PROFIT HIT — {label} ({USER.upper()})</b>\n"
            f"<code>{'━' * 24}</code>\n"
            f"Symbol  » <code>{symbol}</code>\n"
            f"Lots    » <code>{lots:,}</code>\n"
            f"Entry   » <code>${float(state['entry_mark']):.4f}</code>\n"
            f"Exit    » <code>${fill:.4f}</code>\n"
            f"P&L     » <code>+${real:.2f}</code> 🎯\n"
            f"OrderID » <code>{o.get('id')}</code>"
        )
        return True
    else:
        log.error("SELL FAILED: %s", result)
        send_telegram(f"⚠️ <b>TP SELL FAILED — {SLOT.upper()} ({USER.upper()})</b>\n<code>{result}</code>")
        return False


def main():
    log.info("=" * 56)
    log.info("TP Monitor [%s/%s] started  target=+$%.2f  poll=%ds  state=%s",
             USER, SLOT, TARGET_PNL, POLL_SECS, STATE_FILE)

    state = load_state()
    if state.get("status") != "OPEN":
        log.error("No open %s position in state — exiting.", SLOT)
        sys.exit(1)

    symbol     = state["symbol"]
    entry_mark = float(state["entry_mark"])
    lots       = int(state["lots"])
    cv         = float(state.get("contract_value", 0.001))
    sign       = -1 if state.get("side") == "short" else 1

    log.info("Symbol: %s  entry=%.4f  lots=%d  side=%s  target_pnl=$%.2f",
             symbol, entry_mark, lots,
             "SHORT" if sign < 0 else "LONG", TARGET_PNL)

    while True:
        try:
            state = load_state()
            if state.get("status") != "OPEN":
                log.info("Position no longer OPEN — monitor exiting.")
                break

            mark = get_mark(symbol)
            pnl  = (mark - entry_mark) * cv * lots * sign

            log.info("mark=%.4f  pnl=$%.2f  target=$%.2f", mark, pnl, TARGET_PNL)

            if pnl >= TARGET_PNL:
                if close_position(state, mark, pnl):
                    log.info("Monitor done.")
                    break
        except Exception as e:
            log.warning("Poll error: %s", e)

        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
