"""
tp_monitor.py — TP/SL/TSL monitor for MOVE and trend-option positions.
Usage:  python tp_monitor.py [--slot morning|evening|trend] [--user <username>]

Watches one user's slot state file (users/<username>/) and market price;
closes the position at the configured profit target using THAT user's own
Delta API keys (users/<username>/account.json, .env keys as fallback).
Slot config (.env):
  evening: TP_TARGET_PNL, TP_POLL_SECS
  morning: TP_TARGET_PNL_MORNING, TP_POLL_SECS_MORNING
  trend:   TP_TARGET_PNL_TREND, TP_POLL_SECS_TREND
"""
import os, sys, time, hmac, hashlib, json, logging, math, signal, requests
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from dotenv import load_dotenv
from risk_controls import account_file_lock

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


def _has_flag(flag: str) -> bool:
    return flag in sys.argv


SLOT = _arg("--slot", "evening")
if SLOT not in ("morning", "evening", "trend"):
    print(f"Invalid slot: {SLOT}")
    sys.exit(1)

USER     = _arg("--user", os.getenv("BOT_USER", os.getenv("DASH_USER", "mathi")))
USER_DIR = BASE_DIR / "users" / USER
REMOVE_PROTECTION = _has_flag("--remove-protection")
CONFIGURATION_ERRORS = []

# The monitored account's own credentials; .env keys as fallback
_account_path = USER_DIR / "account.json"
if _account_path.exists():
    try:
        _acct = json.loads(_account_path.read_text(encoding="utf-8"))
        if not isinstance(_acct, dict) or not _acct.get("api_key") or not _acct.get("api_secret"):
            raise ValueError("account API credentials are missing")
    except (OSError, ValueError, TypeError) as exc:
        _acct = {}
        CONFIGURATION_ERRORS.append(f"invalid account.json: {exc}")
else:
    _acct = {}

# Per-account config overrides (users/<name>/config.json) — the TP target
# and poll interval below must be THIS account's settings, not the globals.
_config_path = USER_DIR / "config.json"
if _config_path.exists():
    try:
        _config_doc = json.loads(_config_path.read_text(encoding="utf-8"))
        if not isinstance(_config_doc, dict):
            raise ValueError("config document is not an object")
        for _k, _v in _config_doc.items():
            os.environ[str(_k)] = str(_v)
    except (OSError, ValueError, TypeError) as exc:
        CONFIGURATION_ERRORS.append(f"invalid config.json: {exc}")
if _account_path.exists() and CONFIGURATION_ERRORS and not _acct:
    API_KEY = API_SECRET = ""
else:
    API_KEY = _acct.get("api_key") or os.getenv("API_KEY", "")
    API_SECRET = _acct.get("api_secret") or os.getenv("API_SECRET", "")
BASE_URL   = os.getenv("BASE_URL", "https://api.india.delta.exchange")
TG_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT    = os.getenv("TELEGRAM_CHAT_ID", "")

def _f(key, default=0.0):
    try:
        return float(os.getenv(key) or default)
    except ValueError:
        return default

def _slot_settings(slot):
    """Return protection settings with backwards-compatible TSL defaults.

    Historically ``TSL_TARGET_PNL[_SLOT]`` was used both as the profit needed
    to arm the trail and as the permitted give-back.  The two controls can now
    be configured independently; installations that only have the legacy key
    retain exactly their old behaviour.
    """
    if slot == "morning":
        suffix, tp_default, sl_default, tsl_default = "_MORNING", 300, 0, 0
        state_name = "morning_state.json"
    elif slot == "trend":
        suffix, tp_default, sl_default, tsl_default = "_TREND", 100, 50, 50
        state_name = "trend_state.json"
    else:
        suffix, tp_default, sl_default, tsl_default = "", 105, 0, 0
        state_name = "straddle_state.json"

    legacy_tsl = abs(_f(f"TSL_TARGET_PNL{suffix}", tsl_default))
    return {
        "state_name": state_name,
        "target_pnl": max(_f(f"TP_TARGET_PNL{suffix}", tp_default), 1),
        "sl_pnl": abs(_f(f"SL_TARGET_PNL{suffix}", sl_default)),
        "tsl_legacy_pnl": legacy_tsl,
        "tsl_arm_pnl": abs(_f(f"TSL_ARM_PNL{suffix}", legacy_tsl)),
        "tsl_trail_pnl": abs(_f(f"TSL_TRAIL_PNL{suffix}", legacy_tsl)),
        "poll_secs": max(int(_f(f"TP_POLL_SECS{suffix}", 30)), 10),
    }


_SETTINGS = _slot_settings(SLOT)
STATE_FILE = USER_DIR / _SETTINGS["state_name"]
TARGET_PNL = _SETTINGS["target_pnl"]
SL_PNL = _SETTINGS["sl_pnl"]
# TSL_PNL remains as a compatibility alias for code/tools importing this file.
TSL_PNL = _SETTINGS["tsl_legacy_pnl"]
TSL_ARM_PNL = _SETTINGS["tsl_arm_pnl"]
TSL_TRAIL_PNL = _SETTINGS["tsl_trail_pnl"]
POLL_SECS = _SETTINGS["poll_secs"]
LOG_NAME = f"tp_{USER}_{SLOT}.log"

HISTORY_FILE = USER_DIR / "trade_history.json"
HEALTH_FILE = USER_DIR / f"tp_{SLOT}_health.json"
RECONCILE_SECS = max(int(_f("TP_ORDER_RECONCILE_SECS", 60)), 30)
# When exchange-resident protection is unavailable, keep the local fallback
# responsive while remaining well below normal REST API request-rate limits.
LOCAL_FALLBACK_POLL_SECS = max(
    10, min(POLL_SECS, int(_f("TP_LOCAL_FALLBACK_POLL_SECS", 10)))
)
OPTION_FEE_RATE = max(_f("OPTION_FEE_RATE", 0.00010), 0)
OPTION_FEE_CAP_PCT = max(_f("OPTION_FEE_CAP_PCT", 0.035), 0)

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


def _slot_label():
    return {"morning": "🌅 MORNING", "evening": "🌇 EVENING",
            "trend": "📈 TREND OPTION"}[SLOT]


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
            if str(pos.get("product_id")) == str(product_id):
                return int(float(pos.get("size", 0)))
        return 0
    except Exception as e:
        log.warning("Position check error: %s", e)
        return None


_CLIENT_ORDER_SEQUENCE = 0


def _protection_client_order_id(kind):
    """Short, unique and recognisable id (Delta limits client ids to 32 chars)."""
    global _CLIENT_ORDER_SEQUENCE
    _CLIENT_ORDER_SEQUENCE = (_CLIENT_ORDER_SEQUENCE + 1) % 100
    clean_user = "".join(c for c in USER.lower() if c.isalnum())[:4] or "bot"
    millis = int(time.time() * 1000) % 100_000_000
    return (f"nithi-tp-{clean_user}-{SLOT[:1]}-{kind[:2]}-"
            f"{millis:08d}-{_CLIENT_ORDER_SEQUENCE:02d}")[:32]


def place_order(product_id, symbol, side, size, *, reduce_only=True,
                client_order_id=None):
    client_order_id = client_order_id or _protection_client_order_id("close")
    payload = {"product_id": product_id, "size": size, "side": side,
               "order_type": "market_order", "reduce_only": bool(reduce_only),
               "client_order_id": client_order_id}
    body    = json.dumps(payload, separators=(",", ":"))
    hdrs    = _sign("POST", "/v2/orders", "", body)
    r       = requests.post(f"{BASE_URL}/v2/orders", data=body, headers=hdrs, timeout=15)
    result = r.json()
    if result.get("success") and isinstance(result.get("result"), dict):
        result["result"].setdefault("client_order_id", client_order_id)
    return result


# ─────────────────────────────────────────────────────────────
# Exchange-resident stop orders (the fixed SL and the armed TSL both live
# ON Delta as a single resting reduce-only stop, so the position stays
# protected even if this monitor or the server dies)
# ─────────────────────────────────────────────────────────────
def place_stop_order(product_id, side, size, stop_price, order_kind="stop_loss_order"):
    """Reduce-only stop-market: triggers on MARK price (same basis as our
    P&L math). reduce_only guarantees it can only ever close exposure.
    order_kind: 'stop_loss_order' (SL / TSL) or 'take_profit_order' (TP)."""
    client_order_id = _protection_client_order_id(
        "tp" if order_kind == "take_profit_order" else "sl"
    )
    payload = {
        "product_id":          product_id,
        "size":                size,
        "side":                side,
        "order_type":          "market_order",
        "stop_order_type":     order_kind,
        "stop_price":          f"{stop_price:.1f}",
        "stop_trigger_method": "mark_price",
        "reduce_only":         True,
        "client_order_id":     client_order_id,
    }
    body = json.dumps(payload, separators=(",", ":"))
    hdrs = _sign("POST", "/v2/orders", "", body)
    r    = requests.post(f"{BASE_URL}/v2/orders", data=body, headers=hdrs, timeout=15)
    result = r.json()
    if result.get("success") and isinstance(result.get("result"), dict):
        result["result"].setdefault("client_order_id", client_order_id)
    return result


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


def get_order_by_client_id(client_order_id, product_id=None):
    """Return (exact order, conclusive lookup).

    The current-order endpoint is queried first, then closed-order history.
    ``conclusive=False`` means transport/authentication failed and callers must
    not create a different close identity while the outcome is ambiguous.
    """
    if not client_order_id:
        return {}, False
    def exact(result):
        if isinstance(result, list):
            rows = result
        elif isinstance(result, dict):
            nested = result.get("orders") or result.get("data")
            rows = nested if isinstance(nested, list) else [result]
        else:
            rows = []
        for row in rows:
            if str(row.get("client_order_id") or "") != str(client_order_id):
                continue
            if product_id not in (None, "") and str(row.get("product_id")) != str(product_id):
                continue
            return row
        return {}

    open_lookup_ok = False
    history_lookup_ok = False
    try:
        path = "/v2/orders"
        params = {"states": "open", "page_size": 100}
        query = "?" + urlencode(params)
        response = requests.get(f"{BASE_URL}{path}", params=params,
                                headers=_sign("GET", path, query), timeout=10).json()
        if response.get("success"):
            open_lookup_ok = True
            found = exact(response.get("result"))
            if found:
                return found, True
    except Exception as exc:
        log.warning("Client-order lookup %s failed: %s", client_order_id, exc)

    try:
        path = "/v2/orders/history"
        hist_params = {"page_size": 100}
        query = "?" + urlencode(hist_params)
        response = requests.get(f"{BASE_URL}{path}", params=hist_params,
                                headers=_sign("GET", path, query), timeout=10).json()
        if response.get("success"):
            history_lookup_ok = True
            found = exact(response.get("result"))
            if found:
                return found, True
    except Exception as exc:
        log.warning("Client-order history lookup %s failed: %s", client_order_id, exc)
    # Absence is authoritative only when neither the active nor terminal order
    # collection could have hidden the identity behind a failed read.
    return {}, open_lookup_ok and history_lookup_ok


