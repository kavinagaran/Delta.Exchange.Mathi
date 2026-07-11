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

# Per-account config overrides (users/<name>/config.json) — the TP target
# and poll interval below must be THIS account's settings, not the globals.
try:
    for _k, _v in json.loads((USER_DIR / "config.json").read_text(encoding="utf-8")).items():
        os.environ[str(_k)] = str(_v)
except Exception:
    pass
API_KEY    = _acct.get("api_key")    or os.getenv("API_KEY", "")
API_SECRET = _acct.get("api_secret") or os.getenv("API_SECRET", "")
BASE_URL   = os.getenv("BASE_URL", "https://api.india.delta.exchange")
TG_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT    = os.getenv("TELEGRAM_CHAT_ID", "")

def _f(key, default=0.0):
    try:
        return float(os.getenv(key) or default)
    except ValueError:
        return default

if SLOT == "morning":
    STATE_FILE = USER_DIR / "morning_state.json"
    TARGET_PNL = _f("TP_TARGET_PNL_MORNING", 300)
    SL_PNL     = abs(_f("SL_TARGET_PNL_MORNING", 0))    # 0 = stop-loss disabled
    TSL_PNL    = abs(_f("TSL_TARGET_PNL_MORNING", 0))   # 0 = trailing stop disabled
    POLL_SECS  = int(_f("TP_POLL_SECS_MORNING", 30))
else:
    STATE_FILE = USER_DIR / "straddle_state.json"
    TARGET_PNL = _f("TP_TARGET_PNL", 105)
    SL_PNL     = abs(_f("SL_TARGET_PNL", 0))             # 0 = stop-loss disabled
    TSL_PNL    = abs(_f("TSL_TARGET_PNL", 0))            # 0 = trailing stop disabled
    POLL_SECS  = int(_f("TP_POLL_SECS", 30))
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


# ─────────────────────────────────────────────────────────────
# Exchange-resident stop orders (the armed TSL lives ON Delta, so the
# position stays protected even if this monitor or the server dies)
# ─────────────────────────────────────────────────────────────
def place_stop_order(product_id, side, size, stop_price):
    """Reduce-only stop-market: triggers on MARK price (same basis as our
    P&L math). reduce_only guarantees it can only ever close exposure."""
    payload = {
        "product_id":          product_id,
        "size":                size,
        "side":                side,
        "order_type":          "market_order",
        "stop_order_type":     "stop_loss_order",
        "stop_price":          f"{stop_price:.1f}",
        "stop_trigger_method": "mark_price",
        "reduce_only":         True,
    }
    body = json.dumps(payload, separators=(",", ":"))
    hdrs = _sign("POST", "/v2/orders", "", body)
    r    = requests.post(f"{BASE_URL}/v2/orders", data=body, headers=hdrs, timeout=15)
    return r.json()


def edit_stop_price(order_id, product_id, stop_price):
    payload = {"id": order_id, "product_id": product_id, "stop_price": f"{stop_price:.1f}"}
    body = json.dumps(payload, separators=(",", ":"))
    hdrs = _sign("PUT", "/v2/orders", "", body)
    r    = requests.put(f"{BASE_URL}/v2/orders", data=body, headers=hdrs, timeout=15)
    return r.json()


def cancel_order(order_id, product_id):
    try:
        payload = {"id": order_id, "product_id": product_id}
        body = json.dumps(payload, separators=(",", ":"))
        hdrs = _sign("DELETE", "/v2/orders", "", body)
        r = requests.delete(f"{BASE_URL}/v2/orders", data=body, headers=hdrs, timeout=15)
        return r.json()
    except Exception as e:
        log.warning("Cancel order %s failed: %s", order_id, e)
        return {}


def get_order(order_id):
    try:
        path = f"/v2/orders/{order_id}"
        hdrs = _sign("GET", path)
        r = requests.get(f"{BASE_URL}{path}", headers=hdrs, timeout=10)
        return r.json().get("result", {}) or {}
    except Exception as e:
        log.warning("Get order %s failed: %s", order_id, e)
        return {}


