"""
tp_monitor.py — Take-profit monitor for MV-BTC position.
Closes position when live P&L >= TARGET_PNL.
"""
import os, sys, time, hmac, hashlib, json, logging, requests
from pathlib import Path
from dotenv import load_dotenv

# Force IPv4 — Delta's whitelist holds our IPv4; IPv6 rotates and gets rejected
import socket
import urllib3.util.connection as _u3c
_u3c.allowed_gai_family = lambda: socket.AF_INET

BASE_DIR   = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

API_KEY    = os.getenv("API_KEY", "")
API_SECRET = os.getenv("API_SECRET", "")
BASE_URL   = os.getenv("BASE_URL", "https://api.india.delta.exchange")
TG_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT    = os.getenv("TELEGRAM_CHAT_ID", "")

STATE_FILE = BASE_DIR / "straddle_state.json"
TARGET_PNL = float(os.getenv("TP_TARGET_PNL", "105"))
POLL_SECS  = int(os.getenv("TP_POLL_SECS", "30"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(BASE_DIR / "logs" / "tp_monitor.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("tp_monitor")


def _sign(method, path, query="", body=""):
    ts  = str(int(time.time()))
    msg = method + ts + path + query + body
    sig = hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return {"api-key": API_KEY, "timestamp": ts, "signature": sig,
            "Content-Type": "application/json", "User-Agent": "tp-monitor/1.0"}


def get_mark(symbol):
    r = requests.get(f"{BASE_URL}/v2/tickers/{symbol}", timeout=8)
    return float(r.json().get("result", {}).get("mark_price") or 0)

def get_exchange_pnl(product_id):
    """Returns (unrealized_pnl, mark_price) direct from exchange."""
    hdrs = _sign("GET", "/v2/positions/margined")
    r = requests.get(f"{BASE_URL}/v2/positions/margined", headers=hdrs, timeout=8)
    for pos in r.json().get("result", []):
        if pos.get("product_id") == product_id and float(pos.get("size", 0)) != 0:
            return float(pos.get("unrealized_pnl", 0)), float(pos.get("mark_price", 0))
    return None, None


def place_sell(product_id, symbol, size):
    payload = {"product_id": product_id, "size": size, "side": "sell", "order_type": "market_order"}
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


def close_position(state, mark, pnl):
    product_id = state["product_id"]
    symbol     = state["symbol"]
    lots       = state["lots"]

    # Never sell blind — verify the position still exists on the exchange.
    # Selling without one would open a naked short.
    live_size = get_exchange_size(product_id)
    if live_size is None:
        log.warning("Cannot verify position — skipping sell this cycle.")
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
    lots = live_size   # sell exactly what the exchange holds

    log.info("TP HIT — P&L $%.2f  mark $%.4f  selling %d lots...", pnl, mark, lots)

    result = place_sell(product_id, symbol, lots)
    if result.get("success"):
        o = result.get("result", {})
        log.info("SELL ORDER PLACED  order_id=%s  state=%s  avg=%s",
                 o.get("id"), o.get("state"), o.get("average_fill_price", "pending"))
        # Update state
        state.update({
            "status":        "CLOSED",
            "exit_time_utc": time.strftime("%H:%M:%S", time.gmtime()),
            "exit_mark":     mark,
            "pnl_usd":       round(pnl, 4),
            "exit_trigger":  "take_profit",
        })
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")

        send_telegram(
            f"✅ <b>TAKE PROFIT HIT — MATHI</b>\n"
            f"<code>{'━' * 24}</code>\n"
            f"Symbol  » <code>{symbol}</code>\n"
            f"Lots    » <code>{lots:,}</code>\n"
            f"Entry   » <code>${state['entry_mark']:.4f}</code>\n"
            f"Exit    » <code>${mark:.4f}</code>\n"
            f"P&L     » <code>+${pnl:.2f}</code> 🎯\n"
            f"OrderID » <code>{o.get('id')}</code>"
        )
        return True
    else:
        log.error("SELL FAILED: %s", result)
        send_telegram(f"⚠️ <b>TP SELL FAILED — MATHI</b>\n<code>{result}</code>")
        return False


def main():
    log.info("=" * 56)
    log.info("Take-Profit Monitor started  target=+$%.2f  poll=%ds", TARGET_PNL, POLL_SECS)

    state = load_state()
    if state.get("status") != "OPEN":
        log.error("No open position in state — exiting.")
        sys.exit(1)

    symbol     = state["symbol"]
    entry_mark = float(state["entry_mark"])
    lots       = int(state["lots"])
    cv         = float(state.get("contract_value", 0.001))

    log.info("Symbol: %s  entry=%.4f  lots=%d  target_pnl=$%.2f",
             symbol, entry_mark, lots, TARGET_PNL)

    while True:
        try:
            state = load_state()
            if state.get("status") != "OPEN":
                log.info("Position no longer OPEN — monitor exiting.")
                break

            mark = get_mark(symbol)
            pnl  = (mark - entry_mark) * cv * lots

            log.info("mark=%.4f  pnl=$%.2f  target=$%.2f", mark, pnl, TARGET_PNL)

            if pnl >= TARGET_PNL:
                closed = close_position(state, mark, pnl)
                if closed:
                    log.info("Position closed. Monitor done.")
                    break
        except Exception as e:
            log.warning("Poll error: %s", e)

        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
