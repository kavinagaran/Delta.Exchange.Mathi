"""
dashboard.py — NITHI-BOT · MV-BTC Straddle Web Dashboard
Run  : python dashboard.py
Open : http://localhost:5001
"""

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests as req
from dotenv import load_dotenv, set_key
from flask import (Flask, jsonify, request, abort, send_file, session,
                   redirect, render_template, has_request_context, g)

# Force IPv4 — Delta's whitelist holds our IPv4; IPv6 rotates and gets rejected
import socket as _socket
import urllib3.util.connection as _u3c
_u3c.allowed_gai_family = lambda: _socket.AF_INET

load_dotenv()

API_KEY    = os.getenv("API_KEY", "")
API_SECRET = os.getenv("API_SECRET", "")
API_BASE   = os.getenv("BASE_URL", "https://api.india.delta.exchange")

def _sign(method, path, query="", body="", key=None, secret=None):
    """Delta HMAC headers. Defaults to the ACTIVE account's credentials —
    the logged-in user's own keys (users/<name>/account.json), falling back
    to the .env keys for Basic-auth clients and non-request contexts. Every
    account is a full trading account scoped to its own folder."""
    if not key or not secret:
        key, secret = _active_creds()
    ts  = str(int(time.time()))
    msg = method + ts + path + query + body
    sig = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return {"api-key": key, "timestamp": ts, "signature": sig,
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

BASE     = Path(__file__).parent
ENV_FILE = BASE / ".env"

MOVE_SLOTS = ("morning", "evening")
SLOTS = (*MOVE_SLOTS, "trend")

SLOT_STATE_FILES = {
    "morning": "morning_state.json",
    "evening": "straddle_state.json",
    "trend":   "trend_state.json",
}

_tp_procs: dict = {}   # "<user>:<slot>" -> subprocess.Popen
_trend_entry_lock = threading.Lock()
_trend_auto_last_attempt: dict[str, float] = {}


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


def _pid_file(user: str, slot: str) -> Path:
    return USERS_DIR / user / f"tp_{slot}.pid"


def _tp_running(user: str, slot: str) -> bool:
    key  = f"{user}:{slot}"
    proc = _tp_procs.get(key)
    if proc is not None:
        if proc.poll() is None:
            return True
        _tp_procs[key] = None
    # Fallback: check PID file (survives dashboard restart)
    pid_file = _pid_file(user, slot)
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            if _pid_alive(pid):
                return True
        except (ValueError, OSError):
            pass
        pid_file.unlink(missing_ok=True)
    return False


def _spawn_tp(user: str, slot: str):
    """Start a TP monitor for a user's slot; returns the Popen or None."""
    script = BASE / "tp_monitor.py"
    if not script.exists():
        return None
    proc = subprocess.Popen(
        [sys.executable, str(script), "--slot", slot, "--user", user],
        cwd=str(BASE), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    _tp_procs[f"{user}:{slot}"] = proc
    pf = _pid_file(user, slot)
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(str(proc.pid))
    return proc

# Keys the dashboard is allowed to read/write
CONFIG_KEYS = [
    "DRY_RUN", "STRADDLE_LOTS", "STRIKE_STEP",
    "EVENING_ENABLED", "EVENING_EXIT_ENABLED", "EVENING_SIDE",
    "ENTRY_H_UTC", "ENTRY_M_UTC", "EXIT_H_UTC", "EXIT_M_UTC",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_ALERTS",
    "TP_TARGET_PNL", "TP_POLL_SECS", "SL_TARGET_PNL", "TSL_TARGET_PNL",
    "MORNING_ENABLED", "MORNING_LOTS", "MORNING_H_UTC", "MORNING_M_UTC",
    "MORNING_EXIT_ENABLED", "MORNING_EXIT_H_UTC", "MORNING_EXIT_M_UTC",
    "MORNING_SIDE",
    "DYNAMIC_LOTS",
    "TP_TARGET_PNL_MORNING", "TP_POLL_SECS_MORNING", "SL_TARGET_PNL_MORNING",
    "TSL_TARGET_PNL_MORNING",
    "TREND_LOTS", "TP_TARGET_PNL_TREND", "TP_POLL_SECS_TREND",
    "SL_TARGET_PNL_TREND", "TSL_TARGET_PNL_TREND",
    "TREND_AUTO_ENTRY_ENABLED",
    "MAX_TRADES_PER_DAY",
]

app = Flask(__name__,
            static_folder=str(BASE / "static"), static_url_path="/static",
            template_folder=str(BASE / "templates"))


@app.template_global()
def asset_v(path: str) -> str:
    """Cache-busting version for a static asset: its mtime. Deploys restart
    the dashboard, so a changed file always gets a fresh URL and browsers
    never serve a stale stylesheet/script."""
    try:
        return str(int((BASE / "static" / path).stat().st_mtime))
    except OSError:
        return "0"

# Stable session secret across restarts so logins survive a redeploy.
_SECRET_FILE = BASE / ".dash_secret"
if not _SECRET_FILE.exists():
    _SECRET_FILE.write_text(secrets.token_hex(32), encoding="utf-8")
app.secret_key = _SECRET_FILE.read_text(encoding="utf-8").strip()
app.permanent_session_lifetime = timedelta(days=30)

# ─────────────────────────────────────────────────────────────
# ACCOUNTS & AUTH — one folder per user
#
# users/<username>/ (gitignored) holds everything belonging to an account:
#   account.json          credentials: display name, password hash, API keys
#   straddle_state.json   that user's evening slot
#   morning_state.json    that user's morning slot
#   trend_state.json      that user's independent CE/PE trend position
#   trade_history.json    that user's closed trades
#   tp_evening.pid / tp_morning.pid / tp_trend.pid   TP monitor PIDs
#
# EVERY account is a full (primary) account: whoever is logged in trades
# with their own Delta keys against their own state files. The scheduled
# bot engine (Delta_Straddle_Live.py) runs on BOT_USER's folder and keys.
#
# The Android app keeps using HTTP Basic (DASH_USER/DASH_PASS from .env) on
# /api/* — that path must never break; it maps to DASH_USER's account.
# ─────────────────────────────────────────────────────────────
# Default must stay "mathi" — the Android app hardcodes it for Basic auth.
DASH_USER = os.getenv("DASH_USER", "mathi")
DASH_PASS = os.getenv("DASH_PASS", "")
BOT_USER  = os.getenv("BOT_USER", DASH_USER)   # account the scheduled bot trades

USERS_DIR = BASE / "users"
_LEGACY_ACCOUNTS_FILE = BASE / "accounts.json"

_USERNAME_RE = re.compile(r"^[a-z0-9_-]{2,24}$")

_DATA_FILES = (*SLOT_STATE_FILES.values(), "trade_history.json")


def _safe_user(username: str) -> str:
    """Usernames become directory names — reject anything path-unsafe."""
    u = (username or "").strip().lower()
    return u if _USERNAME_RE.match(u) else ""


def _udir(username: str) -> Path:
    return USERS_DIR / _safe_user(username)


def _account_file(username: str) -> Path:
    return _udir(username) / "account.json"


def _hash_pw(password: str, salt: str = "") -> str:
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return f"{salt}${h.hex()}"


def _verify_pw(password: str, stored: str) -> bool:
    try:
        salt, _ = stored.split("$", 1)
        return hmac.compare_digest(_hash_pw(password, salt), stored)
    except (ValueError, AttributeError):
        return False


# PBKDF2 is deliberately slow (~100k+ iterations) — Basic-auth clients like
# the Android app resend credentials on EVERY request, so memoize the verdict
# per (username, password-hash) to avoid burning CPU on each poll.
_basic_cache: dict = {}


def _save_account(acct: dict) -> None:
    d = _udir(acct["username"])
    d.mkdir(parents=True, exist_ok=True)
    (d / "account.json").write_text(json.dumps(acct, indent=2), encoding="utf-8")
    _drop_basic_cache(acct["username"])


def _drop_basic_cache(username: str) -> None:
    """Forget memoized Basic-auth verdicts for a user so a password change
    or account deletion takes effect immediately, not on next restart."""
    for k in [k for k in _basic_cache if k[0] == username]:
        del _basic_cache[k]


def _bootstrap_users() -> None:
    """One-time setup/migration of the users/ tree:
    1. Migrate legacy accounts.json entries into users/<name>/account.json.
    2. Or, on a truly fresh install, create BOT_USER's account from .env.
    3. Move the legacy repo-root state/history files into BOT_USER's folder
       (they always belonged to the bot's account)."""
    USERS_DIR.mkdir(exist_ok=True)
    legacy = _load_json(_LEGACY_ACCOUNTS_FILE, None)
    if isinstance(legacy, dict):
        legacy = legacy.get("accounts", [])
    if legacy:
        for a in legacy:
            a.pop("primary", None)          # every account is primary now
            if _safe_user(a.get("username", "")) and not _account_file(a["username"]).exists():
                _save_account(a)
        _LEGACY_ACCOUNTS_FILE.rename(_LEGACY_ACCOUNTS_FILE.with_suffix(".json.migrated"))
    if not _account_file(BOT_USER).exists():
        _save_account({
            "username":     _safe_user(BOT_USER) or "mathi",
            "display_name": os.getenv("ACCOUNT_NAME", BOT_USER.capitalize()),
            "pw_hash":      _hash_pw(DASH_PASS or BOT_USER),
            "api_key":      API_KEY,
            "api_secret":   API_SECRET,
        })
    for name in _DATA_FILES:
        src, dst = BASE / name, _udir(BOT_USER) / name
        if src.exists() and not dst.exists():
            shutil.move(str(src), str(dst))


def _load_accounts() -> list:
    if not USERS_DIR.exists() or _LEGACY_ACCOUNTS_FILE.exists() \
       or not _account_file(BOT_USER).exists():
        _bootstrap_users()
    out = []
    for d in sorted(USERS_DIR.iterdir()):
        acct = _load_json(d / "account.json", None)
        if isinstance(acct, dict) and acct.get("username"):
            out.append(acct)
    return out


def _find_account(username: str) -> dict | None:
    u = _safe_user(username)
    if not u:
        return None
    acct = _load_json(_account_file(u), None)
    if not (isinstance(acct, dict) and acct.get("username")):
        # Bootstrap path (first request ever may look up the login user)
        acct = next((a for a in _load_accounts()
                     if a.get("username", "").lower() == u), None)
    return acct


def _basic_account_ok(username: str, password: str) -> bool:
    u = _safe_user(username)
    if not u:
        return False
    key = (u, hashlib.sha256(password.encode()).hexdigest())
    if key in _basic_cache:
        return _basic_cache[key]
    acct = _find_account(u)
    ok = bool(acct and _verify_pw(password, acct.get("pw_hash", "")))
    if len(_basic_cache) > 256:
        _basic_cache.clear()
    _basic_cache[key] = ok
    return ok


def _session_account() -> dict | None:
    if has_request_context():
        if session.get("user"):
            return _find_account(session["user"])
        u = getattr(g, "basic_user", None)   # Basic-auth (Android app) account
        if u:
            return _find_account(u)
    return None


def _active_user() -> str:
    """The account all data/creds are scoped to for this request: the
    session user, else DASH_USER (Basic-auth Android app, local dev)."""
    acct = _session_account()
    return acct["username"] if acct else (_safe_user(DASH_USER) or "mathi")


def _active_creds() -> tuple:
    """(api_key, api_secret) of the active account. A logged-in account uses
    ONLY its own keys — an account without keys gets none, never another
    account's. The .env keys back just the legacy no-account paths
    (DASH_USER/DASH_PASS Basic auth, local dev with no users tree)."""
    acct = _session_account()
    if acct:
        return acct.get("api_key", ""), acct.get("api_secret", "")
    acct = _find_account(DASH_USER)
    if acct and acct.get("api_key") and acct.get("api_secret"):
        return acct["api_key"], acct["api_secret"]
    return API_KEY, API_SECRET


# Per-request data paths — every user has their own slot state and history.
def _user_dir() -> Path:
    d = _udir(_active_user())
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slot_file(slot: str) -> Path:
    return _user_dir() / SLOT_STATE_FILES.get(slot, SLOT_STATE_FILES["evening"])


def _hist_file() -> Path:
    return _user_dir() / "trade_history.json"


def _cfg_file() -> Path:
    return _user_dir() / "config.json"


def _user_cfg() -> dict:
    """The active account's strategy config: .env values as global defaults,
    overridden key by key by users/<name>/config.json."""
    cfg = {k: os.getenv(k, "") for k in CONFIG_KEYS}
    saved = _load_json(_cfg_file(), {})
    if isinstance(saved, dict):
        cfg.update({k: str(v) for k, v in saved.items() if k in CONFIG_KEYS})
    return cfg


def _cfg(key: str, default: str = "") -> str:
    v = _user_cfg().get(key, "")
    return v if v != "" else default


def _cfg_bool(key: str, default: bool = False) -> bool:
    v = _cfg(key)
    return v.lower() in ("1", "true", "yes") if v else default


@app.before_request
def _auth_gate():
    open_paths = ("/login", "/static/", "/favicon.ico", "/health")
    if request.path.startswith(open_paths):
        return None
    # Path 1: HTTP Basic (Android app, curl) — either the .env credentials
    # (maps to DASH_USER's account) or ANY account's own username/password,
    # so the app can sign in and switch between accounts.
    a = request.authorization
    if a:
        if DASH_PASS and a.username == DASH_USER and a.password == DASH_PASS:
            return None
        if _basic_account_ok(a.username or "", a.password or ""):
            g.basic_user = _safe_user(a.username)
            return None
    # Path 2: browser session
    if session.get("user") and _find_account(session["user"]):
        return None
    if not DASH_PASS and not USERS_DIR.exists():
        return None   # local dev box with no password configured
    if request.path.startswith("/api/") or request.path.startswith("/download/"):
        return jsonify({"error": "authentication required"}), 401
    return redirect("/login")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if session.get("user") and _find_account(session["user"]):
            return redirect("/")
        return render_template("login.html")
    data = request.get_json(silent=True) or request.form
    acct = _find_account(data.get("username", ""))
    if not acct or not _verify_pw(data.get("password", ""), acct.get("pw_hash", "")):
        return jsonify({"ok": False, "error": "Invalid username or password"}), 401
    session.permanent = True
    session["user"] = acct["username"]
    return jsonify({"ok": True, "display_name": acct.get("display_name", acct["username"])})


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return redirect("/login")


@app.route("/health")
def health():
    return jsonify({"ok": True})


@app.route("/api/me")
def api_me():
    acct = _session_account()
    if acct:
        return jsonify({"username":     acct["username"],
                        "display_name": acct.get("display_name", acct["username"]),
                        "bot":          acct["username"] == _safe_user(BOT_USER)})
    return jsonify({"username": DASH_USER,
                    "display_name": os.getenv("ACCOUNT_NAME", DASH_USER.capitalize()),
                    "bot": True})


def _mask(s: str) -> str:
    return (s[:4] + "•" * 8 + s[-4:]) if s and len(s) > 8 else ("•" * 8 if s else "")


@app.route("/api/accounts", methods=["GET"])
def api_accounts_list():
    return jsonify([{
        "username":     a.get("username", ""),
        "display_name": a.get("display_name", ""),
        "api_key":      _mask(a.get("api_key", "")),
        "has_secret":   bool(a.get("api_secret")),
        "bot":          a.get("username", "").lower() == _safe_user(BOT_USER),
    } for a in _load_accounts()])


@app.route("/api/accounts", methods=["POST"])
def api_accounts_save():
    """Create or update an account. Every account is a full (primary)
    account trading its own keys against its own data folder."""
    data = request.get_json(silent=True) or {}
    username = _safe_user(data.get("username", ""))
    if not username:
        return jsonify({"ok": False,
                        "error": "Username must be 2-24 chars: a-z, 0-9, - or _"}), 400
    acct   = _find_account(username)
    is_new = acct is None
    if is_new:
        if not data.get("password"):
            return jsonify({"ok": False, "error": "password is required for a new account"}), 400
        acct = {"username": username}
    if data.get("display_name"):
        acct["display_name"] = data["display_name"].strip()
    if data.get("password"):
        acct["pw_hash"] = _hash_pw(data["password"])
    # Keys are optional on update — an empty field means "keep existing"
    if data.get("api_key"):
        acct["api_key"] = data["api_key"].strip()
    if data.get("api_secret"):
        acct["api_secret"] = data["api_secret"].strip()
    _save_account(acct)
    return jsonify({"ok": True, "created": is_new})


@app.route("/api/accounts/<username>", methods=["DELETE"])
def api_accounts_delete(username):
    username = _safe_user(username)
    acct = _find_account(username)
    if not acct:
        return jsonify({"ok": False, "error": "No such account"}), 404
    if username == _safe_user(BOT_USER):
        return jsonify({"ok": False, "error": "The bot engine's account cannot be deleted"}), 400
    if _session_account() and _session_account()["username"] == username:
        return jsonify({"ok": False, "error": "You cannot delete the account you are signed in as"}), 400
    if _bot_active(username):
        return jsonify({"ok": False, "error": "Stop this account's bot before deleting it"}), 400
    # Remove only the login (account.json); trade data stays on disk so
    # history is never silently destroyed.
    _account_file(username).unlink(missing_ok=True)
    _drop_basic_cache(username)
    return jsonify({"ok": True})


@app.route("/api/accounts/test", methods=["POST"])
def api_accounts_test():
    """Verify a key/secret pair actually authenticates against Delta."""
    data = request.get_json(silent=True) or {}
    key, secret = data.get("api_key", "").strip(), data.get("api_secret", "").strip()
    if not key or not secret:
        # Fall back to a stored account's keys
        acct = _find_account(data.get("username", ""))
        if acct:
            key, secret = acct.get("api_key", ""), acct.get("api_secret", "")
    if not key or not secret:
        return jsonify({"ok": False, "error": "No credentials to test"}), 400
    try:
        hdrs = _sign("GET", "/v2/wallet/balances", key=key, secret=secret)
        r = req.get(f"{API_BASE}/v2/wallet/balances", headers=hdrs, timeout=8).json()
        if r.get("success"):
            usd = next((w for w in r.get("result", []) if w.get("asset_symbol") == "USD"), {})
            return jsonify({"ok": True, "usd_balance": round(float(usd.get("balance") or 0), 2)})
        return jsonify({"ok": False, "error": str(r.get("error", "authentication failed"))}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


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
_PAGES = {
    "":          ("overview.html",  "Overview"),
    "trades":    ("trades.html",    "Trades & P&L"),
    "positions": ("positions.html", "Positions"),
    "config":    ("config.html",    "Bot Config"),
    "accounts":  ("accounts.html",  "API Accounts"),
    "logs":      ("logs.html",      "Logs"),
}


@app.route("/")
@app.route("/<page>")
def render_page(page=""):
    if page not in _PAGES:
        abort(404)
    tmpl, title = _PAGES[page]
    acct = _session_account()
    return render_template(
        tmpl,
        page=page or "overview",
        page_title=title,
        display_name=(acct or {}).get("display_name",
                       os.getenv("ACCOUNT_NAME", DASH_USER.capitalize())),
    )


_last_sync: dict = {}   # username -> last exchange-sync epoch

def _state_matches(state: dict, pid: int, size: int, entry: float) -> bool:
    side = "short" if size < 0 else "long"
    return (state.get("status") == "OPEN"
            and int(state.get("product_id", 0) or 0) == pid
            and int(state.get("lots", 0) or 0) == abs(size)
            and state.get("side", "long") == side
            and abs(float(state.get("entry_mark", 0) or 0) - entry) < 0.01)


def _closed_blocks_adoption(other: dict, created: str) -> bool:
    """Should a CLOSED slot record block a new exchange position (created at
    `created`, ISO UTC) from being adopted into that slot?  Only while the new
    position isn't provably newer than the close — a close that happened
    before the new trade was even opened is pure history (already recorded in
    trade_history.json) and must not strand the slot. Blocking on *any*
    exit_time_utc, as this used to, left a live manual trade invisible on
    2026-07-09 once both slots held same-day closes."""
    if other.get("status") != "CLOSED" or not other.get("exit_time_utc"):
        return False
    try:
        entry_dt = datetime.strptime(
            f"{other.get('entry_date', '')} {other.get('entry_time_utc') or '00:00:00'}",
            "%Y-%m-%d %H:%M:%S")
        exit_dt = datetime.combine(
            entry_dt.date(),
            datetime.strptime(other["exit_time_utc"], "%H:%M:%S").time())
        if exit_dt < entry_dt:
            exit_dt += timedelta(days=1)  # exit rolled past midnight UTC
        created_dt = datetime.strptime(created[:19], "%Y-%m-%dT%H:%M:%S")
        return created_dt <= exit_dt
    except (ValueError, TypeError):
        return True  # can't prove the new position is newer — keep protecting


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
        # A LONG closes with a SELL fill; a SHORT closes with a BUY fill.
        # Looking only at sells (as this used to) attributed a short's exit
        # to one of its own opening sells — wrong price, wrong P&L, and the
        # real buy-back order never appeared anywhere.
        is_short   = state.get("side") == "short"
        close_side = "buy" if is_short else "sell"
        fills = [o for o in orders
                 if o.get("product_id") == pid and o.get("side") == close_side
                 and o.get("state") == "closed" and o.get("average_fill_price")]
        if not fills:
            return state  # Can't reconcile yet — leave as-is, try again next sync
        fills.sort(key=lambda o: str(o.get("created_at", "")), reverse=True)
        fill    = fills[0]
        exit_mk = float(fill["average_fill_price"])
        cv      = float(state.get("contract_value", 0.001))
        lots    = int(state.get("lots", 0))
        entry   = float(state.get("entry_mark", 0))
        sign    = -1 if is_short else 1
        pnl     = round((exit_mk - entry) * cv * lots * sign, 2)
        state.update({
            "status":        "CLOSED",
            "exit_time_utc": str(fill.get("created_at", ""))[11:19],
            "exit_mark":      exit_mk,
            "pnl_usd":        pnl,
            "exit_trigger":   "reconciled_stale",
            "exit_order_id":  fill.get("id"),
        })
        _slot_file(slot).write_text(json.dumps(state, indent=2), encoding="utf-8")
        hist = _load_json(_hist_file(), [])
        if isinstance(hist, list):
            rec = {**state, "date": state.get("entry_date", ""),
                   "entry_time": state.get("entry_time_utc", ""),
                   "exit_time":  state.get("exit_time_utc", ""),
                   "cost_usd":   state.get("total_cost_usd", 0)}
            dup = any(h.get("symbol") == rec["symbol"]
                      and (h.get("entry_time") or h.get("entry_time_utc")) == rec["entry_time"]
                      for h in hist)
            if not dup:
                hist.append(rec)
                _hist_file().write_text(json.dumps(hist, indent=2), encoding="utf-8")
    except Exception:
        pass
    return state


def _sync_states_from_exchange() -> None:
    """Adopt the ACTIVE user's positions without crossing strategy ownership.

    MV-BTC contracts belong only to the morning/evening MOVE channels. A
    vanilla BTC call/put belongs only to the independent trend channel. This
    separation is important: each state owns its own exit orders and monitor.
    Throttled to one authenticated call per user per 30s.
    """
    key, secret = _active_creds()
    if not key or not secret:
        return
    user = _active_user()
    if time.time() - _last_sync.get(user, 0) < 30:
        return
    _last_sync[user] = time.time()
    try:
        hdrs = _sign("GET", "/v2/positions/margined")
        r = req.get(f"{API_BASE}/v2/positions/margined", headers=hdrs, timeout=6)
        data = r.json()
        if not data.get("success"):
            return
        live = [p for p in data.get("result", [])
                if float(p.get("size", 0)) != 0
                and str(p.get("product_symbol", "")).startswith(("MV-BTC", "C-BTC", "P-BTC"))]
        live_pids = {int(p["product_id"]) for p in live}
        states = {slot: _load_json(_slot_file(slot), {}) for slot in SLOTS}

        # One-time migration for states created by the previous sync logic,
        # which incorrectly put manually opened C/P options into a MOVE slot.
        # This changes dashboard ownership only; it does not touch the live
        # position or start a monitor with protection values the user has not
        # reviewed. Newly entered trend trades start their monitor normally.
        if states["trend"].get("status") != "OPEN":
            for legacy_slot in MOVE_SLOTS:
                legacy = states[legacy_slot]
                if legacy.get("status") == "OPEN" and str(legacy.get("symbol", "")).startswith(("C-BTC", "P-BTC")):
                    migrated = {**legacy, "slot": "trend", "migrated_from_slot": legacy_slot}
                    idle = {"slot": legacy_slot, "status": "IDLE",
                            "migrated_to": "trend", "migrated_product_id": legacy.get("product_id")}
                    _slot_file("trend").write_text(json.dumps(migrated, indent=2), encoding="utf-8")
                    _slot_file(legacy_slot).write_text(json.dumps(idle, indent=2), encoding="utf-8")
                    states["trend"], states[legacy_slot] = migrated, idle
                    break
        states = {slot: _reconcile_stale_close(slot, s, live_pids) for slot, s in states.items()}
        for p in live:
            pid   = int(p["product_id"])
            size  = int(float(p["size"]))
            entry = float(p.get("entry_price") or 0)
            if any(_state_matches(s, pid, size, entry) for s in states.values()):
                continue
            product_symbol = str(p.get("product_symbol", ""))
            created = str(p.get("created_at", ""))
            if product_symbol.startswith(("C-BTC", "P-BTC")):
                slot = "trend"
                other = states[slot]
                if (other.get("status") == "OPEN" and int(other.get("product_id", 0) or 0) != pid) \
                   or _closed_blocks_adoption(other, created):
                    continue
            else:
                try:
                    # Bucket MOVE positions by IST time-of-day, not raw UTC.
                    h_utc, m_utc = int(created[11:13]), int(created[14:16])
                    ist_hour = ((h_utc * 60 + m_utc + 330) % 1440) // 60
                except (ValueError, IndexError):
                    ist_hour = 12
                slot = "morning" if ist_hour < 11 else "evening"
                # Don't clobber a different open MOVE position; try the other
                # MOVE channel, but never spill it into the trend channel.
                other = states[slot]
                if (other.get("status") == "OPEN" and int(other.get("product_id", 0) or 0) != pid) \
                   or _closed_blocks_adoption(other, created):
                    slot = "evening" if slot == "morning" else "morning"
                    other = states[slot]
                    if (other.get("status") == "OPEN" and int(other.get("product_id", 0) or 0) != pid) \
                       or _closed_blocks_adoption(other, created):
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
            _slot_file(slot).write_text(json.dumps(new_state, indent=2), encoding="utf-8")
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


_last_revive = {"ts": 0.0}


@app.route("/api/status")
def api_status():
    # Piggyback monitor revival on the UI's status polling: a TP monitor that
    # died mid-day (OOM, crash) gets respawned within a minute instead of
    # only at dashboard startup.
    if time.time() - _last_revive["ts"] > 60:
        _last_revive["ts"] = time.time()
        try:
            _revive_tp_monitors()
        except Exception:
            pass
    _sync_states_from_exchange()
    state   = _enrich_live(_load_json(_slot_file("evening"), {}))
    morning = _enrich_live(_load_json(_slot_file("morning"), {}))
    trend   = _enrich_live(_load_json(_slot_file("trend"), {}))
    state["morning"] = morning
    state["trend"]   = trend
    # BTC futures (perpetual) live price
    try:
        r_btc = req.get(
            "https://api.india.delta.exchange/v2/tickers/BTCUSD",
            timeout=5,
        )
        state["btc_futures_price"] = float(r_btc.json().get("result", {}).get("mark_price") or 0)
    except Exception:
        state["btc_futures_price"] = None
    # IST schedule strings for the UI, from the active account's own config
    cfg = _user_cfg()
    def _ist_str(h_key, m_key, dflt_h, dflt_m):
        try:
            h, m = int(cfg.get(h_key) or dflt_h), int(cfg.get(m_key) or dflt_m)
        except ValueError:
            h, m = dflt_h, dflt_m
        t = (h * 60 + m + 330) % 1440
        hh, mm = divmod(t, 60)
        return f"{(hh + 11) % 12 + 1}:{mm:02d} {'PM' if hh >= 12 else 'AM'} IST"
    def _cfg_on(key, default):
        v = cfg.get(key, "")
        return v.lower() in ("1", "true", "yes") if v else default
    state["entry_ist"]         = _ist_str("ENTRY_H_UTC", "ENTRY_M_UTC", 12, 5)
    state["exit_ist"]          = (_ist_str("EXIT_H_UTC", "EXIT_M_UTC", 19, 30)
                                  if _cfg_on("EVENING_EXIT_ENABLED", True)
                                  else "TP / settlement only")
    state["morning_entry_ist"] = _ist_str("MORNING_H_UTC", "MORNING_M_UTC", 0, 15)
    state["morning_exit_ist"]  = (_ist_str("MORNING_EXIT_H_UTC", "MORNING_EXIT_M_UTC", 11, 30)
                                  if _cfg_on("MORNING_EXIT_ENABLED", False)
                                  else "TP / settlement only")
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
    trades  = _load_json(_hist_file(), [])
    today_t = [t for t in trades
               if _ist_calendar_date(t.get("entry_date") or t.get("date", ""),
                                      t.get("entry_time") or t.get("entry_time_utc", "")) == today_ist]
    # Include any open slot position as a live row with real-time mark & P&L
    for slot in SLOTS:
        s = _load_json(_slot_file(slot), {})
        if s.get("status") == "OPEN" and _ist_calendar_date(s.get("entry_date", ""), s.get("entry_time_utc", "")) == today_ist:
            s["_live"] = True
            s["slot"]  = slot
            s = _enrich_live(s)
            today_t = [s] + today_t
    return jsonify(today_t)


@app.route("/api/square-off", methods=["POST"])
def api_square_off():
    slot       = _slot_arg()
    state_file = _slot_file(slot)
    state      = _load_json(state_file, {})
    if state.get("status") != "OPEN":
        return jsonify({"ok": False, "error": f"No open {slot} position"}), 400

    product_id = int(state.get("product_id", 0))
    symbol     = state.get("symbol", "")
    lots       = int(state.get("lots", 1000))
    entry_mark = float(state.get("entry_mark", 0))
    cval       = float(state.get("contract_value", 0.001))

    # Simulated positions have no exchange position to close. Keep the dry-run
    # workflow usable while making it impossible for this path to place an order.
    if state.get("dry_run"):
        try:
            mark = float(req.get(f"{API_BASE}/v2/tickers/{symbol}", timeout=5)
                         .json().get("result", {}).get("mark_price") or entry_mark)
        except Exception:
            mark = entry_mark
        sign = -1 if state.get("side") == "short" else 1
        pnl = round((mark - entry_mark) * cval * lots * sign, 2)
        state.update({"status": "CLOSED", "exit_time_utc": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                      "exit_mark": mark, "pnl_usd": pnl,
                      "exit_trigger": "manual_squareoff_simulated"})
        state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
        return jsonify({"ok": True, "pnl": pnl, "fill": mark,
                        "order_id": None, "dry_run": True})

    key, secret = _active_creds()
    if not key or not secret:
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

        # Cancel any resting exchange stop/TP the monitor owns for this
        # position BEFORE closing — they must not race our market close, and
        # an orphaned reduce-only stop could fire against a later position
        # in the same contract.
        for oid_key in ("tsl_stop_order_id", "tp_stop_order_id"):
            oid = state.get(oid_key)
            if oid:
                try:
                    body_c = json.dumps({"id": oid, "product_id": product_id},
                                        separators=(",", ":"))
                    hdrs_c = _sign("DELETE", "/v2/orders", "", body_c)
                    req.delete(f"{API_BASE}/v2/orders", data=body_c,
                               headers=hdrs_c, timeout=10)
                except Exception:
                    pass

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
                   "side": close_side, "order_type": "market_order",
                   "reduce_only": True}
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
            trades   = _load_json(_hist_file(), [])
            if not isinstance(trades, list):
                trades = []
            trades.append(state)
            _hist_file().write_text(
                __import__("json").dumps(trades, indent=2), encoding="utf-8")
            return jsonify({"ok": True, "pnl": pnl, "fill": fill,
                            "order_id": o.get("id")})
        else:
            return jsonify({"ok": False, "error": result.get("error", result)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _slot_arg(move_only: bool = False) -> str:
    slot = request.args.get("slot", "")
    if not slot:
        slot = (request.get_json(silent=True) or {}).get("slot", "")
    allowed = MOVE_SLOTS if move_only else SLOTS
    return slot if slot in allowed else "evening"


def _send_telegram(text: str) -> None:
    token = _cfg("TELEGRAM_BOT_TOKEN")
    chat  = _cfg("TELEGRAM_CHAT_ID")
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


# Rejection codes meaning "balance doesn't cover this size" — fixable by
# downsizing, never by retrying the same order.
BALANCE_REJECTIONS = ("insufficient_commission", "insufficient_margin",
                      "insufficient_balance")


def _downsized_lots(size: int, ctx: dict) -> int | None:
    """The exchange's rejection context reports available_balance and
    required_additional_balance — together the TRUE total cost of the
    rejected order (margin + premium + commission, whatever Delta's formula
    is), so the per-lot cost and truly affordable size follow exactly.
    None = can't downsize (context unusable or already at 1 lot)."""
    try:
        avail = float(ctx.get("available_balance") or 0)
        extra = float(ctx.get("required_additional_balance") or 0)
    except (TypeError, ValueError):
        return None
    if avail <= 0 or extra <= 0 or size <= 1:
        return None
    per_lot = (avail + extra) / size
    new = min(int(avail * 0.98 / per_lot), size - 1)   # 2% slippage buffer
    return new if new >= 1 else None


def _manual_entry_lots(slot: str, mark: float, cv: float, strike: float = 0.0) -> int:
    """Usual sizing: slot's configured lots, sized DOWN by dynamic sizing
    (min of configured and affordable-with-balance) when DYNAMIC_LOTS is on —
    an order never exceeds either the configured size or the balance.
    Delta charges the options taker fee on the underlying NOTIONAL, not the
    premium, so each lot must be funded for premium + fee or the exchange
    rejects the order with insufficient_commission."""
    lot_key, lot_default = {
        "morning": ("MORNING_LOTS", "2000"),
        "evening": ("STRADDLE_LOTS", "800"),
        "trend":   ("TREND_LOTS", "100"),
    }.get(slot, ("STRADDLE_LOTS", "800"))
    try:
        configured = max(int(_cfg(lot_key, lot_default)), 1)
    except ValueError:
        configured = int(lot_default)
    max_lots = int(os.getenv("MAX_ORDER_LOTS", 5000))
    # Trend entries always honor min(configured, affordable), even if the
    # legacy MOVE sizing toggle is disabled. This prevents an automatic trend
    # order from ever using the configured lots as an unsafe upper bound.
    is_trend = slot == "trend"
    if not is_trend and not _cfg_bool("DYNAMIC_LOTS", True):
        return min(configured, max_lots)
    try:
        hdrs = _sign("GET", "/v2/wallet/balances")
        data = req.get(f"{API_BASE}/v2/wallet/balances", headers=hdrs, timeout=8).json()
        if not data.get("success"):
            raise RuntimeError("wallet balance unavailable")
        bal = 0.0
        for w in data.get("result", []):
            if w.get("asset_symbol") == "USD":
                bal = float(w.get("available_balance") or 0)
                break
        # 0.05% of notional pads Delta's 0.03% taker rate; the exchange caps
        # the fee at 10% of premium (also the fallback when strike is unknown).
        fee = (min(0.0005 * strike, 0.10 * mark) if strike > 0 else 0.10 * mark) * cv
        afford = int((bal * 0.98) / (mark * cv + fee)) if mark > 0 else 0
        affordable = min(configured, afford, max_lots)
        return max(affordable, 0) if is_trend else max(affordable, 1)
    except Exception:
        # A Trend entry must not proceed when affordability cannot be
        # established; MOVE keeps its historical configured-lot fallback.
        return 0 if is_trend else min(configured, max_lots)


@app.route("/api/manual-entry/preview")
def api_manual_entry_preview():
    """What a manual buy/sell would do right now: contract, strike, sizing."""
    slot = _slot_arg(move_only=True)
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
    lots = _manual_entry_lots(slot, mark, cv, float(contract.get("strike_price") or 0))
    return jsonify({
        "ok":         True,
        "slot":       slot,
        "symbol":     symbol,
        "strike":     float(contract.get("strike_price") or 0),
        "mark":       round(mark, 4),
        "lots":       lots,
        "est_value":  round(mark * cv * lots, 2),
        "settlement": contract.get("settlement_time", ""),
        "dry_run":    _cfg_bool("DRY_RUN", False),
    })


@app.route("/api/manual-entry", methods=["POST"])
def api_manual_entry():
    """BUY (long) or SELL (short-to-open) the current ATM straddle for a slot.
    Only allowed when the slot has no open position; afterwards the position
    is BAU — TP monitor, square-off, scheduled exits all apply."""
    slot = _slot_arg(move_only=True)
    data = request.get_json(silent=True) or {}
    side = (data.get("side") or request.args.get("side") or "").lower()
    if side not in ("buy", "sell"):
        return jsonify({"ok": False, "error": "side must be buy or sell"}), 400
    key, secret = _active_creds()
    if not key or not secret:
        return jsonify({"ok": False, "error": "API credentials not configured"}), 400

    state_file = _slot_file(slot)
    state      = _load_json(state_file, {})
    if state.get("status") == "OPEN":
        return jsonify({"ok": False, "error": f"{slot} already has an open position"}), 400

    contract = _current_atm_mv()
    if not contract:
        return jsonify({"ok": False, "error": "No live MV contract found"}), 502
    pid    = int(contract["id"])
    symbol = contract["symbol"]
    cv     = float(contract.get("contract_value") or 0.001)

    # Two slots must never share one contract: exchange positions are per
    # product, so one slot's stop firing would close BOTH slots' exposure and
    # the P&L attribution would be garbage.
    other_slot = "morning" if slot == "evening" else "evening"
    other = _load_json(_slot_file(other_slot), {})
    if other.get("status") == "OPEN" and int(other.get("product_id", 0) or 0) == pid:
        return jsonify({"ok": False,
                        "error": f"The {other_slot} slot already holds {symbol} — "
                                 "two slots can't share one contract"}), 400
    try:
        mark = float(req.get(f"{API_BASE}/v2/tickers/{symbol}", timeout=6)
                     .json().get("result", {}).get("mark_price") or 0)
    except Exception:
        mark = 0.0
    lots = _manual_entry_lots(slot, mark, cv, float(contract.get("strike_price") or 0))

    # Manual entries must respect Mode the same way the scheduled bot does —
    # previously this always fired a REAL order regardless of the DRY RUN
    # toggle, so "simulating" via the Mode switch gave no real protection
    # against an accidental click on Buy/Sell.
    is_dry_run = _cfg_bool("DRY_RUN", False)

    try:
        if is_dry_run:
            o = {"id": 0, "average_fill_price": mark}
        else:
            # Entry orders auto-downsize on balance/commission/margin
            # rejections: the sizing estimate can't perfectly model Delta's
            # margin+fee formulas (SELL/short margin especially), but the
            # rejection context reports exactly how much balance the order
            # truly needed — resize from it and retry.
            o = None
            for _ in range(3):
                payload = {"product_id": pid, "size": lots, "side": side,
                           "order_type": "market_order"}
                body    = json.dumps(payload, separators=(",", ":"))
                hdrs    = _sign("POST", "/v2/orders", "", body)
                result  = req.post(f"{API_BASE}/v2/orders", data=body,
                                   headers=hdrs, timeout=15).json()
                if result.get("success") and (result.get("result") or {}).get("id"):
                    o = result["result"]
                    break
                err = result.get("error") or {}
                new_lots = (_downsized_lots(lots, err.get("context") or {})
                            if str(err.get("code")) in BALANCE_REJECTIONS else None)
                if not new_lots:
                    return jsonify({"ok": False, "error": result.get("error", result)}), 400
                lots = new_lots
            if o is None:
                return jsonify({"ok": False, "error": result.get("error", result)}), 400

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
            f"🖐 <b>MANUAL {side.upper()} — {icon} {slot.upper()} ({_active_user().upper()})</b>{mode}\n"
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
    """The active account's TP target / poll / SL / TSL for a slot (their
    config.json, .env defaults as fallback). SL/TSL 0 = disabled."""
    if slot == "morning":
        keys = ("TP_TARGET_PNL_MORNING", "TP_POLL_SECS_MORNING",
                "SL_TARGET_PNL_MORNING", "TSL_TARGET_PNL_MORNING")
        dflt = (300.0, 30, 0.0, 0.0)
    elif slot == "trend":
        keys = ("TP_TARGET_PNL_TREND", "TP_POLL_SECS_TREND",
                "SL_TARGET_PNL_TREND", "TSL_TARGET_PNL_TREND")
        dflt = (100.0, 30, 50.0, 50.0)
    else:
        keys = ("TP_TARGET_PNL", "TP_POLL_SECS", "SL_TARGET_PNL", "TSL_TARGET_PNL")
        dflt = (105.0, 30, 0.0, 0.0)
    try:
        target = max(float(_cfg(keys[0]) or dflt[0]), 1.0)
    except ValueError:
        target = dflt[0]
    try:
        poll = max(int(float(_cfg(keys[1]) or dflt[1])), 10)
    except ValueError:
        poll = dflt[1]
    def _loss(key, default=0.0):
        try:
            return abs(float(_cfg(key) or default))
        except ValueError:
            return default
    return target, poll, _loss(keys[2], dflt[2]), _loss(keys[3], dflt[3])


@app.route("/api/tp-monitor", methods=["GET"])
def tp_monitor_status():
    user = _active_user()
    out = {}
    for slot in SLOTS:
        target, poll, sl, tsl = _tp_env(slot)
        st = _load_json(_slot_file(slot), {})
        open_stop = bool(st.get("tsl_stop_order_id")) and st.get("status") == "OPEN"
        out[slot] = {"running": _tp_running(user, slot), "target_pnl": target,
                     "poll_secs": poll, "sl_pnl": sl, "tsl_pnl": tsl,
                     # live trail bookkeeping persisted by the monitor — SL and TSL
                     # share one resting exchange stop, distinguished by stop_kind
                     "tsl_armed":       bool(st.get("tsl_armed")) and st.get("status") == "OPEN",
                     "tsl_floor":       st.get("tsl_floor"),
                     "tsl_on_exchange": open_stop and st.get("stop_kind") == "tsl",
                     "sl_on_exchange":  open_stop and st.get("stop_kind", "sl") == "sl",
                     "tp_on_exchange":  bool(st.get("tp_stop_order_id")) and st.get("status") == "OPEN"}
    # Back-compat top-level fields = evening
    out.update(out["evening"])
    return jsonify(out)


@app.route("/api/tp-monitor/start", methods=["POST"])
def tp_monitor_start():
    slot = _slot_arg()
    user = _active_user()
    if _tp_running(user, slot):
        return jsonify({"ok": False, "error": f"{slot} monitor already running"}), 400
    state = _load_json(_slot_file(slot), {})
    if state.get("status") != "OPEN":
        return jsonify({"ok": False, "error": f"No open {slot} position to monitor"}), 400
    proc = _spawn_tp(user, slot)
    if proc is None:
        return jsonify({"ok": False, "error": "tp_monitor.py not found"}), 404
    return jsonify({"ok": True, "slot": slot, "pid": proc.pid})


@app.route("/api/tp-monitor/stop", methods=["POST"])
def tp_monitor_stop():
    slot = _slot_arg()
    user = _active_user()
    stopped = False
    key  = f"{user}:{slot}"
    proc = _tp_procs.get(key)
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        _tp_procs[key] = None
        stopped = True
    pid_file = _pid_file(user, slot)
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
    # Each bot instance logs to its own file; fall back to the legacy
    # shared straddle.log for history from before the per-user split.
    log_file = BASE / "logs" / f"straddle_{_active_user()}.log"
    if not log_file.exists():
        log_file = BASE / "logs" / "straddle.log"
    n = min(int(request.args.get("n", 100)), 500)
    try:
        text = log_file.read_text(encoding="utf-8", errors="replace")
        rows = [l for l in text.splitlines() if l.strip()]
        return jsonify({"lines": rows[-n:]})
    except FileNotFoundError:
        return jsonify({"lines": []})


# ─────────────────────────────────────────────────────────────
# PER-ACCOUNT BOT INSTANCES — one systemd unit per user
# (`mathi-bot@<username>` template; each trades that user's own keys
#  against that user's folder)
# ─────────────────────────────────────────────────────────────
def _bot_unit(user: str) -> str:
    return f"mathi-bot@{user}.service"


def _systemctl(*args) -> tuple:
    try:
        r = subprocess.run(list(args), capture_output=True, text=True, timeout=15)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:
        return 1, str(e)


def _bot_active(user: str) -> bool:
    if not shutil.which("systemctl"):
        return False
    rc, out = _systemctl("systemctl", "is-active", _bot_unit(user))
    return out == "active"


@app.route("/api/bots")
def api_bots():
    supported = bool(shutil.which("systemctl"))
    return jsonify({a["username"]: {"supported": supported,
                                    "active": supported and _bot_active(a["username"])}
                    for a in _load_accounts()})


@app.route("/api/bots/<username>/<action>", methods=["POST"])
def api_bot_action(username, action):
    username = _safe_user(username)
    acct = _find_account(username) if username else None
    if not acct:
        return jsonify({"ok": False, "error": "No such account"}), 404
    if action not in ("start", "stop"):
        return jsonify({"ok": False, "error": "action must be start or stop"}), 400
    if not shutil.which("systemctl"):
        return jsonify({"ok": False, "error": "systemd not available on this host"}), 501
    if action == "start" and not (acct.get("api_key") and acct.get("api_secret")):
        return jsonify({"ok": False, "error": "Set the account's API key & secret first"}), 400
    # enable --now / disable --now so the choice survives server reboots
    verb = "enable" if action == "start" else "disable"
    rc, out = _systemctl("sudo", "-n", "systemctl", verb, "--now", _bot_unit(username))
    if rc != 0:
        return jsonify({"ok": False, "error": out or "systemctl failed"}), 500
    return jsonify({"ok": True, "unit": _bot_unit(username), "action": action})


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


def _fetch_reconstructed_trades(skip_prefixes=("MV-BTC",)) -> list:
    """Closed round-trip trades reconstructed from the logged-in account's
    order history. For the primary account MV symbols are skipped (the bot's
    own log already tracks them precisely); a secondary account has no bot
    log, so everything it traded gets reconstructed."""
    key, secret = _active_creds()
    if not key or not secret:
        return []
    try:
        hdrs = _sign("GET", "/v2/orders/history", "?page_size=500", key=key, secret=secret)
        r = req.get(f"{API_BASE}/v2/orders/history", params={"page_size": 500},
                    headers=hdrs, timeout=15)
        data = r.json()
        if not data.get("success"):
            return []
        return _reconstruct_trades_from_orders(data.get("result", []), skip_prefixes)
    except Exception:
        return []


def _all_trades_merged() -> list:
    """The active user's tracked trades (their trade_history.json — written
    by the bot engine, square-offs, TP monitors and stale-close reconciling)
    plus trades reconstructed from their own Delta order history for anything
    never tracked, deduped on (symbol, date, entry_time)."""
    def _key(t: dict) -> tuple:
        # Bot/reconcile records store entry_time_utc; reconstructed ones
        # store entry_time — same value, either name must match.
        return (t.get("symbol"),
                t.get("date") or t.get("entry_date", ""),
                t.get("entry_time") or t.get("entry_time_utc", ""))
    mv_trades    = _load_json(_hist_file(), [])
    tracked_keys = {_key(t) for t in mv_trades}
    other = [t for t in _fetch_reconstructed_trades() if _key(t) not in tracked_keys]
    merged = mv_trades + other
    for t in merged:
        # Older records (square-offs, resumed states) carry only entry_date /
        # entry_time_utc — normalize so every consumer (table, chart) can rely
        # on date & entry_time being present.
        t.setdefault("date", t.get("entry_date", ""))
        t.setdefault("entry_time", t.get("entry_time_utc", ""))
        t.setdefault("exit_time", t.get("exit_time_utc", ""))
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


# ─────────────────────────────────────────────────────────────
# BTC MULTI-TIMEFRAME TREND — EMA 9/21 + RSI(14) confirmation
# ─────────────────────────────────────────────────────────────
def _ema(vals: list, n: int) -> float:
    k = 2 / (n + 1)
    e = sum(vals[:n]) / n
    for v in vals[n:]:
        e = v * k + e * (1 - k)
    return e


def _rsi(vals: list, n: int = 14) -> float:
    """Wilder-smoothed RSI over the whole series."""
    if len(vals) < n + 1:
        return 50.0
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = vals[i] - vals[i - 1]
        gains  += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(vals)):
        d = vals[i] - vals[i - 1]
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
    return 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)


TREND_TIMEFRAMES = {
    "5m":  {"resolution": "5m",  "seconds": 300,  "label": "5M"},
    "15m": {"resolution": "15m", "seconds": 900,  "label": "15M"},
    "1h":  {"resolution": "1h",  "seconds": 3600, "label": "1H"},
}

_trend_cache = {"ts": 0.0, "data": None}


def _trend_metrics(closes: list, candle_time=None) -> dict:
    """Pure trend calculation shared by every timeframe."""
    if len(closes) < 40:
        raise ValueError("not enough candle data")
    ema9, ema21, rsi = _ema(closes, 9), _ema(closes, 21), _rsi(closes, 14)
    close = closes[-1]
    if ema9 > ema21 and close > ema21 and rsi > 50:
        trend = "up"
    elif ema9 < ema21 and close < ema21 and rsi < 50:
        trend = "down"
    else:
        trend = "neutral"
    return {"trend": trend, "ema9": round(ema9, 2), "ema21": round(ema21, 2),
            "rsi": round(rsi, 1), "close": round(close, 2),
            "candle_time": candle_time}


def _trend_snapshot(force: bool = False) -> dict:
    """Return 5m/15m/1h metrics computed only from completed candles."""
    if not force and _trend_cache["data"] and time.time() - _trend_cache["ts"] < 60:
        return _trend_cache["data"]
    end = int(time.time())
    frames = {}
    for key, spec in TREND_TIMEFRAMES.items():
        seconds = spec["seconds"]
        r = req.get(f"{API_BASE}/v2/history/candles",
                    params={"resolution": spec["resolution"], "symbol": "BTCUSD",
                            "start": end - seconds * 300, "end": end},
                    timeout=10).json()
        candles = sorted(r.get("result") or [], key=lambda c: c.get("time", 0))
        current_bucket = end - end % seconds
        if candles and candles[-1].get("time", 0) >= current_bucket:
            candles = candles[:-1]
        closes = [float(c["close"]) for c in candles]
        frames[key] = _trend_metrics(closes, candles[-1].get("time") if candles else None)

    directions = [frames[k]["trend"] for k in TREND_TIMEFRAMES]
    combined = directions[0] if len(set(directions)) == 1 and directions[0] in ("up", "down") else "neutral"
    # Preserve the original top-level 1H fields for existing Android/API clients.
    data = {**frames["1h"], "combined": combined, "timeframes": frames,
            "all_aligned": combined in ("up", "down")}
    _trend_cache.update(ts=time.time(), data=data)
    return data


@app.route("/api/trend")
def api_trend():
    """5m, 15m and 1h BTC trends. Entry is eligible only when all align."""
    try:
        return jsonify(_trend_snapshot())
    except Exception as e:
        return jsonify({"trend": "na", "combined": "na", "timeframes": {},
                        "error": str(e)}), 502


def _pick_two_step_itm(products: list, spot: float, option_type: str) -> dict | None:
    """Pick two strike-ladder steps ITM from ATM in the nearest usable expiry.

    CE: ATM index - 2. PE: ATM index + 2. This matches the strategy's
    historical ``itm_strike`` definition while still using live products.
    Products, rather than STRIKE_STEP arithmetic, are authoritative because
    Delta's strike spacing varies by expiry and market conditions.
    """
    prefix = "C-BTC" if option_type == "CE" else "P-BTC"
    now_plus_buffer = datetime.now(timezone.utc) + timedelta(hours=1)
    usable = []
    for p in products:
        if not str(p.get("symbol", "")).startswith(prefix):
            continue
        try:
            settlement = datetime.fromisoformat(str(p.get("settlement_time", "")).replace("Z", "+00:00"))
            strike = float(p.get("strike_price") or 0)
        except (TypeError, ValueError):
            continue
        if settlement <= now_plus_buffer:
            continue
        usable.append((settlement, strike, p))
    for settlement in sorted({x[0] for x in usable}):
        batch = [x for x in usable if x[0] == settlement]
        batch.sort(key=lambda x: x[1])
        distinct = []
        seen = set()
        for _, strike, product in batch:
            if strike not in seen:
                seen.add(strike)
                distinct.append((strike, product))
        if len(distinct) < 5:
            continue
        atm_idx = min(range(len(distinct)), key=lambda i: abs(distinct[i][0] - spot))
        target_idx = atm_idx - 2 if option_type == "CE" else atm_idx + 2
        if 0 <= target_idx < len(distinct):
            strike, product = distinct[target_idx]
            if (option_type == "CE" and strike < spot) or (option_type == "PE" and strike > spot):
                return product
    return None


def _current_trend_option(direction: str) -> tuple[dict | None, float]:
    option_type = "CE" if direction == "up" else "PE"
    try:
        spot = float(req.get(f"{API_BASE}/v2/tickers/BTCUSD", timeout=6)
                     .json().get("result", {}).get("mark_price") or 0)
        products = req.get(f"{API_BASE}/v2/products",
                           params={"contract_types": "call_options,put_options",
                                   "states": "live", "page_size": 500},
                           timeout=12).json().get("result", [])
        return _pick_two_step_itm(products, spot, option_type), spot
    except Exception:
        return None, 0.0


def _trend_entry_preview_data() -> tuple[dict, int]:
    try:
        trend = _trend_snapshot()
    except Exception as e:
        return {"ok": False, "can_enter": False, "error": str(e)}, 502
    direction = trend.get("combined")
    option_type = "CE" if direction == "up" else "PE" if direction == "down" else None
    signal_key = "|".join([str(direction or "na")] + [
        str((trend.get("timeframes", {}).get(k) or {}).get("candle_time", ""))
        for k in ("5m", "15m", "1h")
    ])
    state = _load_json(_slot_file("trend"), {})
    if state.get("status") == "OPEN":
        return {"ok": True, "can_enter": False, "reason": "A trend position is already open",
                "direction": direction, "option_type": option_type,
                "signal_key": signal_key}, 200
    if not option_type:
        return {"ok": True, "can_enter": False,
                "reason": "5M, 15M and 1H trends are not aligned",
                "direction": direction, "signal_key": signal_key}, 200
    contract, spot = _current_trend_option(direction)
    if not contract:
        return {"ok": False, "can_enter": False,
                "error": f"No usable 2-step ITM {option_type} contract found"}, 502
    symbol = contract["symbol"]
    cv = float(contract.get("contract_value") or 0.001)
    try:
        mark = float(req.get(f"{API_BASE}/v2/tickers/{symbol}", timeout=6)
                     .json().get("result", {}).get("mark_price") or 0)
    except Exception:
        mark = 0.0
    if mark <= 0:
        return {"ok": False, "can_enter": False,
                "error": f"Live mark unavailable for {symbol}"}, 502
    lots = _manual_entry_lots("trend", mark, cv, float(contract.get("strike_price") or 0))
    if lots < 1:
        return {"ok": False, "can_enter": False,
                "error": "No affordable Trend lots available for the account"}, 200
    return {"ok": True, "can_enter": True, "direction": direction,
            "option_type": option_type, "symbol": symbol, "product_id": int(contract["id"]),
            "strike": float(contract.get("strike_price") or 0), "spot": round(spot, 2),
            "mark": round(mark, 4), "lots": lots,
            "est_value": round(mark * cv * lots, 2),
            "settlement": contract.get("settlement_time", ""),
            "contract_value": cv, "dry_run": _cfg_bool("DRY_RUN", False),
            "signal_key": signal_key, "timeframes": trend.get("timeframes", {})}, 200


@app.route("/api/trend-entry/preview")
def api_trend_entry_preview():
    data, status = _trend_entry_preview_data()
    return jsonify(data), status


def _execute_trend_entry(auto: bool = False):
    """Buy the eligible 2-step ITM CE/PE; direction is always server-derived."""
    _sync_states_from_exchange()
    preview, status = _trend_entry_preview_data()
    if status != 200 or not preview.get("can_enter"):
        return jsonify(preview), 400 if status == 200 else status
    key, secret = _active_creds()
    if not key or not secret:
        return jsonify({"ok": False, "error": "API credentials not configured"}), 400

    pid, lots = int(preview["product_id"]), int(preview["lots"])
    mark, cv = float(preview["mark"]), float(preview["contract_value"])
    is_dry_run = bool(preview["dry_run"])
    try:
        if is_dry_run:
            order = {"id": 0, "average_fill_price": mark}
        else:
            order = None
            result = {}
            for _ in range(3):
                payload = {"product_id": pid, "size": lots, "side": "buy",
                           "order_type": "market_order"}
                body = json.dumps(payload, separators=(",", ":"))
                result = req.post(f"{API_BASE}/v2/orders", data=body,
                                  headers=_sign("POST", "/v2/orders", "", body),
                                  timeout=15).json()
                if result.get("success") and (result.get("result") or {}).get("id"):
                    order = result["result"]
                    break
                err = result.get("error") or {}
                new_lots = (_downsized_lots(lots, err.get("context") or {})
                            if str(err.get("code")) in BALANCE_REJECTIONS else None)
                if not new_lots:
                    return jsonify({"ok": False, "error": result.get("error", result)}), 400
                lots = new_lots
            if order is None:
                return jsonify({"ok": False, "error": result.get("error", result)}), 400

        fill = float(order.get("average_fill_price") or mark)
        now = datetime.now(timezone.utc)
        new_state = {
            "slot": "trend", "status": "OPEN", "side": "long",
            "option_type": preview["option_type"], "trend_signal": preview["direction"],
            "entry_date": now.strftime("%Y-%m-%d"),
            "entry_time_utc": now.strftime("%H:%M:%S"),
            "symbol": preview["symbol"], "product_id": pid,
            "strike": preview["strike"], "settlement": preview["settlement"],
            "contract_value": cv, "lots": lots, "entry_mark": round(fill, 4),
            "btc_at_entry": preview["spot"],
            "total_cost_usd": round(fill * cv * lots, 2),
            "order_id": order.get("id"),
            "entry_trigger": "trend_auto" if auto else "trend_alignment",
            "auto_signal_key": preview.get("signal_key"),
            "trend_timeframes": preview.get("timeframes", {}),
            "dry_run": is_dry_run,
        }
        _slot_file("trend").write_text(json.dumps(new_state, indent=2), encoding="utf-8")
        monitor_started = False
        if not is_dry_run and not _tp_running(_active_user(), "trend"):
            monitor_started = _spawn_tp(_active_user(), "trend") is not None
        mode = " — DRY-RUN (simulated)" if is_dry_run else ""
        _send_telegram(
            f"📈 <b>TREND ENTRY — {preview['option_type']} ({_active_user().upper()})</b>{mode}\n"
            f"<code>{'━' * 24}</code>\nSymbol  » <code>{preview['symbol']}</code>\n"
            f"Signal  » <code>5M + 15M + 1H {preview['direction'].upper()}</code>\n"
            f"Lots    » <code>{lots:,}</code>\nFill    » <code>${fill:.4f}</code>\n"
            f"Value   » <code>${fill * cv * lots:,.2f}</code>"
        )
        return jsonify({"ok": True, "slot": "trend", "side": "long",
                        "option_type": preview["option_type"], "symbol": preview["symbol"],
                        "lots": lots, "fill": fill, "order_id": order.get("id"),
                        "dry_run": is_dry_run, "monitor_started": monitor_started,
                        "auto": auto})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/trend-entry", methods=["POST"])
def api_trend_entry():
    if not _trend_entry_lock.acquire(blocking=False):
        return jsonify({"ok": False, "error": "Another Trend entry is being processed"}), 409
    try:
        return _execute_trend_entry(auto=False)
    finally:
        _trend_entry_lock.release()


def _maybe_auto_trend_entry() -> bool:
    """Place at most one automatic entry per aligned candle set per account."""
    if not _cfg_bool("TREND_AUTO_ENTRY_ENABLED", False):
        return False
    user = _active_user()
    now = time.time()
    if now - _trend_auto_last_attempt.get(user, 0.0) < 30:
        return False
    if not _trend_entry_lock.acquire(blocking=False):
        return False
    try:
        _sync_states_from_exchange()
        state = _load_json(_slot_file("trend"), {})
        if state.get("status") == "OPEN":
            return False
        preview, status = _trend_entry_preview_data()
        if status != 200 or not preview.get("can_enter"):
            return False
        if state.get("auto_signal_key") == preview.get("signal_key"):
            return False
        _trend_auto_last_attempt[user] = now
        response = _execute_trend_entry(auto=True)
        return bool(getattr(response, "status_code", 500) < 300)
    finally:
        _trend_entry_lock.release()


def _trend_auto_loop() -> None:
    """Background per-account trigger; works even when no browser is open."""
    while True:
        try:
            for acct in _load_accounts():
                user = _safe_user(acct.get("username", ""))
                if not user:
                    continue
                try:
                    with app.test_request_context("/api/trend-entry"):
                        g.basic_user = user
                        _maybe_auto_trend_entry()
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(30)


@app.route("/api/wallet")
def api_wallet():
    """Account value in USD and INR — scoped to the logged-in account."""
    key, secret = _active_creds()
    if not key or not secret:
        return jsonify({"error": "no api credentials"}), 503
    try:
        hdrs = _sign("GET", "/v2/wallet/balances", key=key, secret=secret)
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
    """Every currently open position on the logged-in Delta account — MV
    straddles, calls/puts, perpetual futures — not just what the bot tracks."""
    key, secret = _active_creds()
    if not key or not secret:
        return jsonify([])
    try:
        hdrs = _sign("GET", "/v2/positions/margined", key=key, secret=secret)
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
    return jsonify(_user_cfg())


def _restart_tp_monitor(user: str, slot: str) -> bool:
    """Stop and respawn a user's slot TP monitor so freshly saved targets
    apply. Only called for monitors that are already running."""
    key  = f"{user}:{slot}"
    proc = _tp_procs.get(key)
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        _tp_procs[key] = None
    pid_file = _pid_file(user, slot)
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text().strip()), 15)
        except Exception:
            pass
        pid_file.unlink(missing_ok=True)
    state = _load_json(USERS_DIR / user / SLOT_STATE_FILES[slot], {})
    if state.get("status") != "OPEN":
        return False
    return _spawn_tp(user, slot) is not None


