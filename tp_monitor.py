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
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode
from dotenv import load_dotenv
from risk_controls import account_file_lock, audit_event

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


class _RetryMonitorCycle(RuntimeError):
    """Internal control flow: release account locks before the poll delay."""

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


def get_exchange_position(product_id):
    """Return Delta's real-time size/average-entry snapshot for one product.

    Delta documents ``/v2/positions`` as the real-time endpoint for position
    ownership decisions.  ``/v2/positions/margined`` can lag a fill, which is
    unsafe when a newly added lot needs its protection resized immediately.
    ``None`` means the read was not authoritative; callers must fail closed.
    """
    try:
        product_id = int(product_id)
        path = "/v2/positions"
        params = {"product_id": product_id}
        query = "?" + urlencode(params)
        hdrs = _sign("GET", path, query)
        r = requests.get(
            f"{BASE_URL}{path}", params=params, headers=hdrs, timeout=8,
        )
        data = r.json()
        if not data.get("success"):
            log.warning("Position check failed: %s", data.get("error"))
            return None
        result = data.get("result")
        if isinstance(result, list):
            result = next((
                row for row in result if isinstance(row, dict)
                and str(row.get("product_id")) == str(product_id)
            ), None)
        if not isinstance(result, dict):
            log.warning("Position check returned an invalid result: %r", result)
            return None
        position = dict(result)
        returned_product = position.get("product_id")
        if (returned_product not in (None, "")
                and str(returned_product) != str(product_id)):
            log.warning(
                "Position check returned product %r while %s was requested",
                position.get("product_id"), product_id,
            )
            return None
        # The signed query binds this response to one product. Delta's current
        # single-position payload omits product_id, so fill it only after
        # rejecting any explicit conflict.
        position.setdefault("product_id", product_id)
        return position
    except Exception as e:
        log.warning("Position check error: %s", e)
        return None


