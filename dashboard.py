"""
dashboard.py — MV-BTC Straddle Web Dashboard (Mathi)
Run  : python dashboard.py
Open : http://localhost:5001
"""

import csv
import hashlib
import hmac
import json
import math
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests as req
from dotenv import load_dotenv, set_key
from flask import Flask, jsonify, request, abort

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

BASE         = Path(__file__).parent
STATE_FILE   = BASE / "straddle_state.json"
HISTORY_FILE = BASE / "trade_history.json"
BACKTEST_CSV = BASE / "backtest_mv_2026_daywise.csv"
ENV_FILE     = BASE / ".env"
TP_PID_FILE  = BASE / "tp_monitor.pid"

_tp_proc: subprocess.Popen | None = None


def _pid_alive(pid: int) -> bool:
    """Windows-safe process existence check (os.kill(pid, 0) terminates on Windows)."""
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


def _tp_running() -> bool:
    global _tp_proc
    if _tp_proc is not None:
        if _tp_proc.poll() is None:
            return True
        _tp_proc = None
    # Fallback: check PID file (survives dashboard restart)
    if TP_PID_FILE.exists():
        try:
            pid = int(TP_PID_FILE.read_text().strip())
            if _pid_alive(pid):
                return True
        except (ValueError, OSError):
            pass
        TP_PID_FILE.unlink(missing_ok=True)
    return False

# Keys the dashboard is allowed to read/write
CONFIG_KEYS = [
    "DRY_RUN", "STRADDLE_LOTS", "STRIKE_STEP",
    "ENTRY_H_UTC", "ENTRY_M_UTC", "EXIT_H_UTC", "EXIT_M_UTC",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_ALERTS",
    "TP_TARGET_PNL", "TP_POLL_SECS",
]

app = Flask(__name__, static_folder=str(BASE), static_url_path="/static")


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
    pnls   = [float(t.get("pnl_usd", 0)) for t in trades if t.get("pnl_usd") is not None]
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

def _sync_state_from_exchange(state: dict) -> dict:
    """Adopt any open MV-BTC position from the exchange that the local state
    file doesn't reflect (e.g. manually placed orders), so the dashboard
    always shows reality. Throttled to one authenticated call per 30s."""
    global _last_sync
    if not API_KEY or not API_SECRET or time.time() - _last_sync < 30:
        return state
    _last_sync = time.time()
    try:
        hdrs = _sign("GET", "/v2/positions/margined")
        r = req.get(f"{API_BASE}/v2/positions/margined", headers=hdrs, timeout=6)
        live = [p for p in r.json().get("result", [])
                if float(p.get("size", 0)) != 0
                and str(p.get("product_symbol", "")).startswith("MV-BTC")]
        if not live:
            return state
        p     = live[0]
        pid   = int(p["product_id"])
        size  = int(float(p["size"]))
        entry = float(p.get("entry_price") or 0)
        # Already in sync?
        if (state.get("status") == "OPEN"
                and int(state.get("product_id", 0)) == pid
                and int(state.get("lots", 0)) == size
                and abs(float(state.get("entry_mark", 0)) - entry) < 0.01):
            return state
        # Fetch product details for strike/settlement/contract value
        pr = req.get(f"{API_BASE}/v2/products/{pid}", timeout=6).json().get("result", {})
        cv = float(pr.get("contract_value") or 0.001)
        created = str(p.get("created_at", ""))
        state = {
            "status":         "OPEN",
            "entry_date":     created[:10] or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "entry_time_utc": created[11:19],
            "symbol":         pr.get("symbol", p.get("product_symbol", "")),
            "product_id":     pid,
            "strike":         float(pr.get("strike_price") or 0),
            "settlement":     pr.get("settlement_time", ""),
            "contract_value": cv,
            "lots":           size,
            "entry_mark":     entry,
            "btc_at_entry":   state.get("btc_at_entry", 0),
            "total_cost_usd": round(entry * cv * size, 2),
            "entry_trigger":  "exchange_sync",
        }
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        pass
    return state


@app.route("/api/status")
def api_status():
    state = _load_json(STATE_FILE, {})
    state = _sync_state_from_exchange(state)
    if state.get("status") == "OPEN":
        symbol = state.get("symbol", "")
        try:
            r    = req.get(f"{API_BASE}/v2/tickers/{symbol}", timeout=5)
            mark = float(r.json().get("result", {}).get("mark_price") or 0)
            cval = float(state.get("contract_value", 0.001))
            lots = int(state.get("lots", 1000))
            em   = float(state.get("entry_mark", 0))
            state["current_mark"] = round(mark, 4)
            state["live_pnl"]     = round((mark - em) * cval * lots, 2) if mark else None
        except Exception:
            state["current_mark"] = None
            state["live_pnl"]     = None
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



@app.route("/api/today-trades")
def api_today_trades():
    from datetime import date
    today = date.today().isoformat()
    trades  = _load_json(HISTORY_FILE, [])
    state   = _load_json(STATE_FILE, {})
    today_t = [t for t in trades if t.get("entry_date", "") == today]
    # Include current open position as a live row with real-time mark & P&L
    if state.get("status") == "OPEN" and state.get("entry_date", "") == today:
        state["_live"] = True
        try:
            symbol = state.get("symbol", "")
            r = req.get(f"{API_BASE}/v2/tickers/{symbol}", timeout=4)
            mark = float(r.json().get("result", {}).get("mark_price") or 0)
            em   = float(state.get("entry_mark", 0))
            cv   = float(state.get("contract_value", 0.001))
            lots = int(state.get("lots", 1000))
            state["current_mark"] = round(mark, 4)
            state["live_pnl"]     = round((mark - em) * cv * lots, 2)
        except Exception:
            pass
        today_t = [state] + today_t
    return jsonify(today_t)