def save_state_fields(**kw):
    """Persist monitor bookkeeping (peak, armed, resting stop id/floor) into
    the slot state file so restarts resume instead of forgetting the trail."""
    try:
        st = load_state()
        st.update(kw)
        STATE_FILE.write_text(json.dumps(st, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("State persist failed: %s", e)


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


def close_position(state, mark, pnl, reason="take_profit"):
    """reason: 'take_profit' or 'stop_loss' — sets the trigger + alert."""
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

    tag = {"take_profit": "TP", "stop_loss": "SL", "trailing_stop": "TSL"}.get(reason, "TP")
    log.info("%s HIT — P&L $%.2f  mark $%.4f  %sing %d lots to close...",
             tag, pnl, mark, close_side, lots)
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
            "exit_trigger":  f"{reason}_{SLOT}",
        })
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
        append_history(state)

        label = "🌅 MORNING" if SLOT == "morning" else "🌇 EVENING"
        head  = {
            "take_profit":   f"✅ <b>TAKE PROFIT HIT — {label} ({USER.upper()})</b>",
            "stop_loss":     f"🛑 <b>STOP LOSS HIT — {label} ({USER.upper()})</b>",
            "trailing_stop": f"🔻 <b>TRAILING STOP HIT — {label} ({USER.upper()})</b>",
        }.get(reason, f"✅ <b>{tag} — {label} ({USER.upper()})</b>")
        psign = "+" if real >= 0 else "-"
        send_telegram(
            f"{head}\n"
            f"<code>{'━' * 24}</code>\n"
            f"Symbol  » <code>{symbol}</code>\n"
            f"Lots    » <code>{lots:,}</code>\n"
            f"Entry   » <code>${float(state['entry_mark']):.4f}</code>\n"
            f"Exit    » <code>${fill:.4f}</code>\n"
            f"P&L     » <code>{psign}${abs(real):.2f}</code> {'🎯' if reason == 'take_profit' else '🛑'}\n"
            f"OrderID » <code>{o.get('id')}</code>"
        )
        return True
    else:
        log.error("%s CLOSE FAILED: %s", tag, result)
        send_telegram(f"⚠️ <b>{tag} CLOSE FAILED — {SLOT.upper()} ({USER.upper()})</b>\n<code>{result}</code>")
        return False