def _lookup_pending_close(state):
    order_id = state.get("pending_close_order_id")
    client_id = state.get("pending_close_client_order_id")
    product_id = state.get("product_id")
    if order_id:
        order = get_order(order_id)
        if order and (not client_id or str(order.get("client_order_id") or client_id) == str(client_id)):
            return order, True
    if client_id:
        return get_order_by_client_id(client_id, product_id)
    return {}, False


def _close_order_identity_error(order, state, close_side):
    """Return why an order cannot be proven to be this exact close intent."""
    if not order:
        return ""
    expected_order_id = state.get("pending_close_order_id")
    expected_client_id = state.get("pending_close_client_order_id")
    returned_order_id = order.get("id")
    returned_client_id = order.get("client_order_id")
    returned_product_id = order.get("product_id")
    returned_side = str(order.get("side") or "").lower()
    if not (returned_order_id or expected_order_id):
        return "recovered close order has no exchange order id"
    if (expected_order_id and returned_order_id
            and str(returned_order_id) != str(expected_order_id)):
        return f"close order id mismatch: {returned_order_id} != {expected_order_id}"
    if (expected_client_id and returned_client_id
            and str(returned_client_id) != str(expected_client_id)):
        return f"close client id mismatch: {returned_client_id} != {expected_client_id}"
    if (returned_product_id not in (None, "")
            and str(returned_product_id) != str(state.get("product_id"))):
        return f"close product mismatch: {returned_product_id} != {state.get('product_id')}"
    if returned_side and returned_side != close_side:
        return f"close side mismatch: {returned_side} != {close_side}"
    if order.get("reduce_only") is False:
        return "recovered close order is not reduce-only"
    return ""


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write_json(path, value):
    """Replace a JSON file atomically so dashboard readers never see a
    partially-written state/heartbeat document."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path != HEALTH_FILE:
        raw = path.read_text(encoding="utf-8")
        try:
            json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise RuntimeError(f"refusing to overwrite corrupt JSON: {path}") from exc
        backup = path.with_suffix(path.suffix + ".bak")
        backup_tmp = backup.with_name(f".{backup.name}.{os.getpid()}.tmp")
        backup_tmp.write_text(raw, encoding="utf-8")
        os.replace(backup_tmp, backup)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    tmp.write_text(json.dumps(value, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def save_state_fields(**kw):
    """Persist monitor bookkeeping (peak, armed, resting stop id/floor) into
    the slot state file so restarts resume instead of forgetting the trail."""
    try:
        st = load_state()
        st.update(kw)
        _atomic_write_json(STATE_FILE, st)
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
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("state is not an object")
        return state
    except (OSError, ValueError, TypeError) as exc:
        backup = STATE_FILE.with_suffix(STATE_FILE.suffix + ".bak")
        try:
            state = json.loads(backup.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                raise ValueError("backup state is not an object")
            log.critical("Primary state corrupt (%s); using validated backup %s", exc, backup)
            return state
        except (OSError, ValueError, TypeError):
            log.critical("State and backup are unreadable: %s", STATE_FILE)
            return {}


def write_monitor_health(status, *, last_error="", persist_state=True, **fields):
    """Publish a machine-readable liveness/protection heartbeat.

    The separate file remains readable even when the position state is being
    reconciled.  A compact status is also mirrored into the state file for old
    dashboard/API clients that only inspect that document.
    """
    try:
        try:
            health = json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
        except Exception:
            health = {}
        heartbeat = _utc_now()
        try:
            identity_state = load_state()
        except Exception:
            identity_state = {}
        health.update({
            "user": USER,
            "slot": SLOT,
            "pid": os.getpid(),
            "status": status,
            "heartbeat_utc": heartbeat,
            "last_error": str(last_error or ""),
            "product_id": identity_state.get("product_id"),
            "entry_order_id": identity_state.get("order_id")
                              or identity_state.get("entry_order_id"),
            "entry_client_order_id": identity_state.get("client_order_id"),
        })
        health.update(fields)
        _atomic_write_json(HEALTH_FILE, health)
        if persist_state:
            save_state_fields(
                protection_monitor_status=status,
                protection_heartbeat_utc=heartbeat,
                protection_last_error=str(last_error or ""),
                protection_established=bool(fields.get(
                    "protection_established", health.get("protection_established", False)
                )),
            )
        return health
    except Exception as e:
        log.warning("Health heartbeat failed: %s", e)
        return {}


def _handle_termination(signum, _frame):
    """Stop the process without withdrawing exchange-resident protection.

    A SIGTERM is used both for dashboard Stop and for normal service restarts.
    Neither is authority to remove risk controls from an OPEN position.
    """
    state = load_state()
    retained = state.get("status") == "OPEN" and bool(
        state.get("tsl_stop_order_id") or state.get("tp_stop_order_id")
    )
    write_monitor_health(
        "stopped",
        last_error=f"terminated by signal {signum}; exchange protection retained",
        state_status=state.get("status"),
        protection_retained=retained,
        protection_established=retained,
    )
    log.info("Termination requested — exchange protection retained for OPEN position.")
    raise SystemExit(0)


def install_signal_handlers():
    signal.signal(signal.SIGTERM, _handle_termination)


def _extract_order_fee(order):
    """Return an actual commission/fee reported by Delta, when present.

    API payloads have used several field names over time.  We deliberately
    choose the first explicit total instead of summing aliases and accidentally
    double-counting the same commission.
    """
    if not isinstance(order, dict):
        return None
    for key in (
        "total_commission", "paid_commission_usd", "paid_commission",
        "commission_usd", "commission", "commission_amount",
        "total_fee", "total_fees_usd", "fees", "fee",
    ):
        value = order.get(key)
        if isinstance(value, dict):
            value = value.get("amount") or value.get("value")
        try:
            if value not in (None, ""):
                fee = abs(float(value))
                if math.isfinite(fee):
                    return fee
        except (TypeError, ValueError):
            continue
    for key in ("meta_data", "meta", "fill", "fills"):
        nested = order.get(key)
        if isinstance(nested, dict):
            found = _extract_order_fee(nested)
            if found is not None:
                return found
        elif isinstance(nested, list):
            values = [_extract_order_fee(item) for item in nested]
            values = [value for value in values if value is not None]
            if values:
                return sum(values)
    return None


def _entry_fee(state):
    for key in ("entry_fee_usd", "entry_fees_usd", "entry_commission_usd",
                "entry_commission", "entry_fee"):
        try:
            value = state.get(key)
            if value not in (None, ""):
                return abs(float(value))
        except (TypeError, ValueError):
            pass
    entry_order_id = state.get("order_id") or state.get("entry_order_id")
    if entry_order_id:
        return _extract_order_fee(get_order(entry_order_id))
    return None


def _apply_exit_accounting(state, gross_pnl, exit_order):
    """Attach complete actual-or-conservative fee accounting and net P&L."""
    entry_fee = _entry_fee(state)
    exit_fee = _extract_order_fee(exit_order)

    cv = max(float(state.get("contract_value") or 0.001), 0)
    lots = abs(int(float(state.get("lots") or (exit_order or {}).get("size") or 0)))
    spot = max(float(state.get("btc_at_exit") or state.get("btc_at_entry") or 0), 0)

    def estimate(price):
        premium = max(float(price or 0), 0)
        basis = (min(OPTION_FEE_RATE * spot, OPTION_FEE_CAP_PCT * premium)
                 if spot > 0 else OPTION_FEE_CAP_PCT * premium)
        return basis * cv * lots

    entry_source = str(state.get("entry_fee_source") or "")
    if entry_fee is None or (entry_fee == 0 and not entry_source):
        entry_fee = estimate(state.get("entry_mark"))
        entry_source = "configured_estimate"
    else:
        entry_source = entry_source or "exchange"
    exit_source = "exchange"
    if exit_fee is None:
        exit_fee = estimate((exit_order or {}).get("average_fill_price")
                            or state.get("exit_mark"))
        exit_source = "configured_estimate"

    previous_gross = float(state.get("partial_exit_gross_pnl_usd") or 0)
    previous_exit_fees = float(state.get("partial_exit_fees_usd") or 0)
    gross_total = previous_gross + float(gross_pnl)
    exit_total = previous_exit_fees + float(exit_fee)
    total = round(float(entry_fee) + exit_total, 8)
    state["gross_pnl_usd"] = round(gross_total, 2)
    state["entry_fee_usd"] = round(float(entry_fee), 8)
    state["exit_fee_usd"] = round(exit_total, 8)
    state["entry_fee_source"] = entry_source
    state["exit_fee_source"] = exit_source
    state["fees_available"] = entry_source == "exchange" and exit_source == "exchange"
    state["fees_complete"] = True
    state["fees_estimated"] = not state["fees_available"]
    state["fees_usd"] = total
    state["pnl_usd"] = round(gross_total - total, 2)
    state["pnl_includes_fees"] = True
    return state["pnl_usd"]


_ACTIVE_ORDER_STATES = {
    "open", "pending", "partially_filled", "partially-filled", "untriggered", "triggered",
}
_TERMINAL_ORDER_STATES = {
    "closed", "filled", "cancelled", "canceled", "rejected", "expired", "failed",
}


def _order_state(order):
    return str((order or {}).get("state") or (order or {}).get("status") or "").lower()


def _parse_utc_datetime(value):
    """Parse Delta/state ISO timestamps into comparable UTC datetimes."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _state_entry_datetime(state):
    for key in ("entry_created_at_utc", "entry_created_at", "entry_timestamp_utc"):
        parsed = _parse_utc_datetime(state.get(key))
        if parsed is not None:
            return parsed
    entry_date = str(state.get("entry_date") or state.get("date") or "").strip()
    entry_time = str(state.get("entry_time_utc") or state.get("entry_time") or "").strip()
    if not entry_date or not entry_time:
        return None
    return _parse_utc_datetime(f"{entry_date}T{entry_time}")


def _order_filled_size(order):
    """Return the proven filled size, never the merely requested size."""
    if not isinstance(order, dict):
        return 0
    for key in ("filled_size", "filled_quantity", "executed_size"):
        value = order.get(key)
        if value not in (None, ""):
            try:
                return max(abs(int(float(value))), 0)
            except (TypeError, ValueError, OverflowError):
                return 0
    try:
        requested = abs(int(float(order.get("size") or 0)))
    except (TypeError, ValueError, OverflowError):
        return 0
    if order.get("unfilled_size") not in (None, ""):
        try:
            unfilled = abs(int(float(order["unfilled_size"])))
            return max(requested - unfilled, 0)
        except (TypeError, ValueError, OverflowError):
            return 0
    # ``filled`` explicitly proves the whole requested size.  Delta also uses
    # ``closed`` for terminal partially-filled/cancelled orders, so a positive
    # average price alone cannot prove their requested quantity was executed.
    if _order_state(order) == "filled":
        return requested
    return 0


