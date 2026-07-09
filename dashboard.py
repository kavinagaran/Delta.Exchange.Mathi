"""
dashboard.py — MV-BTC Straddle Web Dashboard (Mathi)
Run  : python dashboard.py
Open : http://localhost:5001
"""

import hashlib
import hmac
import json
import math
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests as req
from dotenv import load_dotenv, set_key
from flask import Flask, jsonify, request, abort, send_file

# Force IPv4 — Delta's whitelist holds our IPv4; IPv6 rotates and gets rejected
import socket as _socket
import urllib3.util.connection as _u3c
_u3c.allowed_gai_family = lambda: _socket.AF_INET

load_dotenv()

API_KEY    = os.getenv("API_KEY", "")
API_SECRET = os.getenv("API_SECRET", "")
API_BASE   = os.getenv("BASE_URL", "https://api.india.delta.exchange")

def _sign(method, path, query="", body=""):
    ts  = str(int(time.time()))
    msg = method + ts + path + query + body
    sig = hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return {"api-key": API_KEY, "timestamp": ts, "signature": sig,
            "Content-Type": "application/json"}

def _exchange_pnl(product_id: int):
    """Fetch live unrealized P&L directly from exchange positions API."""
    try:
        hdrs = _sign("GET", "/v2/positions/margined")
        r = req.get(API_BASE + "/v2/positions/margined", headers=hdrs, timeout=5)
        for pos in r.json().get("result", []):
            if pos.get("product_id") == product_id and float(pos.get("size", 0)) != 0:
                return {
                    "live_pnl":     float(pos.get("unrealized_pnl", 0)),
                    "current_mark": float(pos.get("mark_price", 0)),
                }
    except Exception:
        pass
    return None

BASE               = Path(__file__).parent
STATE_FILE         = BASE / "straddle_state.json"
MORNING_STATE_FILE = BASE / "morning_state.json"
HISTORY_FILE       = BASE / "trade_history.json"
ENV_FILE           = BASE / ".env"

SLOT_STATE = {"evening": STATE_FILE, "morning": MORNING_STATE_FILE}
SLOT_PID   = {"evening": BASE / "tp_monitor.pid", "morning": BASE / "tp_monitor_morning.pid"}

_tp_procs: dict = {"evening": None, "morning": None}


def _pid_alive(pid: int) -> bool:
    """Cross-platform process existence check. On Windows os.kill(pid, 0)
    can terminate the target, so use the Win32 API there; on POSIX,
    signal 0 is the standard no-op liveness probe."""
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            return bool(ok) and code.value == STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # exists, owned by someone else
    except OSError:
        return False


def _tp_running(slot: str = "evening") -> bool:
    proc = _tp_procs.get(slot)
    if proc is not None:
        if proc.poll() is None:
            return True
        _tp_procs[slot] = None
    # Fallback: check PID file (survives dashboard restart)
    pid_file = SLOT_PID[slot]
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            if _pid_alive(pid):
                return True
        except (ValueError, OSError):
            pass
        pid_file.unlink(missing_ok=True)
    return False

# Keys the dashboard is allowed to read/write
CONFIG_KEYS = [
    "DRY_RUN", "STRADDLE_LOTS", "STRIKE_STEP",
    "ENTRY_H_UTC", "ENTRY_M_UTC", "EXIT_H_UTC", "EXIT_M_UTC",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_ALERTS",
    "TP_TARGET_PNL", "TP_POLL_SECS",
    "MORNING_ENABLED", "MORNING_LOTS", "MORNING_H_UTC", "MORNING_M_UTC",
    "MORNING_EXIT_ENABLED", "MORNING_EXIT_H_UTC", "MORNING_EXIT_M_UTC",
    "DYNAMIC_LOTS",
    "TP_TARGET_PNL_MORNING", "TP_POLL_SECS_MORNING",
    "MAX_TRADES_PER_DAY",
]

app = Flask(__name__, static_folder=str(BASE), static_url_path="/static")

# Optional HTTP Basic Auth — active only when DASH_PASS is set in .env.
# Unset on the home-LAN Windows box (no login prompt there); set on the
# public cloud server, where an unauthenticated dashboard would let anyone
# who finds the port square off live positions.
DASH_USER = os.getenv("DASH_USER", "mathi")
DASH_PASS = os.getenv("DASH_PASS", "")

@app.before_request
def _basic_auth_gate():
    if not DASH_PASS:
        return None
    a = request.authorization
    if a and a.username == DASH_USER and a.password == DASH_PASS:
        return None
    from flask import Response
    return Response("Authentication required", 401,
                    {"WWW-Authenticate": 'Basic realm="Mathi Bot"'})


# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────
def _load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return default