_TP_KEYS_BY_SLOT = {
    "evening": {"TP_TARGET_PNL", "TP_POLL_SECS", "SL_TARGET_PNL", "TSL_TARGET_PNL"},
    "morning": {"TP_TARGET_PNL_MORNING", "TP_POLL_SECS_MORNING",
                "SL_TARGET_PNL_MORNING", "TSL_TARGET_PNL_MORNING"},
    "trend": {"TP_TARGET_PNL_TREND", "TP_POLL_SECS_TREND",
              "SL_TARGET_PNL_TREND", "TSL_TARGET_PNL_TREND"},
}


@app.route("/api/config", methods=["POST"])
def set_config():
    """Save strategy settings for the ACTIVE account only — written to
    users/<name>/config.json, never to the shared .env (which now serves
    purely as the global default for keys an account hasn't set). The
    account's bot instance watches its config.json and self-reloads."""
    data = request.json or {}
    saved = _load_json(_cfg_file(), {})
    if not isinstance(saved, dict):
        saved = {}
    saved.update({k: str(v) for k, v in data.items() if k in CONFIG_KEYS})
    _cfg_file().write_text(json.dumps(saved, indent=2), encoding="utf-8")
    # Running TP monitors don't watch config — bounce any of the active
    # user's monitors whose targets just changed.
    user = _active_user()
    tp_restarted = []
    for slot, keys in _TP_KEYS_BY_SLOT.items():
        if keys & set(data) and _tp_running(user, slot):
            if _restart_tp_monitor(user, slot):
                tp_restarted.append(slot)
    return jsonify({"ok": True, "tp_restarted": tp_restarted})