def _owned_close_lots(state):
    for key in ("owned_entry_lots", "entry_lots", "lots"):
        try:
            value = abs(int(float(state.get(key) or 0)))
        except (TypeError, ValueError, OverflowError):
            continue
        if value > 0:
            return value
    return 0


def _resolve_external_close_order(state):
    """Find a provable full close from authenticated terminal order history.

    A same-product order is not enough: it must be an opposite-side filled
    order submitted after this state's entry and must exactly match all
    bot-owned lots.  A larger same-product order may include external exposure,
    whose fill fee cannot safely be charged to this strategy.
    Known protection identities are preferred, otherwise the latest qualifying
    fill wins.  The conclusive flag distinguishes "no match yet" from an API
    failure so callers can preserve pending accounting safely.
    """
    entered = _state_entry_datetime(state)
    detected = _parse_utc_datetime(state.get("exit_detected_at_utc"))
    expected_lots = _owned_close_lots(state)
    product_id = state.get("product_id")
    close_side = "buy" if str(state.get("side") or "").lower() == "short" else "sell"
    if entered is None:
        return {}, False, "entry timestamp is unavailable; post-entry close cannot be proven"
    if detected is None:
        return {}, False, "persisted exit detection timestamp is unavailable"
    if detected <= entered:
        return {}, False, "exit detection timestamp does not follow the entry"
    if product_id in (None, "") or expected_lots <= 0:
        return {}, False, "product identity or owned lot count is unavailable"

    path = "/v2/orders/history"
    # Delta currently caps this endpoint at 50.  Filtering server-side keeps
    # the relevant product's recent close inside that bounded page.
    params = {"page_size": 50, "product_ids": str(product_id)}
    query = "?" + urlencode(params)
    try:
        response = requests.get(
            f"{BASE_URL}{path}", params=params,
            headers=_sign("GET", path, query), timeout=10,
        ).json()
    except Exception as exc:
        log.warning("External close history lookup failed: %s", exc)
        return {}, False, f"order history unavailable: {exc}"
    if not isinstance(response, dict) or not response.get("success"):
        error = response.get("error") if isinstance(response, dict) else response
        return {}, False, f"order history rejected: {error or 'unknown response'}"

    result = response.get("result") or []
    if isinstance(result, dict):
        result = result.get("orders") or result.get("data") or []
    if not isinstance(result, list):
        return {}, False, "order history response did not contain an order list"

    known_order_ids = {
        str(value) for value in (
            state.get("tp_stop_order_id"), state.get("tsl_stop_order_id"),
            state.get("pending_close_order_id"),
        ) if value not in (None, "")
    }
    known_client_ids = {
        str(value) for value in (
            state.get("tp_client_order_id"), state.get("stop_client_order_id"),
            state.get("pending_close_client_order_id"),
        ) if value not in (None, "")
    }
    candidates = []
    for order in result:
        if not isinstance(order, dict):
            continue
        if str(order.get("product_id")) != str(product_id):
            continue
        if str(order.get("side") or "").lower() != close_side:
            continue
        if _order_state(order) not in {"closed", "filled"}:
            continue
        try:
            fill = float(order.get("average_fill_price") or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(fill) or fill <= 0:
            continue
        created = _parse_utc_datetime(
            order.get("created_at") or order.get("created_at_utc")
            or order.get("order_created_at")
        )
        if created is None or created <= entered or created > detected:
            continue
        if _order_filled_size(order) != expected_lots:
            continue
        is_known = (
            str(order.get("id") or "") in known_order_ids
            or str(order.get("client_order_id") or "") in known_client_ids
        )
        reduce_value = order.get("reduce_only")
        reduce_known = reduce_value not in (None, "")
        reduce_only = (
            reduce_value is True
            or str(reduce_value).strip().lower() in {"1", "true", "yes"}
        )
        # A known protection identity was created reduce-only by this monitor,
        # and some terminal-history payloads omit the flag.  Unknown orders do
        # not get that trust: explicit reduce_only=true is required so an
        # opposite-side reversal cannot be misattributed as this strategy's exit.
        if (is_known and reduce_known and not reduce_only) or (not is_known and not reduce_only):
            continue
        candidates.append((1 if is_known else 0, created, order))

    if not candidates:
        return {}, True, "no authoritative post-entry opposite-side full fill found"
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return dict(candidates[0][2]), True, ""


def _cancel_confirmed(response, order_id):
    if isinstance(response, dict) and response.get("success"):
        return True, "cancelled"
    error = response.get("error") if isinstance(response, dict) else None
    code = error.get("code") if isinstance(error, dict) else error
    if str(code or "").lower() in {
        "not_found", "order_not_found", "order_not_found_for_cancellation",
        "order_already_cancelled", "order_already_canceled", "already_cancelled",
    }:
        return True, str(code).lower()
    status = _order_state(get_order(order_id))
    return status in _TERMINAL_ORDER_STATES, status


def remove_exchange_protection(state=None, *, confirmed_closed=False, explicit=False,
                               reason=""):
    """Cancel persisted TP/SL orders only with explicit authority.

    Merely stopping/restarting the monitor is intentionally insufficient.  The
    caller must either have confirmed that exchange position size is zero, or
    be handling an explicit remove-protection request/action.
    """
    if not (confirmed_closed or explicit):
        raise PermissionError("protection cleanup requires confirmed close or explicit removal")
    state = dict(state or load_state())
    pending = []
    cleared = {}
    product_id = state.get("product_id")
    owned = [
        ("tsl_stop_order_id", "last_tsl_stop_order_id", "stop_order_state"),
        ("tp_stop_order_id", "last_tp_stop_order_id", "tp_order_state"),
    ]
    for id_key, last_key, status_key in owned:
        order_id = state.get(id_key)
        if not order_id:
            continue
        response = cancel_order(order_id, product_id)
        cancelled, terminal_state = _cancel_confirmed(response, order_id)
        if cancelled:
            cleared.update({id_key: None, last_key: order_id, status_key: terminal_state})
            log.info("Resting protection order %s removed (%s).", order_id,
                     reason or ("position closed" if confirmed_closed else "explicit request"))
        else:
            pending.append(order_id)
            log.error("Could not remove resting protection order %s; id retained for retry.", order_id)

    remaining_orphans = []
    for order_id in state.get("orphan_protection_order_ids") or []:
        response = cancel_order(order_id, product_id)
        removed, _ = _cancel_confirmed(response, order_id)
        if not removed:
            pending.append(order_id)
            remaining_orphans.append(order_id)
        else:
            log.info("Orphaned protection order %s removed.", order_id)
    if state.get("orphan_protection_order_ids") is not None:
        cleared["orphan_protection_order_ids"] = remaining_orphans

    if cleared:
        if "tsl_stop_order_id" in cleared:
            cleared.update({"stop_lots": 0})
        if "tp_stop_order_id" in cleared:
            cleared.update({"tp_lots": 0})
        save_state_fields(**cleared)
    if explicit:
        save_state_fields(
            remove_protection_requested=bool(pending),
            protection_removed_utc=_utc_now() if not pending else None,
            protection_remove_error=("orders still pending: " + ", ".join(map(str, pending)))
                                    if pending else "",
        )
    return not pending


def _history_accounting_complete(record):
    """Return whether a row is final, without stranding legacy/dry-run rows.

    Externally-flat reconciliation is deliberately strict because that is where
    a missing fill previously became fake zero P&L.  Older non-external and
    dry-run records predate fee fields; their explicit exit/P&L remains final.
    """
    if not isinstance(record, dict):
        return False

    def finite_number(key, *, minimum=None, positive=False):
        value = record.get(key)
        if value is None or isinstance(value, bool):
            return False
        try:
            value = float(value)
        except (TypeError, ValueError, OverflowError):
            return False
        if not math.isfinite(value):
            return False
        if positive and value <= 0:
            return False
        return minimum is None or value >= minimum

    if not finite_number("exit_mark", minimum=0) or not finite_number("pnl_usd"):
        return False
    dry_run = record.get("dry_run")
    is_dry_run = dry_run is True or str(dry_run).strip().lower() in {
        "1", "true", "yes", "on",
    }
    strict = not is_dry_run and (
        str(record.get("exit_trigger") or "").lower() == "closed_externally"
        or str(record.get("exit_reconciliation_status") or "").startswith("pending")
        or str(record.get("accounting_status") or "").lower() == "pending"
    )
    if not strict:
        return True
    includes_fees = record.get("pnl_includes_fees")
    if not (includes_fees is True
            or str(includes_fees).strip().lower() in {"1", "true", "yes", "on"}):
        return False
    return (
        finite_number("exit_mark", positive=True)
        and finite_number("gross_pnl_usd")
        and finite_number("fees_usd", minimum=0)
        and finite_number("entry_fee_usd", minimum=0)
        and finite_number("exit_fee_usd", minimum=0)
    )


def _history_entry_ids(record):
    if not isinstance(record, dict):
        return set()
    identities = set()
    for key in ("entry_client_order_id", "client_order_id"):
        value = record.get(key)
        if value not in (None, ""):
            identities.add(("client", str(value)))
    for value in record.get("client_order_ids") or []:
        if value not in (None, ""):
            identities.add(("client", str(value)))
    for key in ("entry_order_id", "order_id"):
        value = record.get(key)
        if value not in (None, ""):
            identities.add(("order", str(value)))
    for value in record.get("order_ids") or []:
        if value not in (None, ""):
            identities.add(("order", str(value)))
    return identities


def _same_history_trade(left, right):
    """Match a trade by stable entry identity, with a legacy timestamp fallback."""
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    left_slot = str(left.get("slot") or "").lower()
    right_slot = str(right.get("slot") or "").lower()
    if left_slot and right_slot and left_slot != right_slot:
        return False
    left_ids = _history_entry_ids(left)
    right_ids = _history_entry_ids(right)
    if left_ids and right_ids:
        return bool(left_ids & right_ids)
    return (
        str(left.get("symbol") or "") == str(right.get("symbol") or "")
        and str(left.get("entry_date") or left.get("date") or "")
        == str(right.get("entry_date") or right.get("date") or "")
        and str(left.get("entry_time_utc") or left.get("entry_time") or "")
        == str(right.get("entry_time_utc") or right.get("entry_time") or "")
    )


def append_history(state):
    """Record or repair the closed trade so history never freezes fake zeros."""
    try:
        rec = {
            "slot":         state.get("slot") or SLOT,
            "date":         state.get("entry_date", ""),
            "entry_date":   state.get("entry_date", ""),
            "trading_date": state.get("trading_date", state.get("entry_date", "")),
            "symbol":       state.get("symbol", ""),
            "strike":       state.get("strike", 0),
            "lots":         state.get("owned_entry_lots") or state.get("entry_lots")
                            or state.get("lots", 0),
            "exit_lots":    state.get("closed_lots") or state.get("lots", 0),
            "entry_mark":   state.get("entry_mark", 0),
            # Missing realized accounting is deliberately JSON null.  Zero is
            # a legitimate result and must never be used as an unknown marker.
            "exit_mark":    state.get("exit_mark"),
            "pnl_usd":      state.get("pnl_usd"),
            "gross_pnl_usd": state.get("gross_pnl_usd"),
            "fees_usd":     state.get("fees_usd"),
            "entry_fee_usd": state.get("entry_fee_usd"),
            "exit_fee_usd": state.get("exit_fee_usd"),
            "fees_available": bool(state.get("fees_available")),
            "fees_complete": bool(state.get("fees_complete")),
            "fees_estimated": bool(state.get("fees_estimated")),
            "pnl_includes_fees": bool(state.get("pnl_includes_fees")),
            "cost_usd":     state.get("total_cost_usd", 0),
            "entry_time":   state.get("entry_time_utc", ""),
            "entry_time_utc": state.get("entry_time_utc", ""),
            "exit_time":    state.get("exit_time_utc", ""),
            "exit_time_utc": state.get("exit_time_utc", ""),
            "exit_date":    state.get("exit_date"),
            "exit_at_utc":  state.get("exit_at_utc"),
            "exit_trigger": state.get("exit_trigger", ""),
            "side":         state.get("side", "long"),
            "dry_run":      bool(state.get("dry_run", False)),
            "order_id":     state.get("order_id") or state.get("entry_order_id"),
            "entry_order_id": state.get("entry_order_id") or state.get("order_id"),
            "order_ids":    state.get("order_ids", []),
            "client_order_id": state.get("client_order_id"),
            "entry_client_order_id": state.get("entry_client_order_id")
                                     or state.get("client_order_id"),
            "client_order_ids": state.get("client_order_ids", []),
            "exit_order_id": state.get("exit_order_id"),
            "exit_client_order_id": state.get("exit_client_order_id"),
            "exit_reconciliation_status": state.get("exit_reconciliation_status"),
            "entry_fee_source": state.get("entry_fee_source"),
            "exit_fee_source": state.get("exit_fee_source"),
        }
        rec["accounting_status"] = (
            "complete" if _history_accounting_complete(rec) else "pending"
        )
        with account_file_lock(USER_DIR, "history", f"tp-{SLOT}-{os.getpid()}",
                               stale_after_sec=30, wait_sec=5) as acquired:
            if not acquired:
                raise RuntimeError("trade-history lock unavailable")
            hist = json.loads(HISTORY_FILE.read_text()) if HISTORY_FILE.exists() else []
            duplicate_index = next((
                index for index, row in enumerate(hist)
                if _same_history_trade(row, rec)
            ), None)
            if duplicate_index is None:
                hist.append(rec)
                stored = rec
                _atomic_write_json(HISTORY_FILE, hist)
            else:
                existing = hist[duplicate_index]
                if _history_accounting_complete(existing):
                    stored = existing
                else:
                    # An incomplete row may have come from an earlier flat
                    # detection.  Replace its synthetic zeros/nulls with the
                    # now-authoritative accounting without creating a duplicate.
                    stored = {**existing, **rec}
                    stored["accounting_status"] = (
                        "complete" if _history_accounting_complete(stored) else "pending"
                    )
                    if stored != existing:
                        hist[duplicate_index] = stored
                        _atomic_write_json(HISTORY_FILE, hist)
        complete = _history_accounting_complete(stored)
        history_fields = {
            "history_pending": not complete,
            "history_logged": complete,
        }
        if complete:
            history_fields["history_logged_at_utc"] = _utc_now()
        save_state_fields(**history_fields)
        return True
    except Exception as e:
        log.warning("History append failed: %s", e)
        return False


def _hydrate_complete_history(state):
    """Restore final accounting from the ledger before doing another lookup."""
    try:
        history = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(history, list):
        return None
    existing = next((
        row for row in reversed(history)
        if _same_history_trade(row, state) and _history_accounting_complete(row)
    ), None)
    if existing is None:
        return None
    hydrated = dict(state)
    direct_keys = (
        "exit_date", "exit_at_utc", "exit_mark", "pnl_usd", "gross_pnl_usd",
        "fees_usd", "entry_fee_usd", "exit_fee_usd", "fees_available",
        "fees_complete", "fees_estimated", "pnl_includes_fees",
        "entry_fee_source", "exit_fee_source", "exit_trigger", "exit_order_id",
        "exit_client_order_id", "exit_reconciliation_status", "closed_lots",
    )
    for key in direct_keys:
        if key in existing and existing.get(key) is not None:
            hydrated[key] = existing[key]
    exit_clock = existing.get("exit_time_utc") or existing.get("exit_time")
    if exit_clock:
        hydrated["exit_time_utc"] = exit_clock
    hydrated.update({
        "status": "CLOSED",
        "history_pending": False,
        "history_logged": True,
        "history_logged_at_utc": hydrated.get("history_logged_at_utc") or _utc_now(),
    })
    _atomic_write_json(STATE_FILE, hydrated)
    return hydrated


def _finalize_external_flat_close_locked(state):
    """Persist an externally-flat close with proven history accounting.

    Exchange size zero proves exposure is gone, but it does not prove a fill
    price.  Until history supplies a full post-entry opposite-side fill, keep
    accounting null and retryable instead of publishing a false $0 result.
    """
    latest = load_state()
    if isinstance(latest, dict):
        state = {**state, **latest}
    if _history_accounting_complete(state):
        append_history(state)
        return True
    hydrated = _hydrate_complete_history(state)
    if hydrated is not None:
        log.info("Recovered complete %s accounting from trade history.", state.get("symbol"))
        return True

    now = datetime.now(timezone.utc)
    if not state.get("exit_detected_at_utc"):
        # Persist the upper attribution bound before querying.  A later order
        # in this product can then never be pulled backward into this close.
        state["exit_detected_at_utc"] = now.isoformat(timespec="seconds")
        _atomic_write_json(STATE_FILE, state)
    order, conclusive, error = _resolve_external_close_order(state)
    lots = _owned_close_lots(state)

    if order:
        try:
            fill = float(order["average_fill_price"])
            entry = float(state["entry_mark"])
            cv = float(state.get("contract_value"))
            lots = int(lots)
            if not (math.isfinite(fill) and fill > 0):
                raise ValueError("exit fill must be finite and positive")
            if not (math.isfinite(entry) and entry > 0):
                raise ValueError("entry fill must be finite and positive")
            if not (math.isfinite(cv) and cv > 0):
                raise ValueError("contract value must be finite and positive")
            if lots <= 0 or _order_filled_size(order) != lots:
                raise ValueError("exit order does not exactly fill the owned lots")
            sign = -1 if str(state.get("side") or "").lower() == "short" else 1
            gross = (fill - entry) * cv * lots * sign
            if not math.isfinite(gross):
                raise ValueError("gross P&L is not finite")
            real = _apply_exit_accounting(state, gross, order)
            exited = _parse_utc_datetime(
                order.get("updated_at") or order.get("closed_at")
                or order.get("created_at") or order.get("created_at_utc")
            )
            exited = exited or now
            state.update({
                "status": "CLOSED",
                "exit_date": exited.strftime("%Y-%m-%d"),
                "exit_time_utc": exited.strftime("%H:%M:%S"),
                "exit_at_utc": exited.isoformat().replace("+00:00", "Z"),
                "exit_mark": fill,
                "exit_trigger": "closed_externally",
                "exit_order_id": order.get("id"),
                "exit_client_order_id": order.get("client_order_id"),
                "closed_lots": lots,
                "exit_reconciliation_status": "resolved_order_history",
                "exit_reconciliation_error": "",
                "exit_history_lookup_conclusive": bool(conclusive),
                "history_pending": True,
                "history_logged": False,
            })
            _atomic_write_json(STATE_FILE, state)
            append_history(state)
            log.info(
                "Externally-flat %s reconciled to order %s at %.4f; net P&L $%.2f.",
                state.get("symbol"), order.get("id"), fill, real,
            )
            return True
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            error = f"close fill accounting failed: {exc}"
            conclusive = False

    pending_fields = {
        "status": "CLOSED",
        "exit_trigger": "closed_externally",
        "closed_lots": lots,
        "exit_detected_at_utc": state.get("exit_detected_at_utc"),
        "exit_reconciliation_status": "pending_order_history",
        "exit_reconciliation_error": error,
        "exit_history_lookup_conclusive": bool(conclusive),
        "history_pending": True,
        "history_logged": False,
    }
    # Never replace already-known accounting with null merely because a later
    # history request failed.  Unknown fields stay absent/null naturally.
    state.update(pending_fields)
    for key in (
        "exit_date", "exit_time_utc", "exit_at_utc", "exit_mark",
        "exit_order_id", "exit_client_order_id", "gross_pnl_usd",
        "pnl_usd", "fees_usd", "exit_fee_usd",
    ):
        state.setdefault(key, None)
    state.setdefault("fees_available", False)
    state.setdefault("fees_complete", False)
    state.setdefault("fees_estimated", False)
    state.setdefault("pnl_includes_fees", False)
    _atomic_write_json(STATE_FILE, state)
    append_history(state)
    log.warning("Externally-flat %s has pending realized accounting: %s",
                state.get("symbol"), error)
    return True


def _finalize_external_flat_close(state):
    """Serialize external-close reconciliation across monitor/dashboard actions."""
    with account_file_lock(
        USER_DIR, f"close-{SLOT}", f"tp-external-close-{os.getpid()}",
        stale_after_sec=30, wait_sec=2,
    ) as acquired:
        if not acquired:
            log.warning("Another %s close reconciliation is in progress.", SLOT)
            return False
        return _finalize_external_flat_close_locked(state)


def _finalize_confirmed_market_close_locked(state, order, mark, lots, reason):
    """Finalize accounting only after exchange position size is verified zero."""
    latest = load_state()
    if isinstance(latest, dict):
        state = {**state, **latest}
    order = dict(order or {})
    order.setdefault("id", state.get("pending_close_order_id"))
    order.setdefault("client_order_id", state.get("pending_close_client_order_id"))
    try:
        fill = float(order.get("average_fill_price") or 0)
    except (TypeError, ValueError, OverflowError):
        fill = 0
    try:
        expected_lots = abs(int(lots))
        entry = float(state.get("entry_mark"))
        cv = float(state.get("contract_value"))
    except (TypeError, ValueError, OverflowError):
        expected_lots, entry, cv = 0, 0, 0
    filled_lots = _order_filled_size(order)
    if (not math.isfinite(fill) or fill <= 0
            or not math.isfinite(entry) or entry <= 0
            or not math.isfinite(cv) or cv <= 0
            or expected_lots <= 0 or filled_lots != expected_lots):
        log.warning(
            "Exchange is flat but close order %s lacks valid exact accounting "
            "(%d/%d lots, entry %s, exit %s, cv %s); falling back to strict history.",
            order.get("id"), filled_lots, expected_lots, state.get("entry_mark"),
            order.get("average_fill_price"),
            state.get("contract_value"),
        )
        return _finalize_external_flat_close_locked(state)
    sign = -1 if state.get("side") == "short" else 1
    gross = (fill - entry) * cv * expected_lots * sign
    if not math.isfinite(gross):
        return _finalize_external_flat_close_locked(state)
    real = _apply_exit_accounting(state, gross, order)
    exited = datetime.now(timezone.utc)
    state.update({
        "status": "CLOSED",
        "exit_date": exited.strftime("%Y-%m-%d"),
        "exit_time_utc": exited.strftime("%H:%M:%S"),
        "exit_at_utc": exited.isoformat().replace("+00:00", "Z"),
        "exit_mark": fill,
        "exit_trigger": f"{reason}_{SLOT}",
        "exit_order_id": order.get("id"),
        "exit_client_order_id": order.get("client_order_id"),
        "pending_close_order_id": None,
        "pending_close_client_order_id": None,
        "pending_close_reason": "",
        "pending_close_state": "confirmed_flat",
        "pending_close_error": "",
        "history_pending": True,
        "history_logged": False,
        "closed_lots": int(state.get("owned_entry_lots") or expected_lots),
    })
    _atomic_write_json(STATE_FILE, state)
    append_history(state)

    tag = {"take_profit": "TP", "stop_loss": "SL", "trailing_stop": "TSL"}.get(reason, "TP")
    label = _slot_label()
    head = {
        "take_profit": f"✅ <b>TAKE PROFIT HIT — {label} ({USER.upper()})</b>",
        "stop_loss": f"🛑 <b>STOP LOSS HIT — {label} ({USER.upper()})</b>",
        "trailing_stop": f"🔻 <b>TRAILING STOP HIT — {label} ({USER.upper()})</b>",
    }.get(reason, f"✅ <b>{tag} — {label} ({USER.upper()})</b>")
    psign = "+" if real >= 0 else "-"
    send_telegram(
        f"{head}\n"
        f"<code>{'━' * 24}</code>\n"
        f"Symbol  » <code>{state.get('symbol')}</code>\n"
        f"Lots    » <code>{expected_lots:,}</code>\n"
        f"Entry   » <code>${float(state['entry_mark']):.4f}</code>\n"
        f"Exit    » <code>${fill:.4f}</code>\n"
        f"P&L     » <code>{psign}${abs(real):.2f}</code> {'🎯' if reason == 'take_profit' else '🛑'}\n"
        f"OrderID » <code>{order.get('id')}</code>"
    )
    return True


def _finalize_confirmed_market_close(state, order, mark, lots, reason):
    """Public serialized wrapper for confirmed-flat market accounting."""
    with account_file_lock(
        USER_DIR, f"close-{SLOT}", f"tp-market-finalize-{os.getpid()}",
        stale_after_sec=30, wait_sec=2,
    ) as acquired:
        if not acquired:
            log.warning("Another %s close reconciliation is in progress.", SLOT)
            return False
        return _finalize_confirmed_market_close_locked(state, order, mark, lots, reason)


def _persist_close_fields(state, **fields):
    """Durably persist close identity/state; False means no order may be sent."""
    try:
        latest = load_state()
        merged = {**state, **latest} if isinstance(latest, dict) else dict(state)
        merged.update(fields)
        _atomic_write_json(STATE_FILE, merged)
        state.update(fields)
        return True
    except Exception as exc:
        log.error("Cannot persist close intent safely: %s", exc)
        return False


def _reconcile_uncertain_close(state, response_order, mark, lots, reason, error):
    """Reconcile a submitted close whose API outcome is not trustworthy.

    The persisted client id is the idempotency boundary.  An exception, timeout,
    duplicate-client response, or other failed response must never cause us to
    invent a second identity until the exact first identity and live exposure
    have both been checked.
    """
    latest = load_state()
    if isinstance(latest, dict):
        state = {**state, **latest}

    try:
        exact_order, lookup_conclusive = _lookup_pending_close(state)
    except Exception as exc:
        log.warning("Exact close-order reconciliation failed: %s", exc)
        exact_order, lookup_conclusive = {}, False

    order = dict(response_order or {})
    if exact_order:
        # Exact exchange data wins, while retaining synchronous response fields
        # (such as commission) that an order lookup may omit.
        order = {**order, **exact_order}
    close_side = state.get("pending_close_side") or (
        "buy" if state.get("side") == "short" else "sell"
    )
    identity_error = _close_order_identity_error(order, state, close_side)
    if identity_error:
        log.error("Ignoring mismatched close-order recovery: %s", identity_error)
        order = {}
        exact_order = {}
        lookup_conclusive = False
        error = f"{error}; {identity_error}"
    order_id = order.get("id") or state.get("pending_close_order_id")
    client_order_id = (
        order.get("client_order_id") or state.get("pending_close_client_order_id")
    )
    order_state = _order_state(order)
    _persist_close_fields(
        state,
        pending_close_order_id=order_id,
        pending_close_client_order_id=client_order_id,
        pending_close_reason=state.get("pending_close_reason") or reason,
        pending_close_exchange_state=order_state,
        pending_close_lookup_conclusive=bool(lookup_conclusive),
        pending_close_error=str(error),
        pending_close_last_reconciled_utc=_utc_now(),
    )

    # Position size is the final authority for whether protection may be
    # withdrawn.  Even a terminal order is not treated as a full close until
    # this reads zero.
    live_after = get_exchange_size(state["product_id"])
    if live_after == 0:
        return _finalize_confirmed_market_close_locked(state, order, mark, lots, reason)

    if live_after is None:
        pending_state = "ambiguous"
        detail = f"{error}; position verification failed"
    elif order_state in _ACTIVE_ORDER_STATES:
        pending_state = "active"
        detail = f"{error}; exact close order remains {order_state}"
    elif order_state in _TERMINAL_ORDER_STATES:
        pending_state = "terminal_position_open"
        detail = f"{error}; exact close order is {order_state}, {abs(int(live_after))} lots remain"
    elif lookup_conclusive and not exact_order:
        # A later cycle may safely retry the *same* client identity.  We do not
        # retry in this cycle because an just-submitted order can take a moment
        # to become visible through the read endpoints.
        pending_state = "not_found_retryable"
        detail = f"{error}; exact identity not found, {abs(int(live_after))} lots remain"
    else:
        pending_state = "ambiguous"
        detail = f"{error}; close outcome unresolved, {abs(int(live_after))} lots remain"

    _persist_close_fields(
        state,
        pending_close_state=pending_state,
        pending_close_error=detail,
        pending_close_live_size=(None if live_after is None else int(live_after)),
        lots=(state.get("lots") if live_after is None else abs(int(live_after))),
        protection_last_error=detail,
    )
    log.warning("Close identity %s remains unresolved; protection retained: %s",
                client_order_id, detail)
    return False


def close_position(state, mark, pnl, reason="take_profit"):
    with account_file_lock(
        USER_DIR, f"close-{SLOT}", f"tp-close-{os.getpid()}",
        stale_after_sec=30, wait_sec=2,
    ) as acquired:
        if not acquired:
            log.warning("Another %s close/reconciliation is in progress.", SLOT)
            return False
        return _close_position_locked(state, mark, pnl, reason)


def _close_position_locked(state, mark, pnl, reason="take_profit"):
    """Close once, reconciling one durable client identity until resolved."""
    latest = load_state()
    if isinstance(latest, dict):
        state = {**state, **latest}

    product_id = state["product_id"]
    symbol = state["symbol"]
    configured_lots = int(state.get("lots") or 0)
    is_short = state.get("side") == "short"
    close_side = "buy" if is_short else "sell"
    pending_client_id = state.get("pending_close_client_order_id")
    pending_order_id = state.get("pending_close_order_id")
    pending_reason = state.get("pending_close_reason") or reason
    stored_close_side = str(state.get("pending_close_side") or "").lower()
    if stored_close_side and stored_close_side != close_side:
        detail = f"pending close side mismatch: {stored_close_side} != {close_side}"
        _persist_close_fields(
            state, pending_close_state="ambiguous", pending_close_error=detail,
        )
        log.error("%s; close submission blocked.", detail)
        return False

    # Never close blind.  This also gives the definitive flat check needed
    # before the caller is allowed to remove exchange-resident protection.
    live_size = get_exchange_size(product_id)
    if live_size is None:
        log.warning("Cannot verify position — skipping close this cycle.")
        return False

    exact_order = {}
    lookup_conclusive = False
    retry_same_identity = False
    resolved_previous = {}

    if pending_client_id or pending_order_id:
        try:
            exact_order, lookup_conclusive = _lookup_pending_close(state)
        except Exception as exc:
            log.warning("Pending close lookup failed: %s", exc)
            exact_order, lookup_conclusive = {}, False

        identity_error = _close_order_identity_error(exact_order, state, close_side)
        if identity_error:
            _persist_close_fields(
                state, pending_close_state="ambiguous",
                pending_close_error=identity_error,
                pending_close_lookup_conclusive=False,
                pending_close_last_reconciled_utc=_utc_now(),
                pending_close_live_size=int(live_size),
            )
            log.error("%s; no close submission will be made.", identity_error)
            return False

        # A zero exchange position is authoritative even if the order-detail
        # endpoint is temporarily unavailable.  Preserve the persisted identity
        # for accounting/audit fallback.
        if live_size == 0:
            log.info("Pending close identity %s is verified flat.", pending_client_id)
            return _finalize_confirmed_market_close_locked(
                state, exact_order, mark, configured_lots, pending_reason
            )

        exact_state = _order_state(exact_order)
        if exact_state in _ACTIVE_ORDER_STATES:
            _persist_close_fields(
                state,
                pending_close_order_id=exact_order.get("id") or pending_order_id,
                pending_close_client_order_id=(
                    exact_order.get("client_order_id") or pending_client_id
                ),
                pending_close_state="active",
                pending_close_exchange_state=exact_state,
                pending_close_last_reconciled_utc=_utc_now(),
                pending_close_live_size=int(live_size),
            )
            log.info("Close order %s is still active; not submitting a duplicate.",
                     exact_order.get("id") or pending_order_id)
            return False

        if exact_order and exact_state not in _TERMINAL_ORDER_STATES:
            detail = f"exact close order has unresolved state {exact_state or 'unknown'}"
            _persist_close_fields(
                state, pending_close_state="ambiguous", pending_close_error=detail,
                pending_close_last_reconciled_utc=_utc_now(),
                pending_close_live_size=int(live_size),
            )
            log.warning("%s; protection retained.", detail)
            return False

        if not lookup_conclusive:
            detail = "exact close identity lookup inconclusive"
            _persist_close_fields(
                state, pending_close_state="ambiguous", pending_close_error=detail,
                pending_close_last_reconciled_utc=_utc_now(),
                pending_close_live_size=int(live_size),
            )
            log.warning("%s; no new close identity will be created.", detail)
            return False

        if exact_order and exact_state in _TERMINAL_ORDER_STATES:
            # The old identity is conclusively consumed.  Recheck exposure after
            # observing terminal state before creating an identity for any
            # remainder (partial fills and rejected/cancelled closes included).
            live_recheck = get_exchange_size(product_id)
            if live_recheck is None:
                _persist_close_fields(
                    state, pending_close_state="terminal_position_unverified",
                    pending_close_exchange_state=exact_state,
                    pending_close_error="terminal close found; remaining exposure unverified",
                )
                return False
            if live_recheck == 0:
                return _finalize_confirmed_market_close_locked(
                    state, exact_order, mark, configured_lots, pending_reason
                )
            live_size = live_recheck
            resolved_previous = {
                "last_close_order_id": exact_order.get("id") or pending_order_id,
                "last_close_client_order_id": (
                    exact_order.get("client_order_id") or pending_client_id
                ),
                "last_close_order_state": exact_state,
            }
            pending_client_id = None
            pending_order_id = None
        else:
            # Both open/history lookups authoritatively found no such order.
            # Retrying the same client id is safe; replacing it is not.
            if not pending_client_id:
                detail = "legacy close order id is unresolved without a client identity"
                _persist_close_fields(
                    state, pending_close_state="ambiguous", pending_close_error=detail,
                )
                return False
            retry_same_identity = True

    elif live_size == 0:
        log.info("Position already closed on exchange — reconciling its actual close fill.")
        return _finalize_external_flat_close_locked(state)

    lots = abs(int(live_size))
    client_order_id = pending_client_id or _protection_client_order_id("close")
    created_utc = (
        state.get("pending_close_created_utc")
        if retry_same_identity else _utc_now()
    ) or _utc_now()
    previous_attempts = int(state.get("pending_close_attempts") or 0)
    attempts = previous_attempts + 1 if retry_same_identity else 1
    intent_state = "retrying_same_identity" if retry_same_identity else "intent_persisted"

    # This write must complete before the irreversible POST.  If the process
    # dies or the response is lost, the next monitor instance can only reconcile
    # or reuse this exact identity.
    if not _persist_close_fields(
        state,
        **resolved_previous,
        pending_close_client_order_id=client_order_id,
        pending_close_order_id=None,
        pending_close_reason=pending_reason,
        pending_close_state=intent_state,
        pending_close_exchange_state="",
        pending_close_lots=lots,
        pending_close_side=close_side,
        pending_close_created_utc=created_utc,
        pending_close_last_attempt_utc=_utc_now(),
        pending_close_attempts=attempts,
        pending_close_error="",
        pending_close_lookup_conclusive=False,
        pending_close_live_size=int(live_size),
    ):
        log.error("Close intent was not durable; refusing to submit an order.")
        return False

    tag = {"take_profit": "TP", "stop_loss": "SL", "trailing_stop": "TSL"}.get(
        pending_reason, "TP"
    )
    log.info("%s HIT — P&L $%.2f  mark $%.4f  %sing %d lots to close (client %s)...",
             tag, pnl, mark, close_side, lots, client_order_id)
    try:
        result = place_order(
            product_id, symbol, close_side, lots, reduce_only=True,
            client_order_id=client_order_id,
        )
    except Exception as exc:
        log.error("%s close submit raised after durable intent: %s", tag, exc)
        return _reconcile_uncertain_close(
            state, {}, mark, lots, pending_reason, f"submit exception: {exc}"
        )

    if not isinstance(result, dict) or not result.get("success"):
        log.error("%s CLOSE FAILED/UNCERTAIN: %s", tag, result)
        return _reconcile_uncertain_close(
            state, {}, mark, lots, pending_reason, f"submit response: {result}"
        )

    order = result.get("result", {}) or {}
    if not isinstance(order, dict):
        order = {}
    order.setdefault("client_order_id", client_order_id)
    identity_error = _close_order_identity_error(order, state, close_side)
    if identity_error:
        log.error("%s close response identity is invalid: %s", tag, identity_error)
        return _reconcile_uncertain_close(
            state, order, mark, lots, pending_reason,
            f"invalid submit response: {identity_error}",
        )
    order_id = order.get("id")
    # Persist the exchange order id immediately, but keep the durable client id
    # as the primary recovery key if this write or a subsequent read fails.
    _persist_close_fields(
        state,
        pending_close_order_id=order_id,
        pending_close_client_order_id=client_order_id,
        pending_close_state="submitted",
        pending_close_exchange_state=_order_state(order),
        pending_close_error="",
    )

    live_after = get_exchange_size(product_id)
    if live_after == 0:
        try:
            full_order, _ = _lookup_pending_close(state)
        except Exception as exc:
            log.warning("Closed position verified, but exact order lookup failed: %s", exc)
            full_order = {}
        if full_order:
            order = {**order, **full_order}
        return _finalize_confirmed_market_close_locked(
            state, order, mark, lots, pending_reason
        )

    if live_after is None:
        detail = "close submitted; position verification failed"
        pending_state = "submitted_unverified"
    else:
        detail = f"close left {abs(int(live_after))} lots open"
        pending_state = "submitted_position_open"
    _persist_close_fields(
        state,
        pending_close_state=pending_state,
        pending_close_error=detail,
        pending_close_live_size=(None if live_after is None else int(live_after)),
        lots=(state.get("lots") if live_after is None else abs(int(live_after))),
        protection_last_error=detail,
    )
    log.warning("Close order %s (%s) is not verified flat; protection retained: %s",
                order_id, client_order_id, detail)
    return False


def main():
    state = load_state()
    write_monitor_health("starting", state_status=state.get("status"),
                         protection_established=False)

    # Explicit removal is deliberately a separate action from stopping the
    # process.  A dashboard/API can request it by setting the state flag, and an
    # operator can invoke ``--remove-protection`` directly.
    if REMOVE_PROTECTION:
        ok = remove_exchange_protection(state, explicit=True, reason="--remove-protection")
        write_monitor_health(
            "protection_removed" if ok else "degraded",
            last_error="" if ok else "one or more exchange orders could not be removed",
            state_status=state.get("status"), protection_established=False,
        )
        return 0 if ok else 2

    try:
        install_signal_handlers()
    except (ValueError, OSError, AttributeError):
        # Signal registration is only legal in the main thread.  Monitoring is
        # still safe because an unhandled process termination also leaves the
        # exchange orders untouched.
        pass

    if state.get("status") == "CLOSED":
        # CLOSED workers are deliberately one-shot.  Dashboard supervision
        # periodically respawns an externally-flat/history-pending worker, so a
        # delayed order-history fill gets another chance without leaving an old
        # monitor alive to collide with a later position in this slot.
        product_id = state.get("product_id")
        try:
            live_size = get_exchange_size(product_id) if product_id not in (None, "") else None
        except Exception as exc:
            live_size = None
            log.warning("Closed-state exposure verification failed: %s", exc)
        if live_size != 0:
            detail = (
                "closed state has unverified exchange exposure; protection retained"
                if live_size is None else
                f"state says CLOSED but exchange still has {abs(int(live_size))} lots; "
                "protection retained"
            )
            write_monitor_health(
                "degraded", last_error=detail, state_status="CLOSED",
                exchange_position_size=(None if live_size is None else int(live_size)),
                protection_established=bool(
                    state.get("tsl_stop_order_id") or state.get("tp_stop_order_id")
                ),
                reconciliation_pending=bool(state.get("history_pending")),
            )
            log.error(detail)
            return 0
        if state.get("history_pending"):
            if (state.get("exit_trigger") == "closed_externally"
                    and not _history_accounting_complete(state)):
                _finalize_external_flat_close(state)
            else:
                append_history(state)
            state = load_state()
        cleanup_error = ""
        try:
            cleanup_ok = remove_exchange_protection(
                state, confirmed_closed=True,
                reason="one-shot CLOSED state reconciliation",
            )
        except Exception as exc:
            cleanup_ok = False
            cleanup_error = f"closed-state protection cleanup failed: {exc}"
            log.warning(cleanup_error)
        state = load_state()
        accounting_pending = bool(state.get("history_pending"))
        if accounting_pending:
            status = "reconciling"
            last_error = str(
                state.get("exit_reconciliation_error")
                or "realised accounting is awaiting authoritative order history"
            )
        elif not cleanup_ok:
            status = "degraded"
            last_error = cleanup_error or "closed-state protection cleanup needs retry"
        else:
            status = "closed"
            last_error = ""
        write_monitor_health(
            status, last_error=last_error, state_status="CLOSED",
            exchange_position_size=int(live_size), protection_established=False,
            reconciliation_pending=accounting_pending,
        )
        return 0

    if not state or not state.get("product_id"):
        write_monitor_health("error", last_error="position state is missing product_id",
                             state_status=state.get("status"), protection_established=False)
        log.error("No usable %s position state — exiting.", SLOT)
        return 1

    configured = state.get("protection_config") or {}
    account_errors = [error for error in CONFIGURATION_ERRORS if error.startswith("invalid account")]
    if account_errors or (CONFIGURATION_ERRORS and not configured):
        message = "; ".join(CONFIGURATION_ERRORS)
        write_monitor_health(
            "error", last_error=message, state_status=state.get("status"),
            protection_established=False,
        )
        log.critical("Protection configuration is unusable: %s", message)
        return 2

    def _configured_number(key, fallback, *, minimum=0.0):
        value = configured.get(key)
        if value in (None, ""):
            value = fallback
        try:
            return max(abs(float(value)), minimum)
        except (TypeError, ValueError):
            return max(abs(float(fallback)), minimum)

    target_pnl = _configured_number("tp_target_pnl", TARGET_PNL, minimum=1.0)
    sl_pnl = _configured_number("sl_target_pnl", SL_PNL)
    legacy_snapshot_tsl = configured.get("tsl_target_pnl")
    arm_fallback = TSL_ARM_PNL if legacy_snapshot_tsl in (None, "") else legacy_snapshot_tsl
    trail_fallback = TSL_TRAIL_PNL if legacy_snapshot_tsl in (None, "") else legacy_snapshot_tsl
    tsl_arm_pnl = _configured_number("tsl_arm_pnl", arm_fallback)
    tsl_trail_pnl = _configured_number("tsl_trail_pnl", trail_fallback)
    tsl_lock_min_pnl = _configured_number(
        "tsl_lock_min_pnl", _f(f"TSL_LOCK_MIN_PNL{'_' + SLOT.upper() if SLOT != 'evening' else ''}", 0)
    )
    try:
        poll_secs = max(int(float(configured.get("poll_secs") or POLL_SECS)), 10)
    except (TypeError, ValueError):
        poll_secs = POLL_SECS
    local_fallback_poll = max(10, min(poll_secs, LOCAL_FALLBACK_POLL_SECS))
    tsl_enabled = tsl_arm_pnl > 0 and tsl_trail_pnl > 0

    log.info("=" * 56)
    log.info(
        "TP/SL Monitor [%s/%s] started  tp=+$%.2f  sl=%s  "
        "tsl=arm +$%.2f / trail $%.2f  poll=%ds  state=%s",
        USER, SLOT, target_pnl, f"-${sl_pnl:.2f}" if sl_pnl > 0 else "off",
        tsl_arm_pnl, tsl_trail_pnl, poll_secs, STATE_FILE,
    )
    save_state_fields(protection_config_resolved={
        "tp_target_pnl": target_pnl,
        "sl_target_pnl": sl_pnl,
        "tsl_arm_pnl": tsl_arm_pnl,
        "tsl_trail_pnl": tsl_trail_pnl,
        "tsl_lock_min_pnl": tsl_lock_min_pnl,
        "poll_secs": poll_secs,
    })

    symbol = state["symbol"]
    entry_mark = float(state["entry_mark"])
    lots = abs(int(float(state["lots"])))
    cv = float(state.get("contract_value", 0.001))
    sign = -1 if str(state.get("side", "")).lower() == "short" else 1
    product_id = state["product_id"]
    close_side = "buy" if sign < 0 else "sell"
    peak_pnl = float(state.get("tsl_peak") or 0.0)
    tsl_armed = bool(state.get("tsl_armed"))
    stop_id = state.get("tsl_stop_order_id")
    stop_floor = float(state.get("tsl_floor") or 0.0)
    stop_kind = state.get("stop_kind") or ("tsl" if tsl_armed else "sl")
    stop_lots = int(state.get("stop_lots") or lots)
    tp_id = state.get("tp_stop_order_id")
    tp_lots = int(state.get("tp_lots") or lots)
    persist_pk = peak_pnl
    ratchet_min = max(_f("TSL_RATCHET_MIN_PNL", 1.0), tsl_trail_pnl * 0.05)
    exch_unsupported = state.get("exchange_protection_supported") is False
    alert_codes = set(state.get("protection_alert_codes") or [])
    last_reconcile = 0.0
    consecutive_errors = 0
    local_fallback_active = exch_unsupported

    def alert_once(code, message):
        if code in alert_codes:
            return
        alert_codes.add(code)
        save_state_fields(protection_alert_codes=sorted(alert_codes),
                          protection_alerted_at_utc=_utc_now())
        send_telegram(
            f"⚠️ <b>PROTECTION ALERT — {_slot_label()} ({USER.upper()})</b>\n"
            f"<code>{message}</code>"
        )

    def _remember_orphan(order_id):
        if not order_id:
            return
        current = load_state().get("orphan_protection_order_ids") or []
        if order_id not in current:
            current.append(order_id)
            save_state_fields(orphan_protection_order_ids=current)

    def _mark_unsupported(response, what):
        nonlocal exch_unsupported, local_fallback_active
        error = response.get("error") if isinstance(response, dict) else response
        code = error.get("code") if isinstance(error, dict) else error
        if str(code).lower() == "unsupported":
            exch_unsupported = True
            local_fallback_active = True
            save_state_fields(exchange_protection_supported=False,
                              exchange_protection_error=f"{what} unsupported")
            log.warning("%s is unsupported on this product; local fallback is active.", what)
            alert_once("exchange_orders_unsupported",
                       f"{symbol}: exchange-resident {what.lower()} is unsupported; "
                       f"local monitor fallback is active every {local_fallback_poll}s")
            return True
        return False

    def floor_price(floor_usd):
        if lots <= 0 or cv <= 0:
            raise ValueError("lots and contract_value must be positive")
        return max(round(entry_mark + sign * floor_usd / (cv * lots), 1), 0.1)

    def ensure_stop(floor_usd, kind):
        nonlocal stop_id, stop_floor, stop_kind, stop_lots, local_fallback_active
        if exch_unsupported:
            return False
        price = floor_price(floor_usd)
        tag = kind.upper()
        if stop_id and stop_lots == lots:
            response = edit_stop_price(stop_id, product_id, price)
            if response.get("success"):
                stop_floor, stop_kind = floor_usd, kind
                save_state_fields(
                    tsl_peak=round(peak_pnl, 2), tsl_armed=tsl_armed,
                    tsl_floor=round(floor_usd, 2), tsl_stop_order_id=stop_id,
                    stop_kind=kind, stop_lots=stop_lots, stop_order_state="open",
                    exchange_protection_supported=True,
                )
                log.info("%s stop ratcheted to %.1f (floor $%.2f, order %s).",
                         tag, price, floor_usd, stop_id)
                return True
            log.warning("Stop edit failed (%s); placing replacement before removing old order.",
                        response.get("error"))
        response = place_stop_order(product_id, close_side, lots, price)
        response_order = response.get("result") or {}
        new_id = response_order.get("id") if response.get("success") else None
        if not new_id:
            local_fallback_active = True
            if not _mark_unsupported(response, "stop order"):
                error = response.get("error", response)
                log.warning("Stop placement failed: %s; local trigger remains active.", error)
                save_state_fields(exchange_protection_error=f"stop placement failed: {error}")
                alert_once("stop_placement_failed",
                           f"{symbol}: exchange stop could not be established; local fallback is active")
            return False
        old = stop_id
        stop_id, stop_floor, stop_kind, stop_lots = new_id, floor_usd, kind, lots
        save_state_fields(
            tsl_peak=round(peak_pnl, 2), tsl_armed=tsl_armed,
            tsl_floor=round(floor_usd, 2), tsl_stop_order_id=stop_id,
            stop_kind=kind, stop_lots=lots, stop_order_state="open",
            stop_client_order_id=response_order.get("client_order_id"),
            exchange_protection_supported=True, exchange_protection_error="",
        )
        if old:
            cancelled = cancel_order(old, product_id)
            if not cancelled.get("success"):
                _remember_orphan(old)
                alert_once("old_stop_cancel_failed",
                           f"{symbol}: replacement stop placed, but old stop {old} needs reconciliation")
        log.info("%s stop placed: %s %d lots @ %.1f (floor $%.2f, order %s).",
                 tag, close_side.upper(), lots, price, floor_usd, stop_id)
        return True

    def ensure_tp(force=False):
        nonlocal tp_id, tp_lots, local_fallback_active
        if exch_unsupported:
            return False
        if tp_id and tp_lots == lots and not force:
            return True
        price = max(round(entry_mark + sign * target_pnl / (cv * lots), 1), 0.1)
        response = place_stop_order(product_id, close_side, lots, price, "take_profit_order")
        response_order = response.get("result") or {}
        new_id = response_order.get("id") if response.get("success") else None
        if not new_id:
            local_fallback_active = True
            if not _mark_unsupported(response, "take-profit order"):
                error = response.get("error", response)
                log.warning("TP placement failed: %s; local trigger remains active.", error)
                save_state_fields(exchange_protection_error=f"TP placement failed: {error}")
                alert_once("tp_placement_failed",
                           f"{symbol}: exchange TP could not be established; local fallback is active")
            return False
        old = tp_id
        tp_id, tp_lots = new_id, lots
        save_state_fields(tp_stop_order_id=tp_id, tp_lots=tp_lots,
                          tp_order_state="open",
                          tp_client_order_id=response_order.get("client_order_id"),
                          exchange_protection_supported=True,
                          exchange_protection_error="")
        if old:
            cancelled = cancel_order(old, product_id)
            if not cancelled.get("success"):
                _remember_orphan(old)
                alert_once("old_tp_cancel_failed",
                           f"{symbol}: replacement TP placed, but old TP {old} needs reconciliation")
        log.info("TP order placed: %s %d lots @ %.1f (order %s).",
                 close_side.upper(), lots, price, tp_id)
        return True

    def reconcile_order(order_id, id_key, last_key, status_key, kind):
        """Validate a persisted order id without dropping it on transient API errors."""
        if not order_id:
            return None
        order = get_order(order_id)
        if not order:
            save_state_fields(**{status_key: "unknown",
                                 "protection_reconciled_utc": _utc_now()})
            return order_id
        order_product = order.get("product_id")
        if order_product not in (None, "", product_id, str(product_id)) and \
                str(order_product) != str(product_id):
            save_state_fields(**{
                id_key: None, last_key: order_id, status_key: "product_mismatch",
                "protection_reconciled_utc": _utc_now(),
            })
            alert_once(f"{kind}_product_mismatch",
                       f"Persisted {kind} order {order_id} belongs to another product; it was not cancelled")
            return None
        status = _order_state(order)
        save_state_fields(**{status_key: status or "unknown",
                             "protection_reconciled_utc": _utc_now()})
        if status in _TERMINAL_ORDER_STATES:
            save_state_fields(**{id_key: None, last_key: order_id})
            log.warning("Persisted %s order %s is %s; it will be replaced if position remains open.",
                        kind, order_id, status)
            return None
        return order_id

    def reconcile_orphans():
        remaining = []
        for order_id in load_state().get("orphan_protection_order_ids") or []:
            response = cancel_order(order_id, product_id)
            removed, _ = _cancel_confirmed(response, order_id)
            if not removed:
                remaining.append(order_id)
        save_state_fields(orphan_protection_order_ids=remaining)

    def finalize_stop_fill(order, kind):
        with account_file_lock(
            USER_DIR, f"close-{SLOT}", f"tp-stop-finalize-{os.getpid()}",
            stale_after_sec=30, wait_sec=2,
        ) as acquired:
            if not acquired:
                raise RuntimeError("another close reconciliation is in progress")
            return finalize_stop_fill_locked(order, kind)

    def finalize_stop_fill_locked(order, kind):
        closed_state = load_state()
        expected = abs(int(float(closed_state.get("lots") or lots)))
        proven_filled = _order_filled_size(order)
        try:
            fill = float(order.get("average_fill_price") or 0)
        except (TypeError, ValueError, OverflowError):
            fill = 0
        if (not math.isfinite(fill) or fill <= 0
                or not math.isfinite(entry_mark) or entry_mark <= 0
                or not math.isfinite(cv) or cv <= 0
                or expected <= 0 or proven_filled != expected):
            log.warning(
                "Terminal protection order %s does not prove a full fill "
                "(%d/%d lots at %s); using strict order-history reconciliation.",
                order.get("id"), proven_filled, expected,
                order.get("average_fill_price"),
            )
            return _finalize_external_flat_close_locked(closed_state)
        done = expected
        gross = (fill - entry_mark) * cv * done * sign
        if not math.isfinite(gross):
            return _finalize_external_flat_close_locked(closed_state)
        exit_trigger = {"tp": f"take_profit_{SLOT}",
                        "tsl": f"trailing_stop_{SLOT}"}.get(kind, f"stop_loss_{SLOT}")
        real = _apply_exit_accounting(closed_state, gross, order)
        closed_state.update({
            "status": "CLOSED",
            "exit_time_utc": time.strftime("%H:%M:%S", time.gmtime()),
            "exit_mark": fill,
            "exit_trigger": exit_trigger,
            "exit_order_id": order.get("id"),
            "pending_close_order_id": None,
            "history_pending": True,
            "history_logged": False,
            "closed_lots": int(closed_state.get("owned_entry_lots") or done),
        })
        _atomic_write_json(STATE_FILE, closed_state)
        append_history(closed_state)
        label = _slot_label()
        psign = "+" if real >= 0 else "-"
        head_label = {"tp": "TAKE PROFIT", "tsl": "TRAILING STOP"}.get(kind, "STOP LOSS")
        emoji = {"tp": "✅", "tsl": "🔻"}.get(kind, "🛑")
        send_telegram(
            f"{emoji} <b>{head_label} EXECUTED (exchange) — {label} ({USER.upper()})</b>\n"
            f"<code>{'━' * 24}</code>\nSymbol  » <code>{symbol}</code>\n"
            f"Lots    » <code>{done:,}</code>\nEntry   » <code>${entry_mark:.4f}</code>\n"
            f"Exit    » <code>${fill:.4f}</code>\nP&L     » <code>{psign}${abs(real):.2f}</code>\n"
            f"OrderID » <code>{order.get('id')}</code>"
        )

    if stop_id or tp_id:
        log.info("Resuming persisted orders: %s=%s (floor $%.2f), TP=%s, peak=$%.2f.",
                 stop_kind.upper(), stop_id, stop_floor, tp_id, peak_pnl)

    while True:
        sleep_secs = poll_secs
        try:
            state = load_state()
            if state.get("remove_protection_requested"):
                removed = remove_exchange_protection(
                    state, explicit=True, reason="state remove_protection_requested action"
                )
                write_monitor_health(
                    "protection_removed" if removed else "degraded",
                    last_error="" if removed else "explicit protection removal is awaiting retry",
                    state_status=state.get("status"), protection_established=False,
                )
                if removed:
                    return 0
                time.sleep(local_fallback_poll)
                continue

            # Re-sync ids written by a previous monitor or recovery action.
            stop_id = stop_id or state.get("tsl_stop_order_id")
            tp_id = tp_id or state.get("tp_stop_order_id")
            live = get_exchange_size(product_id)
            if live is None:
                consecutive_errors += 1
                message = "exchange position could not be verified; protection orders retained"
                write_monitor_health(
                    "degraded", last_error=message, state_status=state.get("status"),
                    stop_order_id=stop_id, tp_order_id=tp_id,
                    protection_established=bool(stop_id or tp_id),
                    consecutive_errors=consecutive_errors,
                )
                alert_once("position_verification_failed", f"{symbol}: {message}")
                sleep_secs = min(60, max(local_fallback_poll, 2 ** min(consecutive_errors, 5)))
                time.sleep(sleep_secs)
                continue

            # Zero exchange size is the confirmation required before cleanup.
            if live == 0:
                done_kind = None
                for order_id, kind in ((tp_id, "tp"), (stop_id, stop_kind)):
                    if not order_id:
                        continue
                    order = get_order(order_id)
                    if _order_state(order) in {"closed", "filled"}:
                        try:
                            fill = float(order.get("average_fill_price") or 0)
                        except (TypeError, ValueError, OverflowError):
                            fill = 0
                        if math.isfinite(fill) and fill > 0:
                            done_kind = kind
                            if state.get("status") == "OPEN":
                                finalize_stop_fill(order, kind)
                            break
                        log.warning(
                            "Terminal protection order %s has no fill price; "
                            "resolving it through order history.", order_id,
                        )
                if state.get("status") == "OPEN" and not done_kind:
                    _finalize_external_flat_close(state)
                cleaned = remove_exchange_protection(
                    load_state(), confirmed_closed=True, reason="exchange position confirmed zero"
                )
                if cleaned:
                    write_monitor_health(
                        "closed", state_status="CLOSED", exchange_position_size=0,
                        protection_established=False,
                    )
                    log.info("Monitor done; exchange position is zero and resting protection is reconciled.")
                    return 0
                write_monitor_health(
                    "degraded", last_error="position closed but protection cleanup needs retry",
                    state_status="CLOSED", exchange_position_size=0,
                    protection_established=False,
                )
                time.sleep(local_fallback_poll)
                continue

            if state.get("status") != "OPEN":
                message = (f"state says {state.get('status')!r}, but exchange still has "
                           f"{abs(int(live))} lots; orders retained")
                alert_once("state_exchange_mismatch", f"{symbol}: {message}")
                log.error(message)

            # Exchange size is authoritative for partial closes/top-ups.
            pos_changed = False
            new_lots = abs(int(live))
            owned_cap = abs(int(float(state.get("owned_entry_lots") or lots)))
            if new_lots > owned_cap:
                message = (f"exchange position grew beyond bot ownership "
                           f"({new_lots} > {owned_cap}); external same-product lots are not adopted")
                write_monitor_health(
                    "degraded", last_error=message, state_status=state.get("status"),
                    exchange_position_size=live, owned_entry_lots=owned_cap,
                    stop_order_id=stop_id, tp_order_id=tp_id,
                    protection_established=bool(stop_id or tp_id),
                )
                alert_once("same_product_external_growth", f"{symbol}: {message}")
                log.critical(message)
                time.sleep(local_fallback_poll)
                continue
            new_entry = float(state.get("entry_mark") or entry_mark)
            if new_lots != lots or new_entry != entry_mark:
                log.info("Position changed: lots %d -> %d, entry %.4f -> %.4f; resizing protection.",
                         lots, new_lots, entry_mark, new_entry)
                lots, entry_mark = new_lots, new_entry
                pos_changed = True
                save_state_fields(lots=lots, entry_mark=entry_mark)

            now_mono = time.monotonic()
            if now_mono - last_reconcile >= RECONCILE_SECS:
                stop_id = reconcile_order(
                    stop_id, "tsl_stop_order_id", "last_tsl_stop_order_id",
                    "stop_order_state", "stop",
                )
                tp_id = reconcile_order(
                    tp_id, "tp_stop_order_id", "last_tp_stop_order_id",
                    "tp_order_state", "take-profit",
                )
                reconcile_orphans()
                last_reconcile = now_mono

            mark = get_mark(symbol)
            if not math.isfinite(mark) or mark <= 0:
                raise ValueError(f"invalid mark price {mark!r}")
            pnl = (mark - entry_mark) * cv * lots * sign
            peak_pnl = max(peak_pnl, pnl)
            if peak_pnl - persist_pk >= 1.0:
                persist_pk = peak_pnl
                save_state_fields(tsl_peak=round(peak_pnl, 2), tsl_armed=tsl_armed)

            if tsl_enabled and not tsl_armed and peak_pnl >= tsl_arm_pnl:
                tsl_armed = True
                save_state_fields(tsl_peak=round(peak_pnl, 2), tsl_armed=True,
                                  tsl_armed_utc=_utc_now())
                log.info("TSL armed at peak $%.2f; arm=$%.2f, trail=$%.2f.",
                         peak_pnl, tsl_arm_pnl, tsl_trail_pnl)

            active_tsl = tsl_enabled and tsl_armed
            tsl_floor = (max(tsl_lock_min_pnl, peak_pnl - tsl_trail_pnl)
                         if active_tsl else None)
            if active_tsl:
                if stop_id is None or tsl_floor - stop_floor >= ratchet_min or pos_changed:
                    ensure_stop(tsl_floor, "tsl")
            elif sl_pnl > 0 and (stop_id is None or pos_changed):
                ensure_stop(-sl_pnl, "sl")
            ensure_tp(force=pos_changed)

            stop_required = sl_pnl > 0 or active_tsl
            exchange_complete = bool(tp_id) and (not stop_required or bool(stop_id))
            local_fallback_active = exch_unsupported or not exchange_complete
            sleep_secs = local_fallback_poll if local_fallback_active else poll_secs
            status = "healthy" if exchange_complete else "degraded"
            error = "" if exchange_complete else "exchange protection incomplete; local fallback active"
            write_monitor_health(
                status, last_error=error, state_status=state.get("status"), symbol=symbol,
                exchange_position_size=live, last_mark=mark, last_pnl=round(pnl, 2),
                peak_pnl=round(peak_pnl, 2), stop_order_id=stop_id, tp_order_id=tp_id,
                stop_kind=stop_kind, stop_floor=round(stop_floor, 2), tsl_armed=tsl_armed,
                exchange_protection_supported=not exch_unsupported,
                exchange_protection_complete=exchange_complete,
                local_fallback_active=local_fallback_active,
                protection_established=exchange_complete or local_fallback_active,
                consecutive_errors=0, next_poll_secs=sleep_secs,
            )
            consecutive_errors = 0

            log.info(
                "mark=%.4f pnl=$%.2f peak=$%.2f TP=+$%.2f%s SL=%s TSL=%s",
                mark, pnl, peak_pnl, target_pnl, " [exchange]" if tp_id else " [local]",
                (f"-${sl_pnl:.2f}" + (" [exchange]" if stop_id and stop_kind == "sl" else " [local]"))
                if sl_pnl > 0 else "off",
                "off" if not tsl_enabled else
                (f"floor ${tsl_floor:.2f}" + (" [exchange]" if stop_id and stop_kind == "tsl" else " [local]"))
                if active_tsl else f"unarmed (arm +${tsl_arm_pnl:.2f}, trail ${tsl_trail_pnl:.2f})",
            )

            # Local exits remain available when a product rejects exchange
            # stop orders.  Existing resting orders are intentionally retained
            # until the reduce-only market close is confirmed by position size.
            closed = False
            if tp_id is None and pnl >= target_pnl:
                closed = close_position(state, mark, pnl, "take_profit")
            elif active_tsl and stop_id is None and pnl <= tsl_floor:
                closed = close_position(state, mark, pnl, "trailing_stop")
            elif sl_pnl > 0 and stop_id is None and pnl <= -sl_pnl:
                closed = close_position(state, mark, pnl, "stop_loss")
            if closed:
                cleaned = remove_exchange_protection(
                    load_state(), confirmed_closed=True, reason="local reduce-only close confirmed"
                )
                if cleaned:
                    write_monitor_health("closed", state_status="CLOSED",
                                         exchange_position_size=0,
                                         protection_established=False)
                    return 0
                sleep_secs = local_fallback_poll
        except Exception as exc:
            consecutive_errors += 1
            log.warning("Poll error: %s", exc)
            write_monitor_health(
                "degraded", last_error=str(exc), state_status=load_state().get("status"),
                stop_order_id=stop_id, tp_order_id=tp_id,
                protection_established=bool(stop_id or tp_id),
                consecutive_errors=consecutive_errors,
            )
            sleep_secs = min(60, max(local_fallback_poll, 2 ** min(consecutive_errors, 5)))

        time.sleep(sleep_secs)


if __name__ == "__main__":
    raise SystemExit(main())