def _pnl_stats(trades: list) -> dict:
    # DRY-RUN trades produced no real money outcome — never let a simulated
    # result contribute to real performance stats (same principle as
    # excluding imported backtest rows).
    pnls   = [float(t.get("pnl_usd", 0)) for t in trades
              if t.get("pnl_usd") is not None and not t.get("dry_run")]
    if not pnls:
        return {}
    wins   = [p for p in pnls if p >= 0]
    losses = [p for p in pnls if p < 0]
    total  = sum(pnls)
    wr     = len(wins) / len(pnls) * 100
    aw     = sum(wins)   / len(wins)   if wins   else 0.0
    al     = sum(losses) / len(losses) if losses else 0.0
    rr     = abs(aw / al) if al else 0.0
    cum    = 0.0; peak = 0.0; max_dd = 0.0
    for p in pnls:
        cum += p
        if cum > peak: peak = cum
        dd = cum - peak
        if dd < max_dd: max_dd = dd
    return {
        "total_days": len(pnls),
        "wins":       len(wins),
        "losses":     len(losses),
        "win_rate":   round(wr, 1),
        "total_pnl":  round(total, 2),
        "avg_win":    round(aw,    2),
        "avg_loss":   round(al,    2),
        "rr":         round(rr,    2),
        "max_dd":     round(max_dd, 2),
    }


# ─────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return (BASE / "dashboard.html").read_text(encoding="utf-8")


_last_sync = 0.0

def _state_matches(state: dict, pid: int, size: int, entry: float) -> bool:
    side = "short" if size < 0 else "long"
    return (state.get("status") == "OPEN"
            and int(state.get("product_id", 0) or 0) == pid
            and int(state.get("lots", 0) or 0) == abs(size)
            and state.get("side", "long") == side
            and abs(float(state.get("entry_mark", 0) or 0) - entry) < 0.01)


def _reconcile_stale_close(slot: str, state: dict, live_pids: set) -> dict:
    """If a slot's on-disk state says OPEN but that position no longer exists
    on the exchange (closed while the dashboard/bot was down, or by a manual
    trade this process never saw), close it out using the real fill from
    order history instead of leaving stale data blocking future syncs."""
    pid = int(state.get("product_id", 0) or 0)
    if state.get("status") != "OPEN" or pid == 0 or pid in live_pids:
        return state
    try:
        hdrs = _sign("GET", "/v2/orders/history", "?page_size=20")
        r = req.get(f"{API_BASE}/v2/orders/history", params={"page_size": 20}, headers=hdrs, timeout=6)
        orders = r.json().get("result", [])
        sells = [o for o in orders
                 if o.get("product_id") == pid and o.get("side") == "sell"
                 and o.get("state") == "closed" and o.get("average_fill_price")]
        if not sells:
            return state  # Can't reconcile yet — leave as-is, try again next sync
        sells.sort(key=lambda o: str(o.get("created_at", "")), reverse=True)
        fill    = sells[0]
        exit_mk = float(fill["average_fill_price"])
        cv      = float(state.get("contract_value", 0.001))
        lots    = int(state.get("lots", 0))
        entry   = float(state.get("entry_mark", 0))
        pnl     = round((exit_mk - entry) * cv * lots, 2)
        state.update({
            "status":        "CLOSED",
            "exit_time_utc": str(fill.get("created_at", ""))[11:19],
            "exit_mark":      exit_mk,
            "pnl_usd":        pnl,
            "exit_trigger":   "reconciled_stale",
        })
        SLOT_STATE[slot].write_text(json.dumps(state, indent=2), encoding="utf-8")
        hist = _load_json(HISTORY_FILE, [])
        if isinstance(hist, list):
            rec = {**state, "date": state.get("entry_date", ""), "cost_usd": state.get("total_cost_usd", 0)}
            dup = any(h.get("symbol") == rec["symbol"] and h.get("entry_time") == rec.get("entry_time_utc")
                      for h in hist)
            if not dup:
                hist.append(rec)
                HISTORY_FILE.write_text(json.dumps(hist, indent=2), encoding="utf-8")
    except Exception:
        pass
    return state


def _sync_states_from_exchange() -> None:
    """Adopt open MV-BTC positions from the exchange into the correct slot
    (entries before 11:00 UTC -> morning, else evening) so manually placed
    trades always show. Throttled to one authenticated call per 30s."""
    global _last_sync
    if not API_KEY or not API_SECRET or time.time() - _last_sync < 30:
        return
    _last_sync = time.time()
    try:
        hdrs = _sign("GET", "/v2/positions/margined")
        r = req.get(f"{API_BASE}/v2/positions/margined", headers=hdrs, timeout=6)
        data = r.json()
        if not data.get("success"):
            return
        live = [p for p in data.get("result", [])
                if float(p.get("size", 0)) != 0
                and str(p.get("product_symbol", "")).startswith("MV-BTC")]
        live_pids = {int(p["product_id"]) for p in live}
        states = {slot: _load_json(f, {}) for slot, f in SLOT_STATE.items()}
        states = {slot: _reconcile_stale_close(slot, s, live_pids) for slot, s in states.items()}
        for p in live:
            pid   = int(p["product_id"])
            size  = int(float(p["size"]))
            entry = float(p.get("entry_price") or 0)
            if any(_state_matches(s, pid, size, entry) for s in states.values()):
                continue
            created = str(p.get("created_at", ""))
            try:
                # Bucket by IST time-of-day, not raw UTC hour — IST is UTC+5:30,
                # so a trade at e.g. 22:54 UTC is 04:24 IST the *next* day (morning),
                # not "evening" as a naive UTC-hour check would conclude.
                h_utc, m_utc = int(created[11:13]), int(created[14:16])
                ist_hour = ((h_utc * 60 + m_utc + 330) % 1440) // 60
            except (ValueError, IndexError):
                ist_hour = 12
            slot = "morning" if ist_hour < 11 else "evening"
            # Don't clobber: (1) different open position in slot, or (2) just-closed position in slot
            other = states[slot]
            if (other.get("status") == "OPEN" and int(other.get("product_id", 0) or 0) != pid) \
               or (other.get("status") == "CLOSED" and other.get("exit_time_utc")):  # Recent close
                slot = "evening" if slot == "morning" else "morning"
                other = states[slot]
                if (other.get("status") == "OPEN" and int(other.get("product_id", 0) or 0) != pid) \
                   or (other.get("status") == "CLOSED" and other.get("exit_time_utc")):
                    continue
            pr = req.get(f"{API_BASE}/v2/products/{pid}", timeout=6).json().get("result", {})
            cv = float(pr.get("contract_value") or 0.001)
            new_state = {
                "slot":           slot,
                "status":         "OPEN",
                "side":           "short" if size < 0 else "long",
                "entry_date":     created[:10] or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "entry_time_utc": created[11:19],
                "symbol":         pr.get("symbol", p.get("product_symbol", "")),
                "product_id":     pid,
                "strike":         float(pr.get("strike_price") or 0),
                "settlement":     pr.get("settlement_time", ""),
                "contract_value": cv,
                "lots":           abs(size),
                "entry_mark":     entry,
                "btc_at_entry":   0,
                "total_cost_usd": round(entry * cv * abs(size), 2),
                "entry_trigger":  "exchange_sync",
            }
            SLOT_STATE[slot].write_text(json.dumps(new_state, indent=2), encoding="utf-8")
            states[slot] = new_state
    except Exception:
        pass