@app.route("/api/test-telegram", methods=["POST"])
def test_telegram():
    token   = _cfg("TELEGRAM_BOT_TOKEN")
    chat_id = _cfg("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return jsonify({"ok": False, "error": "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not configured"}), 400
    try:
        r = req.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id":    chat_id,
                "text":       "✅ <b>NITHI-BOT</b> — Telegram alerts are connected!\n<code>Test message from dashboard.</code>",
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
    them while their PID files and OPEN state survive. On startup, walk every
    user's folder and respawn any monitor whose PID file is stale but whose
    slot is still OPEN — without this, a reboot silently drops take-profit
    protection."""
    if not USERS_DIR.exists():
        return
    for udir in USERS_DIR.iterdir():
        if not (udir / "account.json").exists():
            continue
        user = udir.name
        for slot in SLOTS:
            pid_file = _pid_file(user, slot)
            if not pid_file.exists():
                continue
            try:
                if _pid_alive(int(pid_file.read_text().strip())):
                    continue      # survived (not a reboot) — leave it be
            except (ValueError, OSError):
                pass
            pid_file.unlink(missing_ok=True)
            state = _load_json(udir / SLOT_STATE_FILES[slot], {})
            if state.get("status") != "OPEN":
                continue
            proc = _spawn_tp(user, slot)
            if proc:
                print(f"Revived {user}/{slot} TP monitor (pid {proc.pid}) after restart")


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 50)
    print("  NITHI-BOT — MV-BTC Straddle Dashboard")
    print("  http://localhost:5001")
    print("=" * 50)
    _revive_tp_monitors()
    threading.Thread(target=_trend_auto_loop, name="trend-auto-entry", daemon=True).start()
    app.run(host="0.0.0.0", port=5001, debug=False)