def get_exchange_size(product_id):
    """Actual open size on the exchange; 0 if none (or None if check failed)."""
    position = get_exchange_position(product_id)
    if position is None:
        return None
    try:
        size = Decimal(str(position.get("size")))
        if not size.is_finite() or size != size.to_integral_value():
            raise ValueError("position size is not an integer")
        return int(size)
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        log.warning("Position check returned an invalid size: %r", position.get("size"))
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
def place_stop_order(product_id, side, size, stop_price, order_kind="stop_loss_order",
                     client_order_id=None):
    """Reduce-only stop-market: triggers on MARK price (same basis as our
    P&L math). reduce_only guarantees it can only ever close exposure.
    order_kind: 'stop_loss_order' (SL / TSL) or 'take_profit_order' (TP)."""
    client_order_id = client_order_id or _protection_client_order_id(
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


def edit_stop_price(order_id, product_id, stop_price, size=None):
    """Edit one resting stop in place, including its total size when supplied.

    Delta's Edit Order contract supports both ``size`` and ``stop_price``.
    Resizing the existing identity avoids a window with two full-size triggers.
    """
    payload = {"id": order_id, "product_id": product_id,
               "stop_price": f"{stop_price:.1f}"}
    if size is not None:
        payload["size"] = int(size)
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


def get_order_checked(order_id):
    """Return ``(order, conclusive, error)`` for one authenticated order read."""
    try:
        path = f"/v2/orders/{order_id}"
        hdrs = _sign("GET", path)
        r = requests.get(f"{BASE_URL}{path}", headers=hdrs, timeout=10)
        data = r.json()
        if not isinstance(data, dict) or not data.get("success"):
            error = data.get("error") if isinstance(data, dict) else data
            return {}, False, str(error or "order lookup rejected")
        order = data.get("result")
        if not isinstance(order, dict) or not order:
            return {}, True, "order was not found"
        return order, True, ""
    except Exception as e:
        log.warning("Get order %s failed: %s", order_id, e)
        return {}, False, str(e)


def get_order(order_id):
    return get_order_checked(order_id)[0]


def get_order_by_client_id(client_order_id, product_id=None):
    """Return (exact order, conclusive lookup).

    Delta exposes a direct client-order-id endpoint.  It is the idempotency
    authority for response-loss recovery; list pages are not proof of absence.
    ``conclusive=False`` means callers must retain the journalled identity and
    must not submit a different order.
    """
    if not client_order_id:
        return {}, False
    encoded = quote(str(client_order_id), safe="")
    path = f"/v2/orders/client_order_id/{encoded}"
    try:
        raw = requests.get(
            f"{BASE_URL}{path}", headers=_sign("GET", path), timeout=10,
        )
        status_code = getattr(raw, "status_code", None)
        response = raw.json()
        if isinstance(response, dict) and response.get("success"):
            order = response.get("result")
            if not isinstance(order, dict) or not order:
                return {}, False
            if str(order.get("client_order_id") or "") != str(client_order_id):
                log.error("Exact client-order endpoint returned a different identity")
                return {}, False
            if (product_id not in (None, "")
                    and str(order.get("product_id") or "") != str(product_id)):
                log.error("Exact client-order endpoint returned a different product")
                return {}, False
            return order, True
        error = response.get("error") if isinstance(response, dict) else None
        code = error.get("code") if isinstance(error, dict) else error
        code = str(code or "").strip().lower()
        if status_code == 404 or code in {
            "not_found", "order_not_found", "resource_not_found",
        }:
            return {}, True
        log.warning("Client-order lookup %s was rejected: %s", client_order_id,
                    error or response)
        return {}, False
    except Exception as exc:
        log.warning("Client-order lookup %s failed: %s", client_order_id, exc)
        return {}, False


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


def _verify_trend_close_cycle(state, expected_live_size):
    """Prove the original Trend cycle immediately before a close POST.

    Filesystem locks serialize our own monitor/dashboard actions, but an
    external/manual order can close and reopen the same product at the same net
    size without taking that lock.  A real-time fill-ledger proof at the final
    submission boundary prevents a stale local TP/SL decision from reducing
    that unrelated replacement position.
    """
    if SLOT != "trend":
        return True, ""
    position = get_exchange_position(state.get("product_id"))
    if position is None:
        return False, "real-time Trend position is unavailable"
    try:
        live_value = Decimal(str(position.get("size")))
        expected_value = Decimal(str(expected_live_size))
        if (not live_value.is_finite() or not expected_value.is_finite()
                or live_value != live_value.to_integral_value()
                or expected_value != expected_value.to_integral_value()):
            raise ValueError("position size is not integral")
        live_size = int(live_value)
        expected_size = int(expected_value)
    except (InvalidOperation, TypeError, ValueError, OverflowError) as exc:
        return False, f"Trend close size is invalid: {exc}"
    if live_size != expected_size or live_size == 0:
        return False, (
            f"Trend position changed before close submission "
            f"({expected_size} -> {live_size})"
        )
    continuity = _trend_cycle_continuity(state, position)
    if (not continuity.get("verified")
            or continuity.get("status") != "continuous"
            or int(continuity.get("signed_size") or 0) != live_size):
        return False, str(
            continuity.get("reason") or continuity.get("status")
            or "Trend fill-ledger continuity is unverified"
        )
    expected_cycle = str(state.get("position_cycle_id") or "")
    proven_cycle = str(continuity.get("position_cycle_id") or "")
    if expected_cycle and proven_cycle != expected_cycle:
        return False, "Trend position-cycle identity changed before close submission"
    return True, ""


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


_CLOSE_LOCK_DEPTH = 0


@contextmanager
def _close_state_lock(owner, *, stale_after_sec=30, wait_sec=2):
    """Process-local reentrant wrapper for the cross-process slot mutex."""
    global _CLOSE_LOCK_DEPTH
    if _CLOSE_LOCK_DEPTH:
        _CLOSE_LOCK_DEPTH += 1
        try:
            yield True
        finally:
            _CLOSE_LOCK_DEPTH -= 1
        return
    with account_file_lock(
        USER_DIR, f"close-{SLOT}", owner,
        stale_after_sec=stale_after_sec, wait_sec=wait_sec,
    ) as acquired:
        if acquired:
            _CLOSE_LOCK_DEPTH = 1
        try:
            yield acquired
        finally:
            if acquired:
                _CLOSE_LOCK_DEPTH = 0


def save_state_fields(**kw):
    """Persist monitor bookkeeping (peak, armed, resting stop id/floor) into
    the slot state file so restarts resume instead of forgetting the trail."""
    def persist():
        st = load_state()
        st.update(kw)
        _atomic_write_json(STATE_FILE, st)

    try:
        if _CLOSE_LOCK_DEPTH:
            persist()
            return True
        with _close_state_lock(
            f"tp-state-{SLOT}-{os.getpid()}", stale_after_sec=30, wait_sec=2,
        ) as acquired:
            if not acquired:
                log.warning("State persist skipped because the close/protection lock is busy")
                return False
            persist()
            return True
    except Exception as e:
        log.warning("State persist failed: %s", e)
        return False


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


_HEALTH_PROOF_DEFAULTS = {
    # Every heartbeat is a complete snapshot, not a patch over an earlier
    # proof.  Missing evidence is deliberately false/zero so a transient
    # failure can never inherit and relabel stale coverage.
    "exchange_position_size": None,
    "protected_lots": 0,
    "stop_order_lots": 0,
    "tp_order_lots": 0,
    "exchange_protected_lots": 0,
    "unprotected_same_product_lots": None,
    "exchange_protection_complete": False,
    "local_fallback_active": False,
    "local_tp_fallback_active": False,
    "local_stop_fallback_active": False,
    "protection_established": False,
    "continuity_verified": False,
    "continuity_verified_size": None,
    "stop_order_proof": None,
    "tp_order_proof": None,
}


def write_monitor_health(status, *, last_error="", persist_state=True,
                         identity_state=None, **fields):
    """Publish a machine-readable liveness/protection heartbeat.

    The separate file remains readable even when the position state is being
    reconciled.  A compact status is also mirrored into the state file for old
    dashboard/API clients that only inspect that document.
    """
    try:
        heartbeat = _utc_now()
        if not isinstance(identity_state, dict):
            try:
                identity_state = load_state()
            except Exception:
                identity_state = {}
        else:
            # Freeze the exact state generation against which this cycle's
            # evidence was obtained.  A concurrent later revision will then
            # make the heartbeat fail dashboard identity matching, rather
            # than blessing old proof under the newer revision.
            identity_state = dict(identity_state)
        health = {
            **_HEALTH_PROOF_DEFAULTS,
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
            "protection_revision": int(
                identity_state.get("protection_revision") or 0
            ),
            "position_cycle_id": identity_state.get("position_cycle_id"),
            "continuity_revision": int(
                identity_state.get("continuity_revision") or 0
            ),
        }
        health.update(fields)
        # Identity is reserved metadata and may not be overridden by arbitrary
        # proof fields supplied by a caller.
        health.update({
            "product_id": identity_state.get("product_id"),
            "entry_order_id": identity_state.get("order_id")
                              or identity_state.get("entry_order_id"),
            "entry_client_order_id": identity_state.get("client_order_id"),
            "protection_revision": int(
                identity_state.get("protection_revision") or 0
            ),
            "position_cycle_id": identity_state.get("position_cycle_id"),
            "continuity_revision": int(
                identity_state.get("continuity_revision") or 0
            ),
        })
        _atomic_write_json(HEALTH_FILE, health)
        if persist_state:
            current_state = load_state()
            identity_keys = (
                "product_id", "protection_revision", "position_cycle_id",
                "continuity_revision",
            )
            same_identity = all(
                str(current_state.get(key) or "")
                == str(identity_state.get(key) or "")
                for key in identity_keys
            )
            if same_identity:
                save_state_fields(
                    protection_monitor_status=status,
                    protection_heartbeat_utc=heartbeat,
                    protection_last_error=str(last_error or ""),
                    protection_established=bool(
                        health.get("protection_established")
                    ),
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


def _finite_float(value, default=None):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _estimate_option_entry_fee(state, price, lots):
    """Conservative configured fee estimate for an option entry component."""
    price = _finite_float(price, 0.0)
    cv = _finite_float(state.get("contract_value"), 0.0)
    reference = _finite_float(
        state.get("btc_at_entry") or state.get("strike"), 0.0,
    )
    try:
        lots = abs(int(float(lots)))
    except (TypeError, ValueError, OverflowError):
        lots = 0
    if price <= 0 or cv <= 0 or lots <= 0:
        return 0.0
    basis = (min(OPTION_FEE_RATE * reference, OPTION_FEE_CAP_PCT * price)
             if reference > 0 else OPTION_FEE_CAP_PCT * price)
    return max(basis * cv * lots, 0.0)


def _stored_entry_fee_component(state):
    """Return the durable entry fee and how authoritative that value is."""
    source = str(state.get("entry_fee_source") or "").strip().lower()
    for key in ("entry_fee_usd", "entry_fees_usd", "entry_commission_usd",
                "entry_commission", "entry_fee"):
        value = _finite_float(state.get(key))
        if value is not None:
            if not source:
                # Dashboard-created states populate entry_fees_usd directly
                # from the acknowledged entry execution.
                source = "exchange" if key == "entry_fees_usd" else "stored"
            return abs(value), source
    return None, source


def _original_bot_entry_fee_component(state):
    """Return the original bot-entry fee with conservative provenance.

    A post-entry fill ledger can prove every later fee while a legacy state is
    still missing the original entry commission.  In that case the missing
    component must remain pending; treating its numeric fallback as exchange
    evidence would permanently understate realised fees.
    """
    stored_fee, stored_source = _stored_entry_fee_component(state)
    original_fee = _finite_float(state.get("original_bot_entry_fee_usd"), None)
    source = str(
        state.get("original_bot_entry_fee_source") or stored_source or ""
    ).strip().lower()
    if original_fee is None:
        original_fee = stored_fee
    authoritative = bool(
        original_fee is not None
        and source
        and "estimate" not in source
        and "pending" not in source
    )
    return original_fee, source, authoritative


def _adopt_matching_external_trend_lots(state, position, previous_lots, continuity=None):
    """Serialized public wrapper for extending a Trend position aggregate."""
    with _close_state_lock(f"tp-adopt-{os.getpid()}") as acquired:
        if not acquired:
            raise RuntimeError("Trend close/protection lock is unavailable")
        return _adopt_matching_external_trend_lots_locked(
            state, position, previous_lots, continuity,
        )


def _adopt_matching_external_trend_lots_locked(state, position, previous_lots,
                                                continuity=None):
    """Durably extend one Trend cycle to matching externally added lots.

    The exchange's aggregate position is the only safe price basis after a
    same-product top-up.  Product, direction, state generation and unresolved
    close identity are all checked before the state is rebased.  Persistence is
    mandatory: callers may resize exchange orders only after this returns.
    """
    if SLOT != "trend":
        raise RuntimeError("same-product lot adoption is limited to the Trend slot")
    if not isinstance(state, dict) or not isinstance(position, dict):
        raise RuntimeError("position adoption requires valid state and exchange data")
    if str(state.get("status") or "").upper() != "OPEN":
        raise RuntimeError("Trend state is not OPEN")
    if state.get("pending_close_client_order_id") or state.get("pending_close_order_id"):
        raise RuntimeError("an existing close identity is unresolved")

    try:
        product_id = int(state.get("product_id"))
        previous_lots = abs(int(float(previous_lots)))
        live_size = int(float(position.get("size")))
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError("position identity or size is invalid") from exc
    returned_product = position.get("product_id")
    if (returned_product not in (None, "")
            and str(returned_product) != str(product_id)):
        raise RuntimeError("real-time position belongs to a different product")
    expected_sign = -1 if str(state.get("side") or "").lower() == "short" else 1
    if live_size == 0 or live_size * expected_sign <= 0:
        raise RuntimeError("real-time position direction no longer matches Trend")
    new_lots = abs(live_size)
    if previous_lots <= 0 or new_lots <= previous_lots:
        raise RuntimeError("real-time position does not prove an added lot")
    if (not isinstance(continuity, dict) or not continuity.get("verified")
            or continuity.get("status") != "continuous"
            or int(continuity.get("signed_size") or 0) != live_size):
        reason = (continuity or {}).get("reason") if isinstance(continuity, dict) else None
        raise RuntimeError(
            "fill-ledger continuity is not proven" + (f": {reason}" if reason else "")
        )
    entry_mark = _finite_float(position.get("entry_price"), 0.0)
    old_entry_mark = _finite_float(state.get("entry_mark"), 0.0)
    cv = _finite_float(state.get("contract_value"), 0.0)
    if entry_mark <= 0 or old_entry_mark <= 0 or cv <= 0:
        raise RuntimeError("aggregate entry price or contract value is unavailable")

    # Re-read immediately before the durable write. A dashboard close/config
    # action that changed the state generation must win over this adoption.
    latest = load_state()
    try:
        latest_lots = abs(int(float(latest.get("lots") or 0)))
        latest_revision = int(latest.get("protection_revision") or 0)
        expected_revision = int(state.get("protection_revision") or 0)
    except (TypeError, ValueError, OverflowError):
        latest_lots = 0
        latest_revision = expected_revision = -1
    if (str(latest.get("status") or "").upper() != "OPEN"
            or str(latest.get("product_id")) != str(product_id)
            or str(latest.get("side") or "long").lower()
            != str(state.get("side") or "long").lower()
            or latest_lots != previous_lots
            or latest_revision != expected_revision
            or latest.get("pending_close_client_order_id")
            or latest.get("pending_close_order_id")):
        raise RuntimeError("Trend state changed while external lots were being verified")
    state = {**state, **latest}

    added_lots = new_lots - previous_lots
    added_notional = entry_mark * new_lots - old_entry_mark * previous_lots
    added_entry_mark = added_notional / added_lots if added_lots else 0.0
    stored_fee, stored_source = _stored_entry_fee_component(state)
    original_fee_source = str(
        state.get("original_bot_entry_fee_source") or stored_source or ""
    ).strip().lower()
    original_fee_authoritative = bool(
        original_fee_source
        and "estimate" not in original_fee_source
        and "pending" not in original_fee_source
    )
    if stored_fee is None:
        stored_fee = _estimate_option_entry_fee(state, old_entry_mark, previous_lots)
        stored_source = "configured_estimate"
        original_fee_source = "configured_estimate"
        original_fee_authoritative = False
    original_bot_fee = _finite_float(
        state.get("original_bot_entry_fee_usd"), stored_fee,
    )
    previous_estimated_component = _finite_float(
        state.get("entry_fee_estimated_component_usd"), None,
    )
    if previous_estimated_component is None:
        previous_estimated_component = (
            0.0 if stored_source in {"exchange", "stored"} else stored_fee
        )
    added_fee = _estimate_option_entry_fee(state, added_entry_mark, added_lots)
    if not math.isfinite(added_entry_mark) or added_entry_mark <= 0:
        # Multiple manual fills between polls can make a single incremental
        # fill price unknowable. Estimate the complete combined position rather
        # than storing a misleading negative/zero fee component.
        combined_fee = _estimate_option_entry_fee(state, entry_mark, new_lots)
        added_fee = combined_fee
        estimated_component_total = combined_fee
        fee_source = "combined_configured_estimate"
    else:
        combined_fee = stored_fee + added_fee
        estimated_component_total = previous_estimated_component + added_fee
        fee_source = ("mixed_exchange_estimate"
                      if stored_source in {"exchange", "stored", "mixed_exchange_estimate"}
                      else "combined_configured_estimate")

    try:
        original_owned = abs(int(float(
            state.get("original_owned_entry_lots")
            or state.get("owned_entry_lots") or previous_lots
        )))
    except (TypeError, ValueError, OverflowError):
        original_owned = previous_lots
    try:
        adopted_before = abs(int(float(
            state.get("externally_added_lots_adopted") or 0
        )))
    except (TypeError, ValueError, OverflowError):
        adopted_before = 0
    try:
        max_protected = abs(int(float(
            state.get("max_protected_lots") or previous_lots
        )))
    except (TypeError, ValueError, OverflowError):
        max_protected = previous_lots
    try:
        protection_revision = max(int(float(
            state.get("protection_revision") or 0
        )), 0) + 1
    except (TypeError, ValueError, OverflowError):
        protection_revision = 1

    adopted_at = _utc_now()
    events = [dict(item) for item in (state.get("protection_adoptions") or [])
              if isinstance(item, dict)][-49:]
    events.append({
        "at_utc": adopted_at,
        "product_id": product_id,
        "previous_lots": previous_lots,
        "added_lots": added_lots,
        "protected_lots": new_lots,
        "previous_entry_mark": round(old_entry_mark, 8),
        "aggregate_entry_mark": round(entry_mark, 8),
        "entry_basis": "exchange_realtime_aggregate",
    })
    state.update({
        "lots": new_lots,
        "protection_lots": new_lots,
        "max_protected_lots": max(max_protected, new_lots),
        "entry_mark": round(entry_mark, 8),
        "entry_mark_source": "exchange_realtime_aggregate",
        "original_bot_entry_mark": round(float(
            state.get("original_bot_entry_mark") or old_entry_mark
        ), 8),
        "original_bot_entry_fee_usd": round(float(original_bot_fee), 8),
        "original_bot_entry_fee_source": original_fee_source,
        "total_cost_usd": round(entry_mark * cv * new_lots, 2),
        "original_owned_entry_lots": original_owned,
        "owned_entry_lots": int(state.get("owned_entry_lots") or original_owned),
        "externally_added_lots_adopted": adopted_before + added_lots,
        "last_external_lots_added": added_lots,
        "last_external_adoption_utc": adopted_at,
        "external_adoption_notification_pending": True,
        "protection_scope": "trend_plus_same_product_external",
        "position_composition": "mixed_bot_and_external",
        "protection_revision": protection_revision,
        "protection_adoptions": events,
        "entry_fee_usd": round(combined_fee, 8),
        "entry_fees_usd": round(combined_fee, 8),
        "entry_fee_source": fee_source,
        "entry_fee_estimated_component_usd": round(
            estimated_component_total, 8,
        ),
        "fees_usd": round(combined_fee, 8),
        "fees_available": False,
        "fees_complete": False,
        "fees_estimated": True,
        "position_cycle_id": continuity.get("position_cycle_id") or _trend_cycle_id(state),
        "continuity_verified": True,
        "continuity_status": "continuous",
        "continuity_verified_size": live_size,
        "continuity_verified_at_utc": continuity.get("verified_at_utc") or adopted_at,
        "continuity_fill_ids": continuity.get("fill_ids", []),
        "continuity_last_fill_id": continuity.get("last_fill_id"),
        "continuity_revision": int(state.get("continuity_revision") or 0) + 1,
        "cycle_entry_lots_total": continuity.get("cycle_entry_lots_total", new_lots),
        "cycle_exit_lots_total": continuity.get("cycle_exit_lots_total", 0),
        "partial_exit_gross_pnl_usd": continuity.get(
            "partial_exit_gross_pnl_usd", 0.0),
        "partial_exit_fees_usd": continuity.get("partial_exit_fees_usd", 0.0),
        "partial_exit_accounting_status": (
            "complete" if continuity.get("fill_fees_complete") else "fee_pending"
        ),
        "unreconciled_partial_exit_lots": 0,
        # Dollar TSL state belongs to the old, smaller exposure.  The newly
        # composed aggregate starts a fresh protection segment so an inherited
        # peak/floor cannot immediately liquidate the external top-up.
        "tsl_peak": 0.0,
        "tsl_armed": False,
        "tsl_floor": None,
        "stop_kind": "sl",
        "tsl_rebased_at_utc": adopted_at,
        "tsl_rebase_reason": "external_lot_adoption",
    })
    if continuity.get("fill_fees_complete") and original_fee_authoritative:
        exact_entry_fee = float(original_bot_fee) + float(
            continuity.get("added_entry_fees_usd") or 0
        )
        state.update({
            "entry_fee_usd": round(exact_entry_fee, 8),
            "entry_fees_usd": round(exact_entry_fee, 8),
            "entry_fee_source": "exchange_fill_ledger",
            "entry_fee_estimated_component_usd": 0.0,
            "fees_usd": round(exact_entry_fee + float(
                continuity.get("partial_exit_fees_usd") or 0
            ), 8),
            "fees_estimated": False,
        })
    _atomic_write_json(STATE_FILE, state)
    try:
        audit_event(USER_DIR, "trend_external_lots_adopted", {
            "slot": SLOT, "symbol": state.get("symbol"),
            "product_id": product_id, "previous_lots": previous_lots,
            "added_lots": added_lots, "protected_lots": new_lots,
            "previous_entry_mark": old_entry_mark,
            "aggregate_entry_mark": entry_mark,
            "fee_source": fee_source,
        })
    except Exception as exc:
        log.warning("External-lot adoption audit append failed: %s", exc)
    return state


def _rebase_matching_trend_reduction(state, position, previous_lots, continuity=None):
    with _close_state_lock(f"tp-reduction-{os.getpid()}") as acquired:
        if not acquired:
            raise RuntimeError("Trend close/protection lock is unavailable")
        return _rebase_matching_trend_reduction_locked(
            state, position, previous_lots, continuity,
        )


def _rebase_matching_trend_reduction_locked(state, position, previous_lots,
                                             continuity=None, *,
                                             allow_pending_close=False):
    """Persist a fill-ledger-proven partial reduction before order resizing."""
    if SLOT != "trend" or str(state.get("status") or "").upper() != "OPEN":
        raise RuntimeError("only an OPEN Trend cycle can be rebased")
    pending_close = bool(
        state.get("pending_close_client_order_id")
        or state.get("pending_close_order_id")
    )
    if pending_close and not allow_pending_close:
        raise RuntimeError("an existing close identity owns this reduction")
    try:
        product_id = int(state.get("product_id"))
        previous_lots = abs(int(Decimal(str(previous_lots))))
        live_size = int(Decimal(str(position.get("size"))))
    except (InvalidOperation, ValueError, TypeError, OverflowError) as exc:
        raise RuntimeError("reduction identity or size is invalid") from exc
    expected_sign = -1 if str(state.get("side") or "").lower() == "short" else 1
    new_lots = abs(live_size)
    if (live_size * expected_sign <= 0 or new_lots <= 0
            or new_lots >= previous_lots):
        raise RuntimeError("exchange data does not prove a same-cycle partial reduction")
    if (not isinstance(continuity, dict) or not continuity.get("verified")
            or continuity.get("status") != "continuous"
            or int(continuity.get("signed_size") or 0) != live_size):
        reason = (continuity or {}).get("reason") if isinstance(continuity, dict) else None
        raise RuntimeError(
            "fill-ledger continuity is not proven" + (f": {reason}" if reason else "")
        )
    entry_mark = _finite_float(position.get("entry_price"), 0.0)
    old_entry = _finite_float(state.get("entry_mark"), 0.0)
    cv = _finite_float(state.get("contract_value"), 0.0)
    if entry_mark <= 0 or old_entry <= 0 or cv <= 0:
        raise RuntimeError("remaining aggregate entry basis is unavailable")

    latest = load_state()
    try:
        latest_lots = abs(int(Decimal(str(latest.get("lots") or 0))))
        latest_revision = int(latest.get("protection_revision") or 0)
        expected_revision = int(state.get("protection_revision") or 0)
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        raise RuntimeError("persisted Trend generation is invalid")
    if (str(latest.get("status") or "").upper() != "OPEN"
            or str(latest.get("product_id")) != str(product_id)
            or str(latest.get("side") or "long").lower()
            != str(state.get("side") or "long").lower()
            or latest_lots != previous_lots
            or latest_revision != expected_revision
            or (not allow_pending_close and (
                latest.get("pending_close_client_order_id")
                or latest.get("pending_close_order_id")
            ))):
        raise RuntimeError("Trend state changed while the reduction was being verified")
    state = {**state, **latest}
    revision = expected_revision + 1
    reduced_at = _utc_now()
    events = [dict(item) for item in (state.get("protection_reductions") or [])
              if isinstance(item, dict)][-49:]
    events.append({
        "at_utc": reduced_at, "product_id": product_id,
        "previous_lots": previous_lots, "reduced_lots": previous_lots - new_lots,
        "remaining_lots": new_lots, "previous_entry_mark": round(old_entry, 8),
        "remaining_entry_mark": round(entry_mark, 8),
        "continuity_last_fill_id": continuity.get("last_fill_id"),
        "previous_tsl_peak": _finite_float(state.get("tsl_peak"), 0.0),
        "previous_tsl_armed": bool(state.get("tsl_armed")),
        "accounting_status": (
            "complete" if continuity.get("fill_fees_complete") else "fee_pending"
        ),
    })
    original_fee, _original_fee_source, original_fee_authoritative = (
        _original_bot_entry_fee_component(state)
    )
    total_entry_fee = float(original_fee or 0.0) + float(
        continuity.get("added_entry_fees_usd") or 0
    )
    entry_fees_complete = bool(
        continuity.get("fill_fees_complete") and original_fee_authoritative
    )
    state.update({
        "lots": new_lots,
        "protection_lots": new_lots,
        "entry_mark": round(entry_mark, 8),
        "entry_mark_source": "exchange_realtime_aggregate_after_reduction",
        "total_cost_usd": round(entry_mark * cv * new_lots, 2),
        "protection_revision": revision,
        "continuity_revision": int(state.get("continuity_revision") or 0) + 1,
        "continuity_verified": True,
        "continuity_status": "continuous",
        "continuity_verified_size": live_size,
        "continuity_verified_at_utc": continuity.get("verified_at_utc") or reduced_at,
        "continuity_fill_ids": continuity.get("fill_ids", []),
        "continuity_last_fill_id": continuity.get("last_fill_id"),
        "cycle_entry_lots_total": continuity.get("cycle_entry_lots_total"),
        "cycle_exit_lots_total": continuity.get("cycle_exit_lots_total"),
        "partial_exit_gross_pnl_usd": continuity.get(
            "partial_exit_gross_pnl_usd", 0.0),
        "partial_exit_fees_usd": continuity.get("partial_exit_fees_usd", 0.0),
        "partial_exit_accounting_status": (
            "complete" if continuity.get("fill_fees_complete") else "fee_pending"
        ),
        "unreconciled_partial_exit_lots": 0,
        "position_composition": "fungible_mixed_after_reduction",
        "lot_attribution_status": "fungible_after_reduction",
        "last_position_reduction_utc": reduced_at,
        "last_position_reduction_lots": previous_lots - new_lots,
        "protection_reductions": events,
        # Dollar P&L protection applies to the remaining aggregate.  A peak
        # and floor measured on the larger pre-reduction exposure cannot be
        # compared with its smaller remainder, so begin a fresh TSL segment.
        "tsl_peak": 0.0,
        "tsl_armed": False,
        "tsl_floor": None,
        "stop_kind": "sl",
        "tsl_rebased_at_utc": reduced_at,
        "tsl_rebase_reason": "partial_position_reduction",
        "entry_fee_usd": round(total_entry_fee, 8),
        "entry_fees_usd": round(total_entry_fee, 8),
        "entry_fee_source": ("exchange_fill_ledger"
                             if entry_fees_complete
                             else "fill_ledger_fee_pending"),
        "fees_available": entry_fees_complete,
        "fees_complete": False,
        "fees_estimated": not entry_fees_complete,
    })
    _atomic_write_json(STATE_FILE, state)
    try:
        audit_event(USER_DIR, "trend_aggregate_reduced", {
            "slot": SLOT, "symbol": state.get("symbol"), "product_id": product_id,
            "previous_lots": previous_lots, "remaining_lots": new_lots,
            "protection_revision": revision,
            "partial_exit_accounting_status": state["partial_exit_accounting_status"],
        })
    except Exception as exc:
        log.warning("Trend reduction audit append failed: %s", exc)
    return state


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
    partial_status = str(state.get("partial_exit_accounting_status") or "complete")
    unresolved_partial = int(state.get("unreconciled_partial_exit_lots") or 0)
    segment_complete = partial_status == "complete" and unresolved_partial == 0
    state["fees_available"] = (
        segment_complete
        and entry_source in {"exchange", "exchange_fill_ledger"}
        and exit_source == "exchange"
    )
    state["fees_complete"] = segment_complete
    state["fees_estimated"] = not state["fees_available"]
    state["fees_usd"] = total
    state["pnl_usd"] = round(gross_total - total, 2)
    state["pnl_includes_fees"] = True
    previous_cycle_exits = int(state.get("cycle_exit_lots_total") or 0)
    state["cycle_exit_lots_total"] = previous_cycle_exits + lots
    state["accounting_status"] = "complete" if segment_complete else "pending"
    return state["pnl_usd"]


_ACTIVE_ORDER_STATES = {
    "open", "pending", "partially_filled", "partially-filled", "untriggered", "triggered",
}
_TERMINAL_ORDER_STATES = {
    "closed", "filled", "cancelled", "canceled", "rejected", "expired", "failed",
}


def _order_state(order):
    return str((order or {}).get("state") or (order or {}).get("status") or "").lower()


def _integral_lots(value):
    """Parse an exchange quantity without silently truncating fractions."""
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not number.is_finite() or number <= 0 or number != number.to_integral_value():
        return None
    return int(number)


def _nonnegative_integral_lots(value):
    """Parse a zero-or-positive exchange quantity without truncation."""
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not number.is_finite() or number < 0 or number != number.to_integral_value():
        return None
    return int(number)


def _truthy_exchange_flag(value):
    return value is True or str(value).strip().lower() in {"1", "true", "yes"}


def _protection_identity_error(order, *, order_id, client_order_id=None,
                               product_id, close_side, kind,
                               mark_trigger_attested=False):
    """Return why an exchange order is unsafe to inspect/edit as protection."""
    expected_type = "take_profit_order" if kind == "tp" else "stop_loss_order"
    if not isinstance(order, dict) or not order:
        return "authoritative order data is unavailable"
    if str(order.get("id") or "") != str(order_id or ""):
        return "order id does not match the persisted identity"
    if client_order_id and str(order.get("client_order_id") or "") != str(client_order_id):
        return "client order id does not match the persisted identity"
    if str(order.get("product_id") or "") != str(product_id):
        return "order product does not match the Trend position"
    if str(order.get("side") or "").lower() != str(close_side).lower():
        return "order side is not the required reduce direction"
    if not _truthy_exchange_flag(order.get("reduce_only")):
        return "order is not explicitly reduce-only"
    if str(order.get("order_type") or "").lower() != "market_order":
        return "protection order is not stop-market"
    if str(order.get("stop_order_type") or "").lower() != expected_type:
        return f"order is not the expected {expected_type}"
    trigger_method = str(order.get("stop_trigger_method") or "").lower()
    if trigger_method and trigger_method != "mark_price":
        return "order trigger is not mark price"
    if not trigger_method and not (mark_trigger_attested and client_order_id):
        return "order trigger method is unavailable and not durably attested"
    if _order_state(order) not in {"open", "pending"}:
        return f"order is not active ({_order_state(order) or 'unknown'})"
    return ""


def _protection_edit_total_size(order, *, order_id, client_order_id=None,
                                product_id, close_side, kind,
                                desired_remaining_lots,
                                mark_trigger_attested=False):
    """Total size to PUT while preserving any quantity already filled.

    Delta's edit ``size`` is total order size, while ``unfilled_size`` is the
    executable remainder.  If two lots of a six-lot stop filled and four lots
    remain, editing the remainder to four therefore requires total size six,
    not four.
    """
    reason = _protection_identity_error(
        order, order_id=order_id, client_order_id=client_order_id,
        product_id=product_id, close_side=close_side, kind=kind,
        mark_trigger_attested=mark_trigger_attested,
    )
    desired = _integral_lots(desired_remaining_lots)
    total = _integral_lots((order or {}).get("size"))
    unfilled = _nonnegative_integral_lots((order or {}).get("unfilled_size"))
    filled = _nonnegative_integral_lots((order or {}).get("filled_size"))
    if reason:
        return None, reason
    if desired is None or total is None:
        return None, "desired or total order size is invalid"
    if unfilled is None:
        if filled != 0:
            return None, "order remaining size is unavailable"
        return desired, ""
    if total < unfilled:
        return None, "order total size is smaller than its remaining size"
    proven_filled = total - unfilled
    if filled is not None and filled != proven_filled:
        return None, "order filled and remaining quantities are inconsistent"
    return proven_filled + desired, ""


def _protection_order_proof(order, *, order_id, client_order_id=None,
                            product_id, close_side, kind, expected_lots,
                            expected_stop_price=None,
                            mark_trigger_attested=False):
    """Strict, exchange-object proof that one order covers the full aggregate."""
    def failed(reason, *, conclusive=True):
        return {"ok": False, "conclusive": conclusive, "covered_lots": 0,
                "reason": reason, "order": dict(order or {})}

    identity_error = _protection_identity_error(
        order, order_id=order_id, client_order_id=client_order_id,
        product_id=product_id, close_side=close_side, kind=kind,
        mark_trigger_attested=mark_trigger_attested,
    )
    if identity_error:
        return failed(identity_error, conclusive=bool(order))
    size = _integral_lots(order.get("size"))
    expected_lots = _integral_lots(expected_lots)
    if size is None or expected_lots is None:
        return failed("order size or expected aggregate is invalid")
    unfilled = _nonnegative_integral_lots(order.get("unfilled_size"))
    filled = _nonnegative_integral_lots(order.get("filled_size"))
    if unfilled is None:
        # Delta's remaining-size field is the strongest proof.  If an older
        # response omits it, accept only an explicit zero filled quantity;
        # absence of both fields cannot prove full remaining coverage.
        if filled is None:
            return failed("order remaining size is unavailable")
        if filled != 0 or size != expected_lots:
            return failed("order has already filled and no longer covers every lot")
    elif unfilled != expected_lots:
        return failed("order has already filled or no longer covers every lot")
    elif size < unfilled:
        return failed("order total size is smaller than its remaining size")
    elif filled is not None and filled != size - unfilled:
        return failed("order filled and remaining quantities are inconsistent")
    stop_price = _finite_float(order.get("stop_price"), 0.0)
    if stop_price <= 0:
        return failed("order stop price is unavailable")
    if expected_stop_price is not None:
        expected_price = _finite_float(expected_stop_price, 0.0)
        if expected_price <= 0 or abs(stop_price - expected_price) > 0.051:
            return failed("order stop price does not match the current protection basis")
    return {"ok": True, "conclusive": True, "covered_lots": expected_lots,
            "reason": "", "order": dict(order)}


def _parse_utc_datetime(value):
    """Parse Delta ISO or Unix-second/millisecond/microsecond timestamps."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        numeric = Decimal(raw)
        if numeric.is_finite():
            absolute = abs(numeric)
            divisor = (Decimal(1_000_000) if absolute >= 100_000_000_000_000
                       else Decimal(1_000) if absolute >= 100_000_000_000
                       else Decimal(1))
            return datetime.fromtimestamp(
                float(numeric / divisor), tz=timezone.utc,
            )
    except (InvalidOperation, ValueError, TypeError, OSError, OverflowError):
        pass
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
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


def _exchange_time_us(value):
    """Normalize Delta ISO/epoch timestamps to integer microseconds."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        numeric = Decimal(raw)
        if numeric.is_finite():
            number = int(numeric)
            magnitude = abs(number)
            if magnitude >= 100_000_000_000_000:
                return number
            if magnitude >= 100_000_000_000:
                return number * 1_000
            if magnitude >= 1_000_000_000:
                return number * 1_000_000
    except (InvalidOperation, ValueError, TypeError):
        pass
    parsed = _parse_utc_datetime(raw)
    return int(parsed.timestamp() * 1_000_000) if parsed is not None else None


def _trend_cycle_id(state):
    existing = str(state.get("position_cycle_id") or "").strip()
    if existing:
        return existing
    entry_ids = state.get("order_ids") or [
        state.get("order_id") or state.get("entry_order_id") or "legacy"
    ]
    seed = "|".join((
        str(state.get("product_id") or ""),
        str(state.get("entry_date") or ""),
        str(state.get("entry_time_utc") or ""),
        ",".join(str(item) for item in entry_ids if item not in (None, "")),
    ))
    return "trend-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]


def _trend_cycle_anchor_us(state):
    value = state.get("continuity_anchor_utc")
    parsed = _parse_utc_datetime(value) if value else _state_entry_datetime(state)
    if parsed is None:
        return None
    # Include the complete entry second, then exclude the exact bot order IDs.
    return max(int(parsed.timestamp() * 1_000_000) - 1_000_000, 0)


def _trend_fill_ledger_required(state):
    """Whether final accounting must use the complete Trend fill sequence."""
    return SLOT == "trend" and _trend_cycle_anchor_us(state) is not None


def get_trend_cycle_fills(state):
    """Return the complete authenticated fill ledger after this Trend entry.

    Every page must be valid and cursor traversal must finish.  A partial result
    cannot establish position-cycle continuity and therefore raises.
    """
    product_id = int(state.get("product_id"))
    start_us = _trend_cycle_anchor_us(state)
    if start_us is None:
        raise RuntimeError("Trend entry time is unavailable for continuity verification")
    rows = []
    seen_cursors = set()
    after = None
    for _page in range(50):
        params = {"product_ids": str(product_id), "start_time": start_us,
                  "page_size": 50}
        if after:
            params["after"] = after
        path = "/v2/fills"
        query = "?" + urlencode(params)
        try:
            data = requests.get(
                f"{BASE_URL}{path}", params=params,
                headers=_sign("GET", path, query), timeout=10,
            ).json()
        except Exception as exc:
            raise RuntimeError(f"fill history unavailable: {exc}") from exc
        if not isinstance(data, dict) or not data.get("success"):
            error = data.get("error") if isinstance(data, dict) else data
            raise RuntimeError(f"fill history rejected: {error or 'invalid response'}")
        result = data.get("result")
        if isinstance(result, dict):
            result = result.get("fills") or result.get("data")
        if not isinstance(result, list):
            raise RuntimeError("fill history result is not a list")
        rows.extend(result)
        meta = data.get("meta") or {}
        next_after = meta.get("after") if isinstance(meta, dict) else None
        if not next_after:
            break
        next_after = str(next_after)
        if next_after in seen_cursors:
            raise RuntimeError("fill history cursor repeated")
        seen_cursors.add(next_after)
        after = next_after
    else:
        raise RuntimeError("fill history pagination did not terminate")
    return rows


def _trend_cycle_continuity(state, position, fills=None):
    """Reconstruct the signed Trend cycle and detect a close/reopen between polls."""
    if SLOT != "trend":
        return {"verified": True, "status": "not_required"}
    try:
        product_id = int(state.get("product_id"))
        live_decimal = Decimal(str(position.get("size")))
        base_decimal = Decimal(str(
            state.get("original_owned_entry_lots")
            or state.get("owned_entry_lots") or state.get("lots")
        ))
        if (live_decimal != live_decimal.to_integral_value()
                or base_decimal != base_decimal.to_integral_value()):
            raise ValueError("position lots are fractional")
        live_size = int(live_decimal)
        base_lots = abs(int(base_decimal))
        entry_mark = float(state.get("original_bot_entry_mark")
                           or state.get("entry_mark"))
        cv = float(state.get("contract_value"))
    except (InvalidOperation, ValueError, TypeError, OverflowError) as exc:
        return {"verified": False, "status": "invalid_state", "reason": str(exc)}
    expected_sign = -1 if str(state.get("side") or "").lower() == "short" else 1
    if base_lots <= 0 or entry_mark <= 0 or cv <= 0:
        return {"verified": False, "status": "invalid_state",
                "reason": "entry lots, price, or contract value is invalid"}
    try:
        fills = get_trend_cycle_fills(state) if fills is None else list(fills)
    except Exception as exc:
        return {"verified": False, "status": "history_unavailable", "reason": str(exc)}

    entry_order_ids = {
        str(value) for value in [
            state.get("order_id"), state.get("entry_order_id"),
            *(state.get("order_ids") or []),
        ] if value not in (None, "", 0, "0")
    }
    anchor_us = _trend_cycle_anchor_us(state)
    normalized = []
    seen_ids = set()
    for fill in fills:
        if not isinstance(fill, dict):
            return {"verified": False, "status": "malformed_history",
                    "reason": "fill history contains a non-object"}
        if str(fill.get("product_id") or "") != str(product_id):
            continue
        fill_id = str(fill.get("id") or "").strip()
        side = str(fill.get("side") or "").lower()
        size = _integral_lots(fill.get("size"))
        price = _finite_float(fill.get("price"), 0.0)
        created_us = _exchange_time_us(fill.get("created_at"))
        if (not fill_id or fill_id in seen_ids or side not in {"buy", "sell"}
                or size is None or price <= 0 or created_us is None):
            if fill_id and fill_id in seen_ids:
                continue
            return {"verified": False, "status": "malformed_history",
                    "reason": f"fill {fill_id or '<missing>'} is incomplete"}
        seen_ids.add(fill_id)
        if anchor_us is not None and created_us < anchor_us:
            continue
        if str(fill.get("order_id") or "") in entry_order_ids:
            continue
        normalized.append((created_us, fill_id, fill))
    normalized.sort(key=lambda item: (
        item[0], (0, int(item[1])) if item[1].isdigit() else (1, item[1]),
    ))

    net = expected_sign * base_lots
    average = entry_mark
    cycle_entries = base_lots
    cycle_exits = 0
    realized_gross = 0.0
    added_entry_fees = 0.0
    exit_fees = 0.0
    exit_notional = 0.0
    exit_fill_ids = []
    exit_order_ids = []
    last_exit_created_us = None
    fees_complete = True
    first_zero = None
    applied_ids = []
    for created_us, fill_id, fill in normalized:
        size = _integral_lots(fill.get("size"))
        price = float(fill.get("price"))
        delta = size if str(fill.get("side")).lower() == "buy" else -size
        fee = _extract_order_fee(fill)
        if delta * expected_sign > 0:
            if net == 0 or net * expected_sign < 0:
                return {"verified": False, "status": "broken_reopened",
                        "reason": "the original position reached zero before a new entry",
                        "first_zero_fill_id": first_zero, "reopen_fill_id": fill_id}
            old_lots = abs(net)
            average = (average * old_lots + price * size) / (old_lots + size)
            net += delta
            cycle_entries += size
            if fee is None:
                fees_complete = False
            else:
                added_entry_fees += fee
        else:
            if abs(delta) > abs(net) or net * expected_sign <= 0:
                return {"verified": False, "status": "broken_reopened",
                        "reason": "a fill crossed through the original position direction",
                        "first_zero_fill_id": fill_id}
            realized_gross += (price - average) * cv * size * expected_sign
            exit_notional += price * size
            net += delta
            cycle_exits += size
            exit_fill_ids.append(fill_id)
            if fill.get("order_id") not in (None, ""):
                exit_order_ids.append(str(fill.get("order_id")))
            last_exit_created_us = created_us
            if fee is None:
                fees_complete = False
            else:
                exit_fees += fee
            if net == 0:
                first_zero = fill_id
        applied_ids.append(fill_id)

    if net != live_size:
        return {"verified": False, "status": "ledger_position_mismatch",
                "reason": f"fill ledger reconstructs {net} lots, exchange reports {live_size}",
                "ledger_size": net, "exchange_size": live_size}
    live_entry = _finite_float(position.get("entry_price"), 0.0)
    if live_size and (live_entry <= 0 or abs(live_entry - average) > max(0.02, abs(live_entry) * 0.0001)):
        return {"verified": False, "status": "entry_basis_mismatch",
                "reason": f"fill-ledger entry {average:.8f} != exchange {live_entry:.8f}"}
    if first_zero and live_size:
        return {"verified": False, "status": "broken_reopened",
                "reason": "the original position closed and a later fill reopened it",
                "first_zero_fill_id": first_zero}
    return {
        "verified": True,
        "status": "continuous" if live_size else "closed",
        "position_cycle_id": _trend_cycle_id(state),
        "signed_size": net,
        "entry_mark": round(live_entry or average, 8),
        "cycle_entry_lots_total": cycle_entries,
        "cycle_exit_lots_total": cycle_exits,
        "partial_exit_gross_pnl_usd": round(realized_gross, 8),
        "partial_exit_fees_usd": round(exit_fees, 8),
        "exit_mark": (round(exit_notional / cycle_exits, 8)
                      if cycle_exits else None),
        "exit_fill_ids": exit_fill_ids,
        "exit_order_ids": list(dict.fromkeys(exit_order_ids)),
        "last_exit_created_us": last_exit_created_us,
        "added_entry_fees_usd": round(added_entry_fees, 8),
        "fill_fees_complete": fees_complete,
        "fill_ids": applied_ids,
        "last_fill_id": applied_ids[-1] if applied_ids else None,
        "verified_at_utc": _utc_now(),
    }


def _owned_close_lots(state):
    # ``protection_lots`` is the complete aggregate explicitly adopted by this
    # monitor. ``owned_entry_lots`` remains the immutable bot-entry audit count.
    for key in ("protection_lots", "lots", "owned_entry_lots", "entry_lots"):
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
    journal_removed_order_ids = set()
    # A protection POST is journalled before it is submitted.  If the response
    # is lost, the only durable exchange identity may therefore be the client
    # id inside this intent (there is no tsl_stop_order_id/tp_stop_order_id
    # yet).  Cleanup must reconcile that identity as well; otherwise it could
    # report success while a live reduce-only order survives for a later
    # same-product position.
    for intent_key, last_client_key, last_order_key in (
        ("pending_stop_protection",
         "last_pending_stop_protection_client_order_id",
         "last_pending_stop_protection_order_id"),
        ("pending_tp_protection",
         "last_pending_tp_protection_client_order_id",
         "last_pending_tp_protection_order_id"),
    ):
        intent = state.get(intent_key)
        if intent is None:
            continue
        if not isinstance(intent, dict):
            pending.append(f"{intent_key}:malformed")
            log.error("Cannot reconcile malformed %s journal.", intent_key)
            continue
        client_id = str(intent.get("client_order_id") or "").strip()
        intent_product = intent.get("product_id")
        if intent_product in (None, ""):
            intent_product = product_id
        if not client_id:
            pending.append(f"{intent_key}:missing-client-id")
            log.error("Cannot reconcile %s without a client order id.", intent_key)
            continue
        order, conclusive = get_order_by_client_id(client_id, intent_product)
        if not order:
            if conclusive:
                cleared.update({
                    intent_key: None,
                    last_client_key: client_id,
                })
                log.info("Pending protection identity %s is authoritatively absent.",
                         client_id)
            else:
                pending.append(client_id)
                log.error("Pending protection identity %s is still inconclusive.",
                          client_id)
            continue
        order_id = order.get("id")
        returned_product = order.get("product_id")
        cancel_product = (
            returned_product if returned_product not in (None, "")
            else intent_product
        )
        if not order_id or cancel_product in (None, ""):
            pending.append(client_id)
            log.error("Recovered pending protection %s lacks cancellable identity.",
                      client_id)
            continue
        terminal = _order_state(order) in _TERMINAL_ORDER_STATES
        if terminal:
            removed, terminal_state = True, _order_state(order)
        else:
            response = cancel_order(order_id, cancel_product)
            removed, terminal_state = _cancel_confirmed(response, order_id)
        if removed:
            journal_removed_order_ids.add(str(order_id))
            cleared.update({
                intent_key: None,
                last_client_key: client_id,
                last_order_key: order_id,
            })
            log.info("Pending protection order %s/%s removed (%s).",
                     client_id, order_id, terminal_state or "cancelled")
        else:
            pending.append(order_id)
            log.error("Could not remove pending protection order %s; journal retained.",
                      order_id)
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
        if str(order_id) in journal_removed_order_ids:
            log.info("Orphan protection order %s was already removed by its journal.",
                     order_id)
            continue
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
    if (str(record.get("accounting_status") or "").lower()
            in {"pending", "ambiguous", "partial_reduction_unreconciled"}
            or str(record.get("partial_exit_accounting_status") or "complete").lower()
            != "complete"):
        return False
    try:
        if int(record.get("unreconciled_partial_exit_lots") or 0) > 0:
            return False
    except (TypeError, ValueError, OverflowError):
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
            "product_id":   state.get("product_id"),
            "strike":       state.get("strike", 0),
            "lots":         state.get("cycle_entry_lots_total")
                            or state.get("max_protected_lots")
                            or state.get("protection_lots") or state.get("lots")
                            or state.get("owned_entry_lots")
                            or state.get("entry_lots", 0),
            "exit_lots":    state.get("cycle_exit_lots_total")
                            or state.get("closed_lots")
                            or state.get("protection_lots") or state.get("lots", 0),
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
            "bot_entry_lots": state.get("original_owned_entry_lots")
                              or state.get("owned_entry_lots"),
            "externally_added_lots_adopted": state.get(
                "externally_added_lots_adopted", 0),
            "protection_scope": state.get("protection_scope"),
            "position_composition": state.get("position_composition"),
            "protection_adoptions": state.get("protection_adoptions", []),
            "protection_reductions": state.get("protection_reductions", []),
            "partial_exit_accounting_status": state.get(
                "partial_exit_accounting_status", "complete"),
            "unreconciled_partial_exit_lots": state.get(
                "unreconciled_partial_exit_lots", 0),
            "position_cycle_id": state.get("position_cycle_id"),
        }
        rec["accounting_status"] = str(state.get("accounting_status") or "") or (
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


def _finalize_trend_flat_fill_ledger(state, now):
    """Resolve a flat Trend cycle from all fills, including partial exits.

    Returns ``(attempted, complete, error)``.  Once a state has a usable cycle
    anchor, a single terminal order is no longer sufficient accounting proof:
    manual additions/reductions and multiple protection fills are all part of
    the same fungible exchange position.
    """
    if SLOT != "trend" or _trend_cycle_anchor_us(state) is None:
        return False, False, ""
    product_id = state.get("product_id")
    position = get_exchange_position(product_id)
    if position is None:
        return True, False, "real-time flat position could not be reverified"
    try:
        live = Decimal(str(position.get("size")))
        if not live.is_finite() or live != live.to_integral_value() or int(live) != 0:
            return True, False, "Trend fill-ledger finalization requires a zero position"
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        return True, False, "real-time flat position size is malformed"
    continuity = _trend_cycle_continuity(state, position)
    if not continuity.get("verified") or continuity.get("status") != "closed":
        return True, False, str(
            continuity.get("reason") or continuity.get("status")
            or "Trend fill-ledger continuity is unverified"
        )

    added_entry_fees = _finite_float(
        continuity.get("added_entry_fees_usd"), 0.0,
    )
    original_entry_fee, _original_fee_source, original_fee_authoritative = (
        _original_bot_entry_fee_component(state)
    )
    if (state.get("original_bot_entry_fee_usd") in (None, "")
            and original_entry_fee is not None):
        # A legacy aggregate can store the combined entry fee without the
        # original component.  Subtract only ledger-proven additions, while
        # retaining the source authority decision made above.
        original_entry_fee = max(original_entry_fee - added_entry_fees, 0.0)
    gross = _finite_float(continuity.get("partial_exit_gross_pnl_usd"), None)
    exit_fees = _finite_float(continuity.get("partial_exit_fees_usd"), None)
    exit_mark = _finite_float(continuity.get("exit_mark"), None)
    cycle_entries = int(continuity.get("cycle_entry_lots_total") or 0)
    cycle_exits = int(continuity.get("cycle_exit_lots_total") or 0)
    state.update({
        "continuity_verified": True,
        "continuity_status": "closed",
        "continuity_verified_size": 0,
        "continuity_verified_at_utc": continuity.get("verified_at_utc") or _utc_now(),
        "continuity_fill_ids": continuity.get("fill_ids", []),
        "continuity_last_fill_id": continuity.get("last_fill_id"),
        "cycle_entry_lots_total": cycle_entries,
        "cycle_exit_lots_total": cycle_exits,
        "partial_exit_gross_pnl_usd": gross,
        "partial_exit_fees_usd": exit_fees,
        "unreconciled_partial_exit_lots": 0,
    })
    complete = bool(
        continuity.get("fill_fees_complete")
        and original_entry_fee is not None and original_fee_authoritative
        and gross is not None and exit_fees is not None and exit_mark is not None
        and cycle_entries > 0 and cycle_exits == cycle_entries
    )
    if not complete:
        state.update({
            "partial_exit_accounting_status": "fee_pending",
            "accounting_status": "pending",
            "exit_reconciliation_status": "pending_fill_ledger",
        })
        return True, False, "Trend fill ledger is complete but one or more fees are unavailable"

    entry_fees = original_entry_fee + added_entry_fees
    fees = entry_fees + exit_fees
    pnl = gross - fees
    exit_us = continuity.get("last_exit_created_us")
    try:
        exited = datetime.fromtimestamp(int(exit_us) / 1_000_000, tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        exited = now
    exit_order_ids = continuity.get("exit_order_ids") or []
    state.update({
        "status": "CLOSED",
        "exit_date": exited.strftime("%Y-%m-%d"),
        "exit_time_utc": exited.strftime("%H:%M:%S"),
        "exit_at_utc": exited.isoformat().replace("+00:00", "Z"),
        "exit_mark": round(exit_mark, 8),
        "exit_trigger": state.get("exit_trigger") or "closed_externally",
        "exit_order_id": exit_order_ids[-1] if exit_order_ids else None,
        "exit_order_ids": exit_order_ids,
        "exit_client_order_id": (
            state.get("exit_client_order_id")
            or state.get("pending_close_client_order_id")
        ),
        "closed_lots": cycle_exits,
        "gross_pnl_usd": round(gross, 8),
        "entry_fee_usd": round(entry_fees, 8),
        "entry_fees_usd": round(entry_fees, 8),
        "exit_fee_usd": round(exit_fees, 8),
        "exit_fees_usd": round(exit_fees, 8),
        "fees_usd": round(fees, 8),
        "pnl_usd": round(pnl, 2),
        "pnl_includes_fees": True,
        "fees_available": True,
        "fees_complete": True,
        "fees_estimated": False,
        "entry_fee_source": "exchange_fill_ledger",
        "exit_fee_source": "exchange_fill_ledger",
        "partial_exit_accounting_status": "complete",
        "accounting_status": "complete",
        "exit_reconciliation_status": "resolved_fill_ledger",
        "exit_reconciliation_error": "",
        "exit_history_lookup_conclusive": True,
        "pending_close_order_id": None,
        "pending_close_client_order_id": None,
        "pending_close_reason": "",
        "pending_close_state": "confirmed_flat",
        "pending_close_error": "",
        "history_pending": True,
        "history_logged": False,
    })
    _atomic_write_json(STATE_FILE, state)
    append_history(state)
    log.info(
        "Flat Trend cycle reconciled from %d fills; net P&L $%.2f.",
        len(continuity.get("fill_ids") or []), pnl,
    )
    return True, True, ""


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
    ledger_attempted, ledger_complete, ledger_error = (
        _finalize_trend_flat_fill_ledger(state, now)
    )
    if ledger_complete:
        return True
    if ledger_attempted:
        order, conclusive, error = {}, False, ledger_error
    else:
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
                "closed_lots": int(state.get("cycle_exit_lots_total")
                                   or state.get("max_protected_lots") or lots),
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
        "closed_lots": int(state.get("max_protected_lots") or lots),
        "exit_detected_at_utc": state.get("exit_detected_at_utc"),
        "exit_reconciliation_status": (
            "pending_fill_ledger" if ledger_attempted else "pending_order_history"
        ),
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
    with _close_state_lock(f"tp-external-close-{os.getpid()}") as acquired:
        if not acquired:
            log.warning("Another %s close reconciliation is in progress.", SLOT)
            return False
        return _finalize_external_flat_close_locked(state)


def _finalize_confirmed_market_close_locked(state, order, mark, lots, reason):
    """Finalize accounting only after exchange position size is verified zero."""
    latest = load_state()
    if isinstance(latest, dict):
        state = {**state, **latest}
    if _trend_fill_ledger_required(state):
        # A terminal market order proves only its own fill.  An adopted Trend
        # aggregate may also contain manual additions and earlier reductions,
        # so its complete cycle must be reconstructed from fills before any
        # realised row is declared final.
        ledger_state = dict(state)
        ledger_state["exit_trigger"] = (
            state.get("exit_trigger") or f"{reason}_{SLOT}"
        )
        return _finalize_external_flat_close_locked(ledger_state)
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
        "closed_lots": int(state.get("cycle_exit_lots_total")
                           or state.get("max_protected_lots")
                           or state.get("protection_lots") or expected_lots),
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
    with _close_state_lock(f"tp-market-finalize-{os.getpid()}") as acquired:
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
    with _close_state_lock(f"tp-close-{os.getpid()}") as acquired:
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

    cycle_safe, cycle_error = _verify_trend_close_cycle(state, live_size)
    if not cycle_safe:
        if pending_client_id or pending_order_id:
            _persist_close_fields(
                state, pending_close_state="cycle_unverified",
                pending_close_error=cycle_error,
                pending_close_live_size=int(live_size),
                pending_close_last_reconciled_utc=_utc_now(),
            )
        log.error("Trend close blocked at cycle boundary: %s", cycle_error)
        return False

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

    # Re-prove after the durable-intent write and immediately before the POST.
    # An external same-product close/reopen does not honor our filesystem lock.
    cycle_safe, cycle_error = _verify_trend_close_cycle(state, live_size)
    if not cycle_safe:
        _persist_close_fields(
            state, pending_close_state="cycle_unverified",
            pending_close_error=cycle_error,
            pending_close_live_size=int(live_size),
            pending_close_last_reconciled_utc=_utc_now(),
        )
        log.error("Trend close POST blocked by final cycle proof: %s", cycle_error)
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
            accounting_incomplete = not _history_accounting_complete(state)
            if (accounting_incomplete and (
                    state.get("exit_trigger") == "closed_externally"
                    or _trend_fill_ledger_required(state))):
                # Dashboard square-off can deliberately leave a Trend close in
                # pending_fill_ledger when another fill raced its market order
                # or an order-level fee was unavailable.  That state is just as
                # eligible for fill-ledger reconciliation as an externally
                # detected flat; keying only on exit_trigger stranded it.
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
    monitor_entry_order_id = str(
        state.get("order_id") or state.get("entry_order_id") or ""
    )
    monitor_entry_client_id = str(state.get("client_order_id") or "")
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
    tp_complete = False
    stop_complete = False

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

    def protection_proof(order_id, kind, expected_lots, expected_price,
                         client_order_id=None):
        if not order_id:
            return {"ok": False, "conclusive": True, "covered_lots": 0,
                    "reason": "no persisted order identity", "order": {}}
        identity = load_state()
        trigger_key = "tp_trigger_method" if kind == "tp" else "stop_trigger_method"
        attested = (
            str(identity.get(trigger_key) or "").lower() == "mark_price"
        )
        return _protection_order_proof(
            get_order(order_id), order_id=order_id,
            client_order_id=client_order_id, product_id=product_id,
            close_side=close_side, kind=kind, expected_lots=expected_lots,
            expected_stop_price=expected_price,
            mark_trigger_attested=attested,
        )

    def place_verified_protection(kind, price):
        """Journal, place, fetch, validate, then durably adopt one new order."""
        order_kind = "take_profit_order" if kind == "tp" else "stop_loss_order"
        intent_key = "pending_tp_protection" if kind == "tp" else "pending_stop_protection"
        existing_intent = load_state().get(intent_key)
        if existing_intent and not isinstance(existing_intent, dict):
            return {}, {"success": False, "error": f"{kind} protection intent is malformed"}
        if existing_intent:
            expected = {
                "product_id": str(product_id), "side": close_side,
                "lots": str(lots), "stop_order_type": order_kind,
                "stop_trigger_method": "mark_price",
            }
            actual = {
                "product_id": str(existing_intent.get("product_id")),
                "side": str(existing_intent.get("side") or "").lower(),
                "lots": str(existing_intent.get("lots")),
                "stop_order_type": str(existing_intent.get("stop_order_type") or ""),
                "stop_trigger_method": str(
                    existing_intent.get("stop_trigger_method") or ""
                ).lower(),
            }
            intent_price = _finite_float(existing_intent.get("stop_price"), 0.0)
            client_id = str(existing_intent.get("client_order_id") or "")
            if not client_id:
                return {}, {"success": False, "error": f"{kind} intent has no client id"}
            same_generation = (
                actual == expected and abs(intent_price - price) <= 0.051
            )
            recovered, conclusive = get_order_by_client_id(client_id, product_id)
            if recovered:
                recovered_id = recovered.get("id")
                if same_generation:
                    proof = _protection_order_proof(
                        get_order(recovered_id), order_id=recovered_id,
                        client_order_id=client_id, product_id=product_id,
                        close_side=close_side, kind=kind, expected_lots=lots,
                        expected_stop_price=price,
                        mark_trigger_attested=(
                            actual.get("stop_trigger_method") == "mark_price"
                        ),
                    )
                    if proof["ok"]:
                        return proof["order"], {
                            "success": True, "result": proof["order"],
                        }
                else:
                    proof = {"reason": "intent targets a prior protection generation"}
                if _order_state(recovered) not in _TERMINAL_ORDER_STATES:
                    cancelled = cancel_order(recovered_id, product_id)
                    removed, cancel_status = _cancel_confirmed(cancelled, recovered_id)
                    if not removed:
                        return {}, {
                            "success": False,
                            "error": (f"recovered {kind} order {recovered_id} could not be "
                                      f"retired ({cancel_status or proof['reason']})"),
                        }
                if not save_state_fields(**{intent_key: None}):
                    return {}, {"success": False,
                                "error": f"retired {kind} intent could not be cleared"}
                existing_intent = None
            if not conclusive:
                return {}, {"success": False,
                            "error": f"{kind} intent lookup is inconclusive"}
            if existing_intent and same_generation:
                # Explicit absence: retry the same durable identity so a lost
                # response can never create a second order.
                intent = existing_intent
            else:
                if existing_intent and not save_state_fields(**{intent_key: None}):
                    return {}, {"success": False,
                                "error": f"old {kind} intent could not be cleared"}
                existing_intent = None
        if not existing_intent:
            client_id = _protection_client_order_id(kind)
            intent = {
                "client_order_id": client_id, "product_id": product_id,
                "side": close_side, "lots": lots, "stop_price": price,
                "stop_order_type": order_kind,
                "stop_trigger_method": "mark_price",
                "created_at_utc": _utc_now(),
            }
            if not save_state_fields(**{intent_key: intent}):
                return {}, {"success": False, "error": "protection intent was not durable"}
        else:
            client_id = str(intent.get("client_order_id") or client_id)
        response = {}
        try:
            response = place_stop_order(
                product_id, close_side, lots, price, order_kind,
                client_order_id=client_id,
            )
        except Exception as exc:
            response = {"success": False, "error": f"submit exception: {exc}"}
        response_order = response.get("result") or {} if isinstance(response, dict) else {}
        new_id = response_order.get("id") if isinstance(response_order, dict) else None
        if not new_id:
            recovered, conclusive = get_order_by_client_id(client_id, product_id)
            if recovered:
                response_order = recovered
                new_id = recovered.get("id")
            elif not conclusive:
                save_state_fields(exchange_protection_error=(
                    f"{kind} submit outcome is unresolved for client {client_id}"
                ))
                return {}, response
        if not new_id:
            # Keep the durable identity even after an explicit absence.  A
            # later retry reuses this same client id; it never mints a second
            # identity merely because the submit response was incomplete.
            return {}, response
        proof = _protection_order_proof(
            get_order(new_id), order_id=new_id, client_order_id=client_id,
            product_id=product_id, close_side=close_side, kind=kind,
            expected_lots=lots, expected_stop_price=price,
            mark_trigger_attested=True,
        )
        if not proof["ok"]:
            cancelled = cancel_order(new_id, product_id)
            removed, _ = _cancel_confirmed(cancelled, new_id)
            if not removed:
                _remember_orphan(new_id)
            update = {
                "exchange_protection_error": (
                    f"new {kind} order {new_id} failed verification: {proof['reason']}"
                ),
            }
            # Only a confirmed cancellation permits the journalled identity
            # to be retired. Otherwise the next poll must reconcile the same
            # client id and may not place another order alongside it.
            if removed:
                update[intent_key] = None
            save_state_fields(**update)
            return {}, response
        return proof["order"], response

    def retire_legacy_trigger_identity(order, *, order_id, client_order_id,
                                       kind, id_key, client_key, lots_key,
                                       trigger_key, last_key, status_key):
        """Cancel a pre-attestation protection order before replacing it.

        Older releases always submitted mark-price stops but did not persist
        that trigger choice, and Delta's order read currently omits the field.
        We must not simply bless missing evidence.  Once the exact persisted
        order is proven to have our protection-client provenance and every
        other safety attribute, cancel it conclusively, clear its identity,
        and let the normal journalled placement path create an attested order.
        """
        trigger_method = str((order or {}).get("stop_trigger_method") or "").lower()
        state_trigger = str(load_state().get(trigger_key) or "").lower()
        if state_trigger or trigger_method == "mark_price":
            return False
        client_order_id = str(client_order_id or "")
        returned_client_id = str((order or {}).get("client_order_id") or "")
        if (not client_order_id.startswith("nithi-tp-")
                or returned_client_id != client_order_id):
            return False
        identity_error = _protection_identity_error(
            order, order_id=order_id, client_order_id=client_order_id,
            product_id=product_id, close_side=close_side, kind=kind,
            mark_trigger_attested=False,
        )
        if identity_error not in {
                "order trigger method is unavailable and not durably attested",
                "order trigger is not mark price"}:
            return False
        response = cancel_order(order_id, product_id)
        removed, terminal_state = _cancel_confirmed(response, order_id)
        if not removed:
            save_state_fields(exchange_protection_error=(
                f"legacy {kind} order {order_id} could not be retired safely"
            ))
            return False
        retired = save_state_fields(**{
            id_key: None,
            client_key: None,
            lots_key: 0,
            trigger_key: None,
            last_key: order_id,
            status_key: terminal_state or "cancelled_for_trigger_attestation",
            "exchange_protection_error": "",
        })
        if not retired:
            # The exchange order is gone, but without a durable cleared state
            # this cycle may not mint another identity. Reconciliation on the
            # next poll will observe the terminal order and recover safely.
            return False
        log.warning(
            "Retired legacy %s protection order %s so its replacement can "
            "carry durable mark-price attestation.", kind, order_id,
        )
        return True

    def ensure_stop(floor_usd, kind):
        nonlocal stop_id, stop_floor, stop_kind, stop_lots, local_fallback_active
        if exch_unsupported:
            return False
        price = floor_price(floor_usd)
        tag = kind.upper()
        if stop_id:
            stop_client_id = load_state().get("stop_client_order_id")
            current_order = get_order(stop_id)
            edit_total, edit_error = _protection_edit_total_size(
                current_order, order_id=stop_id,
                client_order_id=stop_client_id, product_id=product_id,
                close_side=close_side, kind="stop",
                desired_remaining_lots=lots,
                mark_trigger_attested=(
                    str(load_state().get("stop_trigger_method") or "").lower()
                    == "mark_price"
                ),
            )
            if edit_total is None:
                if retire_legacy_trigger_identity(
                        current_order, order_id=stop_id,
                        client_order_id=stop_client_id, kind="stop",
                        id_key="tsl_stop_order_id",
                        client_key="stop_client_order_id", lots_key="stop_lots",
                        trigger_key="stop_trigger_method",
                        last_key="last_tsl_stop_order_id",
                        status_key="stop_order_state"):
                    stop_id, stop_lots = None, 0
                    return ensure_stop(floor_usd, kind)
                local_fallback_active = True
                save_state_fields(
                    exchange_protection_error=f"stop edit blocked: {edit_error}",
                )
                alert_once(
                    "stop_edit_identity_unverified",
                    f"{symbol}: exchange stop identity/quantity is unverified; "
                    "local fallback is active",
                )
                return False
            try:
                response = edit_stop_price(
                    stop_id, product_id, price, size=edit_total,
                )
            except Exception as exc:
                response = {"success": False, "error": str(exc)}
            proof = protection_proof(
                stop_id, "stop", lots, price, stop_client_id,
            )
            if proof["ok"]:
                stop_floor, stop_kind = floor_usd, kind
                if not save_state_fields(
                    tsl_peak=round(peak_pnl, 2), tsl_armed=tsl_armed,
                    tsl_floor=round(floor_usd, 2), tsl_stop_order_id=stop_id,
                    stop_kind=kind, stop_lots=lots, stop_order_state=_order_state(
                        proof["order"]),
                    stop_trigger_method="mark_price",
                    exchange_protection_supported=True, exchange_protection_error="",
                ):
                    local_fallback_active = True
                    return False
                stop_lots = lots
                log.info("%s stop ratcheted to %.1f (floor $%.2f, order %s).",
                         tag, price, floor_usd, stop_id)
                return True
            error = proof["reason"] or response.get("error", response)
            local_fallback_active = True
            log.warning("Stop edit/verification failed: %s; existing identity retained.", error)
            save_state_fields(exchange_protection_error=f"stop verification failed: {error}")
            alert_once("stop_verification_failed",
                       f"{symbol}: full-size exchange stop is unverified; local fallback is active")
            return False
        response_order, response = place_verified_protection("stop", price)
        new_id = response_order.get("id") if response_order else None
        if not new_id:
            local_fallback_active = True
            if not _mark_unsupported(response, "stop order"):
                error = response.get("error", response) if isinstance(response, dict) else response
                log.warning("Stop placement failed: %s; local trigger remains active.", error)
                alert_once("stop_placement_failed",
                           f"{symbol}: exchange stop could not be established; local fallback is active")
            return False
        if not save_state_fields(
            tsl_peak=round(peak_pnl, 2), tsl_armed=tsl_armed,
            tsl_floor=round(floor_usd, 2), tsl_stop_order_id=new_id,
            stop_kind=kind, stop_lots=lots, stop_order_state="open",
            stop_client_order_id=response_order.get("client_order_id"),
            stop_trigger_method="mark_price",
            pending_stop_protection=None,
            exchange_protection_supported=True, exchange_protection_error="",
        ):
            cancelled = cancel_order(new_id, product_id)
            removed, _ = _cancel_confirmed(cancelled, new_id)
            if not removed:
                _remember_orphan(new_id)
            local_fallback_active = True
            return False
        stop_id, stop_floor, stop_kind, stop_lots = new_id, floor_usd, kind, lots
        log.info("%s stop placed: %s %d lots @ %.1f (floor $%.2f, order %s).",
                 tag, close_side.upper(), lots, price, floor_usd, stop_id)
        return True

    def retire_disabled_stop():
        """Conclusive cleanup for a stop left from a now-disabled policy."""
        nonlocal stop_id, stop_lots, stop_floor, stop_kind, local_fallback_active
        if not stop_id:
            return True
        identity = load_state()
        client_id = identity.get("stop_client_order_id")
        order = get_order(stop_id)
        order_state = _order_state(order)
        if order_state in _TERMINAL_ORDER_STATES:
            removed, terminal_state = True, order_state
        else:
            identity_error = _protection_identity_error(
                order, order_id=stop_id, client_order_id=client_id,
                product_id=product_id, close_side=close_side, kind="stop",
                mark_trigger_attested=(
                    str(identity.get("stop_trigger_method") or "").lower()
                    == "mark_price"
                ),
            )
            if identity_error:
                if retire_legacy_trigger_identity(
                        order, order_id=stop_id, client_order_id=client_id,
                        kind="stop", id_key="tsl_stop_order_id",
                        client_key="stop_client_order_id", lots_key="stop_lots",
                        trigger_key="stop_trigger_method",
                        last_key="last_tsl_stop_order_id",
                        status_key="stop_order_state"):
                    stop_id, stop_lots, stop_floor, stop_kind = None, 0, 0.0, "sl"
                    return True
                save_state_fields(exchange_protection_error=(
                    f"disabled stop cleanup blocked: {identity_error}"
                ))
                local_fallback_active = True
                return False
            response = cancel_order(stop_id, product_id)
            removed, terminal_state = _cancel_confirmed(response, stop_id)
        if not removed:
            save_state_fields(exchange_protection_error=(
                f"disabled stop order {stop_id} could not be retired safely"
            ))
            local_fallback_active = True
            return False
        retired_id = stop_id
        if not save_state_fields(
                tsl_stop_order_id=None, stop_client_order_id=None,
                stop_lots=0, stop_trigger_method=None,
                last_tsl_stop_order_id=retired_id,
                stop_order_state=terminal_state or "cancelled_policy_disabled",
                tsl_floor=None, stop_kind="sl", exchange_protection_error=""):
            local_fallback_active = True
            return False
        stop_id, stop_lots, stop_floor, stop_kind = None, 0, 0.0, "sl"
        log.info("Retired stop order %s because both SL and TSL are disabled.", retired_id)
        return True

    def ensure_tp(force=False):
        nonlocal tp_id, tp_lots, local_fallback_active
        if exch_unsupported:
            return False
        price = max(round(entry_mark + sign * target_pnl / (cv * lots), 1), 0.1)
        if tp_id and tp_lots == lots and not force:
            current = protection_proof(
                tp_id, "tp", lots, price, load_state().get("tp_client_order_id"),
            )
            if current["ok"]:
                return True
        if tp_id:
            tp_client_id = load_state().get("tp_client_order_id")
            current_order = get_order(tp_id)
            edit_total, edit_error = _protection_edit_total_size(
                current_order, order_id=tp_id,
                client_order_id=tp_client_id, product_id=product_id,
                close_side=close_side, kind="tp",
                desired_remaining_lots=lots,
                mark_trigger_attested=(
                    str(load_state().get("tp_trigger_method") or "").lower()
                    == "mark_price"
                ),
            )
            if edit_total is None:
                if retire_legacy_trigger_identity(
                        current_order, order_id=tp_id,
                        client_order_id=tp_client_id, kind="tp",
                        id_key="tp_stop_order_id",
                        client_key="tp_client_order_id", lots_key="tp_lots",
                        trigger_key="tp_trigger_method",
                        last_key="last_tp_stop_order_id",
                        status_key="tp_order_state"):
                    tp_id, tp_lots = None, 0
                    return ensure_tp(force=True)
                local_fallback_active = True
                save_state_fields(
                    exchange_protection_error=f"TP edit blocked: {edit_error}",
                )
                alert_once(
                    "tp_edit_identity_unverified",
                    f"{symbol}: exchange TP identity/quantity is unverified; "
                    "local fallback is active",
                )
                return False
            try:
                response = edit_stop_price(
                    tp_id, product_id, price, size=edit_total,
                )
            except Exception as exc:
                response = {"success": False, "error": str(exc)}
            proof = protection_proof(
                tp_id, "tp", lots, price, tp_client_id,
            )
            if proof["ok"]:
                if not save_state_fields(
                    tp_stop_order_id=tp_id, tp_lots=lots,
                    tp_order_state=_order_state(proof["order"]),
                    tp_trigger_method="mark_price",
                    exchange_protection_supported=True,
                    exchange_protection_error="",
                ):
                    local_fallback_active = True
                    return False
                tp_lots = lots
                return True
            error = proof["reason"] or response.get("error", response)
            local_fallback_active = True
            save_state_fields(exchange_protection_error=f"TP verification failed: {error}")
            alert_once("tp_verification_failed",
                       f"{symbol}: full-size exchange TP is unverified; local fallback is active")
            return False
        response_order, response = place_verified_protection("tp", price)
        new_id = response_order.get("id") if response_order else None
        if not new_id:
            local_fallback_active = True
            if not _mark_unsupported(response, "take-profit order"):
                error = response.get("error", response) if isinstance(response, dict) else response
                alert_once("tp_placement_failed",
                           f"{symbol}: exchange TP could not be established; local fallback is active")
                log.warning("TP placement failed: %s", error)
            return False
        if not save_state_fields(
            tp_stop_order_id=new_id, tp_lots=lots, tp_order_state="open",
            tp_client_order_id=response_order.get("client_order_id"),
            tp_trigger_method="mark_price",
            pending_tp_protection=None, exchange_protection_supported=True,
            exchange_protection_error="",
        ):
            cancelled = cancel_order(new_id, product_id)
            removed, _ = _cancel_confirmed(cancelled, new_id)
            if not removed:
                _remember_orphan(new_id)
            local_fallback_active = True
            return False
        tp_id, tp_lots = new_id, lots
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
        with _close_state_lock(f"tp-stop-finalize-{os.getpid()}") as acquired:
            if not acquired:
                raise RuntimeError("another close reconciliation is in progress")
            return finalize_stop_fill_locked(order, kind)

    def finalize_stop_fill_locked(order, kind):
        closed_state = load_state()
        exit_trigger = {"tp": f"take_profit_{SLOT}",
                        "tsl": f"trailing_stop_{SLOT}"}.get(
                            kind, f"stop_loss_{SLOT}")
        if _trend_fill_ledger_required(closed_state):
            # The protection order is one segment of a potentially mixed,
            # partially reduced Trend cycle.  Do not finalize the cycle from
            # this one terminal order even when it filled the currently stored
            # remainder.
            ledger_state = dict(closed_state)
            ledger_state["exit_trigger"] = (
                closed_state.get("exit_trigger") or exit_trigger
            )
            return _finalize_external_flat_close_locked(ledger_state)
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
            "closed_lots": int(closed_state.get("cycle_exit_lots_total")
                               or closed_state.get("max_protected_lots")
                               or closed_state.get("protection_lots") or done),
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
            try:
                state_product_id = int(state.get("product_id"))
            except (TypeError, ValueError, OverflowError):
                state_product_id = 0
            state_entry_order_id = str(
                state.get("order_id") or state.get("entry_order_id") or ""
            )
            state_entry_client_id = str(state.get("client_order_id") or "")
            identity_changed = (
                state_product_id != int(product_id)
                or (monitor_entry_order_id and state_entry_order_id
                    and state_entry_order_id != monitor_entry_order_id)
                or (monitor_entry_client_id and state_entry_client_id
                    and state_entry_client_id != monitor_entry_client_id)
            )
            if identity_changed:
                write_monitor_health(
                    "stale", last_error="slot now belongs to a different position identity",
                    identity_state=state, state_status=state.get("status"),
                    protection_established=False,
                )
                log.warning("Position identity changed underneath this monitor; exiting.")
                return 0

            # Dashboard square-off can persist a partial reduction while this
            # worker is sleeping.  Reload the resulting aggregate before any
            # comparison or order edit; otherwise the old lot count can make
            # the fill-ledger generation fail forever and leave oversized
            # protection behind.
            try:
                persisted_lots_value = Decimal(str(state.get("lots")))
                if (not persisted_lots_value.is_finite()
                        or persisted_lots_value != persisted_lots_value.to_integral_value()):
                    raise ValueError("persisted lots are not integral")
                persisted_lots = abs(int(persisted_lots_value))
                persisted_entry = float(state.get("entry_mark"))
                persisted_cv = float(state.get("contract_value", cv))
                persisted_sign = (
                    -1 if str(state.get("side") or "").lower() == "short" else 1
                )
                if (persisted_lots <= 0 or not math.isfinite(persisted_entry)
                        or persisted_entry <= 0 or not math.isfinite(persisted_cv)
                        or persisted_cv <= 0):
                    raise ValueError("invalid persisted position dimensions")
            except (InvalidOperation, TypeError, ValueError, OverflowError):
                persisted_lots = 0
                persisted_entry = 0.0
                persisted_cv = 0.0
                persisted_sign = 0
            if str(state.get("status") or "").upper() == "OPEN":
                if not persisted_lots or persisted_sign != sign:
                    write_monitor_health(
                        "degraded", last_error="persisted OPEN position dimensions are invalid",
                        identity_state=state, state_status=state.get("status"),
                        protection_established=False,
                    )
                    sleep_secs = local_fallback_poll
                    raise _RetryMonitorCycle()
                if persisted_lots != lots:
                    lots = persisted_lots
                    entry_mark = persisted_entry
                    cv = persisted_cv
                    peak_pnl = float(state.get("tsl_peak") or 0.0)
                    persist_pk = peak_pnl
                    tsl_armed = bool(state.get("tsl_armed"))
                    stop_floor = float(state.get("tsl_floor") or 0.0)
                    stop_kind = state.get("stop_kind") or (
                        "tsl" if tsl_armed else "sl"
                    )
                    log.info(
                        "Reloaded persisted aggregate after an external state update: "
                        "%d lots at %.4f.", lots, entry_mark,
                    )
                else:
                    entry_mark = persisted_entry
                    cv = persisted_cv
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

            if str(state.get("status") or "").upper() == "OWNERSHIP_AMBIGUOUS":
                with _close_state_lock(
                    f"tp-ambiguous-cleanup-{os.getpid()}"
                ) as acquired:
                    removed = bool(acquired) and remove_exchange_protection(
                        state, explicit=True,
                        reason="original Trend cycle closed and a new position reopened",
                    )
                write_monitor_health(
                    "ownership_ambiguous", state_status=state.get("status"),
                    last_error=str(state.get("continuity_error") or
                                   "position-cycle continuity was broken"),
                    continuity_verified=False,
                    continuity_status=state.get("continuity_status"),
                    exchange_position_size=state.get("remaining_external_position_lots"),
                    protection_established=False,
                    protection_cleanup_pending=not removed,
                )
                if removed:
                    return 0
                time.sleep(local_fallback_poll)
                continue

            # Re-sync ids written by a previous monitor or recovery action.
            stop_id = state.get("tsl_stop_order_id")
            tp_id = state.get("tp_stop_order_id")
            stop_lots = int(state.get("stop_lots") or stop_lots or 0)
            tp_lots = int(state.get("tp_lots") or tp_lots or 0)
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
                if (state.get("pending_close_client_order_id")
                        or state.get("pending_close_order_id")):
                    # A journalled market-close identity owns this flat outcome.
                    # Reconcile it before considering a generic external close
                    # or cancelling protection, otherwise its exact fill and
                    # fee evidence can be discarded.
                    reconciled = close_position(
                        state,
                        _finite_float(state.get("entry_mark"), 0.0),
                        0.0,
                        state.get("pending_close_reason") or "take_profit",
                    )
                    if not reconciled:
                        write_monitor_health(
                            "reconciling",
                            last_error="flat position has an unresolved close identity",
                            state_status=state.get("status"),
                            exchange_position_size=0,
                            protection_established=False,
                        )
                        time.sleep(local_fallback_poll)
                        continue
                    state = load_state()
                    done_kind = "pending_close"
                else:
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

            # All state rebasing and protection-order mutations share the same
            # lock as dashboard square-off. The real-time position is re-read
            # inside that lock so a close intent always wins a race.
            with _close_state_lock(
                f"tp-protection-{os.getpid()}"
            ) as protection_lock:
                if not protection_lock:
                    write_monitor_health(
                        "degraded", last_error="close/protection lock is busy; no order was changed",
                        state_status=state.get("status"), exchange_position_size=live,
                        continuity_verified=False, protection_established=False,
                    )
                    sleep_secs = local_fallback_poll
                    raise _RetryMonitorCycle()
                state = load_state()
                if state.get("status") != "OPEN":
                    write_monitor_health(
                        "closing", last_error="state changed while protection was being updated",
                        state_status=state.get("status"), exchange_position_size=live,
                        protection_established=False,
                    )
                    sleep_secs = local_fallback_poll
                    raise _RetryMonitorCycle()
                # Another dashboard/bot supervisor can start a second worker
                # at the same instant.  Its previous loop snapshot may predate
                # the first worker's journalled TP/SL placement.  Re-sync every
                # durable identity *after* acquiring the shared mutation lock;
                # otherwise the stale worker could mint a duplicate order.
                locked_entry_order_id = str(
                    state.get("order_id") or state.get("entry_order_id") or ""
                )
                locked_entry_client_id = str(state.get("client_order_id") or "")
                if (str(state.get("product_id") or "") != str(product_id)
                        or (monitor_entry_order_id and locked_entry_order_id
                            and locked_entry_order_id != monitor_entry_order_id)
                        or (monitor_entry_client_id and locked_entry_client_id
                            and locked_entry_client_id != monitor_entry_client_id)):
                    write_monitor_health(
                        "stale",
                        last_error="slot identity changed while protection lock was acquired",
                        identity_state=state, state_status=state.get("status"),
                        protection_established=False,
                    )
                    return 0
                stop_id = state.get("tsl_stop_order_id")
                tp_id = state.get("tp_stop_order_id")
                try:
                    stop_lots = abs(int(Decimal(str(state.get("stop_lots") or 0))))
                    tp_lots = abs(int(Decimal(str(state.get("tp_lots") or 0))))
                    locked_lots_value = Decimal(str(state.get("lots")))
                    if (not locked_lots_value.is_finite()
                            or locked_lots_value != locked_lots_value.to_integral_value()):
                        raise ValueError("persisted lots are not integral")
                    locked_lots = abs(int(locked_lots_value))
                    locked_entry = float(state.get("entry_mark"))
                    locked_cv = float(state.get("contract_value", cv))
                    locked_sign = (
                        -1 if str(state.get("side") or "").lower() == "short" else 1
                    )
                    if (locked_lots <= 0 or locked_sign != sign
                            or not math.isfinite(locked_entry) or locked_entry <= 0
                            or not math.isfinite(locked_cv) or locked_cv <= 0):
                        raise ValueError("invalid persisted position dimensions")
                except (InvalidOperation, TypeError, ValueError, OverflowError):
                    write_monitor_health(
                        "degraded",
                        last_error="persisted position changed or became invalid inside protection lock",
                        identity_state=state, state_status=state.get("status"),
                        protection_established=False,
                    )
                    sleep_secs = local_fallback_poll
                    raise _RetryMonitorCycle()
                if locked_lots != lots:
                    lots = locked_lots
                    entry_mark = locked_entry
                    cv = locked_cv
                    peak_pnl = float(state.get("tsl_peak") or 0.0)
                    persist_pk = peak_pnl
                    tsl_armed = bool(state.get("tsl_armed"))
                    stop_floor = float(state.get("tsl_floor") or 0.0)
                    stop_kind = state.get("stop_kind") or (
                        "tsl" if tsl_armed else "sl"
                    )
                else:
                    entry_mark = locked_entry
                    cv = locked_cv
                position = get_exchange_position(product_id)
                if position is None:
                    write_monitor_health(
                        "degraded", last_error="real-time position recheck failed inside protection lock",
                        state_status=state.get("status"), protection_established=False,
                    )
                    sleep_secs = local_fallback_poll
                    raise _RetryMonitorCycle()
                try:
                    live_decimal = Decimal(str(position.get("size")))
                    if live_decimal != live_decimal.to_integral_value():
                        raise ValueError("fractional exchange size")
                    live = int(live_decimal)
                except (InvalidOperation, ValueError, TypeError, OverflowError):
                    write_monitor_health(
                        "degraded", last_error="real-time position size is malformed",
                        state_status=state.get("status"), protection_established=False,
                    )
                    sleep_secs = local_fallback_poll
                    raise _RetryMonitorCycle()
                if live == 0:
                    write_monitor_health(
                        "verifying", last_error="position became flat during protection update",
                        state_status=state.get("status"), exchange_position_size=0,
                        protection_established=False,
                    )
                    sleep_secs = local_fallback_poll
                    raise _RetryMonitorCycle()

                pos_changed = False
                expected_live_sign = -1 if sign < 0 else 1
                if live * expected_live_sign < 0:
                    message = (f"exchange position direction no longer matches {SLOT} state "
                               f"({live} lots); protection was not repurposed")
                    write_monitor_health(
                        "degraded", last_error=message, state_status=state.get("status"),
                        exchange_position_size=live, protected_lots=lots,
                        stop_order_id=stop_id, tp_order_id=tp_id,
                        protection_established=False,
                        adoption_status="blocked_direction_mismatch",
                        continuity_verified=False, continuity_status="direction_mismatch",
                    )
                    alert_once("position_direction_mismatch", f"{symbol}: {message}")
                    sleep_secs = local_fallback_poll
                    raise _RetryMonitorCycle()

                new_lots = abs(live)
                owned_cap = abs(int(float(state.get("owned_entry_lots") or lots)))
                new_entry = float(state.get("entry_mark") or entry_mark)
                adopted_lots = 0
                # Trend ownership is a position-cycle property, not merely a
                # net-size property.  A sell/buy round trip or full
                # close/reopen can finish at the same lot count, so every open
                # Trend poll must prove the complete fill-ledger continuity.
                continuity_required = SLOT == "trend"
                continuity = ({"verified": True, "status": "not_required",
                               "signed_size": live}
                              if not continuity_required else
                              _trend_cycle_continuity(state, position))
                if continuity_required and not continuity.get("verified"):
                    broken = continuity.get("status") == "broken_reopened"
                    if broken:
                        state.update({
                            "status": "OWNERSHIP_AMBIGUOUS",
                            "continuity_verified": False,
                            "continuity_status": "broken_reopened",
                            "continuity_error": continuity.get("reason"),
                            "continuity_broken_at_utc": _utc_now(),
                            "remaining_external_position_lots": live,
                            "protection_revision": int(
                                state.get("protection_revision") or 0) + 1,
                            "continuity_revision": int(
                                state.get("continuity_revision") or 0) + 1,
                            "accounting_status": "pending",
                        })
                        _atomic_write_json(STATE_FILE, state)
                        removed = remove_exchange_protection(
                            state, explicit=True,
                            reason="fill ledger proved the original Trend cycle closed and reopened",
                        )
                        write_monitor_health(
                            "ownership_ambiguous", last_error=continuity.get("reason") or
                            "original cycle closed and a new position reopened",
                            state_status="OWNERSHIP_AMBIGUOUS",
                            exchange_position_size=live, continuity_verified=False,
                            continuity_status="broken_reopened",
                            protection_established=False,
                            protection_cleanup_pending=not removed,
                        )
                        alert_once(
                            "trend_cycle_reopened",
                            f"{symbol}: original Trend cycle closed; the current position is external",
                        )
                        if removed:
                            return 0
                    else:
                        message = ("position-cycle continuity is unverified: "
                                   + str(continuity.get("reason") or continuity.get("status")))
                        write_monitor_health(
                            "degraded", last_error=message,
                            state_status=state.get("status"), exchange_position_size=live,
                            protected_lots=lots, stop_order_id=stop_id, tp_order_id=tp_id,
                            continuity_verified=False,
                            continuity_status=continuity.get("status"),
                            protection_established=False,
                            adoption_status="blocked_continuity_unverified",
                        )
                        alert_once("trend_continuity_unverified", f"{symbol}: {message}")
                    sleep_secs = local_fallback_poll
                    raise _RetryMonitorCycle()

                if new_lots > lots and SLOT == "trend":
                    try:
                        previous_lots = lots
                        state = _adopt_matching_external_trend_lots_locked(
                            state, position, previous_lots, continuity,
                        )
                        new_lots = int(state["lots"])
                        new_entry = float(state["entry_mark"])
                        adopted_lots = new_lots - previous_lots
                        peak_pnl = 0.0
                        persist_pk = 0.0
                        tsl_armed = False
                        stop_floor = 0.0
                        stop_kind = "sl"
                        log.warning(
                            "Adopted %d externally added same-product lots; aggregate protection "
                            "is resizing %d -> %d at exchange entry %.4f.",
                            adopted_lots, previous_lots, new_lots, new_entry,
                        )
                    except Exception as exc:
                        message = (f"external same-product growth is awaiting safe adoption "
                                   f"({new_lots} > {lots}): {exc}")
                        write_monitor_health(
                            "degraded", last_error=message,
                            state_status=state.get("status"), exchange_position_size=live,
                            protected_lots=lots, owned_entry_lots=owned_cap,
                            unprotected_same_product_lots=max(new_lots - lots, 0),
                            stop_order_id=stop_id, tp_order_id=tp_id,
                            protection_established=False, adoption_status="blocked",
                            continuity_verified=bool(continuity.get("verified")),
                            continuity_status=continuity.get("status"),
                        )
                        alert_once("same_product_external_adoption_blocked", f"{symbol}: {message}")
                        sleep_secs = local_fallback_poll
                        raise _RetryMonitorCycle()
                elif new_lots < lots and SLOT == "trend" and continuity_required:
                    try:
                        state = _rebase_matching_trend_reduction_locked(
                            state, position, lots, continuity,
                            allow_pending_close=bool(
                                state.get("pending_close_client_order_id")
                                or state.get("pending_close_order_id")
                            ),
                        )
                        new_entry = float(state["entry_mark"])
                        peak_pnl = 0.0
                        persist_pk = 0.0
                        tsl_armed = False
                        stop_floor = 0.0
                        stop_kind = "sl"
                    except Exception as exc:
                        message = f"partial Trend reduction could not be reconciled: {exc}"
                        write_monitor_health(
                            "degraded", last_error=message, state_status=state.get("status"),
                            exchange_position_size=live, protected_lots=lots,
                            continuity_verified=False, protection_established=False,
                        )
                        sleep_secs = local_fallback_poll
                        raise _RetryMonitorCycle()
                elif new_lots > owned_cap and SLOT != "trend":
                    message = (f"exchange position grew beyond bot ownership "
                               f"({new_lots} > {owned_cap}); automatic adoption is Trend-only")
                    write_monitor_health(
                        "degraded", last_error=message, state_status=state.get("status"),
                        exchange_position_size=live, owned_entry_lots=owned_cap,
                        protected_lots=lots, stop_order_id=stop_id, tp_order_id=tp_id,
                        protection_established=False, adoption_status="not_allowed_for_slot",
                    )
                    sleep_secs = local_fallback_poll
                    raise _RetryMonitorCycle()

                if continuity_required and new_lots == lots:
                    live_basis = _finite_float(position.get("entry_price"), new_entry)
                    ledger_changed = list(state.get("continuity_fill_ids") or []) != list(
                        continuity.get("fill_ids") or []
                    )
                    if ledger_changed or abs(live_basis - new_entry) > 1e-8:
                        new_entry = live_basis
                        original_fee, _original_fee_source, original_fee_authoritative = (
                            _original_bot_entry_fee_component(state)
                        )
                        total_entry_fee = float(original_fee or 0.0) + float(
                            continuity.get("added_entry_fees_usd") or 0
                        )
                        entry_fees_complete = bool(
                            continuity.get("fill_fees_complete")
                            and original_fee_authoritative
                        )
                        state.update({
                            "entry_mark": round(new_entry, 8),
                            "entry_mark_source": "exchange_realtime_aggregate",
                            "protection_revision": int(
                                state.get("protection_revision") or 0) + 1,
                            "continuity_revision": int(
                                state.get("continuity_revision") or 0) + 1,
                            "continuity_fill_ids": continuity.get("fill_ids", []),
                            "continuity_last_fill_id": continuity.get("last_fill_id"),
                            "cycle_entry_lots_total": continuity.get("cycle_entry_lots_total"),
                            "cycle_exit_lots_total": continuity.get("cycle_exit_lots_total"),
                            "partial_exit_gross_pnl_usd": continuity.get(
                                "partial_exit_gross_pnl_usd", 0),
                            "partial_exit_fees_usd": continuity.get(
                                "partial_exit_fees_usd", 0),
                            "partial_exit_accounting_status": (
                                "complete" if continuity.get("fill_fees_complete")
                                else "fee_pending"
                            ),
                            "entry_fee_usd": round(total_entry_fee, 8),
                            "entry_fees_usd": round(total_entry_fee, 8),
                            "entry_fee_source": (
                                "exchange_fill_ledger"
                                if entry_fees_complete
                                else "fill_ledger_fee_pending"
                            ),
                            "fees_available": entry_fees_complete,
                            "fees_estimated": not entry_fees_complete,
                            "lot_attribution_status": "fungible_after_reduction"
                            if continuity.get("cycle_exit_lots_total") else
                            state.get("lot_attribution_status"),
                        })
                        _atomic_write_json(STATE_FILE, state)
                        pos_changed = True

                if new_lots != lots or new_entry != entry_mark:
                    log.info(
                        "Position changed: lots %d -> %d, entry %.4f -> %.4f; resizing protection.",
                        lots, new_lots, entry_mark, new_entry,
                    )
                    lots, entry_mark = new_lots, new_entry
                    pos_changed = True
                    if not save_state_fields(lots=lots, protection_lots=lots,
                                             entry_mark=entry_mark):
                        raise RuntimeError("position resize could not be persisted")

                # A durable close identity must be reconciled by the monitor,
                # not merely advertised as "closing" forever.  We reach this
                # point only after a real-time direction check and a verified
                # Trend fill-ledger generation, so a same-product close/reopen
                # cannot cause the residual close to target an unrelated cycle.
                state = load_state()
                if (state.get("pending_close_client_order_id")
                        or state.get("pending_close_order_id")):
                    try:
                        close_mark = get_mark(symbol)
                    except Exception as exc:
                        log.warning("Mark lookup failed during close reconciliation: %s", exc)
                        close_mark = entry_mark
                    if not math.isfinite(close_mark) or close_mark <= 0:
                        close_mark = entry_mark
                    close_pnl = (close_mark - entry_mark) * cv * lots * sign
                    closed = _close_position_locked(
                        state, close_mark, close_pnl,
                        state.get("pending_close_reason") or "manual_squareoff",
                    )
                    if closed:
                        cleaned = remove_exchange_protection(
                            load_state(), confirmed_closed=True,
                            reason="pending reduce-only close confirmed",
                        )
                        if cleaned:
                            write_monitor_health(
                                "closed", state_status="CLOSED",
                                exchange_position_size=0,
                                protection_established=False,
                            )
                            return 0
                    latest = load_state()
                    write_monitor_health(
                        "closing",
                        last_error=str(latest.get("pending_close_error")
                                       or "durable close identity is being reconciled"),
                        identity_state=latest, state_status=latest.get("status"),
                        exchange_position_size=latest.get("pending_close_live_size", live),
                        protection_established=False,
                    )
                    sleep_secs = local_fallback_poll
                    raise _RetryMonitorCycle()

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
                    if (stop_id is None or stop_lots != lots
                            or not stop_complete
                            or tsl_floor - stop_floor >= ratchet_min or pos_changed):
                        ensure_stop(tsl_floor, "tsl")
                elif sl_pnl > 0 and (stop_id is None or stop_lots != lots
                                     or not stop_complete or pos_changed):
                    ensure_stop(-sl_pnl, "sl")
                ensure_tp(force=pos_changed or tp_lots != lots or tp_id is None)

                stop_required = sl_pnl > 0 or active_tsl
                desired_tp_price = max(
                    round(entry_mark + sign * target_pnl / (cv * lots), 1), 0.1,
                )
                tp_proof = protection_proof(
                    tp_id, "tp", lots, desired_tp_price,
                    load_state().get("tp_client_order_id"),
                )
                if stop_required:
                    desired_stop_price = floor_price(
                        tsl_floor if active_tsl else -sl_pnl
                    )
                    stop_proof = protection_proof(
                        stop_id, "stop", lots, desired_stop_price,
                        load_state().get("stop_client_order_id"),
                    )
                else:
                    stop_retired = retire_disabled_stop()
                    stop_proof = (
                        {"ok": True, "covered_lots": lots,
                         "reason": "stop is disabled", "order": {}}
                        if stop_retired else
                        {"ok": False, "covered_lots": 0,
                         "reason": "disabled stop cleanup is unverified", "order": {}}
                    )
                tp_complete = bool(tp_proof["ok"])
                stop_complete = bool(stop_proof["ok"])

                # External/manual orders do not participate in our filesystem
                # lock.  Re-read the position immediately after both order
                # proofs and require the exact same aggregate and fill-ledger
                # generation before publishing protection or notifying.
                final_position = get_exchange_position(product_id)
                if final_position is None:
                    write_monitor_health(
                        "degraded",
                        last_error="final real-time position verification failed",
                        identity_state=state, state_status=state.get("status"),
                        continuity_verified=False, protection_established=False,
                    )
                    sleep_secs = local_fallback_poll
                    raise _RetryMonitorCycle()
                try:
                    final_size_value = Decimal(str(final_position.get("size")))
                    if (not final_size_value.is_finite()
                            or final_size_value != final_size_value.to_integral_value()):
                        raise ValueError("final position size is not integral")
                    final_live = int(final_size_value)
                    initial_basis = _finite_float(position.get("entry_price"), 0.0)
                    final_basis = _finite_float(final_position.get("entry_price"), 0.0)
                except (InvalidOperation, TypeError, ValueError, OverflowError):
                    final_live = None
                    initial_basis = final_basis = 0.0
                basis_tolerance = max(0.02, abs(initial_basis) * 0.0001)
                if (final_live != live or initial_basis <= 0 or final_basis <= 0
                        or abs(final_basis - initial_basis) > basis_tolerance):
                    write_monitor_health(
                        "degraded",
                        last_error=("position changed while protection was being verified; "
                                    "reconciliation will restart"),
                        identity_state=state, state_status=state.get("status"),
                        exchange_position_size=final_live,
                        continuity_verified=False, protection_established=False,
                    )
                    sleep_secs = local_fallback_poll
                    raise _RetryMonitorCycle()
                if continuity_required:
                    final_continuity = _trend_cycle_continuity(state, final_position)
                    generation_fields = (
                        "position_cycle_id", "signed_size", "entry_mark",
                        "cycle_entry_lots_total", "cycle_exit_lots_total",
                        "partial_exit_gross_pnl_usd", "partial_exit_fees_usd",
                        "added_entry_fees_usd", "fill_fees_complete", "fill_ids",
                    )
                    same_generation = (
                        final_continuity.get("verified") is True
                        and all(final_continuity.get(key) == continuity.get(key)
                                for key in generation_fields)
                    )
                    if not same_generation:
                        write_monitor_health(
                            "degraded",
                            last_error=("Trend fill ledger changed while protection was "
                                        "being verified; reconciliation will restart"),
                            identity_state=state, state_status=state.get("status"),
                            exchange_position_size=final_live,
                            continuity_verified=False,
                            continuity_status=final_continuity.get("status"),
                            protection_established=False,
                        )
                        sleep_secs = local_fallback_poll
                        raise _RetryMonitorCycle()
                    continuity = final_continuity
                proof_position = get_exchange_position(product_id)
                try:
                    proof_size_value = Decimal(str((proof_position or {}).get("size")))
                    if (not proof_size_value.is_finite()
                            or proof_size_value != proof_size_value.to_integral_value()):
                        raise ValueError("proof position size is not integral")
                    proof_live = int(proof_size_value)
                    proof_basis = _finite_float(
                        (proof_position or {}).get("entry_price"), 0.0,
                    )
                except (InvalidOperation, TypeError, ValueError, OverflowError):
                    proof_live, proof_basis = None, 0.0
                if (proof_live != final_live or proof_basis <= 0
                        or abs(proof_basis - final_basis) > basis_tolerance):
                    write_monitor_health(
                        "degraded",
                        last_error=("position changed after fill-ledger verification; "
                                    "reconciliation will restart"),
                        identity_state=state, state_status=state.get("status"),
                        exchange_position_size=proof_live,
                        continuity_verified=False, protection_established=False,
                    )
                    sleep_secs = local_fallback_poll
                    raise _RetryMonitorCycle()
                exchange_complete = tp_complete and stop_complete
                exchange_protected_lots = min(
                    int(tp_proof.get("covered_lots") or 0),
                    int(stop_proof.get("covered_lots") or 0),
                )
                continuity_verified = bool(
                    not continuity_required or continuity.get("verified")
                )
                tp_local_fallback = continuity_verified and not tp_complete
                stop_local_fallback = (
                    continuity_verified and stop_required and not stop_complete
                )
                local_fallback_active = tp_local_fallback or stop_local_fallback
                protection_established = continuity_verified and (
                    tp_complete or tp_local_fallback
                ) and (stop_complete or stop_local_fallback)
                sleep_secs = local_fallback_poll if local_fallback_active else poll_secs
                status = "healthy" if exchange_complete else "degraded"
                error = "" if exchange_complete else (
                    "exchange protection incomplete; executable local fallback active"
                    if local_fallback_active else "protection coverage is unverified"
                )
                write_monitor_health(
                    status, last_error=error, identity_state=state,
                    state_status=state.get("status"), symbol=symbol,
                    exchange_position_size=live, last_mark=mark, last_pnl=round(pnl, 2),
                    peak_pnl=round(peak_pnl, 2), stop_order_id=stop_id,
                    tp_order_id=tp_id, protected_lots=lots,
                    stop_order_lots=int(stop_proof.get("covered_lots") or 0),
                    tp_order_lots=int(tp_proof.get("covered_lots") or 0),
                    stop_order_proof=stop_proof, tp_order_proof=tp_proof,
                    owned_entry_lots=state.get("original_owned_entry_lots")
                                     or state.get("owned_entry_lots") or lots,
                    externally_added_lots_adopted=state.get(
                        "externally_added_lots_adopted", 0),
                    adoption_status="adopted" if state.get(
                        "externally_added_lots_adopted") else "not_needed",
                    stop_kind=stop_kind, stop_floor=round(stop_floor, 2),
                    tsl_armed=tsl_armed,
                    exchange_protection_supported=not exch_unsupported,
                    exchange_protection_complete=exchange_complete,
                    exchange_protected_lots=exchange_protected_lots,
                    unprotected_same_product_lots=max(lots - exchange_protected_lots, 0),
                    local_fallback_active=local_fallback_active,
                    local_tp_fallback_active=tp_local_fallback,
                    local_stop_fallback_active=stop_local_fallback,
                    protection_established=protection_established,
                    continuity_verified=continuity_verified,
                    continuity_status=continuity.get("status"),
                    continuity_verified_size=live if continuity_verified else None,
                    continuity_verified_at_utc=continuity.get("verified_at_utc") or _utc_now(),
                    consecutive_errors=0, next_poll_secs=sleep_secs,
                )
                consecutive_errors = 0

                notification_pending = bool(
                    adopted_lots or state.get("external_adoption_notification_pending")
                )
                if notification_pending and exchange_complete:
                    send_telegram(
                        f"🛡️ <b>EXTERNAL LOTS PROTECTED — {_slot_label()} ({USER.upper()})</b>\n"
                        f"<code>{symbol}</code>\nTotal  » <code>{lots:,}</code> matching lots\n"
                        f"Coverage » <code>TP + SL/TSL verified on exchange</code>\n"
                        f"Basis  » <code>${entry_mark:.4f}</code> aggregate entry"
                    )
                    save_state_fields(external_adoption_notification_pending=False,
                                      external_adoption_notified_at_utc=_utc_now())
                elif adopted_lots:
                    save_state_fields(external_adoption_notification_pending=True)

                log.info(
                    "mark=%.4f pnl=$%.2f peak=$%.2f TP=+$%.2f%s SL=%s TSL=%s",
                    mark, pnl, peak_pnl, target_pnl,
                    " [exchange verified]" if tp_complete else " [local fallback]",
                    (f"-${sl_pnl:.2f}" + (
                        " [exchange verified]" if stop_complete else " [local fallback]"))
                    if sl_pnl > 0 else "off",
                    "off" if not tsl_enabled else
                    (f"floor ${tsl_floor:.2f}" + (
                        " [exchange verified]" if stop_complete else " [local fallback]"))
                    if active_tsl else
                    f"unarmed (arm +${tsl_arm_pnl:.2f}, trail ${tsl_trail_pnl:.2f})",
                )

            # A component falls back locally whenever its exchange proof is
            # absent or undersized, even if an older partial-size ID remains.
            # The close path re-verifies the complete real-time position under
            # the same close lock before submitting one reduce-only order.
            closed = False
            if not tp_complete and pnl >= target_pnl:
                closed = close_position(state, mark, pnl, "take_profit")
            elif active_tsl and not stop_complete and pnl <= tsl_floor:
                closed = close_position(state, mark, pnl, "trailing_stop")
            elif sl_pnl > 0 and not stop_complete and pnl <= -sl_pnl:
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
        except _RetryMonitorCycle:
            pass
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