def _enrich_live(state: dict) -> dict:
    """Attach current_mark and live_pnl to an OPEN state dict (side-aware)."""
    if state.get("status") == "OPEN":
        symbol = state.get("symbol", "")
        try:
            r    = req.get(f"{API_BASE}/v2/tickers/{symbol}", timeout=5)
            mark = float(r.json().get("result", {}).get("mark_price") or 0)
            cval = float(state.get("contract_value", 0.001))
            lots = int(state.get("lots", 1000))
            em   = float(state.get("entry_mark", 0))
            sign = -1 if state.get("side") == "short" else 1
            state["current_mark"] = round(mark, 4)
            state["live_pnl"]     = round((mark - em) * cval * lots * sign, 2) if mark else None
        except Exception:
            state["current_mark"] = None
            state["live_pnl"]     = None
    return state


@app.route("/api/status")
def api_status():
    _sync_states_from_exchange()
    state   = _enrich_live(_load_json(STATE_FILE, {}))
    morning = _enrich_live(_load_json(MORNING_STATE_FILE, {}))
    state["morning"] = morning
    # BTC futures (perpetual) live price
    try:
        r_btc = req.get(
            "https://api.india.delta.exchange/v2/tickers/BTCUSD",
            timeout=5,
        )
        state["btc_futures_price"] = float(r_btc.json().get("result", {}).get("mark_price") or 0)
    except Exception:
        state["btc_futures_price"] = None
    # IST info helper for the UI
    state["entry_ist"] = "5:35 PM IST  (12:05 UTC)"
    state["exit_ist"]  = "1:00 AM IST  (19:30 UTC)"
    return jsonify(state)