@app.route("/api/square-off", methods=["POST"])
def api_square_off():
    state = _load_json(STATE_FILE, {})
    if state.get("status") != "OPEN":
        return jsonify({"ok": False, "error": "No open position"}), 400

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

        # Use actual exchange size (may differ from state)
        lots = live_size

        # Get current mark for P&L
        ticker = req.get(f"{API_BASE}/v2/tickers/{symbol}", timeout=5).json()
        mark   = float(ticker.get("result", {}).get("mark_price") or 0)

        # Place market sell
        import json as _json
        payload = {"product_id": product_id, "size": lots,
                   "side": "sell", "order_type": "market_order"}
        body    = _json.dumps(payload, separators=(",", ":"))
        hdrs    = _sign("POST", "/v2/orders", "", body)
        r       = req.post(f"{API_BASE}/v2/orders", data=body, headers=hdrs, timeout=15)
        result  = r.json()

        if result.get("success"):
            o       = result.get("result", {})
            fill    = float(o.get("average_fill_price") or mark)
            pnl     = round((fill - entry_mark) * cval * lots, 2)
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
            STATE_FILE.write_text(
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


@app.route("/api/tp-monitor", methods=["GET"])
def tp_monitor_status():
    try:
        target = float(os.getenv("TP_TARGET_PNL") or 105)
    except ValueError:
        target = 105.0
    try:
        poll = int(float(os.getenv("TP_POLL_SECS") or 30))
    except ValueError:
        poll = 30
    return jsonify({
        "running":    _tp_running(),
        "target_pnl": target,
        "poll_secs":  poll,
    })


@app.route("/api/tp-monitor/start", methods=["POST"])
def tp_monitor_start():
    global _tp_proc
    if _tp_running():
        return jsonify({"ok": False, "error": "Already running"}), 400
    state = _load_json(STATE_FILE, {})
    if state.get("status") != "OPEN":
        return jsonify({"ok": False, "error": "No open position to monitor"}), 400
    script = BASE / "tp_monitor.py"
    if not script.exists():
        return jsonify({"ok": False, "error": "tp_monitor.py not found"}), 404
    _tp_proc = subprocess.Popen(
        [sys.executable, str(script)],
        cwd=str(BASE),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    TP_PID_FILE.write_text(str(_tp_proc.pid))
    return jsonify({"ok": True, "pid": _tp_proc.pid})


@app.route("/api/tp-monitor/stop", methods=["POST"])
def tp_monitor_stop():
    global _tp_proc
    stopped = False
    if _tp_proc is not None and _tp_proc.poll() is None:
        _tp_proc.terminate()
        try:
            _tp_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _tp_proc.kill()
        _tp_proc = None
        stopped = True
    if TP_PID_FILE.exists():
        try:
            pid = int(TP_PID_FILE.read_text().strip())
            os.kill(pid, 15)   # SIGTERM
            stopped = True
        except Exception:
            pass
        TP_PID_FILE.unlink(missing_ok=True)
    if not stopped:
        return jsonify({"ok": False, "error": "Monitor is not running"}), 400
    return jsonify({"ok": True})


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


@app.route("/api/trades")
def api_trades():
    return jsonify(_load_json(HISTORY_FILE, []))


@app.route("/api/summary")
def api_summary():
    trades = _load_json(HISTORY_FILE, [])
    return jsonify(_pnl_stats(trades))


@app.route("/api/config", methods=["GET"])
def get_config():
    return jsonify({k: os.getenv(k, "") for k in CONFIG_KEYS})


@app.route("/api/config", methods=["POST"])
def set_config():
    data = request.json or {}
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    for key, val in data.items():
        if key not in CONFIG_KEYS:
            continue
        replaced = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith(key + "=") or stripped.startswith(key + " ="):
                lines[i] = f"{key} = {val}"
                replaced = True
                break
        if not replaced:
            lines.append(f"{key} = {val}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    load_dotenv(override=True)
    return jsonify({"ok": True})


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


@app.route("/api/import-backtest", methods=["POST"])
def import_backtest():
    """Populate trade_history.json from the 2026 backtest CSV (simulated trades)."""
    if not BACKTEST_CSV.exists():
        return jsonify({"ok": False, "error": "backtest CSV not found"}), 404

    existing = _load_json(HISTORY_FILE, [])
    existing_dates = {r.get("date") for r in existing}
    added = 0

    with open(BACKTEST_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            d = row.get("date", "")
            if d in existing_dates:
                continue
            existing.append({
                "date":         d,
                "symbol":       f"MV-BTC-{row.get('strike','0')}-SIM",
                "strike":       float(row.get("strike", 0)),
                "lots":         1000,
                "entry_mark":   float(row.get("prem_entry", 0)),
                "exit_mark":    float(row.get("prem_exit",  0)),
                "btc_entry":    float(row.get("btc_entry",  0)),
                "btc_exit":     float(row.get("btc_exit",   0)),
                "btc_move_pct": float(row.get("btc_move_pct", 0)),
                "pnl_usd":      float(row.get("pnl_usd",   0)),
                "cost_usd":     float(row.get("cost_usd",   0)),
                "entry_time":   "12:05:00",
                "exit_time":    "19:30:00",
                "source":       "backtest",
            })
            existing_dates.add(d)
            added += 1

    existing.sort(key=lambda r: r.get("date", ""))
    HISTORY_FILE.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    return jsonify({"ok": True, "added": added, "total": len(existing)})


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 50)
    print("  MV-BTC Straddle Dashboard")
    print("  http://localhost:5000")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5001, debug=False)