def main():
    log.info("=" * 56)
    log.info("TP/SL Monitor [%s/%s] started  tp=+$%.2f  sl=%s  tsl=%s  poll=%ds  state=%s",
             USER, SLOT, TARGET_PNL,
             f"-${SL_PNL:.2f}" if SL_PNL > 0 else "off",
             f"peak-${TSL_PNL:.2f}" if TSL_PNL > 0 else "off",
             POLL_SECS, STATE_FILE)

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

    # Trailing stop: trail from the best P&L seen — but it ARMS only once
    # that peak has reached the TSL amount. Until then it stays dormant and
    # losses are exclusively the fixed SL's job. Once armed, the floor
    # (peak - TSL) starts at breakeven and only rises — AND it is placed as
    # a reduce-only stop-market order ON THE EXCHANGE, ratcheted upward as
    # the peak climbs, so the position stays protected even if this monitor
    # or the whole server dies. Peak/armed/stop-order id are persisted into
    # the slot state file so restarts resume instead of forgetting the trail.
    product_id  = state["product_id"]
    is_short    = sign < 0
    close_side  = "buy" if is_short else "sell"
    peak_pnl    = float(state.get("tsl_peak") or 0.0)
    tsl_armed   = bool(state.get("tsl_armed"))
    stop_id     = state.get("tsl_stop_order_id")
    stop_floor  = float(state.get("tsl_floor") or 0.0)
    persist_pk  = peak_pnl
    ratchet_min = max(5.0, TSL_PNL * 0.05)   # move the stop on >= this floor gain

    def floor_price(floor_usd):
        """P&L floor ($) -> option mark trigger price, on the 0.1 tick."""
        return max(round(entry_mark + sign * floor_usd / (cv * lots), 1), 0.1)

    def ensure_stop(floor_usd):
        """Place / ratchet the exchange stop to the new floor. Never moves it
        down. Ratchet order: edit in place; else place NEW first, cancel old
        after — a failed step always leaves one protective stop resting."""
        nonlocal stop_id, stop_floor
        price = floor_price(floor_usd)
        if stop_id:
            resp = edit_stop_price(stop_id, product_id, price)
            if resp.get("success"):
                stop_floor = floor_usd
                save_state_fields(tsl_peak=round(peak_pnl, 2), tsl_armed=True,
                                  tsl_floor=round(floor_usd, 2), tsl_stop_order_id=stop_id)
                log.info("TSL stop RATCHETED to $%.1f (floor $%.2f, order %s).",
                         price, floor_usd, stop_id)
                return
            log.warning("Stop edit failed (%s) — replacing.", resp.get("error"))
        resp = place_stop_order(product_id, close_side, int(state.get("lots", lots)), price)
        new_id = (resp.get("result") or {}).get("id") if resp.get("success") else None
        if new_id:
            old = stop_id
            stop_id, stop_floor = new_id, floor_usd
            if old:
                cancel_order(old, product_id)
            save_state_fields(tsl_peak=round(peak_pnl, 2), tsl_armed=True,
                              tsl_floor=round(floor_usd, 2), tsl_stop_order_id=stop_id)
            log.info("TSL stop PLACED on exchange: %s %d lots @ trigger $%.1f (floor $%.2f, order %s).",
                     close_side.upper(), int(state.get("lots", lots)), price, floor_usd, stop_id)
        else:
            log.warning("Stop placement failed: %s — monitor-side trigger remains the fallback.",
                        resp.get("error", resp))

    def finalize_stop_fill(order):
        """The exchange stop executed — record the real fill as the exit."""
        fill = float(order.get("average_fill_price") or 0)
        done = abs(int(float(order.get("size") or state.get("lots", lots))))
        real = round((fill - entry_mark) * cv * done * sign, 2)
        st = load_state()
        st.update({
            "status":        "CLOSED",
            "exit_time_utc": time.strftime("%H:%M:%S", time.gmtime()),
            "exit_mark":     fill,
            "pnl_usd":       real,
            "exit_trigger":  f"trailing_stop_{SLOT}",
            "exit_order_id": order.get("id"),
        })
        STATE_FILE.write_text(json.dumps(st, indent=2), encoding="utf-8")
        append_history(st)
        label = "🌅 MORNING" if SLOT == "morning" else "🌇 EVENING"
        psign = "+" if real >= 0 else "-"
        send_telegram(
            f"🔻 <b>TRAILING STOP EXECUTED (exchange) — {label} ({USER.upper()})</b>\n"
            f"<code>{'━' * 24}</code>\n"
            f"Symbol  » <code>{symbol}</code>\n"
            f"Lots    » <code>{done:,}</code>\n"
            f"Entry   » <code>${entry_mark:.4f}</code>\n"
            f"Exit    » <code>${fill:.4f}</code>\n"
            f"P&L     » <code>{psign}${abs(real):.2f}</code>\n"
            f"OrderID » <code>{order.get('id')}</code>"
        )

    def cleanup_stop():
        if stop_id:
            cancel_order(stop_id, product_id)
            log.info("Resting TSL stop %s cancelled.", stop_id)

    # If the dashboard stops this monitor (SIGTERM), take the resting stop
    # with us — a stopped monitor must not leave invisible orders behind.
    def _on_term(*_):
        cleanup_stop()
        sys.exit(0)
    try:
        import signal
        signal.signal(signal.SIGTERM, _on_term)
    except Exception:
        pass

    if stop_id:
        log.info("Resumed with resting TSL stop %s (floor $%.2f, peak $%.2f).",
                 stop_id, stop_floor, peak_pnl)

    while True:
        try:
            state = load_state()
            if state.get("status") != "OPEN":
                log.info("Position no longer OPEN — monitor exiting.")
                cleanup_stop()
                break

            # A resting exchange stop may have fired between polls
            if stop_id:
                live = get_exchange_size(product_id)
                if live == 0:
                    order = get_order(stop_id)
                    if str(order.get("state")) == "closed":
                        finalize_stop_fill(order)
                        log.info("Monitor done (trailing stop executed on exchange).")
                    else:
                        log.info("Position closed externally — cancelling resting stop.")
                        cleanup_stop()
                        st = load_state()
                        st.update({"status": "CLOSED",
                                   "exit_time_utc": time.strftime("%H:%M:%S", time.gmtime()),
                                   "exit_trigger": "closed_externally"})
                        STATE_FILE.write_text(json.dumps(st, indent=2), encoding="utf-8")
                    break

            mark = get_mark(symbol)
            pnl  = (mark - entry_mark) * cv * lots * sign
            peak_pnl = max(peak_pnl, pnl)
            if peak_pnl - persist_pk >= 5.0:   # keep restarts from forgetting the peak
                persist_pk = peak_pnl
                save_state_fields(tsl_peak=round(peak_pnl, 2), tsl_armed=tsl_armed)
            if TSL_PNL > 0 and not tsl_armed and peak_pnl >= TSL_PNL:
                tsl_armed = True
                log.info("TSL ARMED — peak $%.2f reached the $%.2f trail; floor now $%.2f.",
                         peak_pnl, TSL_PNL, peak_pnl - TSL_PNL)
            if tsl_armed and TSL_PNL > 0:
                floor = peak_pnl - TSL_PNL
                if stop_id is None or floor - stop_floor >= ratchet_min:
                    ensure_stop(floor)

            log.info("mark=%.4f  pnl=$%.2f  peak=$%.2f  tp=$%.2f  sl=%s  tsl=%s",
                     mark, pnl, peak_pnl, TARGET_PNL,
                     f"-${SL_PNL:.2f}" if SL_PNL > 0 else "off",
                     "off" if TSL_PNL <= 0 else
                     (f"${stop_floor:.2f} (on exchange)" if stop_id else
                      (f"${peak_pnl - TSL_PNL:.2f}" if tsl_armed else
                       f"unarmed (arms at +${TSL_PNL:.2f})")))

            if pnl >= TARGET_PNL:
                cleanup_stop()   # never leave the stop racing our TP close
                stop_id = None
                if close_position(state, mark, pnl, "take_profit"):
                    log.info("Monitor done (take profit).")
                    break
            elif tsl_armed and stop_id is None and pnl <= peak_pnl - TSL_PNL:
                # Fallback only — normally the exchange-resident stop handles this
                if close_position(state, mark, pnl, "trailing_stop"):
                    log.info("Monitor done (trailing stop: peak $%.2f, gave back $%.2f).",
                             peak_pnl, peak_pnl - pnl)
                    break
            elif SL_PNL > 0 and pnl <= -SL_PNL:
                cleanup_stop()
                stop_id = None
                if close_position(state, mark, pnl, "stop_loss"):
                    log.info("Monitor done (stop loss).")
                    break
        except Exception as e:
            log.warning("Poll error: %s", e)

        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