def _ist_calendar_date(date_str: str, time_str: str) -> str:
    """IST (UTC+5:30) calendar date a UTC (date, time-of-day) pair falls on.
    Users think in IST "today", but entry_date/entry_time are always stored
    in UTC, so a straight string compare against a UTC or IST "today" is
    wrong for whichever side of the actual moment doesn't match — this
    converts the trade's own timestamp before comparing calendar dates."""
    try:
        dt_utc = datetime.strptime(f"{date_str} {time_str or '00:00:00'}", "%Y-%m-%d %H:%M:%S")
        return (dt_utc + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return date_str


@app.route("/api/today-trades")
def api_today_trades():
    today_ist = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")
    trades  = _load_json(HISTORY_FILE, [])
    state   = _load_json(STATE_FILE, {})
    today_t = [t for t in trades
               if _ist_calendar_date(t.get("entry_date") or t.get("date", ""),
                                      t.get("entry_time") or t.get("entry_time_utc", "")) == today_ist]
    # Include any open slot position as a live row with real-time mark & P&L
    for slot in ("morning", "evening"):
        s = _load_json(SLOT_STATE[slot], {})
        if s.get("status") == "OPEN" and _ist_calendar_date(s.get("entry_date", ""), s.get("entry_time_utc", "")) == today_ist:
            s["_live"] = True
            s["slot"]  = slot
            s = _enrich_live(s)
            today_t = [s] + today_t
    return jsonify(today_t)


@app.route("/api/square-off", methods=["POST"])
def api_square_off():
    slot       = _slot_arg()
    state_file = SLOT_STATE[slot]
    state      = _load_json(state_file, {})
    if state.get("status") != "OPEN":
        return jsonify({"ok": False, "error": f"No open {slot} position"}), 400

    product_id = int(state.get("product_id", 0))
    symbol     = state.get("symbol", "")
    lots       = int(state.get("lots", 1000))
    entry_mark = float(state.get("entry_mark", 0))
    cval       = float(state.get("contract_value", 0.001))

    if not API_KEY or not API_SECRET:
        return jsonify({"ok": False, "error": "API credentials not configured"}), 400

    try:
        # Verify position still exists on exchange before placing sell
        hdrs_chk = _sign("GET", "/v2/positions/margined")
        pos_resp  = req.get(f"{API_BASE}/v2/positions/margined", headers=hdrs_chk, timeout=8).json()
        live_size = 0
        for pos in pos_resp.get("result", []):
            if pos.get("product_id") == product_id:
                live_size = int(float(pos.get("size", 0)))
                break
        if live_size == 0:
            return jsonify({"ok": False, "error": "Position not found on exchange — may have already been closed."}), 400

        # Use actual exchange size; negative size = short position
        is_short   = live_size < 0 or state.get("side") == "short"
        close_side = "buy" if is_short else "sell"
        pnl_sign   = -1 if is_short else 1
        lots       = abs(live_size)

        # Get current mark for P&L
        ticker = req.get(f"{API_BASE}/v2/tickers/{symbol}", timeout=5).json()
        mark   = float(ticker.get("result", {}).get("mark_price") or 0)

        # Place market close order (sell for long, buy-back for short)
        import json as _json
        payload = {"product_id": product_id, "size": lots,
                   "side": close_side, "order_type": "market_order"}
        body    = _json.dumps(payload, separators=(",", ":"))
        hdrs    = _sign("POST", "/v2/orders", "", body)
        r       = req.post(f"{API_BASE}/v2/orders", data=body, headers=hdrs, timeout=15)
        result  = r.json()

        if result.get("success"):
            o       = result.get("result", {})
            fill    = float(o.get("average_fill_price") or mark)
            pnl     = round((fill - entry_mark) * cval * lots * pnl_sign, 2)
            state.update({
                "status":        "CLOSED",
                "exit_time_utc": __import__("datetime").datetime.now(
                                     __import__("datetime").timezone.utc
                                 ).strftime("%H:%M:%S"),
                "exit_mark":     fill,
                "pnl_usd":       pnl,
                "exit_trigger":  "manual_squareoff",
            })
            # Save state and log trade
            state_file.write_text(
                __import__("json").dumps(state, indent=2), encoding="utf-8")
            trades   = _load_json(HISTORY_FILE, [])
            if not isinstance(trades, list):
                trades = []
            trades.append(state)
            HISTORY_FILE.write_text(
                __import__("json").dumps(trades, indent=2), encoding="utf-8")
            return jsonify({"ok": True, "pnl": pnl, "fill": fill,
                            "order_id": o.get("id")})
        else:
            return jsonify({"ok": False, "error": result.get("error", result)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _slot_arg() -> str:
    slot = request.args.get("slot", "")
    if not slot:
        slot = (request.get_json(silent=True) or {}).get("slot", "")
    return slot if slot in SLOT_STATE else "evening"


def _send_telegram(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat  = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        return
    try:
        req.post(f"https://api.telegram.org/bot{token}/sendMessage",
                 json={"chat_id": chat, "text": text, "parse_mode": "HTML"}, timeout=8)
    except Exception:
        pass


def _current_atm_mv() -> dict | None:
    """The live MV-BTC contract with the nearest settlement whose strike is
    closest to the current BTC price — i.e. 'the current straddle'."""
    try:
        spot = float(req.get(f"{API_BASE}/v2/tickers/BTCUSD", timeout=6)
                     .json().get("result", {}).get("mark_price") or 0)
        prods = req.get(f"{API_BASE}/v2/products",
                        params={"contract_types": "move_options", "states": "live"},
                        timeout=8).json().get("result", [])
        mv = [p for p in prods if str(p.get("symbol", "")).startswith("MV-BTC")
              and p.get("settlement_time")]
        if not mv or spot <= 0:
            return None
        nearest = min(p.get("settlement_time") for p in mv)
        batch   = [p for p in mv if p.get("settlement_time") == nearest]
        return min(batch, key=lambda p: abs(float(p.get("strike_price") or 0) - spot))
    except Exception:
        return None


def _manual_entry_lots(slot: str, mark: float, cv: float) -> int:
    """Usual sizing: slot's configured lots, upgraded by dynamic sizing
    (max of configured and affordable-with-balance) when DYNAMIC_LOTS is on."""
    configured = int(os.getenv("MORNING_LOTS", 2000) if slot == "morning"
                     else os.getenv("STRADDLE_LOTS", 800))
    max_lots = int(os.getenv("MAX_ORDER_LOTS", 5000))
    if os.getenv("DYNAMIC_LOTS", "true").lower() not in ("1", "true", "yes"):
        return min(configured, max_lots)
    try:
        hdrs = _sign("GET", "/v2/wallet/balances")
        data = req.get(f"{API_BASE}/v2/wallet/balances", headers=hdrs, timeout=8).json()
        bal = 0.0
        for w in data.get("result", []):
            if w.get("asset_symbol") == "USD":
                bal = float(w.get("available_balance") or 0)
                break
        afford = int((bal * 0.98) / (mark * cv)) if mark > 0 else 0
        return max(min(max(configured, afford), max_lots), 1)
    except Exception:
        return min(configured, max_lots)


@app.route("/api/manual-entry/preview")
def api_manual_entry_preview():
    """What a manual buy/sell would do right now: contract, strike, sizing."""
    slot = _slot_arg()
    contract = _current_atm_mv()
    if not contract:
        return jsonify({"ok": False, "error": "No live MV contract found"}), 502
    symbol = contract["symbol"]
    cv     = float(contract.get("contract_value") or 0.001)
    try:
        mark = float(req.get(f"{API_BASE}/v2/tickers/{symbol}", timeout=6)
                     .json().get("result", {}).get("mark_price") or 0)
    except Exception:
        mark = 0.0
    lots = _manual_entry_lots(slot, mark, cv)
    return jsonify({
        "ok":         True,
        "slot":       slot,
        "symbol":     symbol,
        "strike":     float(contract.get("strike_price") or 0),
        "mark":       round(mark, 4),
        "lots":       lots,
        "est_value":  round(mark * cv * lots, 2),
        "settlement": contract.get("settlement_time", ""),
        "dry_run":    os.getenv("DRY_RUN", "false").lower() in ("1", "true", "yes"),
    })


@app.route("/api/manual-entry", methods=["POST"])
def api_manual_entry():
    """BUY (long) or SELL (short-to-open) the current ATM straddle for a slot.
    Only allowed when the slot has no open position; afterwards the position
    is BAU — TP monitor, square-off, scheduled exits all apply."""
    slot = _slot_arg()
    data = request.get_json(silent=True) or {}
    side = (data.get("side") or request.args.get("side") or "").lower()
    if side not in ("buy", "sell"):
        return jsonify({"ok": False, "error": "side must be buy or sell"}), 400
    if not API_KEY or not API_SECRET:
        return jsonify({"ok": False, "error": "API credentials not configured"}), 400

    state_file = SLOT_STATE[slot]
    state      = _load_json(state_file, {})
    if state.get("status") == "OPEN":
        return jsonify({"ok": False, "error": f"{slot} already has an open position"}), 400

    contract = _current_atm_mv()
    if not contract:
        return jsonify({"ok": False, "error": "No live MV contract found"}), 502
    pid    = int(contract["id"])
    symbol = contract["symbol"]
    cv     = float(contract.get("contract_value") or 0.001)
    try:
        mark = float(req.get(f"{API_BASE}/v2/tickers/{symbol}", timeout=6)
                     .json().get("result", {}).get("mark_price") or 0)
    except Exception:
        mark = 0.0
    lots = _manual_entry_lots(slot, mark, cv)

    # Manual entries must respect Mode the same way the scheduled bot does —
    # previously this always fired a REAL order regardless of the DRY RUN
    # toggle, so "simulating" via the Mode switch gave no real protection
    # against an accidental click on Buy/Sell.
    is_dry_run = os.getenv("DRY_RUN", "false").lower() in ("1", "true", "yes")

    try:
        if is_dry_run:
            o = {"id": 0, "average_fill_price": mark}
        else:
            import json as _json
            payload = {"product_id": pid, "size": lots, "side": side, "order_type": "market_order"}
            body    = _json.dumps(payload, separators=(",", ":"))
            hdrs    = _sign("POST", "/v2/orders", "", body)
            result  = req.post(f"{API_BASE}/v2/orders", data=body, headers=hdrs, timeout=15).json()
            if not result.get("success") or not (result.get("result") or {}).get("id"):
                return jsonify({"ok": False, "error": result.get("error", result)}), 400
            o = result.get("result", {})

        fill = float(o.get("average_fill_price") or mark)
        now  = datetime.now(timezone.utc)
        pos_side = "long" if side == "buy" else "short"
        new_state = {
            "slot":           slot,
            "status":         "OPEN",
            "side":           pos_side,
            "entry_date":     now.strftime("%Y-%m-%d"),
            "entry_time_utc": now.strftime("%H:%M:%S"),
            "symbol":         symbol,
            "product_id":     pid,
            "strike":         float(contract.get("strike_price") or 0),
            "settlement":     contract.get("settlement_time", ""),
            "contract_value": cv,
            "lots":           lots,
            "entry_mark":     round(fill, 4),
            "btc_at_entry":   0,
            "total_cost_usd": round(fill * cv * lots, 2),
            "order_id":       o.get("id"),
            "entry_trigger":  f"manual_{side}",
            "dry_run":        is_dry_run,
        }
        state_file.write_text(json.dumps(new_state, indent=2), encoding="utf-8")

        icon  = "🌅" if slot == "morning" else "🌇"
        label = "LONG (bought)" if side == "buy" else "SHORT (sold to open)"
        mode  = " — DRY-RUN ⚠ (simulated, no real order)" if is_dry_run else ""
        _send_telegram(
            f"🖐 <b>MANUAL {side.upper()} — {icon} {slot.upper()} (MATHI)</b>{mode}\n"
            f"<code>{'━' * 24}</code>\n"
            f"Symbol  » <code>{symbol}</code>\n"
            f"Side    » <code>{label}</code>\n"
            f"Lots    » <code>{lots:,}</code>\n"
            f"Fill    » <code>${fill:.4f} / BTC</code>\n"
            f"Value   » <code>${fill * cv * lots:,.2f}</code>\n"
            f"Settles » <code>{str(contract.get('settlement_time', '')).replace('T', ' ').replace('Z', ' UTC')}</code>"
        )
        return jsonify({"ok": True, "slot": slot, "side": pos_side, "symbol": symbol,
                        "lots": lots, "fill": fill, "order_id": o.get("id"), "dry_run": is_dry_run})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _tp_env(slot: str):
    if slot == "morning":
        keys, dflt = ("TP_TARGET_PNL_MORNING", "TP_POLL_SECS_MORNING"), (300.0, 30)
    else:
        keys, dflt = ("TP_TARGET_PNL", "TP_POLL_SECS"), (105.0, 30)
    try:
        target = float(os.getenv(keys[0]) or dflt[0])
    except ValueError:
        target = dflt[0]
    try:
        poll = int(float(os.getenv(keys[1]) or dflt[1]))
    except ValueError:
        poll = dflt[1]
    return target, poll


@app.route("/api/tp-monitor", methods=["GET"])
def tp_monitor_status():
    out = {}
    for slot in ("morning", "evening"):
        target, poll = _tp_env(slot)
        out[slot] = {"running": _tp_running(slot), "target_pnl": target, "poll_secs": poll}
    # Back-compat top-level fields = evening
    out.update(out["evening"])
    return jsonify(out)


@app.route("/api/tp-monitor/start", methods=["POST"])
def tp_monitor_start():
    slot = _slot_arg()
    if _tp_running(slot):
        return jsonify({"ok": False, "error": f"{slot} monitor already running"}), 400
    state = _load_json(SLOT_STATE[slot], {})
    if state.get("status") != "OPEN":
        return jsonify({"ok": False, "error": f"No open {slot} position to monitor"}), 400
    script = BASE / "tp_monitor.py"
    if not script.exists():
        return jsonify({"ok": False, "error": "tp_monitor.py not found"}), 404
    proc = subprocess.Popen(
        [sys.executable, str(script), "--slot", slot],
        cwd=str(BASE),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _tp_procs[slot] = proc
    SLOT_PID[slot].write_text(str(proc.pid))
    return jsonify({"ok": True, "slot": slot, "pid": proc.pid})


@app.route("/api/tp-monitor/stop", methods=["POST"])
def tp_monitor_stop():
    slot = _slot_arg()
    stopped = False
    proc = _tp_procs.get(slot)
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        _tp_procs[slot] = None
        stopped = True
    pid_file = SLOT_PID[slot]
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 15)   # SIGTERM (TerminateProcess on Windows)
            stopped = True
        except Exception:
            pass
        pid_file.unlink(missing_ok=True)
    if not stopped:
        return jsonify({"ok": False, "error": f"{slot} monitor is not running"}), 400
    return jsonify({"ok": True, "slot": slot})


@app.route("/download/apk")
def download_apk():
    apk = BASE / "mv_btc_bot" / "build" / "app" / "outputs" / "flutter-apk" / "app-release.apk"
    if not apk.exists():
        abort(404)
    return send_file(str(apk), as_attachment=True, download_name="mathi-bot.apk")


@app.route("/api/logs")
def api_logs():
    log_file = Path(__file__).parent / "logs" / "straddle.log"
    n = min(int(request.args.get("n", 100)), 500)
    try:
        text = log_file.read_text(encoding="utf-8", errors="replace")
        rows = [l for l in text.splitlines() if l.strip()]
        return jsonify({"lines": rows[-n:]})
    except FileNotFoundError:
        return jsonify({"lines": []})


def _parse_strike(symbol: str):
    """Best-effort strike extraction from a Delta symbol, e.g.
    'C-BTC-63000-080726' or 'MV-BTC-62800-080726' -> 63000 / 62800."""
    parts = symbol.split("-")
    for p in parts:
        if p.isdigit():
            return float(p)
    return None


def _reconstruct_trades_from_orders(orders: list, skip_prefixes=("MV-BTC",)) -> list:
    """Rebuild flat-to-flat round-trip trades (entry -> full close) from raw
    fills, for any product Delta reports — calls, puts, futures, etc.
    Handles same-direction adds (weighted-avg entry), partial closes
    (realized P&L accrues, entry price unchanged), and reversals (a close
    that overshoots flat immediately opens a new cycle the other way).
    Symbols in skip_prefixes are excluded because they're already tracked
    precisely elsewhere (the bot's own MV straddle log)."""
    fills = []
    for o in orders:
        if o.get("state") != "closed" or not o.get("average_fill_price"):
            continue
        symbol = o.get("product_symbol", "")
        if not symbol or symbol.startswith(skip_prefixes):
            continue
        try:
            fills.append({
                "symbol":     symbol,
                "product_id": o.get("product_id"),
                "side":       o.get("side", ""),
                "size":       float(o.get("size", 0)),
                "price":      float(o.get("average_fill_price")),
                "time":       str(o.get("created_at", ""))[:19],
            })
        except (TypeError, ValueError):
            continue
    fills.sort(key=lambda f: f["time"])

    state: dict = {}
    trades = []
    for f in fills:
        symbol = f["symbol"]
        delta  = f["size"] if f["side"] == "buy" else -f["size"]
        st = state.get(symbol)

        if st is None or st["net_size"] == 0:
            state[symbol] = {
                "net_size": delta, "entry_price": f["price"], "entry_time": f["time"],
                "realized_pnl": 0.0, "exit_notional": 0.0, "exit_qty": 0.0,
                "product_id": f["product_id"],
            }
            continue

        cv = _product_info(st["product_id"])["contract_value"] if st["product_id"] else 0.001

        if (delta > 0) == (st["net_size"] > 0):
            old_abs, add_abs = abs(st["net_size"]), abs(delta)
            st["entry_price"] = (st["entry_price"] * old_abs + f["price"] * add_abs) / (old_abs + add_abs)
            st["net_size"] += delta
            continue

        old_abs, reduce_abs = abs(st["net_size"]), abs(delta)
        matched = min(old_abs, reduce_abs)
        sign    = 1 if st["net_size"] > 0 else -1
        st["realized_pnl"]  += (f["price"] - st["entry_price"]) * cv * matched * sign
        st["exit_notional"] += f["price"] * matched
        st["exit_qty"]      += matched
        st["net_size"]      += delta

        if abs(st["net_size"]) < 1e-9:
            exit_avg = st["exit_notional"] / st["exit_qty"] if st["exit_qty"] else f["price"]
            trades.append({
                "date":       st["entry_time"][:10],
                "entry_date": st["entry_time"][:10],
                "symbol":     symbol,
                "strike":     _parse_strike(symbol),
                "lots":       old_abs,
                "side":       "LONG" if sign > 0 else "SHORT",
                "entry_mark": round(st["entry_price"], 4),
                "exit_mark":  round(exit_avg, 4),
                "pnl_usd":    round(st["realized_pnl"], 2),
                "entry_time": st["entry_time"][11:],
                "exit_time":  f["time"][11:],
            })
            leftover = reduce_abs - old_abs
            if leftover > 1e-9:
                new_sign = -1 if f["side"] == "sell" else 1
                state[symbol] = {
                    "net_size": leftover * new_sign, "entry_price": f["price"], "entry_time": f["time"],
                    "realized_pnl": 0.0, "exit_notional": 0.0, "exit_qty": 0.0,
                    "product_id": f["product_id"],
                }
            else:
                state[symbol] = {"net_size": 0, "entry_price": 0, "entry_time": "",
                                  "realized_pnl": 0.0, "exit_notional": 0.0, "exit_qty": 0.0,
                                  "product_id": st["product_id"]}
    return trades


def _fetch_non_mv_trades() -> list:
    """Closed round-trip trades for every non-MV product (calls, puts,
    futures) the account has traded, reconstructed from order history."""
    if not API_KEY or not API_SECRET:
        return []
    try:
        hdrs = _sign("GET", "/v2/orders/history", "?page_size=500")
        r = req.get(f"{API_BASE}/v2/orders/history", params={"page_size": 500},
                    headers=hdrs, timeout=15)
        data = r.json()
        if not data.get("success"):
            return []
        return _reconstruct_trades_from_orders(data.get("result", []))
    except Exception:
        return []


def _all_trades_merged() -> list:
    """Bot-tracked MV straddle trades (precise, from trade_history.json)
    plus reconstructed non-MV trades (calls/puts/futures), sorted by date."""
    mv_trades = _load_json(HISTORY_FILE, [])
    other     = _fetch_non_mv_trades()
    merged    = mv_trades + other
    merged.sort(key=lambda t: (t.get("entry_date") or t.get("date", ""),
                                t.get("entry_time", "")))
    return merged


@app.route("/api/trades")
def api_trades():
    return jsonify(_all_trades_merged())


_product_cache: dict = {}   # product_id -> {"contract_value": float, "symbol": str}

def _product_info(product_id: int) -> dict:
    if product_id in _product_cache:
        return _product_cache[product_id]
    try:
        pr = req.get(f"{API_BASE}/v2/products/{product_id}", timeout=6).json().get("result", {})
        info = {"contract_value": float(pr.get("contract_value") or 0.001),
                "symbol": pr.get("symbol", "")}
    except Exception:
        info = {"contract_value": 0.001, "symbol": ""}
    _product_cache[product_id] = info
    return info


_fx_cache = {"rate": 0.0, "ts": 0.0}

def _usd_inr_rate() -> float:
    """USD->INR, cached for an hour (display-only, precision not critical)."""
    if _fx_cache["rate"] and time.time() - _fx_cache["ts"] < 3600:
        return _fx_cache["rate"]
    try:
        r = req.get("https://open.er-api.com/v6/latest/USD", timeout=8).json()
        rate = float(r.get("rates", {}).get("INR") or 0)
        if rate > 0:
            _fx_cache.update(rate=rate, ts=time.time())
        return rate
    except Exception:
        return _fx_cache["rate"]


@app.route("/api/wallet")
def api_wallet():
    """Account value in USD and INR."""
    if not API_KEY or not API_SECRET:
        return jsonify({"error": "no api credentials"}), 503
    try:
        hdrs = _sign("GET", "/v2/wallet/balances")
        r = req.get(f"{API_BASE}/v2/wallet/balances", headers=hdrs, timeout=8)
        data = r.json()
        if not data.get("success"):
            return jsonify({"error": data.get("error", "wallet fetch failed")}), 502
        usd_balance = usd_available = 0.0
        for w in data.get("result", []):
            if w.get("asset_symbol") == "USD":
                usd_balance   = float(w.get("balance") or 0)
                usd_available = float(w.get("available_balance") or 0)
                break
        rate = _usd_inr_rate()
        return jsonify({
            "usd_balance":   round(usd_balance, 2),
            "usd_available": round(usd_available, 2),
            "inr_balance":   round(usd_balance * rate, 2) if rate else None,
            "usd_inr_rate":  round(rate, 2) if rate else None,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/all-positions")
def api_all_positions():
    """Every currently open position on the Delta account — MV straddles,
    calls/puts, perpetual futures, anything — not just what the bot tracks."""
    if not API_KEY or not API_SECRET:
        return jsonify([])
    try:
        hdrs = _sign("GET", "/v2/positions/margined")
        r = req.get(f"{API_BASE}/v2/positions/margined", headers=hdrs, timeout=8)
        data = r.json()
        if not data.get("success"):
            return jsonify([])
        out = []
        for p in data.get("result", []):
            size = float(p.get("size", 0))
            if size == 0:
                continue
            product_id = int(p.get("product_id"))
            symbol     = p.get("product_symbol", "")
            entry      = float(p.get("entry_price") or 0)
            cv         = _product_info(product_id)["contract_value"]
            try:
                tk   = req.get(f"{API_BASE}/v2/tickers/{symbol}", timeout=6).json().get("result", {})
                mark = float(tk.get("mark_price") or 0)
            except Exception:
                mark = 0.0
            pnl = (mark - entry) * cv * size   # signed size handles long vs short
            out.append({
                "symbol":       symbol,
                "product_id":   product_id,
                "side":         "LONG" if size > 0 else "SHORT",
                "size":         abs(size),
                "entry_price":  entry,
                "mark_price":   mark,
                "live_pnl":     round(pnl, 2),
            })
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/summary")
def api_summary():
    return jsonify(_pnl_stats(_all_trades_merged()))


@app.route("/api/config", methods=["GET"])
def get_config():
    return jsonify({k: os.getenv(k, "") for k in CONFIG_KEYS})


def _restart_tp_monitor(slot: str) -> bool:
    """Stop and respawn a slot's TP monitor so freshly saved targets apply.
    Only called for monitors that are already running."""
    proc = _tp_procs.get(slot)
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        _tp_procs[slot] = None
    pid_file = SLOT_PID[slot]
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text().strip()), 15)
        except Exception:
            pass
        pid_file.unlink(missing_ok=True)
    script = BASE / "tp_monitor.py"
    state  = _load_json(SLOT_STATE[slot], {})
    if state.get("status") != "OPEN" or not script.exists():
        return False
    proc = subprocess.Popen(
        [sys.executable, str(script), "--slot", slot],
        cwd=str(BASE), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    _tp_procs[slot] = proc
    pid_file.write_text(str(proc.pid))
    return True


_TP_KEYS_BY_SLOT = {
    "evening": {"TP_TARGET_PNL", "TP_POLL_SECS"},
    "morning": {"TP_TARGET_PNL_MORNING", "TP_POLL_SECS_MORNING"},
}


@app.route("/api/config", methods=["POST"])
def set_config():
    data = request.json or {}
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    for key, val in data.items():
        if key not in CONFIG_KEYS:
            continue
        # Replace EVERY matching line, not just the first: append scripts had
        # produced duplicate keys, and since dotenv takes the LAST occurrence,
        # replacing only the first made saves silently ineffective.
        replaced = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith(key + "=") or stripped.startswith(key + " ="):
                lines[i] = f"{key} = {val}"
                replaced = True
        if not replaced:
            lines.append(f"{key} = {val}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    load_dotenv(override=True)
    # The bot watches .env and reloads itself; running TP monitors don't,
    # so bounce any whose slot's targets just changed.
    tp_restarted = []
    for slot, keys in _TP_KEYS_BY_SLOT.items():
        if keys & set(data) and _tp_running(slot):
            if _restart_tp_monitor(slot):
                tp_restarted.append(slot)
    return jsonify({"ok": True, "tp_restarted": tp_restarted})


@app.route("/api/test-telegram", methods=["POST"])
def test_telegram():
    token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID",   "")
    if not token or not chat_id:
        return jsonify({"ok": False, "error": "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not configured"}), 400
    try:
        r = req.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id":    chat_id,
                "text":       "✅ <b>MV-BTC Bot</b> — Telegram alerts are connected!\n<code>Test message from dashboard.</code>",
                "parse_mode": "HTML",
            },
            timeout=8,
        )
        result = r.json()
        if result.get("ok"):
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": result.get("description", "Telegram API error")}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _revive_tp_monitors():
    """TP monitors are dashboard-spawned subprocesses, so a host reboot kills
    them while their PID files and OPEN state survive. On startup, respawn any
    monitor that was running (stale PID file) and still has an open position —
    without this, a reboot silently drops take-profit protection."""
    script = BASE / "tp_monitor.py"
    for slot, pid_file in SLOT_PID.items():
        if not pid_file.exists():
            continue
        try:
            if _pid_alive(int(pid_file.read_text().strip())):
                continue          # survived (not a reboot) — leave it be
        except (ValueError, OSError):
            pass
        pid_file.unlink(missing_ok=True)
        state = _load_json(SLOT_STATE[slot], {})
        if state.get("status") != "OPEN" or not script.exists():
            continue
        proc = subprocess.Popen(
            [sys.executable, str(script), "--slot", slot],
            cwd=str(BASE), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        _tp_procs[slot] = proc
        pid_file.write_text(str(proc.pid))
        print(f"Revived {slot} TP monitor (pid {proc.pid}) after restart")


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 50)
    print("  MV-BTC Straddle Dashboard")
    print("  http://localhost:5001")
    print("=" * 50)
    _revive_tp_monitors()
    app.run(host="0.0.0.0", port=5001, debug=False)
