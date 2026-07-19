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
from contextlib import ExitStack
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests as req
from dotenv import load_dotenv, set_key
from flask import (Flask, jsonify, request, abort, send_file, session,
                   redirect, render_template, has_request_context, g)

from risk_controls import (account_entry_lock, account_file_lock, audit_event,
                           decision_dict, evaluate_entry, risk_based_lots)

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
_trend_auto_health: dict[str, dict] = {}
_trend_debounce: dict[str, dict] = {}
_trend_shadow_seen: dict[str, str] = {}
_external_options: dict[str, list] = {}


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


def _pid_is_monitor(pid: int, user: str, slot: str) -> bool:
    """Prove a pidfile still names this exact monitor before signalling it."""
    if os.name == "nt":
        # Dashboard-managed Popen handles are used on Windows; an orphaned
        # numeric PID cannot be safely identified through the POSIX cmdline.
        return False
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
        health = _tp_health(user, slot)
        return (
            "tp_monitor.py" in cmdline
            and f"--slot {slot}" in cmdline
            and f"--user {user}" in cmdline
            and int(health.get("pid") or 0) == pid
            and str(health.get("user") or "").lower() == user.lower()
            and str(health.get("slot") or "").lower() == slot.lower()
        )
    except Exception:
        return False


def _tp_health_file(user: str, slot: str) -> Path:
    return USERS_DIR / user / f"tp_{slot}_health.json"


def _tp_health(user: str, slot: str) -> dict:
    data = _load_json(_tp_health_file(user, slot), {})
    return data if isinstance(data, dict) else {}


def _tp_health_fresh(health: dict) -> bool:
    if not health:
        return False
    try:
        beat = datetime.fromisoformat(
            str(health.get("heartbeat_utc", "")).replace("Z", "+00:00"))
        max_age = max(float(health.get("next_poll_secs") or 30) * 3, 90)
        return (datetime.now(timezone.utc) - beat).total_seconds() <= max_age
    except (TypeError, ValueError):
        return False


def _tp_continuity_health_fresh(health: dict) -> bool:
    """Tighter freshness bound for hiding an adopted aggregate from External."""
    if not health:
        return False
    try:
        beat = datetime.fromisoformat(
            str(health.get("heartbeat_utc", "")).replace("Z", "+00:00"))
        poll = max(float(health.get("next_poll_secs") or 30), 1)
        max_age = max(30.0, min(90.0, poll * 2 + 5))
        return (datetime.now(timezone.utc) - beat).total_seconds() <= max_age
    except (TypeError, ValueError):
        return False


def _tp_health_matches(health: dict, state: dict, user: str, slot: str, *,
                       require_protection_identity: bool = True) -> bool:
    if str(health.get("user") or "").lower() != user.lower():
        return False
    if str(health.get("slot") or "").lower() != slot.lower():
        return False
    if str(health.get("product_id") or "") != str(state.get("product_id") or ""):
        return False
    order_id = state.get("order_id") or state.get("entry_order_id")
    if str(health.get("entry_order_id") or "") != str(order_id or ""):
        return False
    client_id = state.get("client_order_id")
    if (str(health.get("entry_client_order_id") or "")
            != str(client_id or "")):
        return False
    try:
        health_revision = int(health.get("protection_revision") or 0)
        state_revision = int(state.get("protection_revision") or 0)
    except (TypeError, ValueError, OverflowError):
        return False
    if health_revision != state_revision:
        return False
    cycle_id = state.get("position_cycle_id")
    if str(health.get("position_cycle_id") or "") != str(cycle_id or ""):
        return False
    try:
        health_continuity_revision = int(health.get("continuity_revision") or 0)
        state_continuity_revision = int(state.get("continuity_revision") or 0)
    except (TypeError, ValueError, OverflowError):
        return False
    if health_continuity_revision != state_continuity_revision:
        return False
    if not require_protection_identity:
        return True

    def protection_identity_matches(*, health_id_key: str,
                                    state_id_key: str,
                                    state_client_key: str,
                                    proof_key: str,
                                    required: bool) -> bool:
        """Bind a protection claim to the exact identities in current state.

        Revisions normally invalidate an old heartbeat, but identity clearing
        or replacement must fail closed even if a buggy/legacy writer did not
        bump the revision.  A complete exchange-protection claim therefore
        requires both the state ID/client ID and the embedded strict order
        proof.  In a local-fallback snapshot, any exchange identity that is
        present must still agree with current state.
        """
        state_id = str(state.get(state_id_key) or "")
        state_client = str(state.get(state_client_key) or "")
        health_id = str(health.get(health_id_key) or "")
        proof = health.get(proof_key)
        proof_order = proof.get("order") if isinstance(proof, dict) else None
        has_claim = bool(
            required or state_id or health_id or proof_order
        )
        if not has_claim:
            return True
        if not state_id or health_id != state_id:
            return False
        if isinstance(proof_order, dict) and proof_order:
            if str(proof_order.get("id") or "") != state_id:
                return False
            if (not state_client
                    or str(proof_order.get("client_order_id") or "")
                    != state_client):
                return False
            if (str(proof_order.get("product_id") or "")
                    != str(state.get("product_id") or "")):
                return False
        elif required:
            return False
        return not required or (
            isinstance(proof, dict) and proof.get("ok") is True
        )

    exchange_complete = health.get("exchange_protection_complete") is True
    stop_proof = health.get("stop_order_proof")
    stop_disabled = bool(
        isinstance(stop_proof, dict)
        and stop_proof.get("ok") is True
        and str(stop_proof.get("reason") or "").lower() == "stop is disabled"
        and not state.get("tsl_stop_order_id")
        and not health.get("stop_order_id")
    )
    if not protection_identity_matches(
            health_id_key="stop_order_id",
            state_id_key="tsl_stop_order_id",
            state_client_key="stop_client_order_id",
            proof_key="stop_order_proof",
            required=exchange_complete and not stop_disabled):
        return False
    if not protection_identity_matches(
            health_id_key="tp_order_id",
            state_id_key="tp_stop_order_id",
            state_client_key="tp_client_order_id",
            proof_key="tp_order_proof",
            required=exchange_complete):
        return False
    return True


def _wait_for_protection(user: str, slot: str, started_at: datetime,
                         timeout_secs: float = 10.0) -> tuple[bool, dict]:
    """Wait briefly for the monitor's first proof of active protection."""
    deadline = time.time() + max(timeout_secs, 0)
    latest = {}
    expected_state = _load_json(USERS_DIR / user / SLOT_STATE_FILES[slot], {})
    while time.time() < deadline:
        latest = _tp_health(user, slot)
        try:
            heartbeat = datetime.fromisoformat(
                str(latest.get("heartbeat_utc", "")).replace("Z", "+00:00"))
            current_run = heartbeat >= started_at - timedelta(seconds=2)
        except (TypeError, ValueError):
            current_run = False
        active = bool(
            latest.get("protection_established")
            and (latest.get("exchange_protection_complete")
                 or latest.get("local_fallback_active"))
        )
        if (current_run and active and _tp_health_matches(latest, expected_state, user, slot)
                and latest.get("status") in {"healthy", "degraded", "running"}):
            return True, latest
        time.sleep(0.5)
    return False, latest


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
                if not _pid_is_monitor(pid, user, slot):
                    print(f"WARNING: live PID {pid} for {user}/{slot} has unproven identity")
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
    "DRY_RUN", "STRADDLE_LOTS", "STRIKE_STEP", "MAX_ORDER_LOTS",
    "EVENING_ENABLED", "EVENING_EXIT_ENABLED",
    "ENTRY_H_UTC", "ENTRY_M_UTC", "EXIT_H_UTC", "EXIT_M_UTC",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_ALERTS",
    "TP_TARGET_PNL", "TP_POLL_SECS", "SL_TARGET_PNL", "TSL_TARGET_PNL",
    "MORNING_ENABLED", "MORNING_LOTS", "MORNING_H_UTC", "MORNING_M_UTC",
    "MORNING_EXIT_ENABLED", "MORNING_EXIT_H_UTC", "MORNING_EXIT_M_UTC",
    "DYNAMIC_LOTS",
    "TP_TARGET_PNL_MORNING", "TP_POLL_SECS_MORNING", "SL_TARGET_PNL_MORNING",
    "TSL_TARGET_PNL_MORNING", "TSL_ARM_PNL_MORNING", "TSL_TRAIL_PNL_MORNING",
    "TSL_LOCK_MIN_PNL_MORNING",
    "TREND_LOTS", "TP_TARGET_PNL_TREND", "TP_POLL_SECS_TREND",
    "SL_TARGET_PNL_TREND", "TSL_TARGET_PNL_TREND", "TSL_ARM_PNL_TREND",
    "TSL_TRAIL_PNL_TREND", "TSL_LOCK_MIN_PNL_TREND",
    "TREND_AUTO_ENTRY_ENABLED", "TREND_AUTO_ENTRY_MODE",
    "TREND_EMA_GAP_PCT", "TREND_RSI_UP", "TREND_RSI_DOWN",
    "TREND_15M_SLOPE_BARS", "TREND_MIN_15M_SLOPE_PCT", "TREND_ADX_MIN",
    "TREND_1H_CONFIRM_SAMPLES", "TREND_MIN_TTE_HOURS", "TREND_TARGET_DELTA",
    "TREND_MAX_SPREAD_PCT", "TREND_MIN_BOOK_DEPTH_LOTS",
    "TREND_BOOK_PARTICIPATION_PCT", "TREND_QUOTE_MAX_AGE_SECS",
    "TREND_MAX_MARK_IV", "TREND_RISK_BUDGET_USD",
    "TREND_MAX_SLIPPAGE_PCT", "TREND_ORDER_CHUNK_LOTS",
    "TREND_MARKET_FALLBACK_ENABLED", "TREND_REENTRY_COOLDOWN_MIN",
    "TREND_ALLOW_MISSING_BOOK",
    "MAX_TRADES_PER_DAY", "MAX_TRADES_PER_DAY_GLOBAL",
    "MAX_DAILY_LOSS_USD", "MAX_OPEN_RISK_USD", "MAX_CONSECUTIVE_LOSSES",
    "LOSS_COOLDOWN_MINUTES", "MAX_ACCOUNT_PREMIUM_AT_RISK_USD",
    "SHORT_MAX_RISK_USD", "RISK_DAY_TZ_OFFSET_MIN", "RISK_FAIL_CLOSED",
    "OPTION_FEE_RATE", "OPTION_FEE_CAP_PCT",
    "TSL_ARM_PNL", "TSL_TRAIL_PNL", "TSL_LOCK_MIN_PNL",
    "ALLOW_EXTERNAL_POSITIONS_WITH_BOT",
    "RISK_PER_TRADE_USD_MORNING", "RISK_PER_TRADE_USD_EVENING",
    "ALLOW_SHORT_MOVE", "SAFE_EXECUTION_ENABLED", "ALLOW_MARKET_ENTRY_FALLBACK",
    "MAX_SPREAD_PCT", "MAX_SLIPPAGE_PCT", "MIN_BOOK_DEPTH_MULTIPLE",
    "MAX_QUOTE_AGE_SEC", "ORDER_CHUNK_LOTS", "MOVE_VALUE_FILTER_ENABLED",
    "MOVE_MIN_EDGE_PCT", "MOVE_MIN_TTE_MINUTES", "MOVE_MAX_TTE_HOURS",
    "MOVE_VOL_LOOKBACK", "MAX_CONCURRENT_MOVE_POSITIONS",
    "MOVE_AUTO_ENTRY_MODE", "MOVE_ALLOW_LONG",
    "MOVE_MIN_LONG_EDGE_ABS_USD", "MOVE_MIN_SHORT_EDGE_ABS_USD",
    "MOVE_MIN_LONG_EDGE_PCT", "MOVE_MIN_SHORT_EDGE_PCT",
    "MOVE_MAX_MODEL_AGE_SEC", "MOVE_MIN_BID_SIZE", "MOVE_MIN_ASK_SIZE",
    "MOVE_MAX_JUMP_SCORE_SHORT", "MOVE_MAX_LONG_PREMIUM_RISK_USD",
    "MOVE_MAX_SHORT_MARGIN_USAGE_PCT", "MOVE_MIN_LIQUIDATION_BUFFER_PCT",
    "MOVE_NO_ENTRY_BEFORE_SETTLEMENT_SEC", "MOVE_REQUIRE_NO_OPEN_ORDERS",
    "MOVE_REQUIRE_FLAT", "MOVE_DRY_RUN_CAPITAL_USD",
    "MOVE_FORECAST_LOOKBACK_DAYS", "MOVE_FORECAST_OUTER_SCENARIOS",
    "MOVE_FORECAST_PATHS_PER_SCENARIO",
]

# One explicit, fail-safe profile for every editable setting on the Config
# page. Do not derive this from .env or the strategy module's historical
# fallbacks: those can enable live entries and differ between installations.
# Telegram credentials are deliberately excluded because account secrets have
# no meaningful shared default and a strategy reset must not erase them.
CONFIG_PAGE_DEFAULTS = {
    # Trading mode and shared guardrails
    "DRY_RUN": "true",
    "STRADDLE_LOTS": "1000",
    "STRIKE_STEP": "200",
    "MAX_ORDER_LOTS": "1000",
    "MAX_TRADES_PER_DAY_GLOBAL": "3",
    "MAX_DAILY_LOSS_USD": "500",
    "MAX_OPEN_RISK_USD": "500",
    "MAX_CONSECUTIVE_LOSSES": "3",
    "LOSS_COOLDOWN_MINUTES": "30",
    "MAX_ACCOUNT_PREMIUM_AT_RISK_USD": "500",
    "RISK_FAIL_CLOSED": "true",
    "ALLOW_EXTERNAL_POSITIONS_WITH_BOT": "false",
    # Morning and evening MOVE slots. Times are UTC; the page renders IST.
    "MORNING_ENABLED": "false",
    "MORNING_LOTS": "1000",
    "RISK_PER_TRADE_USD_MORNING": "200",
    "MORNING_EXIT_ENABLED": "false",
    "MORNING_H_UTC": "0",
    "MORNING_M_UTC": "15",
    "MORNING_EXIT_H_UTC": "11",
    "MORNING_EXIT_M_UTC": "30",
    "EVENING_ENABLED": "false",
    "RISK_PER_TRADE_USD_EVENING": "200",
    "EVENING_EXIT_ENABLED": "false",
    "ENTRY_H_UTC": "12",
    "ENTRY_M_UTC": "5",
    "EXIT_H_UTC": "19",
    "EXIT_M_UTC": "30",
    # MOVE exposure, execution quality and value gates
    "MOVE_AUTO_ENTRY_MODE": "shadow",
    "MOVE_ALLOW_LONG": "true",
    "ALLOW_SHORT_MOVE": "false",
    "SHORT_MAX_RISK_USD": "0",
    "SAFE_EXECUTION_ENABLED": "true",
    "MAX_SPREAD_PCT": "3",
    "MAX_SLIPPAGE_PCT": "1",
    "MIN_BOOK_DEPTH_MULTIPLE": "1",
    "MAX_QUOTE_AGE_SEC": "20",
    "ORDER_CHUNK_LOTS": "1000",
    "MOVE_MIN_LONG_EDGE_ABS_USD": "0.01",
    "MOVE_MIN_SHORT_EDGE_ABS_USD": "0.02",
    "MOVE_MIN_LONG_EDGE_PCT": "5",
    "MOVE_MIN_SHORT_EDGE_PCT": "10",
    "MOVE_MIN_TTE_MINUTES": "90",
    "MOVE_MAX_TTE_HOURS": "30",
    "MAX_CONCURRENT_MOVE_POSITIONS": "1",
    "MOVE_MAX_MODEL_AGE_SEC": "600",
    "MOVE_MIN_BID_SIZE": "1",
    "MOVE_MIN_ASK_SIZE": "1",
    "MOVE_MAX_JUMP_SCORE_SHORT": "0.30",
    "MOVE_MAX_LONG_PREMIUM_RISK_USD": "1000",
    "MOVE_MAX_SHORT_MARGIN_USAGE_PCT": "30",
    "MOVE_MIN_LIQUIDATION_BUFFER_PCT": "50",
    "MOVE_NO_ENTRY_BEFORE_SETTLEMENT_SEC": "3600",
    "MOVE_REQUIRE_NO_OPEN_ORDERS": "true",
    "MOVE_REQUIRE_FLAT": "true",
    "MOVE_DRY_RUN_CAPITAL_USD": "1000",
    "MOVE_FORECAST_LOOKBACK_DAYS": "30",
    "MOVE_FORECAST_OUTER_SCENARIOS": "32",
    "MOVE_FORECAST_PATHS_PER_SCENARIO": "128",
    # Trend signal, contract, liquidity and execution controls
    "TREND_LOTS": "100",
    "TREND_AUTO_ENTRY_MODE": "shadow",
    "TREND_RISK_BUDGET_USD": "100",
    "TREND_REENTRY_COOLDOWN_MIN": "30",
    "TREND_EMA_GAP_PCT": "0.05",
    "TREND_RSI_UP": "55",
    "TREND_RSI_DOWN": "45",
    "TREND_15M_SLOPE_BARS": "3",
    "TREND_MIN_15M_SLOPE_PCT": "0",
    "TREND_ADX_MIN": "18",
    "TREND_1H_CONFIRM_SAMPLES": "2",
    "TREND_MIN_TTE_HOURS": "4",
    "TREND_TARGET_DELTA": "0.65",
    "TREND_MAX_SPREAD_PCT": "12",
    "TREND_MIN_BOOK_DEPTH_LOTS": "10",
    "TREND_BOOK_PARTICIPATION_PCT": "25",
    "TREND_QUOTE_MAX_AGE_SECS": "20",
    "TREND_MAX_MARK_IV": "0",
    "TREND_ALLOW_MISSING_BOOK": "false",
    "TREND_MAX_SLIPPAGE_PCT": "1",
    "TREND_ORDER_CHUNK_LOTS": "1000",
    "TREND_MARKET_FALLBACK_ENABLED": "false",
    "OPTION_FEE_RATE": "0.00010",
    "OPTION_FEE_CAP_PCT": "0.035",
    # Alert behavior resets, but its account-specific credentials do not.
    "TELEGRAM_ALERTS": "true",
}
CONFIG_PAGE_PRESERVED_KEYS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")

assert set(CONFIG_PAGE_DEFAULTS).issubset(CONFIG_KEYS)
assert set(CONFIG_PAGE_PRESERVED_KEYS).issubset(CONFIG_KEYS)

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
# Every account is a full trading account: whoever is logged in trades with
# their own Delta keys against their own state files. PRIMARY_ACCOUNT_USER is
# an explicit administrative/display role only; coexistent accounts retain
# the same independent dashboard and bot capabilities.  The scheduled bot
# engine (Delta_Straddle_Live.py) runs on BOT_USER's folder and keys.
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


def _primary_account_user() -> str:
    """Configured main account, without coupling the role to BOT_USER.

    BOT_USER remains the backward-compatible fallback so existing installs
    keep their current protected account until PRIMARY_ACCOUNT_USER is set.
    """
    configured = _safe_user(os.getenv("PRIMARY_ACCOUNT_USER", ""))
    return configured or _safe_user(BOT_USER) or _safe_user(DASH_USER) or "mathi"


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
    username = _safe_user(acct.get("username", ""))
    if not username:
        raise ValueError("refusing to save an account with an invalid username")
    acct = {**acct, "username": username}
    d = _udir(username)
    d.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(d / "account.json", acct)
    _drop_basic_cache(username)


def _drop_basic_cache(username: str) -> None:
    """Forget memoized Basic-auth verdicts for a user so a password change
    or account deletion takes effect immediately, not on next restart."""
    for k in [k for k in _basic_cache if k[0] == username]:
        del _basic_cache[k]


def _load_account_record(path: Path, expected_username: str) -> dict | None:
    """Load one account only when its identity matches its directory.

    Account credentials are an authentication boundary, so unlike ordinary
    display/state JSON this reader never falls back to a stale ``.bak`` file.
    Missing, malformed and semantically mismatched records all fail closed.
    """
    try:
        acct = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(acct, dict):
        return None
    embedded = _safe_user(acct.get("username", ""))
    if not embedded or embedded != expected_username:
        return None
    return {**acct, "username": embedded}


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
            a.pop("primary", None)          # role is configured centrally now
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
        if not d.is_dir():
            continue
        expected = _safe_user(d.name)
        if not expected or expected != d.name:
            continue
        acct = _load_account_record(d / "account.json", expected)
        if acct is not None:
            out.append(acct)
    return out


def _find_account(username: str) -> dict | None:
    u = _safe_user(username)
    if not u:
        return None
    acct = _load_account_record(_account_file(u), u)
    if acct is None:
        # Bootstrap path (first request ever may look up the login user)
        acct = next((a for a in _load_accounts()
                     if a.get("username") == u), None)
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


def _mode_data_dir(dry_run: bool = False) -> Path:
    """Return the isolated persistence namespace for one execution mode.

    Existing account-root files remain the authoritative LIVE namespace for
    rollback compatibility.  Simulations are never written beside them.
    """
    directory = _user_dir() / "dry_run" if dry_run else _user_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _slot_file(slot: str, *, dry_run: bool = False) -> Path:
    return _mode_data_dir(dry_run) / SLOT_STATE_FILES.get(
        slot, SLOT_STATE_FILES["evening"])


def _hist_file(*, dry_run: bool = False) -> Path:
    return _mode_data_dir(dry_run) / "trade_history.json"


def _is_dry_record(record: dict | None) -> bool:
    if not isinstance(record, dict):
        return False
    mode = str(record.get("execution_mode") or "").strip().lower()
    if mode:
        return mode in {"dry", "dry_run", "simulation", "simulated"}
    value = record.get("dry_run")
    return value is True or str(value or "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _history_accounting_complete(record: dict) -> bool:
    """Whether a closed-trade row contains final realised accounting.

    Legacy and dry-run rows predate fee detail, so an explicit exit price and
    P&L remain sufficient for those.  An externally detected flat position is
    stricter: until both sides' fees and fee-aware net P&L are known, its row
    must remain retryable instead of being mistaken for a completed history
    append by the dashboard poller.
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

    def finite_number(key: str) -> bool:
        value = record.get(key)
        if value is None or isinstance(value, bool):
            return False
        try:
            return math.isfinite(float(value))
        except (TypeError, ValueError, OverflowError):
            return False

    if not finite_number("exit_mark") or not finite_number("pnl_usd"):
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
    return all(finite_number(key) for key in (
        "gross_pnl_usd", "fees_usd", "entry_fee_usd", "exit_fee_usd",
    ))


def _append_trade_history(
    record: dict,
    owner: str,
    *,
    dry_run: bool | None = None,
) -> bool:
    """Cross-process-safe history upsert; true means accounting is final.

    A pending external-close row is intentionally persisted but returns false,
    keeping the slot's ``history_pending`` retry flag set.  Once authoritative
    accounting arrives, the same trade is repaired in place rather than being
    stranded as a duplicate with null or synthetic P&L.
    """
    is_dry_run = _is_dry_record(record) if dry_run is None else bool(dry_run)
    data_dir = _mode_data_dir(is_dry_run)
    with account_file_lock(data_dir, "history", owner, wait_sec=2.0) as acquired:
        if not acquired:
            return False
        path = _hist_file(dry_run=is_dry_run)
        if path.exists():
            try:
                history = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                return False
            if (not isinstance(history, list)
                    or any(not isinstance(row, dict) for row in history)):
                return False
        else:
            history = []
        incoming = dict(record)
        incoming["accounting_status"] = (
            "complete" if _history_accounting_complete(incoming) else "pending"
        )
        key = (incoming.get("simulation_id")
               or incoming.get("client_order_id") or incoming.get("order_id"),
               incoming.get("symbol"), incoming.get("entry_date") or incoming.get("date"),
               incoming.get("entry_time_utc") or incoming.get("entry_time"))

        def row_key(row):
            return (row.get("simulation_id")
                    or row.get("client_order_id") or row.get("order_id"),
                    row.get("symbol"), row.get("entry_date") or row.get("date"),
                    row.get("entry_time_utc") or row.get("entry_time"))

        def same_record(row: dict) -> bool:
            if row_key(row) == key:
                return True
            if not (is_dry_run and _is_dry_record(row)):
                return False
            # First isolated import may meet a legacy paper row that predates
            # simulation_id. Match its immutable entry identity once, then the
            # merged row gains the stable ID for all subsequent upserts.
            return all(str(row.get(field) or "") == str(incoming.get(field) or "")
                       for field in ("slot", "symbol", "lots", "entry_mark")) \
                and str(row.get("entry_date") or row.get("date") or "") == str(
                    incoming.get("entry_date") or incoming.get("date") or "") \
                and str(row.get("entry_time_utc") or row.get("entry_time") or "") == str(
                    incoming.get("entry_time_utc") or incoming.get("entry_time") or "")

        duplicate_index = next((
            index for index, row in enumerate(history)
            if isinstance(row, dict) and same_record(row)
        ), None)
        if duplicate_index is None:
            history.append(incoming)
            _atomic_write_json(path, history)
            stored = incoming
        else:
            existing = history[duplicate_index]
            if _history_accounting_complete(existing):
                stored = existing
            else:
                stored = {**existing, **incoming}
                stored["accounting_status"] = (
                    "complete" if _history_accounting_complete(stored) else "pending"
                )
                if stored != existing:
                    history[duplicate_index] = stored
                    _atomic_write_json(path, history)
        return _history_accounting_complete(stored)


def _flush_pending_history() -> None:
    for slot in SLOTS:
        path = _slot_file(slot)
        state = _load_json(path, {})
        if not state.get("history_pending"):
            continue
        record = {**state, "date": state.get("entry_date", ""),
                  "entry_time": state.get("entry_time_utc", ""),
                  "exit_time": state.get("exit_time_utc", ""),
                  "cost_usd": state.get("total_cost_usd", 0)}
        if _append_trade_history(record, f"dashboard-history-retry:{slot}"):
            state["history_pending"] = False
            _atomic_write_json(path, state)


def _cfg_file() -> Path:
    return _user_dir() / "config.json"


class AccountConfigError(RuntimeError):
    """The active account's persisted strategy config cannot be trusted."""


def _saved_user_cfg() -> tuple[dict, bool]:
    """Return (saved config, exists), rejecting corrupt account configuration.

    A strategy config controls live orders.  Falling back to environment
    defaults after a malformed file would turn a data error into a trading
    decision, so this reader intentionally does not use the generic backup
    fallback.
    """
    path = _cfg_file()
    if not path.exists():
        return {}, False
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise AccountConfigError(
            f"Account configuration is unreadable: {path.name}"
        ) from exc
    if not isinstance(saved, dict):
        raise AccountConfigError("Account configuration must be a JSON object")
    raw_mode = str(saved.get("TREND_AUTO_ENTRY_MODE") or "").strip().lower()
    if raw_mode and raw_mode not in {"disabled", "shadow", "live"}:
        raise AccountConfigError("Account Trend auto-entry mode is invalid")
    if raw_mode:
        saved = {**saved, "TREND_AUTO_ENTRY_MODE": raw_mode}
    raw_move_mode = str(
        saved.get("MOVE_AUTO_ENTRY_MODE") or "").strip().lower()
    if raw_move_mode and raw_move_mode not in {
            "disabled", "shadow", "live"}:
        raise AccountConfigError("Account MOVE auto-entry mode is invalid")
    if raw_move_mode:
        saved = {**saved, "MOVE_AUTO_ENTRY_MODE": raw_move_mode}
    return saved, True


def _user_cfg() -> dict:
    """The active account's strategy config: .env values as global defaults,
    overridden key by key by users/<name>/config.json.

    Trend and MOVE auto-live are exceptions: each must be explicitly
    persisted for the active account and is never inherited from the process
    environment.
    """
    cfg = {k: os.getenv(k, "") for k in CONFIG_KEYS}
    saved, _ = _saved_user_cfg()
    cfg.update({k: str(v) for k, v in saved.items() if k in CONFIG_KEYS})
    mode = str(saved.get("TREND_AUTO_ENTRY_MODE") or "").strip().lower()
    if not mode:
        mode = "shadow"
    cfg["TREND_AUTO_ENTRY_MODE"] = mode
    cfg["TREND_AUTO_ENTRY_ENABLED"] = "true" if mode == "live" else "false"
    move_mode = str(
        saved.get("MOVE_AUTO_ENTRY_MODE") or "").strip().lower()
    cfg["MOVE_AUTO_ENTRY_MODE"] = move_mode or "shadow"
    return cfg


def _cfg(key: str, default: str = "") -> str:
    v = _user_cfg().get(key, "")
    return v if v != "" else default


def _cfg_bool(key: str, default: bool = False) -> bool:
    v = _cfg(key)
    return v.lower() in ("1", "true", "yes") if v else default


def _trading_mode() -> tuple[bool, str]:
    """Return the server-authoritative account mode and a stable revision.

    The revision binds a confirmation to the configuration that produced its
    preview.  A save between preview and submit therefore fails closed rather
    than changing a simulated click into a real order (or vice versa).
    """
    cfg = _user_cfg()
    raw = str(cfg.get("DRY_RUN") or "").strip().lower()
    dry_run = raw in {"1", "true", "yes", "on"}
    canonical = json.dumps(cfg, sort_keys=True, separators=(",", ":"))
    revision = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return dry_run, revision


def _trading_mode_payload() -> dict:
    dry_run, revision = _trading_mode()
    return {
        "dry_run_mode": dry_run,
        "trading_mode": "DRY RUN" if dry_run else "LIVE",
        "execution_mode": "dry_run" if dry_run else "live",
        "mode_revision": revision,
    }


def _mode_expectation_error(data: dict | None) -> str | None:
    """Validate an optional preview/confirmation mode binding."""
    data = data if isinstance(data, dict) else {}
    current = _trading_mode_payload()
    expected_mode = str(
        data.get("expected_mode") or data.get("execution_mode") or ""
    ).strip().lower().replace(" ", "_")
    if expected_mode in {"dry", "simulation", "simulated"}:
        expected_mode = "dry_run"
    if expected_mode and expected_mode not in {"dry_run", "live"}:
        return "Invalid expected trading mode"
    if expected_mode and expected_mode != current["execution_mode"]:
        return (
            "Trading Mode changed after preview. Refresh and review the "
            f"{current['trading_mode']} action before submitting."
        )
    if "dry_run" in data:
        raw = data.get("dry_run")
        expected_dry = raw is True or str(raw or "").strip().lower() in {
            "1", "true", "yes", "on",
        }
        if expected_dry != current["dry_run_mode"]:
            return (
                "Trading Mode changed after preview. Refresh and confirm the "
                "action again."
            )
    revision = str(data.get("mode_revision") or "").strip()
    if revision and revision != current["mode_revision"]:
        return (
            "Configuration changed after preview. Refresh the contract, "
            "sizing and Trading Mode before submitting."
        )
    return None


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
        primary = acct["username"] == _primary_account_user()
        return jsonify({"username":     acct["username"],
                        "display_name": acct.get("display_name", acct["username"]),
                        "bot":          acct["username"] == _safe_user(BOT_USER),
                        "primary":      primary,
                        "role":         "primary" if primary else "coexistent"})
    username = _safe_user(DASH_USER) or "mathi"
    primary = username == _primary_account_user()
    return jsonify({"username": username,
                    "display_name": os.getenv("ACCOUNT_NAME", username.capitalize()),
                    "bot": username == _safe_user(BOT_USER),
                    "primary": primary,
                    "role": "primary" if primary else "coexistent"})


def _mask(s: str) -> str:
    return (s[:4] + "•" * 8 + s[-4:]) if s and len(s) > 8 else ("•" * 8 if s else "")


@app.route("/api/accounts", methods=["GET"])
def api_accounts_list():
    primary_user = _primary_account_user()
    rows = []
    for account in _load_accounts():
        username = _safe_user(account.get("username", ""))
        primary = username == primary_user
        rows.append({
            "username":     username,
            "display_name": account.get("display_name", ""),
            "api_key":      _mask(account.get("api_key", "")),
            "has_secret":   bool(account.get("api_secret")),
            "bot":          username == _safe_user(BOT_USER),
            "primary":      primary,
            "role":         "primary" if primary else "coexistent",
        })
    rows.sort(key=lambda row: (
        not row["primary"],
        str(row.get("display_name") or row["username"]).casefold(),
        row["username"],
    ))
    return jsonify(rows)


@app.route("/api/accounts", methods=["POST"])
def api_accounts_save():
    """Create or update a full, independent trading account."""
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
    if username == _primary_account_user():
        return jsonify({"ok": False, "error": "The primary account cannot be deleted"}), 400
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
            backup = path.with_suffix(path.suffix + ".bak")
            try:
                return json.loads(backup.read_text(encoding="utf-8"))
            except Exception:
                pass
    return default


def _atomic_write_json(path: Path, value) -> None:
    """Durably replace shared JSON so monitors never read a partial file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
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


def _simulation_identity(record: dict, slot: str = "") -> str:
    existing = str(record.get("simulation_id") or "").strip()
    if existing:
        return existing
    basis = "|".join(str(value or "") for value in (
        _active_user(), slot or record.get("slot"), record.get("symbol"),
        record.get("entry_date") or record.get("date"),
        record.get("entry_time_utc") or record.get("entry_time"),
        record.get("lots"), record.get("entry_mark"),
    ))
    return "sim-" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:20]


def _as_dry_record(record: dict, slot: str = "") -> dict:
    result = dict(record)
    result["dry_run"] = True
    result["execution_mode"] = "dry_run"
    result["simulation_id"] = _simulation_identity(result, slot)
    if slot:
        result.setdefault("slot", slot)
    return result


def _import_legacy_dry_records() -> None:
    """Copy explicit legacy simulations into the isolated namespace.

    This migration is deliberately non-destructive: source files are retained
    for rollback, while every real-facing reader filters them out.  The copy
    is idempotent and refuses to replace a different active simulation.
    """
    root = _user_dir()
    dry_dir = _mode_data_dir(True)
    with account_file_lock(
        root, "dry-run-import", f"dashboard-dry-import:{os.getpid()}",
        stale_after_sec=30, wait_sec=0,
    ) as acquired:
        if not acquired:
            return
        legacy_history = _load_json(root / "trade_history.json", [])
        if isinstance(legacy_history, list):
            for row in legacy_history:
                if isinstance(row, dict) and _is_dry_record(row):
                    _append_trade_history(
                        _as_dry_record(row, str(row.get("slot") or "")),
                        "legacy-dry-history-import",
                        dry_run=True,
                    )
        for slot in SLOTS:
            source = _load_json(root / SLOT_STATE_FILES[slot], {})
            if not _is_dry_record(source):
                continue
            imported = _as_dry_record(source, slot)
            destination_path = dry_dir / SLOT_STATE_FILES[slot]
            destination = _load_json(destination_path, {})
            same_cycle = (
                destination.get("simulation_id") == imported["simulation_id"])
            destination_idle = str(destination.get("status") or "").upper() in {
                "", "IDLE",
            }
            if not destination or destination_idle or same_cycle:
                _atomic_write_json(destination_path, imported)
            if str(imported.get("status") or "").upper() == "CLOSED":
                _append_trade_history(
                    imported, f"legacy-dry-state-import:{slot}", dry_run=True)


def _pnl_stats(trades: list, *, dry_run: bool = False) -> dict:
    """Performance statistics for exactly one execution mode."""
    pnls   = [float(t.get("pnl_usd", 0)) for t in trades
              if t.get("pnl_usd") is not None
              and _is_dry_record(t) is bool(dry_run)]
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
    "dry-run":   ("dry_run.html",   "Dry Run Dashboard"),
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
        config_page_defaults=CONFIG_PAGE_DEFAULTS,
        config_page_preserved_keys=CONFIG_PAGE_PRESERVED_KEYS,
    )


_last_sync: dict = {}   # username -> last exchange-sync epoch
EXCHANGE_SYNC_INTERVAL_SECONDS = 8

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


def _is_trend_client_order_id(value) -> bool:
    """Only IDs created by this dashboard establish Trend ownership."""
    return str(value or "").lower().startswith("trend-")


def _trend_position_cycle_id(product_id, entry_at, order_ids) -> str:
    seed = "|".join((
        str(product_id), str(entry_at),
        ",".join(str(value) for value in (order_ids or []) if value not in (None, "")),
    ))
    return "trend-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]


def _exchange_timestamp_iso(value) -> str:
    """Normalize Delta's Unix-microsecond or ISO order time to UTC ISO."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        numeric = int(raw)
        magnitude = abs(numeric)
        if magnitude >= 100_000_000_000_000:
            seconds = numeric / 1_000_000
        elif magnitude >= 100_000_000_000:
            seconds = numeric / 1_000
        else:
            seconds = numeric
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        pass
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        return ""


def _is_owned_trend_state(state: dict) -> bool:
    """Recognise current, legacy, and explicitly managed Trend states.

    Older dashboard releases did not send a client_order_id, so their
    explicit ``trend_alignment`` / ``trend_auto`` trigger remains accepted.
    An ``exchange_sync`` C/P position is deliberately *not* accepted: it may
    have been opened manually or by another strategy on the same account.
    The sole exception is an operator-authorized protection-only record. That
    marker is never inferred from exchange data; it exists so an already
    monitored external position can retain TP/SL supervision across a rollout
    without falsely relabelling its entry as a bot trade.
    """
    if not isinstance(state, dict):
        return False
    if _is_trend_client_order_id(state.get("client_order_id")):
        return True
    if (state.get("operator_authorized_protection_only") is True
            and str(state.get("ownership") or "").lower() == "external_protection_only"):
        return True
    trigger = str(state.get("entry_trigger") or "").lower()
    return trigger in {"trend_alignment", "trend_auto", "trend_recovered",
                       "trend_shadow_promoted"}


def _trend_state_covers_exchange_position(state: dict, product_id: int,
                                           signed_size: int,
                                           health: dict | None = None,
                                           user: str | None = None) -> bool:
    """Whether an adopted aggregate is already covered by this Trend state.

    This narrow predicate prevents the delayed margined-position feed from
    rendering a verified Trend position a second time as external. Bot-only
    and adopted aggregates both require a fresh, revision-matched monitor
    proof; a newly observed excess therefore stays visible until protection
    succeeds.
    """
    health = health if isinstance(health, dict) else {}
    if (not _is_owned_trend_state(state)
            or str(state.get("status") or "").upper() != "OPEN"
            or not _tp_continuity_health_fresh(health)
            or not _tp_health_matches(
                health, state, user or _active_user(), "trend")
            or health.get("continuity_verified") is not True
            or health.get("protection_established") is not True):
        return False
    try:
        if int(state.get("product_id") or 0) != int(product_id):
            return False
        signed_size = int(signed_size)
        expected_sign = -1 if str(state.get("side") or "").lower() == "short" else 1
        protected_lots = abs(int(float(
            state.get("protection_lots") or state.get("lots") or 0
        )))
        health_size = int(health.get("continuity_verified_size"))
        health_protected = abs(int(float(health.get("protected_lots") or 0)))
    except (TypeError, ValueError, OverflowError):
        return False
    expected_size = expected_sign * protected_lots
    exchange_coverage = health.get("exchange_protection_complete") is True
    local_coverage = bool(
        health.get("local_fallback_active")
        and _tp_running(user or _active_user(), "trend")
    )
    has_coverage = exchange_coverage or local_coverage
    return (
        signed_size != 0
        and signed_size == expected_size
        and health_size == expected_size
        and health_protected == protected_lots
        and has_coverage
    )


def _open_owned_trend_same_product(state: dict, product_id: int) -> bool:
    """Keep an existing Trend ownership record authoritative during sync.

    A size/basis mismatch is work for the real-time protection monitor.  The
    dashboard's slower margined-position snapshot must never recover over the
    existing record because doing so would relabel external lots as bot-owned
    and discard the cycle/protection revisions used by that monitor.
    """
    if (not _is_owned_trend_state(state)
            or str(state.get("status") or "").upper() != "OPEN"):
        return False
    try:
        return int(state.get("product_id") or 0) == int(product_id)
    except (TypeError, ValueError, OverflowError):
        return False


def _owned_trend_order(orders: list, product_id: int) -> dict | None:
    # Ownership belongs to the latest non-reduce BUY that created the current
    # long cycle. Looking for *any* historical trend ID would falsely reclaim
    # a later manual reopen in the same product.
    candidates = [o for o in orders
                  if int(o.get("product_id", 0) or 0) == int(product_id)
                  and str(o.get("side", "")).lower() == "buy"
                  and str(o.get("reduce_only", "false")).lower() not in {"1", "true", "yes"}]
    if not candidates:
        return None
    candidates.sort(key=lambda o: str(o.get("created_at", "")), reverse=True)
    latest = candidates[0]
    return latest if _is_trend_client_order_id(latest.get("client_order_id")) else None


def _finite_position_number(position: dict, key: str) -> float | None:
    """Read a finite exchange position field without silently inventing zero."""
    value = position.get(key)
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _external_option_live_pnl(position: dict) -> tuple[float, str]:
    """Return the live, fee-aware net P&L for a visible-only option position.

    Delta reports option entry/exit cashflows separately.  ``unrealized_pnl``
    alone is therefore not the trade's complete mark-to-market result: for a
    long option it omits the premium already paid.  When those cashflow fields
    are present, use the exchange's own net cashflow basis and subtract the
    blocked commission.  Older/malformed payloads fall back to the exchange
    P&L field rather than attempting a price-derived estimate.
    """
    realized_cashflow = _finite_position_number(position, "realized_cashflow")
    unrealized_cashflow = _finite_position_number(position, "unrealized_cashflow")
    commission = _finite_position_number(position, "commission")
    if (realized_cashflow is not None and unrealized_cashflow is not None
            and commission is not None):
        return realized_cashflow + unrealized_cashflow - abs(commission), "cashflow_net"

    fallback = _finite_position_number(position, "unrealized_pnl")
    return (fallback if fallback is not None else 0.0), "unrealized_pnl_fallback"


def _external_option_view(position: dict) -> dict:
    """Safe, UI-facing representation of a non-bot C/P position."""
    live_pnl, pnl_source = _external_option_live_pnl(position)
    return {
        "ownership": "external",
        "product_id": int(position.get("product_id", 0) or 0),
        "symbol": str(position.get("product_symbol", "")),
        "side": "short" if float(position.get("size", 0) or 0) < 0 else "long",
        "lots": abs(int(float(position.get("size", 0) or 0))),
        "entry_mark": float(position.get("entry_price", 0) or 0),
        "mark_price": float(position.get("mark_price", 0) or 0),
        "live_pnl": live_pnl,
        "pnl_source": pnl_source,
        "created_at": str(position.get("created_at", "")),
    }


def _reconcile_stale_close(slot: str, state: dict, live_pids: set) -> dict:
    """Leave an exchange-flat OPEN state for its strict TP monitor to resolve.

    The dashboard cannot safely infer ownership from the latest same-product
    opposite-side order: that order can predate this entry, be partial, open a
    reversal, or belong to another strategy.  Open-state supervision runs
    before exchange sync and the TP monitor verifies post-entry, reduce-only,
    full-size order identity plus fee-aware accounting.  Until it does, keeping
    this state OPEN is safer than publishing a fabricated close or P&L.
    """
    pid = int(state.get("product_id", 0) or 0)
    if state.get("status") != "OPEN" or pid == 0 or pid in live_pids:
        return state
    return state


def _sync_states_from_exchange() -> None:
    """Reconcile bot-owned positions without claiming manual option trades.

    MOVE positions retain their historical time-slot reconciliation. A BTC
    C/P position is attached to Trend only when its opening order carries our
    ``trend-`` client ID (or an already-open legacy Trend state explicitly
    says it came from a Trend trigger). An exact same-product/same-direction
    addition is hidden from the external list only after the protection monitor
    has durably adopted it; all unrelated C/P positions remain external.
    """
    _flush_pending_history()
    key, secret = _active_creds()
    if not key or not secret:
        return
    user = _active_user()
    if time.time() - _last_sync.get(user, 0) < EXCHANGE_SYNC_INTERVAL_SECONDS:
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
        trend_health = _tp_health(user, "trend")

        # Order history is ownership evidence for recovery after a dashboard
        # restart. Failure to fetch it is fail-closed: unknown C/P positions
        # remain external rather than being adopted on a guess.
        orders = []
        try:
            query = "?page_size=100"
            oh = req.get(f"{API_BASE}/v2/orders/history", params={"page_size": 100},
                         headers=_sign("GET", "/v2/orders/history", query), timeout=8).json()
            if oh.get("success"):
                orders = oh.get("result", [])
        except Exception:
            orders = []

        # Repair legacy cross-slot states. Only an explicit legacy Trend
        # trigger is migrated; exchange_sync/manual C/P records are detached.
        trend_state = states["trend"]
        if (trend_state.get("status") == "OPEN"
                and str(trend_state.get("symbol", "")).startswith(("C-BTC", "P-BTC"))
                and not _is_owned_trend_state(trend_state)):
            idle = {"slot": "trend", "status": "IDLE",
                    "detached_external_product_id": trend_state.get("product_id"),
                    "detached_at_utc": datetime.now(timezone.utc).isoformat()}
            _atomic_write_json(_slot_file("trend"), idle)
            states["trend"] = idle
        for legacy_slot in MOVE_SLOTS:
            legacy = states[legacy_slot]
            if not (legacy.get("status") == "OPEN"
                    and str(legacy.get("symbol", "")).startswith(("C-BTC", "P-BTC"))):
                continue
            if (_is_owned_trend_state(legacy)
                    and states["trend"].get("status") != "OPEN"):
                migrated = {**legacy, "slot": "trend", "migrated_from_slot": legacy_slot}
                _atomic_write_json(_slot_file("trend"), migrated)
                states["trend"] = migrated
            idle = {"slot": legacy_slot, "status": "IDLE",
                    "detached_option_product_id": legacy.get("product_id")}
            _atomic_write_json(_slot_file(legacy_slot), idle)
            states[legacy_slot] = idle

        states = {slot: _reconcile_stale_close(slot, s, live_pids) for slot, s in states.items()}
        external = []
        for p in live:
            pid   = int(p["product_id"])
            size  = int(float(p["size"]))
            entry = float(p.get("entry_price") or 0)
            product_symbol = str(p.get("product_symbol", ""))
            # Option ownership is hidden only by a fresh strict Trend health
            # proof below. The generic state matcher has no protection or
            # continuity generation and could otherwise conceal an aggregate
            # while resizing, after a dead heartbeat, or after a same-size
            # cycle replacement.
            if (not product_symbol.startswith(("C-BTC", "P-BTC"))
                    and any(_state_matches(s, pid, size, entry)
                            for s in states.values())):
                continue
            created = (_exchange_timestamp_iso(p.get("created_at"))
                       or str(p.get("created_at", "")))
            if product_symbol.startswith(("C-BTC", "P-BTC")):
                slot = "trend"
                other = states[slot]
                if _trend_state_covers_exchange_position(
                        other, pid, size, trend_health, user):
                    continue
                if (str(other.get("status") or "").upper() == "OWNERSHIP_AMBIGUOUS"
                        or str(other.get("continuity_status") or "")
                        == "broken_reopened"):
                    row = _external_option_view(p)
                    row["ownership"] = "external_after_trend_cycle_close"
                    external.append(row)
                    continue
                if _open_owned_trend_same_product(other, pid):
                    # Preserve the complete Trend cycle and expose the
                    # mismatched aggregate until the real-time monitor has
                    # proven continuity, adopted/rebased it, and published a
                    # matching protection revision.  Never run order-history
                    # recovery over an already-owned OPEN same-product state.
                    row = _external_option_view(p)
                    row["ownership"] = "pending_trend_reconciliation"
                    row["trend_state_lots"] = abs(int(float(
                        other.get("protection_lots") or other.get("lots") or 0
                    )))
                    external.append(row)
                    continue
                owned_order = _owned_trend_order(orders, pid) if size > 0 else None
                if not owned_order:
                    external.append(_external_option_view(p))
                    continue
                created = (_exchange_timestamp_iso(owned_order.get("created_at"))
                           or created)
                if (other.get("status") == "OPEN"
                        and int(other.get("product_id", 0) or 0) != pid) \
                   or _closed_blocks_adoption(other, created):
                    row = _external_option_view(p)
                    row["ownership"] = "bot_conflict"
                    external.append(row)
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
                "entry_trigger":  "trend_recovered" if slot == "trend" else "exchange_sync",
            }
            if slot == "trend":
                new_state.update({
                    "client_order_id": owned_order.get("client_order_id"),
                    "order_id": owned_order.get("id"),
                    "ownership": "trend_bot",
                    "owned_entry_lots": abs(size),
                    "original_owned_entry_lots": abs(size),
                    "protection_lots": abs(size),
                    "max_protected_lots": abs(size),
                    "protection_revision": 0,
                    "continuity_revision": 0,
                    "position_cycle_id": _trend_position_cycle_id(
                        pid, created, [owned_order.get("id")]),
                    "continuity_anchor_utc": created,
                    "continuity_verified": False,
                    "continuity_status": "awaiting_monitor_verification",
                    "original_bot_entry_mark": entry,
                    "original_bot_entry_fee_usd": _order_commission_optional_usd(
                        owned_order),
                    "original_bot_entry_fee_source": (
                        "exchange" if _order_commission_optional_usd(owned_order)
                        is not None else "fee_pending"
                    ),
                    "cycle_entry_lots_total": abs(size),
                    "cycle_exit_lots_total": 0,
                    "partial_exit_accounting_status": "complete",
                    "position_composition": "bot_only",
                })
            _atomic_write_json(_slot_file(slot), new_state)
            states[slot] = new_state
        _external_options[user] = external
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


def _utc_trade_exit_at(record: dict) -> datetime | None:
    """Return a closed trade's complete UTC exit timestamp.

    Older state/history records store only an entry date and UTC clock times.
    For those records an exit clock earlier than the entry clock means the
    position closed after midnight on the following UTC day.  Newer records
    can carry an authoritative ``exit_date`` or ISO ``exit_at_utc`` instead.
    """
    stamp = str(
        record.get("exit_at_utc") or record.get("closed_at_utc") or ""
    ).strip()
    if stamp:
        try:
            dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except (TypeError, ValueError):
            pass

    exit_clock = str(
        record.get("exit_time_utc") or record.get("exit_time") or ""
    ).strip()
    if not exit_clock:
        return None

    # Some imported history formats put a complete ISO timestamp in the
    # exit-time field.  Preserve its date instead of combining it again.
    try:
        dt = datetime.fromisoformat(exit_clock.replace("Z", "+00:00"))
        if "T" in exit_clock or " " in exit_clock:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        pass

    explicit_exit_date = str(record.get("exit_date") or "").strip()
    entry_date = str(record.get("entry_date") or record.get("date") or "").strip()
    exit_date = explicit_exit_date or entry_date
    if not exit_date:
        return None
    try:
        exited = datetime.fromisoformat(
            f"{exit_date}T{exit_clock}".replace("Z", "+00:00")
        )
        if exited.tzinfo is None:
            exited = exited.replace(tzinfo=timezone.utc)
        exited = exited.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None

    # An explicit exit date is authoritative.  Infer a next-day close only
    # for legacy records whose sole calendar date is their entry date.
    if not explicit_exit_date:
        entry_clock = str(
            record.get("entry_time_utc") or record.get("entry_time") or ""
        ).strip()
        if entry_clock:
            try:
                entered = datetime.fromisoformat(
                    f"{entry_date}T{entry_clock}".replace("Z", "+00:00")
                )
                if entered.tzinfo is None:
                    entered = entered.replace(tzinfo=timezone.utc)
                if exited < entered.astimezone(timezone.utc):
                    exited += timedelta(days=1)
            except (TypeError, ValueError):
                pass
    return exited


_IST_TIMEZONE = timezone(timedelta(hours=5, minutes=30))


def _slot_state_visible_on_dashboard(
        state: dict, now: datetime | None = None) -> bool:
    """Whether a slot's position details belong on today's Overview card.

    CLOSED state remains persisted for history, reconciliation and re-entry
    safety.  Its card is presentation-only and expires at the next IST
    midnight.  An unparseable legacy close remains visible because its age
    cannot be proven; only definitively old position details are suppressed.
    """
    if not isinstance(state, dict):
        return False
    if str(state.get("status") or "").upper() != "CLOSED":
        return True
    exited = _utc_trade_exit_at(state)
    if exited is None:
        return True
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return (exited.astimezone(_IST_TIMEZONE).date()
            >= current.astimezone(_IST_TIMEZONE).date())


def _dashboard_slot_view(
        state: dict, now: datetime | None = None) -> dict:
    """Return an API presentation copy without mutating persisted state."""
    view = dict(state) if isinstance(state, dict) else {}
    view["dashboard_visible"] = _slot_state_visible_on_dashboard(view, now)
    return view


def _closed_trade_pnl(record: dict) -> float | None:
    """Return a finite realised P&L without turning missing data into zero."""
    value = record.get("pnl_usd")
    if value is None or isinstance(value, bool):
        return None
    try:
        pnl = float(value)
    except (TypeError, ValueError):
        return None
    return pnl if math.isfinite(pnl) else None


def _latest_closed_trade(history: list, slot_states: dict[str, dict]) -> dict | None:
    """Select the newest real closed trade from the ledger and slot states.

    History is the authoritative closed-trade ledger, but a just-closed slot
    state can precede (or survive failure of) its history append.  Both are
    considered; an exact timestamp tie favours history.  History records from
    older versions commonly omit ``status`` and are still closed by definition.
    """
    candidates: list[tuple[datetime, int, int, dict, str | None]] = []

    def add(record: dict, slot: str | None, source_rank: int, sequence: int,
            require_closed_status: bool) -> None:
        if not isinstance(record, dict):
            return
        status = str(record.get("status") or "").strip().upper()
        if ((require_closed_status and status != "CLOSED")
                or (not require_closed_status and status and status != "CLOSED")):
            return
        dry_run = record.get("dry_run", False)
        if dry_run is True or str(dry_run).strip().lower() in {"1", "true", "yes", "on"}:
            return
        exited = _utc_trade_exit_at(record)
        if exited is None:
            return
        candidates.append((exited, source_rank, sequence, record, slot))

    if isinstance(history, list):
        for sequence, record in enumerate(history):
            add(record, record.get("slot") if isinstance(record, dict) else None,
                1, sequence, False)
    for sequence, (slot, record) in enumerate(slot_states.items()):
        add(record, slot, 0, sequence, True)

    if not candidates:
        return None
    exited, _, _, record, slot = max(
        candidates, key=lambda candidate: candidate[:3]
    )
    return {
        "slot": slot,
        "symbol": str(record.get("symbol") or ""),
        "pnl_usd": _closed_trade_pnl(record),
        "exit_date": exited.strftime("%Y-%m-%d"),
        "exit_time_utc": exited.strftime("%H:%M:%S"),
        "closed_at_utc": exited.isoformat().replace("+00:00", "Z"),
    }


def _move_decision_dashboard_view(
    slot: str,
    *,
    dry_run: bool = False,
) -> dict | None:
    """Return the compact, non-sensitive part of the latest AUTO decision."""
    if slot not in {"morning", "evening"}:
        return None
    raw = _load_json(
        _mode_data_dir(dry_run) / f"move_decision_{slot}.json", {})
    if not isinstance(raw, dict):
        return None
    decision = raw.get("decision")
    forecast = raw.get("forecast")
    normalized = raw.get("normalized_input")
    if not all(isinstance(value, dict)
               for value in (decision, forecast, normalized)):
        return None
    contract = normalized.get("contract")
    metrics = decision.get("metrics")
    failed = decision.get("failed_gates")
    if not all(isinstance(value, dict)
               for value in (contract, metrics, failed)):
        return None

    def _numbers(source: dict, keys: tuple[str, ...]) -> dict:
        result = {}
        for key in keys:
            value = source.get(key)
            if isinstance(value, (int, float)) and math.isfinite(float(value)):
                result[key] = value
        return result

    failed_view = {}
    for group in ("common", "long", "short"):
        values = failed.get(group)
        failed_view[group] = [
            str(value) for value in values[:12]
        ] if isinstance(values, list) else []
    return {
        "schema_version": raw.get("schema_version"),
        "slot": slot,
        "decision_id": str(raw.get("decision_id") or ""),
        "recorded_at_utc": str(raw.get("recorded_at_utc") or ""),
        "auto_mode": str(raw.get("auto_mode") or "disabled").lower(),
        "dry_run": bool(raw.get("dry_run")),
        "symbol": str(contract.get("symbol") or ""),
        "action": str(decision.get("action") or "NO_TRADE"),
        "side": decision.get("side"),
        "conflict": bool(decision.get("conflict")),
        "forecast": {
            **_numbers(forecast, (
                "expected_payoff_low", "expected_payoff_mid",
                "expected_payoff_high", "payoff_p99", "jump_event_score",
                "market_jump_score", "scheduled_event_score",
                "model_timestamp_ms",
            )),
            "event_score_available": bool(
                forecast.get("event_score_available")),
            "event_risk_source": str(
                forecast.get("event_risk_source") or "unknown_high_risk"),
        },
        "metrics": _numbers(metrics, (
            "spread_pct", "quote_age_ms", "model_age_ms",
            "seconds_until_final_settlement", "long_edge_per_contract",
            "short_edge_per_contract", "long_hurdle", "short_hurdle",
            "long_premium_risk_per_contract",
            "short_p99_loss_per_contract",
        )),
        "failed_gates": failed_view,
    }


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
    try:
        _import_legacy_dry_records()
    except Exception as exc:
        print(f"Legacy dry-run import warning for {_active_user()}: {exc}")
    _sync_states_from_exchange()
    raw_state   = _load_json(_slot_file("evening"), {})
    raw_morning = _load_json(_slot_file("morning"), {})
    raw_trend   = _load_json(_slot_file("trend"), {})
    # Explicit legacy simulation rows may remain in the rollback-compatible
    # LIVE files.  They are never presented as real positions.
    raw_state = {} if _is_dry_record(raw_state) else _enrich_live(raw_state)
    raw_morning = {} if _is_dry_record(raw_morning) else _enrich_live(raw_morning)
    raw_trend = {} if _is_dry_record(raw_trend) else _enrich_live(raw_trend)
    latest_closed = _latest_closed_trade(
        _load_json(_hist_file(), []),
        {"evening": raw_state, "morning": raw_morning, "trend": raw_trend},
    )
    view_now = datetime.now(timezone.utc)
    state   = _dashboard_slot_view(raw_state, view_now)
    morning = _dashboard_slot_view(raw_morning, view_now)
    trend   = _dashboard_slot_view(raw_trend, view_now)
    state["latest_closed_trade"] = latest_closed
    state["morning"] = morning
    state["trend"]   = trend
    state["move_auto_mode"] = str(
        _user_cfg().get("MOVE_AUTO_ENTRY_MODE") or "shadow").lower()
    state["move_decisions"] = {
        slot: _move_decision_dashboard_view(slot)
        for slot in ("morning", "evening")
    }
    state.update(_trading_mode_payload())
    # Unrelated C/P positions remain separate. Exact same-product additions
    # disappear from this list only after the Trend monitor confirms adoption.
    state["external_options"] = _external_options.get(_active_user(), [])
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


@app.route("/api/external-options")
def api_external_options():
    _sync_states_from_exchange()
    return jsonify(_external_options.get(_active_user(), []))



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
    today_t = [
        t for t in trades
        if isinstance(t, dict) and not _is_dry_record(t)
        and _ist_calendar_date(
            t.get("entry_date") or t.get("date", ""),
            t.get("entry_time") or t.get("entry_time_utc", ""),
        ) == today_ist
    ]
    # Include any open slot position as a live row with real-time mark & P&L
    for slot in SLOTS:
        s = _load_json(_slot_file(slot), {})
        if (s.get("status") == "OPEN" and not _is_dry_record(s)
                and _ist_calendar_date(
                    s.get("entry_date", ""), s.get("entry_time_utc", ""),
                ) == today_ist):
            s["_live"] = True
            s["slot"]  = slot
            s = _enrich_live(s)
            today_t = [s] + today_t
    return jsonify(today_t)


_DASH_ACTIVE_ORDER_STATES = {
    "open", "pending", "partially_filled", "partially-filled", "untriggered", "triggered",
}
_DASH_TERMINAL_ORDER_STATES = {
    "closed", "filled", "cancelled", "canceled", "rejected", "expired", "failed",
}


def _dash_order_state(order: dict | None) -> str:
    return str((order or {}).get("state") or (order or {}).get("status") or "").lower()


def _move_client_id(action: str, slot: str) -> str:
    user = re.sub(r"[^a-z0-9]", "", _active_user().lower())[:7] or "acct"
    return f"mv-{action[:1]}-{user}-{slot[:1]}-{int(time.time() * 1000):x}-{secrets.token_hex(2)}"[:32]


def _strict_exchange_positions() -> list[dict]:
    data = req.get(f"{API_BASE}/v2/positions/margined",
                   headers=_sign("GET", "/v2/positions/margined"), timeout=8).json()
    if (not isinstance(data, dict) or not data.get("success")
            or not isinstance(data.get("result"), list)):
        error = data.get("error") if isinstance(data, dict) else data
        raise RuntimeError(f"exchange positions could not be verified: {error or data}")
    positions = []
    for position in data["result"]:
        if not isinstance(position, dict):
            raise RuntimeError("exchange positions could not be verified: malformed position")
        raw_size = position.get("size")
        if raw_size in (None, "") or isinstance(raw_size, bool):
            raise RuntimeError(
                "exchange positions could not be verified: missing position size")
        try:
            size = float(raw_size)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuntimeError(
                "exchange positions could not be verified: malformed size"
            ) from exc
        if not math.isfinite(size):
            raise RuntimeError(
                "exchange positions could not be verified: non-finite size")
        # Do not truncate before comparing with zero. Although Delta contracts
        # normally use integer lots, any fractional external exposure must
        # still keep account-wide Trading Mode locked.
        if size != 0:
            positions.append(position)
    return positions


_MODE_INACTIVE_STATE_STATUSES = {"", "IDLE", "CLOSED"}


def _strict_mode_state(path: Path) -> dict:
    """Read one mode state without falling back to a potentially stale backup."""
    if not path.exists():
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError(f"{path.name} is unreadable") from exc
    if not isinstance(state, dict):
        raise RuntimeError(f"{path.name} is not a JSON object")
    return state


def _mode_local_position_blockers() -> list[dict]:
    """Return every local position or unresolved entry that may own exposure.

    LIVE and isolated DRY RUN persistence are intentionally scanned together:
    changing the configured mode does not close or transfer either namespace.
    """
    account_dir = _user_dir()
    blockers = []
    for namespace_dry, data_dir in (
            (False, account_dir), (True, account_dir / "dry_run")):
        namespace = "dry_run" if namespace_dry else "live"
        for slot in SLOTS:
            state = _strict_mode_state(data_dir / SLOT_STATE_FILES[slot])
            status = str(state.get("status") or "").strip().upper()
            unresolved_submission = any(
                str(state.get(key) or "").strip()
                for key in (
                    "pending_entry_submission_state",
                    "pending_close_submission_state",
                )
            )
            if (status in _MODE_INACTIVE_STATE_STATUSES
                    and not unresolved_submission):
                continue
            effective_dry = namespace_dry or _is_dry_record(state)
            blockers.append({
                "source": "state",
                "execution_mode": "dry_run" if effective_dry else "live",
                "slot": slot,
                "status": status or "UNRESOLVED",
                "label": (
                    f"{'DRY RUN' if effective_dry else 'LIVE'} "
                    f"{slot.title()} ({status or 'unresolved'})"
                ),
            })

        # Scheduled MOVE entries keep a separate crash-recovery journal before
        # their state becomes OPEN. Its mere presence means a fill may exist.
        if data_dir.exists():
            for journal in sorted(data_dir.glob("pending_*_entry.json")):
                blockers.append({
                    "source": "entry_journal",
                    "execution_mode": namespace,
                    "slot": journal.stem.removeprefix(
                        "pending_").removesuffix("_entry"),
                    "status": "ENTRY_PENDING",
                    "label": (
                        f"{'DRY RUN' if namespace_dry else 'LIVE'} "
                        f"unresolved entry ({journal.name})"
                    ),
                })
            for journal in sorted(data_dir.glob("pending_trend_order_*.json")):
                blockers.append({
                    "source": "trend_order_intent",
                    "execution_mode": namespace,
                    "slot": "trend",
                    "status": "ENTRY_PENDING",
                    "label": (
                        f"{'DRY RUN' if namespace_dry else 'LIVE'} "
                        f"unresolved Trend order ({journal.name})"
                    ),
                })
    return blockers


def _trading_mode_change_status() -> dict:
    """Prove whether the account is flat enough to change execution mode.

    This is deliberately uncached. The POST path calls it while holding the
    same account entry mutex used by scheduled and manual entries.
    """
    result = {
        **_trading_mode_payload(),
        "mode_change_allowed": False,
        "mode_selection_enabled": False,
        "verification_ok": False,
        "open_position_count": 0,
        "exchange_position_count": None,
        "open_position_labels": [],
        "blockers": [],
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    try:
        local_blockers = _mode_local_position_blockers()
    except Exception as exc:
        app.logger.warning(
            "Trading Mode local position verification failed for %s: %s",
            _active_user(), exc,
        )
        result["mode_lock_reason"] = (
            "Trading Mode is locked because local position status could not "
            "be verified. Repair the account state before switching."
        )
        return result

    if local_blockers:
        result.update({
            "verification_ok": True,
            "open_position_count": len(local_blockers),
            "open_position_labels": [item["label"] for item in local_blockers],
            "blockers": local_blockers,
            "mode_lock_reason": (
                "Trading Mode is locked while a LIVE or DRY RUN position or "
                "unresolved entry is open. Close every position and wait for "
                "pending entries to resolve before switching."
            ),
        })
        return result

    key, secret = _active_creds()
    if not key or not secret:
        result["mode_lock_reason"] = (
            "Trading Mode is locked because exchange positions cannot be "
            "verified without this account's API credentials."
        )
        return result
    try:
        exchange_positions = _strict_exchange_positions()
    except Exception as exc:
        app.logger.warning(
            "Trading Mode exchange position verification failed for %s: %s",
            _active_user(), exc,
        )
        result["mode_lock_reason"] = (
            "Trading Mode is locked because exchange positions could not be "
            "verified. Retry after the exchange connection recovers."
        )
        return result

    if exchange_positions:
        labels = []
        blockers = []
        for position in exchange_positions:
            symbol = str(
                position.get("product_symbol")
                or position.get("symbol")
                or position.get("product_id")
                or "unknown product"
            )[:80]
            label = f"LIVE exchange position ({symbol})"
            labels.append(label)
            blockers.append({
                "source": "exchange",
                "execution_mode": "live",
                "slot": "",
                "status": "OPEN",
                "label": label,
            })
        result.update({
            "verification_ok": True,
            "open_position_count": len(exchange_positions),
            "exchange_position_count": len(exchange_positions),
            "open_position_labels": labels,
            "blockers": blockers,
            "mode_lock_reason": (
                "Trading Mode is locked while exchange positions are open, "
                "including external/manual positions. Close every position "
                "before switching."
            ),
        })
        return result

    result.update({
        "mode_change_allowed": True,
        "mode_selection_enabled": True,
        "verification_ok": True,
        "exchange_position_count": 0,
        "mode_lock_reason": (
            "No open LIVE, DRY RUN, pending, or external positions — "
            "Trading Mode can be changed."
        ),
    })
    return result


def _strict_realtime_position(product_id: int) -> dict:
    """Fetch one product from Delta's non-lagged position endpoint."""
    product_id = int(product_id)
    path = "/v2/positions"
    params = {"product_id": product_id}
    query = f"?product_id={product_id}"
    data = req.get(
        f"{API_BASE}{path}", params=params, headers=_sign("GET", path, query),
        timeout=8,
    ).json()
    result = data.get("result") if isinstance(data, dict) else None
    if (not isinstance(data, dict) or not data.get("success")
            or not isinstance(result, dict)):
        error = data.get("error") if isinstance(data, dict) else data
        raise RuntimeError(f"real-time position could not be verified: {error or data}")
    returned_product = result.get("product_id")
    if (returned_product not in (None, "")
            and str(returned_product) != str(product_id)):
        raise RuntimeError("real-time position returned a different product")
    position = dict(result)
    position.setdefault("product_id", product_id)
    try:
        size = float(position.get("size", 0))
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError("real-time position size is malformed") from exc
    if not math.isfinite(size) or not size.is_integer():
        raise RuntimeError("real-time position size is not an integer")
    return position


def _position_for_product(positions: list[dict], product_id: int) -> dict | None:
    return next((p for p in positions
                 if int(p.get("product_id") or 0) == int(product_id)), None)


def _lookup_dashboard_order(order_id=None, client_order_id=None,
                            product_id=None) -> dict | None:
    """Recover only an order bearing the persisted exchange identity."""
    if order_id:
        try:
            path = f"/v2/orders/{order_id}"
            query = f"?product_id={int(product_id)}" if product_id else ""
            params = {"product_id": int(product_id)} if product_id else None
            data = req.get(f"{API_BASE}{path}", params=params,
                           headers=_sign("GET", path, query), timeout=8).json()
            order = data.get("result") or {}
            if data.get("success") and isinstance(order, dict):
                return order
        except Exception:
            pass
    if not client_order_id:
        return None
    for path, params, query in (
        ("/v2/orders", {"states": "open", "page_size": 100},
         "?states=open&page_size=100"),
        ("/v2/orders/history", {"page_size": 100}, "?page_size=100"),
    ):
        try:
            data = req.get(f"{API_BASE}{path}", params=params,
                           headers=_sign("GET", path, query), timeout=8).json()
            if not data.get("success"):
                continue
            for order in data.get("result") or []:
                if str(order.get("client_order_id") or "") != str(client_order_id):
                    continue
                if product_id and int(order.get("product_id") or 0) != int(product_id):
                    continue
                return order
        except Exception:
            continue
    return None


def _terminal_fill(order: dict | None, requested: int) -> int | None:
    state = _dash_order_state(order)
    if state not in _DASH_TERMINAL_ORDER_STATES:
        return None
    for key in ("filled_size", "filled_quantity", "executed_size"):
        if (order or {}).get(key) not in (None, ""):
            try:
                return min(max(int(float(order[key])), 0), requested)
            except (TypeError, ValueError):
                return None
    if (order or {}).get("unfilled_size") not in (None, ""):
        try:
            return min(max(requested - int(float(order["unfilled_size"])), 0), requested)
        except (TypeError, ValueError):
            return None
    return 0 if state in {"rejected", "failed"} else None


def _wait_dashboard_terminal(order: dict, requested: int, product_id: int,
                             client_order_id: str, timeout_sec: float = 8.0
                             ) -> tuple[dict, int | None]:
    latest = dict(order or {})
    deadline = time.monotonic() + max(timeout_sec, 0)
    while True:
        filled = _terminal_fill(latest, requested)
        if filled is not None:
            if filled <= 0 or float(latest.get("average_fill_price") or 0) > 0:
                return latest, filled
        if time.monotonic() >= deadline:
            return latest, None
        refreshed = _lookup_dashboard_order(
            latest.get("id"), client_order_id, product_id)
        if refreshed:
            latest = refreshed
        time.sleep(0.25)


def _validate_dashboard_order(order: dict, *, product_id: int,
                              client_order_id: str, side: str,
                              reduce_only: bool) -> dict:
    if not isinstance(order, dict) or not order.get("id"):
        raise RuntimeError("exchange order acknowledgement has no identity")
    returned_client = str(order.get("client_order_id") or "")
    if returned_client and returned_client != client_order_id:
        raise RuntimeError("exchange client-order identity mismatch")
    if order.get("product_id") not in (None, "") \
            and int(order.get("product_id") or 0) != int(product_id):
        raise RuntimeError("exchange order product mismatch")
    if order.get("side") and str(order.get("side")).lower() != side:
        raise RuntimeError("exchange order side mismatch")
    returned_reduce = order.get("reduce_only")
    if returned_reduce not in (None, ""):
        parsed_reduce = (returned_reduce if isinstance(returned_reduce, bool)
                         else str(returned_reduce).lower() in {"1", "true", "yes", "on"})
        if parsed_reduce != reduce_only:
            raise RuntimeError("exchange reduce-only identity mismatch")
    return order


def _post_dashboard_order(payload: dict) -> tuple[dict | None, dict]:
    body = json.dumps(payload, separators=(",", ":"))
    data = req.post(f"{API_BASE}/v2/orders", data=body,
                    headers=_sign("POST", "/v2/orders", "", body), timeout=15).json()
    order = data.get("result") if data.get("success") else None
    return (order if isinstance(order, dict) and order.get("id") else None), data


def _cancel_flat_position_protection(state: dict) -> tuple[bool, list]:
    """Cancel only state-owned protection, and only after flat is proven."""
    failures = []
    product_id = int(state.get("product_id") or 0)
    for key in ("tsl_stop_order_id", "tp_stop_order_id"):
        order_id = state.get(key)
        if not order_id:
            continue
        try:
            body = json.dumps({"id": order_id, "product_id": product_id},
                              separators=(",", ":"))
            data = req.delete(f"{API_BASE}/v2/orders", data=body,
                              headers=_sign("DELETE", "/v2/orders", "", body),
                              timeout=10).json()
            if not data.get("success"):
                existing = _lookup_dashboard_order(order_id, None, product_id)
                if not existing or _dash_order_state(existing) in _DASH_ACTIVE_ORDER_STATES:
                    failures.append({"field": key, "order_id": order_id,
                                     "error": data.get("error") or data})
                    continue
            state[key] = None
        except Exception as exc:
            failures.append({"field": key, "order_id": order_id, "error": str(exc)})
    state["protection_cleanup_pending"] = bool(failures)
    state["protection_cleanup_errors"] = failures
    return not failures, failures


def _require_fresh_trend_close_continuity(state: dict, live_size: int) -> None:
    """Fail closed unless the monitor proves this exact live Trend generation."""
    user = _active_user()
    continuity_health = _tp_health(user, "trend")
    try:
        continuity_size = int(
            continuity_health.get("continuity_verified_size")
        )
        health_exchange_size = int(
            continuity_health.get("exchange_position_size")
        )
    except (TypeError, ValueError, OverflowError):
        continuity_size = health_exchange_size = 0
    continuity_proven = bool(
        _tp_continuity_health_fresh(continuity_health)
        and _tp_health_matches(
            continuity_health, state, user, "trend",
            require_protection_identity=False,
        )
        and continuity_health.get("continuity_verified") is True
        and continuity_size == live_size == health_exchange_size
    )
    if not continuity_proven:
        raise RuntimeError(
            "Trend fill-ledger continuity is not freshly proven for "
            "this exact position cycle; close blocked"
        )


def _close_move_state_locked(slot: str, state: dict,
                             reason: str = "manual_squareoff") -> dict:
    """Recover or execute one verified reduce-only close; never guess flat."""
    state_file = _slot_file(slot)
    product_id = int(state.get("product_id") or 0)
    symbol = str(state.get("symbol") or "")
    expected_lots = int(state.get("lots") or 0)
    expected_short = state.get("side") == "short"
    if not product_id or expected_lots <= 0 or not symbol:
        raise RuntimeError("open state has incomplete owned-position identity")

    if slot == "trend":
        position = _strict_realtime_position(product_id)
    else:
        positions = _strict_exchange_positions()
        position = _position_for_product(positions, product_id)
    live_size = int(float((position or {}).get("size") or 0))
    expected_size = -expected_lots if expected_short else expected_lots
    pending_client = str(state.get("pending_close_client_order_id") or "")
    pending_order_id = state.get("pending_close_order_id")
    pending_status = str(state.get("pending_close_submission_state") or "")

    if not pending_client:
        if slot == "trend":
            _require_fresh_trend_close_continuity(state, live_size)
        if live_size == 0:
            raise RuntimeError("exchange is already flat but no owned close identity exists")
        if live_size != expected_size:
            raise RuntimeError(
                f"owned position mismatch: state {expected_size}, exchange {live_size}; close blocked")
        client_id = _move_client_id("close", slot)
        close_side = "buy" if live_size < 0 else "sell"
        state.update({
            "pending_close_client_order_id": client_id,
            "pending_close_order_id": None,
            "pending_close_requested_lots": expected_lots,
            "pending_close_start_size": live_size,
            "pending_close_side": close_side,
            "pending_close_submission_state": "prepared",
            "pending_close_started_at_utc": datetime.now(timezone.utc).isoformat(),
            "pending_close_reason": reason,
            "pending_close_last_error": "",
        })
        _atomic_write_json(state_file, state)
        audit_event(_user_dir(), "dashboard_move_close_intent", {
            "slot": slot, "client_order_id": client_id, "product_id": product_id,
            "size": expected_lots, "side": close_side, "reduce_only": True,
        })
        pending_client, pending_status = client_id, "prepared"
    else:
        client_id = pending_client
        close_side = str(state.get("pending_close_side") or "").lower()
        requested = int(state.get("pending_close_requested_lots") or 0)
        start_size = int(state.get("pending_close_start_size") or 0)
        if (close_side not in {"buy", "sell"} or requested <= 0
                or start_size != expected_size):
            raise RuntimeError("persisted close intent conflicts with owned position")

    requested = int(state.get("pending_close_requested_lots") or expected_lots)
    close_side = str(state.get("pending_close_side") or "")
    order = _lookup_dashboard_order(pending_order_id, client_id, product_id)
    if order:
        order = _validate_dashboard_order(
            order, product_id=product_id, client_order_id=client_id,
            side=close_side, reduce_only=True)
    elif pending_status not in {"", "prepared"}:
        state["pending_close_last_error"] = "close identity is not visible; duplicate blocked"
        _atomic_write_json(state_file, state)
        raise RuntimeError("prior close submission remains unresolved")
    else:
        if slot == "trend":
            # The intent may have remained prepared while the monitor adopted
            # lots or replaced the position cycle.  Re-read and bind the
            # freshest health proof immediately before the only POST path.
            latest_position = _strict_realtime_position(product_id)
            latest_size = int(float((latest_position or {}).get("size") or 0))
            if latest_size != expected_size:
                raise RuntimeError(
                    f"owned position mismatch: state {expected_size}, "
                    f"exchange {latest_size}; close blocked"
                )
            _require_fresh_trend_close_continuity(state, latest_size)
        payload = {"product_id": product_id, "size": requested,
                   "side": close_side, "order_type": "market_order",
                   "reduce_only": True, "client_order_id": client_id}
        try:
            order, data = _post_dashboard_order(payload)
        except Exception as exc:
            order = _lookup_dashboard_order(None, client_id, product_id)
            if not order:
                state.update(pending_close_submission_state="submission_unknown",
                             pending_close_last_error=str(exc))
                _atomic_write_json(state_file, state)
                raise RuntimeError("close response lost; exact recovery pending") from exc
        if not order:
            state["pending_close_last_error"] = str(data.get("error") or data)
            _atomic_write_json(state_file, state)
            raise RuntimeError(str(data.get("error") or data))
        order = _validate_dashboard_order(
            order, product_id=product_id, client_order_id=client_id,
            side=close_side, reduce_only=True)
        state.update(pending_close_order_id=order.get("id"),
                     pending_close_submission_state="acknowledged")
        _atomic_write_json(state_file, state)

    order, proven_fill = _wait_dashboard_terminal(
        order, requested, product_id, client_id,
        timeout_sec=float(os.getenv("CLOSE_ORDER_VERIFY_TIMEOUT_SEC", "8")))
    state["pending_close_order_id"] = order.get("id")
    state["pending_close_order_state"] = _dash_order_state(order)
    if proven_fill is None:
        state["pending_close_submission_state"] = "active_or_ambiguous"
        state["pending_close_last_error"] = "close order is not terminal with a proven fill"
        _atomic_write_json(state_file, state)
        raise RuntimeError("close order remains unverified; state stays OPEN")

    remaining = None
    for attempt in range(4):
        try:
            after = (_strict_realtime_position(product_id) if slot == "trend" else
                     _position_for_product(_strict_exchange_positions(), product_id))
            remaining = int(float((after or {}).get("size") or 0))
            if remaining == 0 or attempt == 3:
                break
        except Exception:
            if attempt == 3:
                break
        time.sleep(0.4)
    if remaining is None:
        state["pending_close_last_error"] = "post-close position could not be verified"
        _atomic_write_json(state_file, state)
        raise RuntimeError("post-close exchange position is unverified; state stays OPEN")
    if remaining and (remaining * expected_size < 0 or abs(remaining) >= abs(expected_size)):
        state["pending_close_last_error"] = f"invalid residual exchange size {remaining}"
        _atomic_write_json(state_file, state)
        raise RuntimeError("close did not verifiably reduce the owned position")

    fill = float(order.get("average_fill_price") or 0)
    # The position delta can include a concurrent manual/protection fill.
    # Attribute it to this dashboard order only when the terminal order proves
    # the exact same quantity; otherwise leave accounting for the complete
    # Trend fill ledger instead of pricing somebody else's lots at this fill.
    filled_lots = abs(expected_size) - abs(remaining)
    fill_attribution_exact = proven_fill == filled_lots and fill > 0
    entry_mark = float(state.get("entry_mark") or 0)
    cval = float(state.get("contract_value") or 0.001)
    pnl_sign = -1 if expected_short else 1
    previous_gross = float(state.get("partial_exit_gross_pnl_usd") or 0)
    previous_exit_fees = float(state.get("partial_exit_fees_usd") or 0)
    current_gross = ((fill - entry_mark) * cval * filled_lots * pnl_sign
                     if fill_attribution_exact else 0.0)
    commission = _order_commission_optional_usd(order)
    segment_complete = fill_attribution_exact and commission is not None
    current_exit_fee = float(commission or 0.0) if segment_complete else 0.0
    cumulative_gross = previous_gross + current_gross
    cumulative_exit_fees = previous_exit_fees + current_exit_fee
    previous_unreconciled = int(state.get("unreconciled_partial_exit_lots") or 0)
    unreconciled_lots = previous_unreconciled + (0 if segment_complete else filled_lots)
    prior_partial_complete = (
        str(state.get("partial_exit_accounting_status") or "complete") == "complete"
        and previous_unreconciled == 0
    )
    partial_accounting_complete = prior_partial_complete and segment_complete
    previous_cycle_exits = int(state.get("cycle_exit_lots_total") or 0)
    cycle_exit_lots = previous_cycle_exits + filled_lots
    original_owned = int(state.get("original_owned_entry_lots")
                         or state.get("owned_entry_lots") or expected_lots)
    if remaining:
        state.update({
            "status": "OPEN", "lots": abs(remaining),
            "protection_lots": abs(remaining),
            "protection_revision": int(state.get("protection_revision") or 0) + 1,
            "continuity_revision": int(state.get("continuity_revision") or 0) + 1,
            "continuity_verified": False,
            "continuity_status": "awaiting_post_close_verification",
            "owned_entry_lots": original_owned,
            "original_owned_entry_lots": original_owned,
            "partial_exit_gross_pnl_usd": round(cumulative_gross, 8),
            "partial_exit_fees_usd": round(cumulative_exit_fees, 8),
            "partial_exit_accounting_status": (
                "complete" if partial_accounting_complete else "fill_ledger_pending"
            ),
            "unreconciled_partial_exit_lots": unreconciled_lots,
            "cycle_exit_lots_total": cycle_exit_lots,
            "position_composition": "fungible_mixed_after_reduction"
            if state.get("externally_added_lots_adopted") else state.get(
                "position_composition", "bot_only"),
            "lot_attribution_status": "fungible_after_reduction",
            "last_close_order_id": order.get("id"),
            "last_close_client_order_id": client_id,
            "last_partial_exit_lots": filled_lots,
            "last_close_order_filled_lots": proven_fill,
            "last_partial_exit_mark": fill,
            "tsl_peak": 0.0,
            "tsl_armed": False,
            "tsl_floor": None,
            "stop_kind": "sl",
            "tsl_rebased_at_utc": datetime.now(timezone.utc).isoformat(),
            "tsl_rebase_reason": "dashboard_partial_position_reduction",
            "pending_close_client_order_id": None,
            "pending_close_order_id": None,
            "pending_close_submission_state": None,
            "pending_close_last_error": f"{abs(remaining)} lots remain open",
        })
        _atomic_write_json(state_file, state)
        raise RuntimeError(f"close was partial; {abs(remaining)} lots remain OPEN")

    gross = cumulative_gross
    entry_fee_raw = state.get("entry_fees_usd")
    if entry_fee_raw in (None, ""):
        entry_fee_raw = state.get("entry_fee_usd")
    fee_sources = (
        str(state.get("entry_fee_source") or "").strip().lower(),
        str(state.get("original_bot_entry_fee_source") or "").strip().lower(),
    )
    explicitly_non_authoritative_entry_fee = any(
        "estimate" in source or "pending" in source
        for source in fee_sources
        if source
    )
    try:
        entry_fees = float(entry_fee_raw)
        entry_fees_known = (
            math.isfinite(entry_fees)
            and entry_fees >= 0
            and not explicitly_non_authoritative_entry_fee
        )
    except (TypeError, ValueError, OverflowError):
        entry_fees, entry_fees_known = 0.0, False
    accounting_complete = (
        partial_accounting_complete and unreconciled_lots == 0
        and entry_fees_known
    )
    fees = entry_fees + cumulative_exit_fees if accounting_complete else None
    pnl = round(gross - fees, 2) if accounting_complete else None
    state.update({
        "status": "CLOSED", "exit_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "exit_time_utc": datetime.now(timezone.utc).strftime("%H:%M:%S"),
        "exit_mark": fill if accounting_complete else None,
        "pnl_usd": pnl, "pnl_includes_fees": accounting_complete,
        "fees_usd": fees, "exit_trigger": reason,
        "fees_complete": accounting_complete,
        "fees_estimated": not accounting_complete,
        "accounting_status": "complete" if accounting_complete else "pending",
        "gross_pnl_usd": round(gross, 8) if accounting_complete else None,
        "exit_fees_usd": (round(cumulative_exit_fees, 8)
                           if accounting_complete else None),
        "partial_exit_gross_pnl_usd": round(cumulative_gross, 8),
        "partial_exit_fees_usd": round(cumulative_exit_fees, 8),
        "partial_exit_accounting_status": (
            "complete" if accounting_complete else "fill_ledger_pending"
        ),
        "unreconciled_partial_exit_lots": unreconciled_lots,
        "exit_reconciliation_status": (
            "complete" if accounting_complete else "pending_fill_ledger"
        ),
        "last_close_order_filled_lots": proven_fill,
        "owned_entry_lots": original_owned,
        "original_owned_entry_lots": original_owned,
        "cycle_exit_lots_total": cycle_exit_lots,
        "closed_lots": cycle_exit_lots,
        "exit_order_id": order.get("id"), "exit_client_order_id": client_id,
        "pending_close_client_order_id": None, "pending_close_order_id": None,
        "pending_close_submission_state": None, "pending_close_last_error": "",
    })
    cleanup_ok, cleanup_errors = _cancel_flat_position_protection(state)
    appended = _append_trade_history(state, f"dashboard-squareoff:{slot}")
    state["history_pending"] = not (appended and accounting_complete)
    _atomic_write_json(state_file, state)
    audit_event(_user_dir(), "dashboard_move_close_verified", {
        "slot": slot, "client_order_id": client_id, "order_id": order.get("id"),
        "filled_lots": filled_lots, "flat_verified": True,
        "protection_cleanup_ok": cleanup_ok,
    })
    return {"pnl": pnl, "fill": fill, "order_id": order.get("id"),
            "history_pending": state["history_pending"],
            "protection_cleanup_ok": cleanup_ok,
            "protection_cleanup_errors": cleanup_errors}


def _dry_run_market_mark(state: dict, *, executable: bool) -> float:
    """Return a fresh public mark for display or simulated execution."""
    entry_mark = float(state.get("entry_mark") or 0)
    symbol = str(state.get("symbol") or "")
    try:
        ticker = req.get(
            f"{API_BASE}/v2/tickers/{symbol}", timeout=5
        ).json().get("result", {})
        quotes = ticker.get("quotes") or {}
        if executable and state.get("side") == "short":
            candidates = (
                quotes.get("best_ask"), ticker.get("best_ask"),
                ticker.get("ask"), ticker.get("mark_price"),
            )
        elif executable:
            candidates = (
                quotes.get("best_bid"), ticker.get("best_bid"),
                ticker.get("bid"), ticker.get("mark_price"),
            )
        elif state.get("side") == "short":
            candidates = (
                ticker.get("mark_price"), quotes.get("best_ask"),
                ticker.get("best_ask"), ticker.get("ask"),
            )
        else:
            candidates = (
                ticker.get("mark_price"), quotes.get("best_bid"),
                ticker.get("best_bid"), ticker.get("bid"),
            )
        mark = next(
            (float(raw) for raw in candidates
             if raw is not None and float(raw) > 0),
            entry_mark,
        )
    except Exception:
        mark = entry_mark
    return mark


def _dry_run_pnl_at_mark(
    state: dict,
    mark: float,
) -> tuple[float, float, float, float]:
    """Return fee-aware simulated P&L at a supplied market price."""
    entry_mark = float(state.get("entry_mark") or 0)
    cv = float(state.get("contract_value") or 0.001)
    lots = int(state.get("lots") or 0)
    sign = -1 if state.get("side") == "short" else 1
    gross = (mark - entry_mark) * cv * lots * sign
    entry_fee = float(
        state.get("entry_fee_usd") or state.get("entry_fees_usd") or 0)
    exit_fee = _option_fee_per_lot(
        mark, cv, float(state.get("strike") or 0)
    ) * lots
    return mark, gross - entry_fee - exit_fee, gross, exit_fee


def _dry_run_mark_and_pnl(state: dict) -> tuple[float, float, float, float]:
    """Public-market-only executable exit estimate for a simulation."""
    mark = _dry_run_market_mark(state, executable=True)
    return _dry_run_pnl_at_mark(state, mark)


def _dry_run_live_mark_and_pnl(
    state: dict,
) -> tuple[float, float, float, float]:
    """Continuously moving mark-price valuation for an open simulation."""
    mark = _dry_run_market_mark(state, executable=False)
    return _dry_run_pnl_at_mark(state, mark)


def _close_dry_simulation_locked(
    slot: str,
    state: dict,
    *,
    trigger: str,
) -> dict:
    """Close one dry state without touching a private exchange endpoint."""
    if not _is_dry_record(state):
        raise RuntimeError("refusing to simulate-close a non-DRY state")
    mark, pnl, gross, exit_fee = _dry_run_mark_and_pnl(state)
    entry_fee = float(
        state.get("entry_fee_usd") or state.get("entry_fees_usd") or 0)
    exited = datetime.now(timezone.utc)
    state = _as_dry_record(state, slot)
    state.update({
        "status": "CLOSED",
        "exit_mark": round(mark, 8),
        "pnl_usd": round(pnl, 2),
        "gross_pnl_usd": round(gross, 8),
        "entry_fee_usd": round(entry_fee, 8),
        "exit_fee_usd": round(exit_fee, 8),
        "fees_usd": round(entry_fee + exit_fee, 8),
        "exit_fee_source": "configured_simulation",
        "pnl_includes_fees": True,
        "accounting_status": "complete",
        "exit_date": exited.strftime("%Y-%m-%d"),
        "exit_time_utc": exited.strftime("%H:%M:%S"),
        "exit_at_utc": exited.isoformat().replace("+00:00", "Z"),
        "exit_trigger": trigger,
        "history_pending": True,
        "history_logged": False,
    })
    state_file = _slot_file(slot, dry_run=True)
    _atomic_write_json(state_file, state)
    appended = _append_trade_history(
        state, f"dry-close:{slot}:{trigger}", dry_run=True)
    state["history_pending"] = not appended
    state["history_logged"] = appended
    if appended:
        state["history_logged_at_utc"] = datetime.now(timezone.utc).isoformat()
    _atomic_write_json(state_file, state)
    return state


@app.route("/api/square-off", methods=["POST"])
def api_square_off():
    slot = _strict_slot_arg(move_only=False)
    if slot is None:
        return jsonify({"ok": False, "error": "slot must be morning, evening, or trend"}), 400
    data = request.get_json(silent=True) or {}
    requested_mode = str(
        data.get("target_mode") or data.get("expected_mode") or ""
    ).strip().lower().replace(" ", "_")
    if requested_mode in {"dry", "simulation", "simulated"}:
        requested_mode = "dry_run"
    if requested_mode and requested_mode not in {"live", "dry_run"}:
        return jsonify({"ok": False, "error": "Invalid square-off target mode"}), 400
    # Exits follow the position's immutable origin, not the mode currently
    # selected for NEW entries. Explicit modern clients choose the namespace;
    # legacy clients fall back to the current account mode.
    dry_run = (
        requested_mode == "dry_run" if requested_mode
        else _trading_mode_payload()["dry_run_mode"]
    )
    user = _active_user()
    with account_entry_lock(_user_dir(), f"dashboard-close:{user}:{slot}") as exposure_lock:
        if not exposure_lock:
            return jsonify({"ok": False, "error": "Another account exposure change is in progress"}), 409
        lock_dir = _mode_data_dir(dry_run)
        with account_file_lock(lock_dir, f"close-{slot}",
                               f"dashboard-close:{os.getpid()}", wait_sec=0) as close_lock:
            if not close_lock:
                return jsonify({"ok": False, "error": f"Another {slot} close is in progress"}), 409
            state_file = _slot_file(slot, dry_run=dry_run)
            state = _load_json(state_file, {})
            if state.get("status") != "OPEN":
                return jsonify({"ok": False, "error": f"No open {slot} position"}), 400
            if _is_dry_record(state) and not dry_run:
                return jsonify({
                    "ok": False,
                    "error": "Legacy simulation is isolated from the LIVE dashboard",
                }), 409
            if _is_dry_record(state):
                closed = _close_dry_simulation_locked(
                    slot, state, trigger="manual_squareoff_simulated")
                return jsonify({"ok": True, "pnl": closed["pnl_usd"],
                                "fill": closed["exit_mark"],
                                "order_id": None, "dry_run": True,
                                "history_pending": closed["history_pending"]})
            if dry_run:
                return jsonify({
                    "ok": False,
                    "error": "Simulation namespace contains a non-DRY state; close blocked",
                }), 409
            key, secret = _active_creds()
            if not key or not secret:
                return jsonify({"ok": False, "error": "API credentials not configured"}), 400
            try:
                result = _close_move_state_locked(slot, state)
                return jsonify({"ok": True, **result})
            except Exception as exc:
                return jsonify({"ok": False, "error": str(exc)}), 409


def _requested_slot() -> str:
    slot = request.args.get("slot", "")
    if not slot:
        slot = (request.get_json(silent=True) or {}).get("slot", "")
    return str(slot or "").strip().lower()


def _strict_slot_arg(move_only: bool = False) -> str | None:
    slot = _requested_slot()
    allowed = MOVE_SLOTS if move_only else SLOTS
    return slot if slot in allowed else None


def _slot_arg(move_only: bool = False) -> str:
    """Legacy UI helper; exposure-changing routes use _strict_slot_arg."""
    return _strict_slot_arg(move_only) or "evening"


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


def _select_atm_mv(products: list, spot: float, slot: str,
                   now: datetime | None = None) -> dict | None:
    """Select the current manual-entry MOVE contract.

    The dashboard assigns a manual position to the active morning/evening
    state slot, but the contract itself must be the nearest *currently listed*
    operational settlement.  Delta may not list the next day's cycle until
    the current cycle rolls, so forcing an evening click to tomorrow's date
    makes otherwise valid manual entries impossible before that rollover.
    """
    if slot not in MOVE_SLOTS:
        raise ValueError("MOVE slot must be morning or evening")
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        min_minutes = float(_cfg("MOVE_MIN_TTE_MINUTES", "90"))
        max_hours = float(_cfg("MOVE_MAX_TTE_HOURS", "30"))
        if (not math.isfinite(min_minutes) or not math.isfinite(max_hours)
                or min_minutes < 0 or max_hours <= 0
                or min_minutes / 60 >= max_hours):
            raise ValueError("invalid MOVE TTE bounds")
        min_tte = min_minutes * 60
        max_tte = max_hours * 3600
    except (TypeError, ValueError):
        min_tte, max_tte = 90 * 60, 30 * 3600
    usable = []
    for product in products or []:
        if not isinstance(product, dict):
            continue
        if not str(product.get("symbol") or "").startswith("MV-BTC-"):
            continue
        product_state = str(product.get("state") or "").lower()
        if product_state and product_state != "live":
            continue
        underlying = (product.get("underlying_asset") or {}).get("symbol")
        if underlying not in (None, "", "BTC"):
            continue
        if str(product.get("trading_status") or "").lower() != "operational":
            continue
        try:
            settlement = datetime.fromisoformat(
                str(product.get("settlement_time") or "").replace("Z", "+00:00")
            )
            if settlement.tzinfo is None:
                settlement = settlement.replace(tzinfo=timezone.utc)
            settlement = settlement.astimezone(timezone.utc)
            strike = float(product.get("strike_price") or 0)
        except (TypeError, ValueError):
            continue
        tte = (settlement - now).total_seconds()
        if (not math.isfinite(strike) or not math.isfinite(tte)
                or strike <= 0 or tte < min_tte or tte > max_tte):
            continue
        usable.append((settlement, product))
    if not usable or not math.isfinite(spot) or spot <= 0:
        return None
    nearest_settlement = min(settlement for settlement, _ in usable)
    cycle = [product for settlement, product in usable
             if settlement == nearest_settlement]
    return min(cycle, key=lambda p: abs(float(p.get("strike_price") or 0) - spot))


def _fetch_live_mv_products() -> list:
    """Fetch every page of live MOVE products or fail closed."""
    products = []
    after = None
    seen_cursors = set()
    for _ in range(20):
        params = {"contract_types": "move_options", "states": "live",
                  "page_size": 100}
        if after:
            params["after"] = after
        payload = req.get(f"{API_BASE}/v2/products", params=params, timeout=8).json()
        page = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(page, list):
            raise RuntimeError("invalid MOVE products response")
        products.extend(page)
        meta = payload.get("meta") or {}
        next_after = meta.get("after") if isinstance(meta, dict) else None
        if not next_after:
            return products
        if not isinstance(next_after, str) or next_after in seen_cursors:
            raise RuntimeError("MOVE products pagination did not advance")
        seen_cursors.add(next_after)
        after = next_after
    raise RuntimeError("MOVE products pagination did not terminate")


def _current_atm_mv(slot: str, now: datetime | None = None) -> dict | None:
    """Fetch and validate the nearest eligible contract for a manual entry."""
    try:
        spot = float(req.get(f"{API_BASE}/v2/tickers/BTCUSD", timeout=6)
                     .json().get("result", {}).get("mark_price") or 0)
        prods = _fetch_live_mv_products()
        return _select_atm_mv(prods, spot, slot, now)
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


def _option_fee_per_lot(mark: float, cv: float, notional_reference: float = 0.0) -> float:
    """Conservative one-way taker-fee estimate used before exchange submit."""
    try:
        rate = max(float(_cfg("OPTION_FEE_RATE", "0.00010")), 0)
        cap_pct = max(float(_cfg("OPTION_FEE_CAP_PCT", "0.035")), 0)
    except (TypeError, ValueError):
        rate, cap_pct = 0.00010, 0.035
    return ((min(rate * notional_reference, cap_pct * mark)
             if notional_reference > 0 else cap_pct * mark)
            * cv)


def _affordable_option_lots(mark: float, cv: float, strike: float = 0.0) -> int | None:
    """Wallet-funded option lots, or None when affordability is unverified."""
    if mark <= 0 or cv <= 0:
        return None
    try:
        hdrs = _sign("GET", "/v2/wallet/balances")
        data = req.get(f"{API_BASE}/v2/wallet/balances", headers=hdrs, timeout=8).json()
        if not data.get("success"):
            return None
        balance = next((float(w.get("available_balance") or 0)
                        for w in data.get("result", [])
                        if w.get("asset_symbol") == "USD"), 0.0)
        per_lot = mark * cv + _option_fee_per_lot(mark, cv, strike)
        return max(int((balance * 0.98) / per_lot), 0) if per_lot > 0 else None
    except Exception:
        return None


def _move_execution_quote(symbol: str, side: str, reference_price: float = 0.0) -> dict:
    """Fresh two-sided MOVE quote and a price-bounded marketable IOC limit."""
    cfg = _user_cfg()
    if not _cfg_bool("SAFE_EXECUTION_ENABLED", True):
        raise RuntimeError("Safe IOC execution is disabled; live MOVE entries are blocked")
    data = req.get(f"{API_BASE}/v2/tickers/{symbol}", timeout=7).json()
    ticker = data.get("result") or {}
    if not ticker:
        raise RuntimeError("fresh MOVE ticker is unavailable")
    quote = _trend_quote_snapshot(ticker)
    bid, ask = float(quote.get("bid") or 0), float(quote.get("ask") or 0)
    if bid <= 0 or ask <= 0 or ask < bid:
        raise RuntimeError("fresh two-sided MOVE quote is unavailable")
    if quote.get("quote_age_secs") is None:
        raise RuntimeError("MOVE quote timestamp is unavailable")
    max_age = max(_as_float(cfg.get("MAX_QUOTE_AGE_SEC") or 20, 20), 0)
    if quote["quote_age_secs"] > max_age:
        raise RuntimeError(
            f"MOVE quote is stale ({quote['quote_age_secs']:.1f}s > {max_age:.1f}s)")
    max_spread = max(_as_float(cfg.get("MAX_SPREAD_PCT") or 3, 3), 0)
    if quote.get("spread_pct") is None or quote["spread_pct"] > max_spread:
        raise RuntimeError(
            f"MOVE spread exceeds configured cap ({quote.get('spread_pct')} > {max_spread})")
    if quote.get("trading_status") not in ("", "operational"):
        raise RuntimeError("MOVE contract is not operational")
    price = ask if side == "buy" else bid
    depth = quote.get("ask_size", 0) if side == "buy" else quote.get("bid_size", 0)
    if price <= 0 or depth <= 0:
        raise RuntimeError(f"verified {side} price/depth is unavailable")
    reference = reference_price or price
    slippage = max(_as_float(cfg.get("MAX_SLIPPAGE_PCT") or 1, 1), 0)
    tick = max(float(quote.get("tick_size") or 0.1), 0.00000001)
    if side == "buy":
        boundary = reference * (1 + slippage / 100)
        limit_price = math.floor((boundary + 1e-12) / tick) * tick
        if ask > limit_price:
            raise RuntimeError(f"fresh ask {ask} exceeds bounded entry price {limit_price}")
        upper = _as_float((quote.get("price_band") or {}).get("upper_limit"), 0)
        if upper and limit_price > upper:
            raise RuntimeError("bounded buy limit exceeds exchange price band")
    else:
        boundary = reference * (1 - slippage / 100)
        limit_price = math.ceil((boundary - 1e-12) / tick) * tick
        if bid < limit_price:
            raise RuntimeError(f"fresh bid {bid} is below bounded entry price {limit_price}")
        lower = _as_float((quote.get("price_band") or {}).get("lower_limit"), 0)
        if lower and limit_price < lower:
            raise RuntimeError("bounded sell limit is below exchange price band")
    return {**quote, "entry_price": price, "entry_depth": float(depth),
            "reference_price": reference, "limit_price": round(limit_price, 10)}


def _move_lot_plan(
    slot: str,
    side: str,
    contract: dict,
    quote: dict,
    *,
    dry_run: bool = False,
) -> dict:
    """Fail-closed minimum of configured, funding, order and risk caps.

    A paper trade has no real-wallet funding requirement.  In DRY RUN its
    configured size is therefore the virtual affordability ceiling.  MOVE
    order-book quantity is diagnostic only and never reduces the planned lots;
    LIVE entries continue to use bounded IOC execution and accept only proven
    fills.  LIVE entries also continue to require a verified exchange wallet.
    """
    cfg = _user_cfg()
    lot_key, lot_default = (("MORNING_LOTS", 2000) if slot == "morning"
                            else ("STRADDLE_LOTS", 800))
    try:
        configured = int(float(cfg.get(lot_key) or lot_default))
        max_order = int(float(cfg.get("MAX_ORDER_LOTS") or 1000))
        chunk_cap = int(float(cfg.get("ORDER_CHUNK_LOTS") or 1000))
    except (TypeError, ValueError):
        configured = max_order = chunk_cap = 0
    cv = float(contract.get("contract_value") or 0)
    strike = float(contract.get("strike_price") or 0)
    price = float(quote.get("entry_price") or 0)
    affordable = (max(configured, 0) if dry_run
                  else _affordable_option_lots(price, cv, strike))
    affordability_source = "paper_configured_cap" if dry_run else "exchange_wallet"
    observed_entry_depth = max(int(float(quote.get("entry_depth") or 0)), 0)
    risk_key = "RISK_PER_TRADE_USD_MORNING" if slot == "morning" \
        else "RISK_PER_TRADE_USD_EVENING"
    risk_budget = max(_as_float(cfg.get(risk_key) or 200, 200), 0)
    _, _, sl_target, _ = _tp_env(slot)
    configured_sl_target = sl_target
    paper_short_risk_assumption = 0.0
    is_short = side == "sell"
    short_cap = max(_as_float(cfg.get("SHORT_MAX_RISK_USD") or 0, 0), 0)
    if is_short:
        if not _cfg_bool("ALLOW_SHORT_MOVE", False):
            return {"lots": 0, "reason": "Short MOVE entries are disabled"}
        if short_cap <= 0:
            return {"lots": 0,
                    "reason": ("Short MOVE simulation requires a positive short-risk cap"
                               if dry_run else
                               "Short MOVE requires a positive SL and short-risk cap")}
        if sl_target <= 0:
            if not dry_run:
                return {"lots": 0,
                        "reason": "Short MOVE requires a positive SL and short-risk cap"}
            # A paper short has no exchange exposure and no exchange stop
            # order. Use its mandatory short-risk cap as the simulated loss
            # assumption for sizing and the paper portfolio-risk ledger.
            sl_target = short_cap
            paper_short_risk_assumption = short_cap
        risk_budget = min(risk_budget, short_cap)
    premium_per_lot = price * cv
    fee_per_lot = 2 * _option_fee_per_lot(price, cv, strike)
    slippage_per_lot = premium_per_lot * max(
        _as_float(cfg.get("MAX_SLIPPAGE_PCT") or 1, 1), 0) / 100
    premium_cap = max_order
    if not is_short:
        account_cap = max(_as_float(
            cfg.get("MAX_ACCOUNT_PREMIUM_AT_RISK_USD") or 500, 500), 0)
        remaining = max(
            account_cap - _open_long_premium_usd(dry_run=dry_run), 0
        ) if account_cap else 0
        premium_cap = int(remaining / premium_per_lot) if premium_per_lot > 0 else 0
    lots = risk_based_lots(
        configured=max(configured, 0), affordable=max(int(affordable or 0), 0),
        # risk_based_lots is shared with Trend, where book participation still
        # is a strategy cap. For MOVE, feed only its independent premium cap;
        # observed order-book quantity must not reduce the planned position.
        liquidity_cap=max(premium_cap, 0),
        max_order_lots=max(min(max_order, chunk_cap), 0),
        risk_budget_usd=risk_budget, stop_loss_usd=sl_target,
        premium_per_lot=premium_per_lot,
        round_trip_fee_per_lot=fee_per_lot,
        slippage_per_lot=slippage_per_lot, short=is_short,
    )
    proposed = (max(sl_target, lots * (premium_per_lot + fee_per_lot + slippage_per_lot))
                if lots and not is_short else sl_target if lots else 0)
    value_filter_enabled = str(cfg.get("MOVE_VALUE_FILTER_ENABLED") or "true").lower() \
        in {"1", "true", "yes", "on"}
    return {
        "lots": lots, "configured": configured, "affordable": affordable,
        "affordability_source": affordability_source,
        "max_order_lots": max_order, "chunk_cap": chunk_cap,
        "observed_entry_depth_lots": observed_entry_depth,
        "book_depth_applied_to_sizing": False,
        "risk_budget_usd": risk_budget,
        "sl_target_pnl": configured_sl_target,
        "risk_stop_loss_usd": sl_target,
        "paper_short_risk_assumption_usd": paper_short_risk_assumption,
        "proposed_risk_usd": proposed,
        "premium_per_lot": premium_per_lot,
        "round_trip_fee_per_lot": fee_per_lot,
        "slippage_per_lot": slippage_per_lot,
        # The scheduled strategy owns the realized-volatility forecast/value
        # model. A dashboard click is explicitly discretionary and must never
        # be represented as having passed that unattended strategy signal.
        "move_value_filter_enabled": value_filter_enabled,
        "move_value_gate_evaluated": False,
        "entry_classification": "discretionary_manual",
        "value_gate_note": ("Scheduled MOVE value eligibility was not claimed; "
                            "this is a discretionary manual entry"),
        "reason": "sizing checks passed" if lots else "No lots pass every safety cap",
    }


def _validate_move_entry_account(positions: list[dict], selected_product_id: int) -> float:
    """Prove every exchange position is either bot-owned or explicitly allowed."""
    states = {slot: _load_json(_slot_file(slot), {}) for slot in SLOTS}
    try:
        max_move = max(int(float(_cfg("MAX_CONCURRENT_MOVE_POSITIONS", "1"))), 1)
    except (TypeError, ValueError):
        raise RuntimeError("MAX_CONCURRENT_MOVE_POSITIONS is invalid")
    open_move = sum(
        1 for slot in MOVE_SLOTS
        if states[slot].get("status") == "OPEN" and not states[slot].get("dry_run")
    )
    if open_move >= max_move:
        raise RuntimeError(
            f"concurrent MOVE position cap reached ({open_move}/{max_move})")
    for slot, state in states.items():
        if state.get("status") in {"ENTRY_PENDING", "CLOSE_PENDING"}:
            raise RuntimeError(f"{slot} has an unresolved order intent")
        if state.get("protection_cleanup_pending"):
            raise RuntimeError(f"{slot} has unresolved exchange protection cleanup")
        if state.get("status") != "OPEN" or state.get("dry_run"):
            continue
        product_id = int(state.get("product_id") or 0)
        expected = -int(state.get("lots") or 0) if state.get("side") == "short" \
            else int(state.get("lots") or 0)
        live = _position_for_product(positions, product_id)
        actual = int(float((live or {}).get("size") or 0))
        if not product_id or not expected or actual != expected:
            raise RuntimeError(f"{slot} state does not exactly match exchange exposure")
    selected = _position_for_product(positions, selected_product_id)
    if selected is not None:
        raise RuntimeError("selected MOVE contract already has exchange exposure")
    owned_products = {
        int(state.get("product_id") or 0): (-int(state.get("lots") or 0)
                                             if state.get("side") == "short"
                                             else int(state.get("lots") or 0))
        for state in states.values() if state.get("status") == "OPEN" and not state.get("dry_run")
    }
    external = [p for p in positions
                if owned_products.get(int(p.get("product_id") or 0))
                != int(float(p.get("size") or 0))]
    if external and not _cfg_bool("ALLOW_EXTERNAL_POSITIONS_WITH_BOT", False):
        raise RuntimeError(f"{len(external)} external/manual position(s) are open")
    return sum(_as_float(p.get("unrealized_pnl"), 0) for p in positions)


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
    try:
        max_lots = max(int(float(_cfg("MAX_ORDER_LOTS", "1000"))), 1)
    except (TypeError, ValueError):
        max_lots = 1000
    try:
        afford = _affordable_option_lots(mark, cv, strike)
        if afford is None:
            raise RuntimeError("wallet balance unavailable")
        affordable = min(configured, afford, max_lots)
        return max(affordable, 0)
    except Exception:
        return 0


@app.route("/api/manual-entry/preview")
def api_manual_entry_preview():
    """Manual MOVE direction selection was retired with scheduled AUTO."""
    return jsonify({
        "ok": False,
        "error": (
            "Manual MOVE BUY/SELL is disabled. Morning and Evening MOVE "
            "directions are selected only by the scheduled forecast engine."
        ),
        "code": "MANUAL_MOVE_DISABLED",
    }), 410

    # Retained temporarily as rollback-compatible implementation context.
    # This block is unreachable and may be removed after the AUTO rollout.
    slot = _strict_slot_arg(move_only=True)
    if slot is None:
        return jsonify({"ok": False, "error": "slot must be morning or evening"}), 400
    side = str(request.args.get("side") or "buy").lower()
    if side not in {"buy", "sell"}:
        return jsonify({"ok": False, "error": "side must be buy or sell"}), 400
    contract = _current_atm_mv(slot)
    if not contract:
        return jsonify({
            "ok": False,
            "error": (f"No eligible operational MV contract is currently "
                      f"listed for manual {slot} entry"),
        }), 502
    symbol = contract["symbol"]
    cv     = float(contract.get("contract_value") or 0.001)
    mode = _trading_mode_payload()
    try:
        quote = _move_execution_quote(symbol, side)
        plan = _move_lot_plan(
            slot, side, contract, quote, dry_run=mode["dry_run_mode"])
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409
    mark = float(quote.get("entry_price") or 0)
    lots = int(plan.get("lots") or 0)
    if lots <= 0:
        return jsonify({"ok": False, "error": plan.get("reason") or
                        "No lots pass every safety cap", "sizing": plan}), 409
    return jsonify({
        "ok":         True,
        "slot":       slot,
        "side":       side,
        "symbol":     symbol,
        "product_id": int(contract.get("id") or 0),
        "strike":     float(contract.get("strike_price") or 0),
        "mark":       round(mark, 4),
        "lots":       lots,
        "est_value":  round(mark * cv * lots, 2),
        "settlement": contract.get("settlement_time", ""),
        "dry_run":    mode["dry_run_mode"],
        "execution_mode": mode["execution_mode"],
        "mode_revision": mode["mode_revision"],
        "sizing":     plan,
        "quote":      quote,
        "entry_classification": "discretionary_manual",
        "move_value_gate_evaluated": False,
        "value_gate_note": plan.get("value_gate_note"),
    })


def _open_state_from_pending(pending: dict, order: dict, filled: int) -> dict:
    fill = float(order.get("average_fill_price") or 0)
    if fill <= 0 or filled <= 0:
        raise RuntimeError("terminal entry fill lacks price or quantity")
    fees = _order_commission_usd(order)
    return {
        **pending,
        "status": "OPEN", "lots": filled, "owned_entry_lots": filled,
        "original_owned_entry_lots": filled,
        "entry_mark": round(fill, 4),
        "total_cost_usd": round(fill * float(pending.get("contract_value") or 0) * filled, 2),
        "entry_fees_usd": fees, "fees_usd": fees,
        "order_id": order.get("id"), "order_ids": [order.get("id")],
        "client_order_id": pending.get("pending_entry_client_order_id"),
        "client_order_ids": [pending.get("pending_entry_client_order_id")],
        "pending_entry_order_id": None,
        "pending_entry_submission_state": None,
        "entry_execution": {
            "kind": "bounded_ioc_limit", "requested": pending.get("pending_entry_requested_lots"),
            "filled": filled, "unfilled": max(
                int(pending.get("pending_entry_requested_lots") or 0) - filled, 0),
            "client_order_id": pending.get("pending_entry_client_order_id"),
            "order_id": order.get("id"), "order_state": _dash_order_state(order),
            "average_fill_price": fill, "paid_commission_usd": fees,
        },
    }


def _flatten_unpersisted_move_fill(state: dict) -> dict:
    """Last-resort flatten when a proven fill cannot be durably made OPEN."""
    product_id = int(state.get("product_id") or 0)
    expected = -int(state.get("lots") or 0) if state.get("side") == "short" \
        else int(state.get("lots") or 0)
    position = _position_for_product(_strict_exchange_positions(), product_id)
    actual = int(float((position or {}).get("size") or 0))
    if actual != expected:
        raise RuntimeError(
            f"cannot emergency-flatten unpersisted fill: expected {expected}, exchange {actual}")
    client_id = _move_client_id("close", str(state.get("slot") or "evening"))
    payload = {"product_id": product_id, "size": abs(actual),
               "side": "buy" if actual < 0 else "sell",
               "order_type": "market_order", "reduce_only": True,
               "client_order_id": client_id}
    try:
        audit_event(_user_dir(), "dashboard_unpersisted_fill_flatten_intent", {
            "client_order_id": client_id, "product_id": product_id,
            "size": abs(actual), "side": payload["side"], "reduce_only": True,
        })
    except Exception:
        # Exposure reduction is safer than abandoning the known fill merely
        # because the secondary audit stream shares the storage outage.
        pass
    order, data = _post_dashboard_order(payload)
    if not order:
        raise RuntimeError(str(data.get("error") or data))
    order = _validate_dashboard_order(
        order, product_id=product_id, client_order_id=client_id,
        side=payload["side"], reduce_only=True)
    order, fill = _wait_dashboard_terminal(order, abs(actual), product_id, client_id)
    if fill is None:
        raise RuntimeError("emergency flatten order is not terminal/proven")
    after = _position_for_product(_strict_exchange_positions(), product_id)
    remaining = int(float((after or {}).get("size") or 0))
    if remaining != 0:
        raise RuntimeError(f"emergency flatten left {remaining} lots open")
    return {"flattened": True, "order_id": order.get("id"),
            "client_order_id": client_id}


def _persist_proven_move_open(slot: str, pending: dict,
                              order: dict, filled: int) -> dict:
    """Persist a proven fill, immediately recover it, or flatten it."""
    opened = _open_state_from_pending(pending, order, filled)
    try:
        _atomic_write_json(_slot_file(slot), opened)
        return opened
    except Exception as first_error:
        # The pre-submit ENTRY_PENDING record still holds the exact client ID.
        # Re-read and recover through that identity before considering a close.
        try:
            durable = _load_json(_slot_file(slot), {})
            recovered, _ = _recover_pending_move_entry(slot, durable)
            if recovered.get("status") == "OPEN":
                recovered["entry_state_write_recovered"] = True
                _atomic_write_json(_slot_file(slot), recovered)
                return recovered
        except Exception:
            pass
        try:
            # A one-off replace/fsync failure may already have cleared.
            opened["entry_state_write_recovered"] = True
            _atomic_write_json(_slot_file(slot), opened)
            return opened
        except Exception:
            flattened = _flatten_unpersisted_move_fill(opened)
            try:
                _atomic_write_json(_slot_file(slot), {
                    "slot": slot, "status": "IDLE",
                    "last_entry_client_order_id": opened.get("client_order_id"),
                    "last_entry_order_id": opened.get("order_id"),
                    "entry_state_write_error": str(first_error),
                    "emergency_flatten": flattened,
                })
            except Exception:
                pass
            raise RuntimeError(
                "proven entry fill could not be persisted and was immediately flattened")


def _recover_pending_move_entry(slot: str, state: dict) -> tuple[dict, bool]:
    """Recover a journalled response-loss entry without product-based adoption."""
    if state.get("status") != "ENTRY_PENDING":
        return state, False
    product_id = int(state.get("product_id") or 0)
    requested = int(state.get("pending_entry_requested_lots") or 0)
    client_id = str(state.get("pending_entry_client_order_id") or "")
    side = str(state.get("pending_entry_side") or "")
    if not product_id or requested <= 0 or not client_id or side not in {"buy", "sell"}:
        raise RuntimeError("pending MOVE entry has incomplete durable identity")
    order = _lookup_dashboard_order(
        state.get("pending_entry_order_id"), client_id, product_id)
    if not order:
        raise RuntimeError(
            f"pending entry {client_id} is not yet visible; duplicate submission blocked")
    order = _validate_dashboard_order(
        order, product_id=product_id, client_order_id=client_id,
        side=side, reduce_only=False)
    order, filled = _wait_dashboard_terminal(order, requested, product_id, client_id)
    if filled is None:
        state.update(pending_entry_order_id=order.get("id"),
                     pending_entry_submission_state="active_or_ambiguous")
        _atomic_write_json(_slot_file(slot), state)
        raise RuntimeError("pending entry has no terminal proven fill")
    if filled == 0:
        idle = {"slot": slot, "status": "IDLE",
                "last_entry_client_order_id": client_id,
                "last_entry_order_id": order.get("id"),
                "last_entry_order_state": _dash_order_state(order),
                "last_entry_attempt_utc": datetime.now(timezone.utc).isoformat()}
        _atomic_write_json(_slot_file(slot), idle)
        return idle, True
    opened = _open_state_from_pending(state, order, filled)
    try:
        _atomic_write_json(_slot_file(slot), opened)
    except Exception:
        try:
            opened["entry_state_write_recovered"] = True
            _atomic_write_json(_slot_file(slot), opened)
        except Exception:
            _flatten_unpersisted_move_fill(opened)
            raise RuntimeError(
                "recovered entry fill could not be persisted and was flattened")
    return opened, True


def _force_flatten_move(slot: str, state: dict, reason: str) -> dict:
    with account_file_lock(_user_dir(), f"close-{slot}",
                           f"dashboard-forced-flatten:{os.getpid()}", wait_sec=0) as acquired:
        if not acquired:
            raise RuntimeError("emergency close lock is unavailable")
        return _close_move_state_locked(slot, state, reason=reason)


def _post_entry_exchange_size(product_id: int, expected_size: int) -> int | None:
    actual_size = None
    for attempt in range(4):
        try:
            live = _position_for_product(_strict_exchange_positions(), product_id)
            actual_size = int(float((live or {}).get("size") or 0))
            if actual_size == expected_size or attempt == 3:
                break
        except Exception:
            if attempt == 3:
                break
        time.sleep(0.35)
    return actual_size


def _protect_or_flatten_move(slot: str, state: dict, started_at: datetime) -> tuple[bool, dict]:
    """Require a matching protection heartbeat; otherwise reduce-only flatten."""
    user = _active_user()
    health = {}
    monitor_error = None
    try:
        health = _tp_health(user, slot)
        running = _tp_running(user, slot)
        if running and not _tp_health_matches(health, state, user, slot):
            _restart_tp_monitor(user, slot)
        elif not running:
            if _spawn_tp(user, slot) is None:
                raise RuntimeError("TP monitor could not be started")
        verified, health = _wait_for_protection(user, slot, started_at, timeout_secs=10)
    except Exception as exc:
        verified = False
        monitor_error = str(exc)
        health = {**health, "last_error": monitor_error}
    latest = _load_json(_slot_file(slot), state)
    latest.update(protection_verified_at_entry=verified,
                  protection_health_at_entry=health,
                  protection_start_error=monitor_error)
    try:
        _atomic_write_json(_slot_file(slot), latest)
    except Exception:
        # The OPEN state was already persisted before monitor startup. Do not
        # let a secondary health annotation failure skip the required flatten.
        pass
    if verified:
        return True, {"protection_health": health}

    _send_telegram(
        f"🚨 <b>MOVE PROTECTION FAILURE ({user.upper()} / {slot.upper()})</b>\n"
        f"<code>{latest.get('symbol', '')}</code> was filled but protection was not verified; "
        "an immediate reduce-only flatten is being attempted.")
    flattened = _force_flatten_move(slot, latest, "protection_failure_flatten")
    return False, {"flattened": True, **flattened, "protection_health": health}


def _submit_manual_move_entry(slot: str, side: str, contract: dict,
                              quote: dict, plan: dict, dry_run: bool) -> tuple[dict, dict]:
    state_file = _slot_file(slot, dry_run=dry_run)
    requested = int(plan.get("lots") or 0)
    product_id = int(contract.get("id") or 0)
    now = datetime.now(timezone.utc)
    protection = _tp_policy(slot)
    if dry_run:
        fill = float(quote.get("entry_price") or 0)
        order = {"id": 0, "state": "filled", "filled_size": requested,
                 "unfilled_size": 0, "average_fill_price": fill}
        pending_client = None
    else:
        pending_client = _move_client_id("entry", slot)
        order = None
    pending = {
        "slot": slot, "status": "ENTRY_PENDING" if not dry_run else "OPEN",
        "side": "long" if side == "buy" else "short",
        "entry_date": now.strftime("%Y-%m-%d"),
        "entry_time_utc": now.strftime("%H:%M:%S"),
        "symbol": contract["symbol"], "product_id": product_id,
        "strike": float(contract.get("strike_price") or 0),
        "settlement": contract.get("settlement_time", ""),
        "contract_value": float(contract.get("contract_value") or 0.001),
        "lots": requested, "btc_at_entry": 0,
        "entry_trigger": f"manual_{side}_bounded_ioc",
        "ownership": "manual_move_bot", "dry_run": dry_run,
        "execution_mode": "dry_run" if dry_run else "live",
        "entry_classification": "discretionary_manual",
        "move_value_gate_evaluated": False,
        "move_value_filter_enabled": plan.get("move_value_filter_enabled", True),
        "move_value_gate_note": plan.get("value_gate_note"),
        "protection_config": protection,
        "sizing_snapshot": plan, "quote_snapshot": quote,
        "risk_at_entry_usd": plan.get("proposed_risk_usd"),
        "pending_entry_client_order_id": pending_client,
        "pending_entry_order_id": None,
        "pending_entry_requested_lots": requested,
        "pending_entry_side": side,
        "pending_entry_submission_state": "prepared" if not dry_run else None,
        "pending_entry_started_at_utc": now.isoformat(),
    }
    if dry_run:
        pending["simulation_id"] = _simulation_identity(pending, slot)
        opened = _open_state_from_pending(pending, order, requested)
        opened["dry_run"] = True
        opened["execution_mode"] = "dry_run"
        entry_fee = _option_fee_per_lot(
            float(opened.get("entry_mark") or 0),
            float(opened.get("contract_value") or 0.001),
            float(opened.get("strike") or 0),
        ) * requested
        opened.update({
            "entry_fees_usd": round(entry_fee, 8),
            "fees_usd": round(entry_fee, 8),
            "entry_fee_source": "configured_simulation",
            "pnl_includes_fees": False,
        })
        _atomic_write_json(state_file, opened)
        return opened, order

    # The exact exchange identity is durable before irreversible network I/O.
    _atomic_write_json(state_file, pending)
    audit_event(_user_dir(), "dashboard_move_entry_intent", {
        "slot": slot, "client_order_id": pending_client,
        "product_id": product_id, "symbol": contract["symbol"],
        "size": requested, "side": side, "order_type": "limit_order",
        "limit_price": quote["limit_price"], "time_in_force": "ioc",
        "entry_classification": "discretionary_manual",
        "move_value_gate_evaluated": False,
    })
    payload = {"product_id": product_id, "size": requested, "side": side,
               "order_type": "limit_order", "limit_price": str(quote["limit_price"]),
               "time_in_force": "ioc", "client_order_id": pending_client}
    try:
        order, data = _post_dashboard_order(payload)
    except Exception as exc:
        order = _lookup_dashboard_order(None, pending_client, product_id)
        if not order:
            pending.update(pending_entry_submission_state="submission_unknown",
                           pending_entry_last_error=str(exc))
            _atomic_write_json(state_file, pending)
            raise RuntimeError("entry response lost; exact recovery pending") from exc
    if not order:
        error = str(data.get("error") or data)
        if data.get("success") is False:
            _atomic_write_json(state_file, {
                "slot": slot, "status": "IDLE",
                "last_entry_client_order_id": pending_client,
                "last_entry_rejection": error,
                "last_entry_attempt_utc": datetime.now(timezone.utc).isoformat(),
            })
        else:
            pending["pending_entry_last_error"] = error
            pending["pending_entry_submission_state"] = "acknowledgement_ambiguous"
            _atomic_write_json(state_file, pending)
        raise RuntimeError(error)
    order = _validate_dashboard_order(
        order, product_id=product_id, client_order_id=pending_client,
        side=side, reduce_only=False)
    pending.update(pending_entry_order_id=order.get("id"),
                   pending_entry_submission_state="acknowledged")
    try:
        _atomic_write_json(state_file, pending)
    except Exception:
        # The already-durable prepared record contains the same client ID;
        # continue to terminal verification so a fill is protected/flattened
        # in this request instead of being abandoned.
        pass
    order, filled = _wait_dashboard_terminal(order, requested, product_id, pending_client)
    if filled is None:
        pending.update(pending_entry_order_id=order.get("id"),
                       pending_entry_submission_state="active_or_ambiguous",
                       pending_entry_last_error="entry fill is not terminal/proven")
        _atomic_write_json(state_file, pending)
        raise RuntimeError("entry order is unverified; duplicate submission blocked")
    if filled <= 0:
        idle = {"slot": slot, "status": "IDLE",
                "last_entry_client_order_id": pending_client,
                "last_entry_order_id": order.get("id"),
                "last_entry_order_state": _dash_order_state(order)}
        _atomic_write_json(state_file, idle)
        raise RuntimeError("bounded IOC order filled zero lots")
    opened = _persist_proven_move_open(slot, pending, order, filled)
    return opened, order


@app.route("/api/manual-entry", methods=["POST"])
def api_manual_entry():
    """Reject discretionary MOVE entries; only scheduled AUTO may open them."""
    return jsonify({
        "ok": False,
        "error": (
            "Manual MOVE BUY/SELL is disabled. Morning and Evening MOVE "
            "entries can be opened only by the scheduled forecast engine."
        ),
        "code": "MANUAL_MOVE_DISABLED",
    }), 410

    # Retained temporarily as rollback-compatible implementation context.
    # This block is unreachable and may be removed after the AUTO rollout.
    slot = _strict_slot_arg(move_only=True)
    if slot is None:
        return jsonify({"ok": False, "error": "slot must be morning or evening"}), 400
    data = request.get_json(silent=True) or {}
    side = (data.get("side") or request.args.get("side") or "").lower()
    if side not in ("buy", "sell"):
        return jsonify({"ok": False, "error": "side must be buy or sell"}), 400
    user = _active_user()
    with account_entry_lock(_user_dir(), f"dashboard-entry:{user}:{slot}") as acquired:
        if not acquired:
            return jsonify({"ok": False, "error": "Another account exposure change is in progress"}), 409
        expectation_error = _mode_expectation_error(data)
        if expectation_error:
            return jsonify({"ok": False, "error": expectation_error}), 409
        mode = _trading_mode_payload()
        dry_run = mode["dry_run_mode"]
        key, secret = _active_creds()
        if not dry_run and (not key or not secret):
            return jsonify({"ok": False, "error": "API credentials not configured"}), 400
        try:
            preview_product_id = int(data.get("product_id") or 0)
            preview_lots = int(data.get("lots") or 0)
            preview_price = float(data.get("mark") or 0)
        except (TypeError, ValueError, OverflowError):
            preview_product_id = preview_lots = 0
            preview_price = 0.0
        preview_symbol = str(data.get("symbol") or "")
        if (preview_product_id <= 0 or preview_lots <= 0 or preview_price <= 0
                or not math.isfinite(preview_price) or not preview_symbol):
            return jsonify({"ok": False,
                            "error": "A fresh MOVE preview is required before entry"}), 409
        state_file = _slot_file(slot, dry_run=dry_run)
        state = _load_json(state_file, {})
        try:
            if not dry_run and state.get("status") == "ENTRY_PENDING":
                state, recovered = _recover_pending_move_entry(slot, state)
                if state.get("status") == "OPEN":
                    product_id = int(state.get("product_id") or 0)
                    expected_size = (-int(state.get("lots") or 0)
                                     if state.get("side") == "short"
                                     else int(state.get("lots") or 0))
                    actual_size = _post_entry_exchange_size(product_id, expected_size)
                    state["position_verified_at_entry"] = actual_size == expected_size
                    state["verified_exchange_size_at_entry"] = actual_size
                    if actual_size != expected_size:
                        if actual_size and actual_size * expected_size > 0:
                            state["lots"] = abs(actual_size)
                            state["position_mismatch_at_recovery"] = {
                                "terminal_fill": expected_size,
                                "exchange_size": actual_size,
                            }
                            try:
                                _atomic_write_json(state_file, state)
                            except Exception:
                                pass
                        try:
                            detail = _force_flatten_move(
                                slot, state, "recovered_entry_position_mismatch_flatten")
                            return jsonify({"ok": False,
                                            "error": "Recovered entry size mismatched and was flattened",
                                            "flattened": True, **detail}), 502
                        except Exception as flatten_exc:
                            return jsonify({"ok": False,
                                            "error": "Recovered entry size is unverified; duplicate entry blocked",
                                            "flatten_error": str(flatten_exc),
                                            "actual_size": actual_size,
                                            "expected_size": expected_size}), 409
                    try:
                        _atomic_write_json(state_file, state)
                    except Exception:
                        pass
                    protected, detail = _protect_or_flatten_move(
                        slot, state, datetime.now(timezone.utc))
                    if not protected:
                        return jsonify({"ok": False, "error": "Recovered entry lacked protection and was flattened",
                                        **detail}), 502
                    return jsonify({"ok": True, "recovered": recovered, "slot": slot,
                                    "side": state["side"], "symbol": state["symbol"],
                                    "lots": state["lots"], "fill": state["entry_mark"],
                                    "order_id": state.get("order_id"), "dry_run": False,
                                    "protection_verified": True})
            if state.get("status") == "OPEN":
                return jsonify({"ok": False, "error": f"{slot} already has an open position"}), 400

            contract = _current_atm_mv(slot)
            if not contract:
                return jsonify({
                    "ok": False,
                    "error": (f"No eligible operational MV contract is currently "
                              f"listed for manual {slot} entry"),
                }), 502
            product_id = int(contract.get("id") or 0)
            if (product_id != preview_product_id
                    or str(contract.get("symbol") or "") != preview_symbol):
                return jsonify({
                    "ok": False,
                    "error": ("MOVE contract changed after preview; review the refreshed "
                              "contract before submitting"),
                }), 409
            if dry_run:
                unrealized = 0.0
            else:
                positions = _strict_exchange_positions()
                unrealized = _validate_move_entry_account(positions, product_id)
            quote = _move_execution_quote(
                contract["symbol"], side, reference_price=preview_price)
            plan = _move_lot_plan(
                slot, side, contract, quote, dry_run=dry_run)
            if int(plan.get("lots") or 0) <= 0:
                return jsonify({"ok": False, "error": plan.get("reason"),
                                "sizing": plan}), 409
            if int(plan.get("lots") or 0) != preview_lots:
                return jsonify({
                    "ok": False,
                    "error": "MOVE sizing changed after preview; review the refreshed lots",
                    "sizing": plan,
                }), 409
            decision = evaluate_entry(
                _mode_data_dir(dry_run), float(plan["proposed_risk_usd"]),
                _user_cfg(), unrealized_pnl_usd=unrealized,
                dry_run=dry_run)
            if not decision.allowed:
                return jsonify({"ok": False, "error": decision.reason,
                                "risk": decision_dict(decision), "sizing": plan}), 409

            opened, order = _submit_manual_move_entry(
                slot, side, contract, quote, plan, dry_run)
            expected_size = -int(opened["lots"]) if opened["side"] == "short" \
                else int(opened["lots"])
            if not dry_run:
                actual_size = _post_entry_exchange_size(product_id, expected_size)
                opened["position_verified_at_entry"] = actual_size == expected_size
                opened["verified_exchange_size_at_entry"] = actual_size
                try:
                    _atomic_write_json(state_file, opened)
                except Exception:
                    # The essential OPEN record is already durable; continue
                    # immediately to protection/flatten despite annotation I/O.
                    pass
                if actual_size != expected_size:
                    if actual_size and actual_size * expected_size > 0:
                        # The selected product was proven flat immediately
                        # before submit, so track the complete new exposure for
                        # the emergency reduce-only close instead of abandoning
                        # an unexplained excess.
                        opened["lots"] = abs(actual_size)
                        opened["owned_entry_lots"] = abs(actual_size)
                        opened["position_mismatch_at_entry"] = {
                            "terminal_fill": expected_size, "exchange_size": actual_size,
                        }
                        try:
                            _atomic_write_json(state_file, opened)
                        except Exception:
                            pass
                    try:
                        detail = _force_flatten_move(
                            slot, opened, "entry_position_mismatch_flatten")
                        return jsonify({"ok": False,
                                        "error": "Entry exchange-size mismatch; exposure was flattened",
                                        "flattened": True, **detail}), 502
                    except Exception as flatten_exc:
                        _send_telegram(
                            f"🚨 <b>MOVE ENTRY RECONCILIATION REQUIRED ({user.upper()})</b>\n"
                            f"Expected <code>{expected_size}</code>, exchange reported "
                            f"<code>{actual_size}</code>; flatten failed: "
                            f"<code>{str(flatten_exc)[:250]}</code>")
                        return jsonify({"ok": False,
                                        "error": "Entry exchange-size mismatch and flatten is unresolved",
                                        "flatten_error": str(flatten_exc),
                                        "slot": slot, "order_id": order.get("id")}), 409
                protected, detail = _protect_or_flatten_move(
                    slot, opened, datetime.now(timezone.utc))
                if not protected:
                    return jsonify({"ok": False,
                                    "error": "Protection was not verified; entry was flattened",
                                    **detail}), 502
            else:
                detail = {"protection_health": {}}

            fill = float(opened["entry_mark"])
            lots = int(opened["lots"])
            _send_telegram(
                f"🖐 <b>MANUAL {side.upper()} — {slot.upper()} ({user.upper()})</b>"
                f"{' — DRY-RUN' if dry_run else ''}\n"
                f"<code>DISCRETIONARY (scheduled value gate not claimed)</code>\n"
                f"<code>{opened['symbol']}</code> · <code>{lots:,}</code> lots · "
                f"IOC fill <code>${fill:.4f}</code>")
            return jsonify({"ok": True, "slot": slot, "side": opened["side"],
                            "symbol": opened["symbol"], "lots": lots,
                            "requested_lots": plan["lots"], "fill": fill,
                            "order_id": order.get("id"), "dry_run": dry_run,
                            "partial_fill": lots < int(plan["lots"]),
                            "protection_verified": True,
                            "entry_classification": "discretionary_manual",
                            "move_value_gate_evaluated": False, **detail})
        except Exception as exc:
            _send_telegram(
                f"🚨 <b>MANUAL MOVE ENTRY ERROR ({user.upper()} / {slot.upper()})</b>\n"
                f"<code>{str(exc)[:400]}</code>")
            return jsonify({"ok": False, "error": str(exc)}), 409


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


def _tp_policy(slot: str) -> dict:
    """Expanded protection policy; legacy TSL remains a safe fallback."""
    target, poll, sl, legacy_tsl = _tp_env(slot)
    suffix = "_MORNING" if slot == "morning" else "_TREND" if slot == "trend" else ""
    def loss(key: str, default: float) -> float:
        try:
            raw = _cfg(key, "")
            return abs(float(raw)) if raw != "" else abs(float(default))
        except (TypeError, ValueError):
            return abs(float(default))
    return {
        "tp_target_pnl": target, "poll_secs": poll, "sl_target_pnl": sl,
        "tsl_arm_pnl": loss(f"TSL_ARM_PNL{suffix}", legacy_tsl),
        "tsl_trail_pnl": loss(f"TSL_TRAIL_PNL{suffix}", legacy_tsl),
        "tsl_lock_min_pnl": loss(f"TSL_LOCK_MIN_PNL{suffix}", 0),
        "tsl_target_pnl": legacy_tsl,
    }


@app.route("/api/tp-monitor", methods=["GET"])
def tp_monitor_status():
    user = _active_user()
    out = {}
    def lot_count(value) -> int:
        try:
            return abs(int(float(value or 0)))
        except (TypeError, ValueError, OverflowError):
            return 0
    for slot in SLOTS:
        policy = _tp_policy(slot)
        target, poll, sl, tsl = (policy["tp_target_pnl"], policy["poll_secs"],
                                 policy["sl_target_pnl"], policy["tsl_target_pnl"])
        st = _load_json(_slot_file(slot), {})
        health = _tp_health(user, slot)
        running = _tp_running(user, slot)
        health_fresh = _tp_health_fresh(health)
        health_matches = _tp_health_matches(health, st, user, slot)
        verified_health = health_fresh and health_matches
        protected_lots = lot_count(
            health.get("protected_lots") if verified_health else
            st.get("protection_lots") or st.get("lots")
        )
        bot_entry_lots = lot_count(
            st.get("original_owned_entry_lots")
            or st.get("owned_entry_lots") or st.get("lots")
        )
        exchange_lots = lot_count(health.get("exchange_position_size")) \
            if verified_health else 0
        exchange_protected_lots = lot_count(
            health.get("exchange_protected_lots")
        ) if verified_health else 0
        external_protected_lots = max(protected_lots - bot_entry_lots, 0)
        continuity_required = slot == "trend" and st.get("status") == "OPEN"
        continuity_ok = bool(
            not continuity_required
            or (verified_health and health.get("continuity_verified") is True
                and lot_count(health.get("continuity_verified_size")) == protected_lots)
        )
        exchange_complete = bool(
            verified_health and health.get("exchange_protection_complete")
            and exchange_protected_lots >= protected_lots > 0
        )
        local_fallback = bool(
            verified_health and health.get("local_fallback_active")
        )
        if not continuity_ok:
            coverage_status = "attention"
        elif running and exchange_lots > exchange_protected_lots:
            coverage_status = "resizing"
        elif exchange_complete:
            coverage_status = "exchange_protected"
        elif local_fallback and health.get("protection_established"):
            coverage_status = "local_fallback"
        elif running and not verified_health:
            coverage_status = "verifying"
        else:
            coverage_status = "attention"
        monitor_error = health.get("last_error") if verified_health else (
            "Protection heartbeat is stale or does not match this position revision"
            if running and st.get("status") == "OPEN" else None
        )
        def strict_order_flag(proof_key: str, state_order_key: str) -> bool:
            proof = health.get(proof_key) if verified_health else None
            if not isinstance(proof, dict) or proof.get("ok") is not True:
                return False
            order = proof.get("order")
            return bool(
                isinstance(order, dict)
                and str(order.get("id") or "") == str(st.get(state_order_key) or "")
                and lot_count(proof.get("covered_lots")) == protected_lots > 0
                and st.get("status") == "OPEN"
            )
        stop_proven = strict_order_flag(
            "stop_order_proof", "tsl_stop_order_id",
        )
        tp_proven = strict_order_flag("tp_order_proof", "tp_stop_order_id")
        out[slot] = {"running": running, "target_pnl": target,
                     "poll_secs": poll, "sl_pnl": sl, "tsl_pnl": tsl,
                     "tsl_arm_pnl": policy["tsl_arm_pnl"],
                     "tsl_trail_pnl": policy["tsl_trail_pnl"],
                     "tsl_lock_min_pnl": policy["tsl_lock_min_pnl"],
                     "healthy": bool(verified_health and health.get("status") == "healthy"),
                     "health_matches": health_matches, "health": health,
                     "protection_established": bool(
                         verified_health and health.get("protection_established")),
                     "local_fallback_active": local_fallback,
                     "continuity_verified": continuity_ok,
                     "continuity_status": health.get("continuity_status")
                                          if verified_health else st.get("continuity_status"),
                     "monitor_error": monitor_error,
                     "coverage_status": coverage_status,
                     "protected_lots": protected_lots,
                     "exchange_position_lots": exchange_lots,
                     "exchange_protected_lots": exchange_protected_lots,
                     "bot_entry_lots": bot_entry_lots,
                     "external_protected_lots": external_protected_lots,
                     "externally_added_lots_adopted": lot_count(
                         st.get("externally_added_lots_adopted")),
                     "last_external_adoption_utc": st.get(
                         "last_external_adoption_utc"),
                     "lot_attribution_status": st.get("lot_attribution_status"),
                     # live trail bookkeeping persisted by the monitor — SL and TSL
                     # share one resting exchange stop, distinguished by stop_kind
                     "tsl_armed":       bool(st.get("tsl_armed")) and st.get("status") == "OPEN",
                     "tsl_floor":       st.get("tsl_floor"),
                     "tsl_on_exchange": stop_proven and st.get("stop_kind") == "tsl",
                     "sl_on_exchange":  stop_proven and st.get("stop_kind", "sl") == "sl",
                     "tp_on_exchange":  tp_proven}
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
    return send_file(str(apk), as_attachment=True, download_name="nithi-bot.apk")


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
                "order_id":   o.get("id"),
                "client_order_id": o.get("client_order_id"),
                "side":       o.get("side", ""),
                "size":       float(o.get("size", 0)),
                "price":      float(o.get("average_fill_price")),
                "time":       str(o.get("created_at", ""))[:19],
            })
        except (TypeError, ValueError):
            continue
    fills.sort(key=lambda f: f["time"])

    def identity_list(value):
        return [] if value in (None, "") else [value]

    def new_cycle(fill: dict, net_size: float) -> dict:
        return {
            "net_size": net_size,
            "entry_price": fill["price"],
            "entry_time": fill["time"],
            "realized_pnl": 0.0,
            "exit_notional": 0.0,
            "exit_qty": 0.0,
            "product_id": fill["product_id"],
            "entry_order_ids": identity_list(fill.get("order_id")),
            "entry_client_order_ids": identity_list(fill.get("client_order_id")),
            "exit_order_ids": [],
            "exit_client_order_ids": [],
        }

    def append_identity(values: list, value) -> None:
        if value not in (None, "") and value not in values:
            values.append(value)

    state: dict = {}
    trades = []
    for f in fills:
        symbol = f["symbol"]
        delta  = f["size"] if f["side"] == "buy" else -f["size"]
        st = state.get(symbol)

        if st is None or st["net_size"] == 0:
            state[symbol] = new_cycle(f, delta)
            continue

        cv = _product_info(st["product_id"])["contract_value"] if st["product_id"] else 0.001

        if (delta > 0) == (st["net_size"] > 0):
            old_abs, add_abs = abs(st["net_size"]), abs(delta)
            st["entry_price"] = (st["entry_price"] * old_abs + f["price"] * add_abs) / (old_abs + add_abs)
            st["net_size"] += delta
            append_identity(st["entry_order_ids"], f.get("order_id"))
            append_identity(st["entry_client_order_ids"], f.get("client_order_id"))
            continue

        old_abs, reduce_abs = abs(st["net_size"]), abs(delta)
        matched = min(old_abs, reduce_abs)
        sign    = 1 if st["net_size"] > 0 else -1
        st["realized_pnl"]  += (f["price"] - st["entry_price"]) * cv * matched * sign
        st["exit_notional"] += f["price"] * matched
        st["exit_qty"]      += matched
        st["net_size"]      += delta
        append_identity(st["exit_order_ids"], f.get("order_id"))
        append_identity(st["exit_client_order_ids"], f.get("client_order_id"))

        if abs(st["net_size"]) < 1e-9:
            exit_avg = st["exit_notional"] / st["exit_qty"] if st["exit_qty"] else f["price"]
            trades.append({
                "date":       st["entry_time"][:10],
                "entry_date": st["entry_time"][:10],
                "symbol":     symbol,
                "product_id": st["product_id"],
                "strike":     _parse_strike(symbol),
                "lots":       old_abs,
                "side":       "LONG" if sign > 0 else "SHORT",
                "entry_mark": round(st["entry_price"], 4),
                "exit_mark":  round(exit_avg, 4),
                "pnl_usd":    round(st["realized_pnl"], 2),
                "entry_time": st["entry_time"][11:],
                "exit_time":  f["time"][11:],
                "entry_order_ids": list(st["entry_order_ids"]),
                "entry_client_order_ids": list(st["entry_client_order_ids"]),
                "exit_order_ids": list(st["exit_order_ids"]),
                "exit_client_order_ids": list(st["exit_client_order_ids"]),
                "order_id": (st["entry_order_ids"][0]
                             if st["entry_order_ids"] else None),
                "client_order_id": (st["entry_client_order_ids"][0]
                                     if st["entry_client_order_ids"] else None),
                "exit_order_id": (st["exit_order_ids"][-1]
                                  if st["exit_order_ids"] else None),
            })
            leftover = reduce_abs - old_abs
            if leftover > 1e-9:
                new_sign = -1 if f["side"] == "sell" else 1
                state[symbol] = new_cycle(f, leftover * new_sign)
            else:
                state[symbol] = {
                    "net_size": 0, "entry_price": 0, "entry_time": "",
                    "realized_pnl": 0.0, "exit_notional": 0.0, "exit_qty": 0.0,
                    "product_id": st["product_id"],
                    "entry_order_ids": [], "entry_client_order_ids": [],
                    "exit_order_ids": [], "exit_client_order_ids": [],
                }
    return trades


def _fetch_reconstructed_trades(skip_prefixes=("MV-BTC",)) -> list:
    """Closed round-trip trades reconstructed from the logged-in account's
    order history. MOVE symbols are skipped by default because each account's
    own bot history tracks them more precisely."""
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


def _trade_phase_ids(record: dict, phase: str) -> set[str]:
    """Stable exchange/client identities attached to one side of a trade."""
    if phase == "entry":
        order_keys = ("entry_order_id", "order_id", "entry_order_ids", "order_ids")
        client_keys = ("entry_client_order_id", "client_order_id",
                       "entry_client_order_ids", "client_order_ids")
    else:
        order_keys = ("exit_order_id", "close_order_id", "exit_order_ids", "close_order_ids")
        client_keys = ("exit_client_order_id", "close_client_order_id",
                       "exit_client_order_ids", "close_client_order_ids")

    identities: set[str] = set()

    def add(prefix: str, value) -> None:
        values = value if isinstance(value, (list, tuple, set)) else [value]
        for item in values:
            if item not in (None, ""):
                identities.add(f"{prefix}:{item}")

    for key in order_keys:
        add("order", record.get(key))
    for key in client_keys:
        add("client", record.get(key))
    if phase == "entry":
        for execution in record.get("executions") or []:
            if isinstance(execution, dict):
                add("order", execution.get("order_id"))
                add("client", execution.get("client_order_id"))
    return identities


def _trade_clock_seconds(record: dict, phase: str) -> int | None:
    value = record.get(f"{phase}_time") or record.get(f"{phase}_time_utc")
    match = re.search(r"(\d{2}):(\d{2}):(\d{2})", str(value or ""))
    if not match:
        return None
    hour, minute, second = map(int, match.groups())
    if hour > 23 or minute > 59 or second > 59:
        return None
    return hour * 3600 + minute * 60 + second


def _trade_clock_distance(left: dict, right: dict, phase: str) -> int | None:
    a = _trade_clock_seconds(left, phase)
    b = _trade_clock_seconds(right, phase)
    if a is None or b is None:
        return None
    difference = abs(a - b)
    return min(difference, 86_400 - difference)


def _trade_side(record: dict) -> str:
    side = str(record.get("side") or "").strip().lower()
    return {"buy": "long", "sell": "short"}.get(side, side)


def _trade_number(record: dict, key: str) -> float | None:
    value = record.get(key)
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _trades_represent_same_round_trip(tracked: dict, reconstructed: dict) -> bool:
    """Conservatively identify one tracked trade reconstructed from orders.

    Stable entry/exit order identities are authoritative. Legacy records that
    lack comparable IDs must match every economic field and both timestamps,
    allowing only the small exchange position-vs-order clock skew observed in
    production.
    """
    if not isinstance(tracked, dict) or not isinstance(reconstructed, dict):
        return False
    if not tracked.get("symbol") or tracked.get("symbol") != reconstructed.get("symbol"):
        return False
    tracked_product = tracked.get("product_id")
    reconstructed_product = reconstructed.get("product_id")
    if (tracked_product not in (None, "") and reconstructed_product not in (None, "")
            and str(tracked_product) != str(reconstructed_product)):
        return False
    tracked_date = tracked.get("date") or tracked.get("entry_date")
    reconstructed_date = reconstructed.get("date") or reconstructed.get("entry_date")
    if not tracked_date or tracked_date != reconstructed_date:
        return False

    tracked_identity = set()
    reconstructed_identity = set()
    for phase in ("entry", "exit"):
        tracked_ids = _trade_phase_ids(tracked, phase)
        reconstructed_ids = _trade_phase_ids(reconstructed, phase)
        tracked_identity.update(tracked_ids)
        reconstructed_identity.update(reconstructed_ids)
        if tracked_ids & reconstructed_ids:
            return True
    # IDs on both records are stronger evidence than similar economics. If no
    # same-phase identity matched, keep both; never cross-match an exit order
    # from one trade to an entry order from a later reversal.
    if tracked_identity and reconstructed_identity:
        return False

    tracked_entry = _trade_clock_seconds(tracked, "entry")
    reconstructed_entry = _trade_clock_seconds(reconstructed, "entry")
    if tracked_entry is not None and tracked_entry == reconstructed_entry:
        # Preserve the historical exact key for sparse legacy records.
        return True

    if _trade_side(tracked) != _trade_side(reconstructed):
        return False
    precision = {"lots": 6, "entry_mark": 4, "exit_mark": 4, "pnl_usd": 2}
    for key, digits in precision.items():
        left = _trade_number(tracked, key)
        right = _trade_number(reconstructed, key)
        if left is None or right is None or round(left, digits) != round(right, digits):
            return False
    entry_distance = _trade_clock_distance(tracked, reconstructed, "entry")
    exit_distance = _trade_clock_distance(tracked, reconstructed, "exit")
    return (entry_distance is not None and entry_distance <= 2
            and exit_distance is not None and exit_distance <= 2)


def _all_trades_merged() -> list:
    """The active user's tracked trades (their trade_history.json — written
    by the bot engine, square-offs, TP monitors and stale-close reconciling)
    plus trades reconstructed from their own Delta order history for anything
    never tracked. The stored ledger is authoritative when both sources refer
    to the same exchange round trip."""
    mv_trades = [
        row for row in _load_json(_hist_file(), [])
        if isinstance(row, dict) and not _is_dry_record(row)
    ]
    other = [
        reconstructed for reconstructed in _fetch_reconstructed_trades()
        if not any(_trades_represent_same_round_trip(tracked, reconstructed)
                   for tracked in mv_trades)
    ]
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


def _dry_run_trades() -> list[dict]:
    try:
        _import_legacy_dry_records()
    except Exception as exc:
        print(f"Legacy dry-run import warning for {_active_user()}: {exc}")
    rows = [
        dict(row) for row in _load_json(_hist_file(dry_run=True), [])
        if isinstance(row, dict) and _is_dry_record(row)
    ]
    # A CLOSED state is an outbox as well as a card.  Include/repair it even
    # when an earlier process died between its state write and history append.
    for slot in SLOTS:
        state = _load_json(_slot_file(slot, dry_run=True), {})
        if (_is_dry_record(state)
                and str(state.get("status") or "").upper() == "CLOSED"):
            state = _as_dry_record(state, slot)
            _append_trade_history(
                state, f"dry-results-repair:{slot}", dry_run=True)
    rows = [
        dict(row) for row in _load_json(_hist_file(dry_run=True), [])
        if isinstance(row, dict) and _is_dry_record(row)
    ]
    for row in rows:
        row.setdefault("date", row.get("entry_date", ""))
        row.setdefault("entry_time", row.get("entry_time_utc", ""))
        row.setdefault("exit_time", row.get("exit_time_utc", ""))
    rows.sort(key=lambda row: (
        row.get("entry_date") or row.get("date", ""),
        row.get("entry_time") or row.get("entry_time_utc", ""),
    ))
    return rows


def _schedule_ist_label(
    cfg: dict,
    hour_key: str,
    minute_key: str,
    default_hour: int,
    default_minute: int,
) -> str:
    try:
        hour = int(cfg.get(hour_key) or default_hour)
        minute = int(cfg.get(minute_key) or default_minute)
    except (TypeError, ValueError):
        hour, minute = default_hour, default_minute
    total = (hour * 60 + minute + 330) % 1440
    hour_ist, minute_ist = divmod(total, 60)
    return (
        f"{(hour_ist + 11) % 12 + 1}:{minute_ist:02d} "
        f"{'PM' if hour_ist >= 12 else 'AM'} IST"
    )


def _dry_protection_policy(state: dict) -> dict:
    """Return the position-snapshotted paper protection policy."""
    policy = state.get("protection_config") if isinstance(state, dict) else None
    if not isinstance(policy, dict):
        slot = str(state.get("slot") or "") if isinstance(state, dict) else ""
        policy = _tp_policy(slot) if slot in SLOTS else {}

    def nonnegative(key: str, default: float = 0.0) -> float:
        value = _as_float(policy.get(key), default)
        return max(value, 0) if math.isfinite(value) else float(default)

    legacy_tsl = nonnegative("tsl_target_pnl")
    raw_poll = _as_float(policy.get("poll_secs"), 30)
    poll_secs = int(raw_poll) if math.isfinite(raw_poll) else 30
    return {
        "tp_target_pnl": nonnegative("tp_target_pnl"),
        "sl_target_pnl": nonnegative("sl_target_pnl"),
        "tsl_arm_pnl": nonnegative("tsl_arm_pnl", legacy_tsl),
        "tsl_trail_pnl": nonnegative("tsl_trail_pnl", legacy_tsl),
        "tsl_lock_min_pnl": nonnegative("tsl_lock_min_pnl"),
        "poll_secs": max(poll_secs, 10),
    }


def _parse_utc_stamp(value) -> datetime | None:
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


def _dry_protection_view(
    state: dict,
    *,
    now: datetime | None = None,
) -> dict:
    """Compact, explicit monitor telemetry for one paper position."""
    policy = _dry_protection_policy(state)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    is_open = (
        str(state.get("status") or "").upper() == "OPEN"
        and _is_dry_record(state)
    )
    checked_at = _parse_utc_stamp(
        state.get("dry_last_protection_check_utc"))
    next_at = _parse_utc_stamp(
        state.get("dry_next_protection_check_utc"))
    last_error = str(state.get("dry_protection_last_error") or "")
    age_secs = (
        max(int((current - checked_at).total_seconds()), 0)
        if checked_at else None
    )
    stale_after = max(policy["poll_secs"] * 2 + 5, 30)
    stale = bool(is_open and checked_at and age_secs > stale_after)
    if not is_open:
        status = "closed"
    elif last_error:
        status = "error"
    elif stale:
        status = "stale"
    elif state.get("dry_tsl_armed"):
        status = "tsl_armed"
    elif checked_at:
        status = "running"
    else:
        status = "starting"
    return {
        **policy,
        "running": is_open,
        "status": status,
        "last_check_utc": (
            checked_at.isoformat().replace("+00:00", "Z")
            if checked_at else None
        ),
        "next_check_utc": (
            next_at.isoformat().replace("+00:00", "Z")
            if next_at else None
        ),
        "last_attempt_utc": state.get(
            "dry_last_protection_attempt_utc"),
        "last_error": last_error,
        "age_secs": age_secs,
        "stale": stale,
        "peak_pnl_usd": (
            round(_as_float(state.get("dry_peak_pnl_usd"), 0), 2)
            if state.get("dry_peak_pnl_usd") is not None else None
        ),
        "tsl_armed": bool(state.get("dry_tsl_armed")),
        "tsl_floor_usd": (
            round(_as_float(state.get("dry_tsl_floor_usd"), 0), 2)
            if state.get("dry_tsl_floor_usd") is not None else None
        ),
    }


def _enrich_dry_state(state: dict) -> dict:
    view = dict(state) if isinstance(state, dict) else {}
    if str(view.get("status") or "").upper() == "OPEN":
        try:
            mark, pnl, _, _ = _dry_run_live_mark_and_pnl(view)
            view["current_mark"] = round(mark, 8)
            view["live_pnl"] = round(pnl, 2)
            view["live_pnl_price_source"] = "mark_price"
        except Exception:
            view["current_mark"] = None
            view["live_pnl"] = None
    view["dry_protection"] = _dry_protection_view(view)
    return view


@app.route("/api/dry-run/status")
def api_dry_run_status():
    try:
        _import_legacy_dry_records()
    except Exception as exc:
        print(f"Legacy dry-run import warning for {_active_user()}: {exc}")
    now = datetime.now(timezone.utc)
    slots = {}
    for slot in SLOTS:
        state = _load_json(_slot_file(slot, dry_run=True), {})
        if state and not _is_dry_record(state):
            # A mode mismatch inside the simulation namespace is invalid and
            # must never be rendered as a paper position.
            state = {"slot": slot, "status": "MODE_MISMATCH"}
        slots[slot] = _dashboard_slot_view(_enrich_dry_state(state), now)
    cfg = _user_cfg()
    mode = _trading_mode_payload()
    return jsonify({
        **mode,
        "mode_active": mode["dry_run_mode"],
        "morning": slots["morning"],
        "evening": slots["evening"],
        "trend": slots["trend"],
        "move_auto_mode": str(
            cfg.get("MOVE_AUTO_ENTRY_MODE") or "shadow").lower(),
        "move_decisions": {
            slot: _move_decision_dashboard_view(slot, dry_run=True)
            for slot in ("morning", "evening")
        },
        "morning_entry_ist": _schedule_ist_label(
            cfg, "MORNING_H_UTC", "MORNING_M_UTC", 0, 15),
        "evening_entry_ist": _schedule_ist_label(
            cfg, "ENTRY_H_UTC", "ENTRY_M_UTC", 12, 5),
        "auto_mode": _trend_auto_mode(),
    })


@app.route("/api/dry-run/trades")
def api_dry_run_trades():
    return jsonify(_dry_run_trades())


@app.route("/api/dry-run/today-trades")
def api_dry_run_today_trades():
    today_ist = datetime.now(_IST_TIMEZONE).strftime("%Y-%m-%d")
    rows = [
        row for row in _dry_run_trades()
        if _ist_calendar_date(
            row.get("entry_date") or row.get("date", ""),
            row.get("entry_time") or row.get("entry_time_utc", ""),
        ) == today_ist
    ]
    for slot in SLOTS:
        state = _load_json(_slot_file(slot, dry_run=True), {})
        if (str(state.get("status") or "").upper() == "OPEN"
                and _is_dry_record(state)
                and _ist_calendar_date(
                    state.get("entry_date", ""),
                    state.get("entry_time_utc", ""),
                ) == today_ist):
            live = _enrich_dry_state(state)
            live.update({"_live": True, "slot": slot})
            rows.insert(0, live)
    return jsonify(rows)


@app.route("/api/dry-run/summary")
def api_dry_run_summary():
    return jsonify(_pnl_stats(_dry_run_trades(), dry_run=True))


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


def _adx(highs: list, lows: list, closes: list, n: int = 14) -> float:
    """Wilder ADX; zero means the supplied candle set is insufficient."""
    if min(len(highs), len(lows), len(closes)) < n * 2 + 1:
        return 0.0
    tr, plus_dm, minus_dm = [], [], []
    for i in range(1, len(closes)):
        up, down = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
        tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                      abs(lows[i] - closes[i - 1])))
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
    atr = sum(tr[:n])
    plus = sum(plus_dm[:n])
    minus = sum(minus_dm[:n])
    dx = []
    for i in range(n - 1, len(tr)):
        if i >= n:
            atr = atr - atr / n + tr[i]
            plus = plus - plus / n + plus_dm[i]
            minus = minus - minus / n + minus_dm[i]
        pdi = 100.0 * plus / atr if atr else 0.0
        mdi = 100.0 * minus / atr if atr else 0.0
        denom = pdi + mdi
        dx.append(100.0 * abs(pdi - mdi) / denom if denom else 0.0)
    if not dx:
        return 0.0
    adx = sum(dx[:n]) / min(len(dx), n)
    for value in dx[n:]:
        adx = (adx * (n - 1) + value) / n
    return adx


TREND_TIMEFRAMES = {
    "5m":  {"resolution": "5m",  "seconds": 300,  "label": "5M", "include_live": False},
    "15m": {"resolution": "15m", "seconds": 900,  "label": "15M", "include_live": False},
    # The hourly candle is deliberately live: a direction change should
    # unlock a Trend entry immediately, without waiting for the hour to close.
    "1h":  {"resolution": "1h",  "seconds": 3600, "label": "1H", "include_live": True},
}

_trend_cache: dict[str, dict] = {}


def _trend_metrics(closes: list, candle_time=None, highs: list | None = None,
                   lows: list | None = None, rsi_up: float = 50.0,
                   rsi_down: float = 50.0,
                   min_ema_gap_pct: float = 0.0) -> dict:
    """Pure trend calculation shared by every timeframe."""
    if len(closes) < 40:
        raise ValueError("not enough candle data")
    ema9, ema21, rsi = _ema(closes, 9), _ema(closes, 21), _rsi(closes, 14)
    close = closes[-1]
    gap_pct = abs(ema9 - ema21) / close * 100 if close else 0.0
    if (ema9 > ema21 and close > ema21 and rsi >= rsi_up
            and gap_pct >= min_ema_gap_pct):
        trend = "up"
    elif (ema9 < ema21 and close < ema21 and rsi <= rsi_down
          and gap_pct >= min_ema_gap_pct):
        trend = "down"
    else:
        trend = "neutral"
    return {"trend": trend, "ema9": round(ema9, 2), "ema21": round(ema21, 2),
            "rsi": round(rsi, 1), "close": round(close, 2),
            "ema_gap_pct": round(gap_pct, 4),
            "adx": round(_adx(highs, lows, closes), 1) if highs and lows else None,
            "candle_time": candle_time}


def _ema21_slope_pct(closes: list, bars: int) -> float:
    bars = max(int(bars), 1)
    if len(closes) < 21 + bars:
        return 0.0
    current = _ema(closes, 21)
    previous = _ema(closes[:-bars], 21)
    return (current - previous) / previous * 100 if previous else 0.0


def _trend_filter_config() -> dict:
    def number(key, default):
        try:
            return float(_cfg(key, str(default)))
        except (TypeError, ValueError):
            return float(default)
    def integer(key, default):
        try:
            return int(float(_cfg(key, str(default))))
        except (TypeError, ValueError):
            return int(default)
    rsi_up = min(max(number("TREND_RSI_UP", 55), 50), 100)
    rsi_down = min(max(number("TREND_RSI_DOWN", 45), 0), 50)
    return {
        "ema_gap_pct": max(number("TREND_EMA_GAP_PCT", 0.05), 0.0),
        "rsi_up": rsi_up,
        "rsi_down": rsi_down,
        "slope_bars": max(integer("TREND_15M_SLOPE_BARS", 3), 1),
        "min_slope_pct": max(number("TREND_MIN_15M_SLOPE_PCT", 0.0), 0.0),
        "adx_min": max(number("TREND_ADX_MIN", 18), 0.0),
        "hour_confirm_samples": max(integer("TREND_1H_CONFIRM_SAMPLES", 2), 1),
    }


def _debounced_hourly_trend(user: str, candidate: str, candle_time,
                            required_samples: int) -> tuple[str, dict]:
    """Require consecutive fresh observations before accepting a 1H flip."""
    state = _trend_debounce.setdefault(user, {
        "candidate": "neutral", "count": 0, "confirmed": "neutral",
        "candle_time": None,
    })
    if candidate not in ("up", "down"):
        state.update(candidate="neutral", count=0, confirmed="neutral",
                     candle_time=candle_time)
        return "neutral", dict(state)
    if candidate == state.get("confirmed"):
        state.update(candidate=candidate, count=max(required_samples, 1),
                     candle_time=candle_time)
        return candidate, dict(state)
    if candidate != state.get("candidate"):
        state.update(candidate=candidate, count=1, candle_time=candle_time)
    else:
        state["count"] = int(state.get("count", 0)) + 1
        state["candle_time"] = candle_time
    if state["count"] >= required_samples:
        state["confirmed"] = candidate
        return candidate, dict(state)
    return "neutral", dict(state)


def _trend_snapshot(force: bool = False) -> dict:
    """Closed 5m/15m signals plus filtered, debounced live-1h signal."""
    user = _active_user()
    cached = _trend_cache.get(user, {})
    if not force and cached.get("data") and time.time() - cached.get("ts", 0) < 15:
        return cached["data"]
    end = int(time.time())
    filters = _trend_filter_config()
    frames = {}
    for key, spec in TREND_TIMEFRAMES.items():
        seconds = spec["seconds"]
        r = req.get(f"{API_BASE}/v2/history/candles",
                    params={"resolution": spec["resolution"], "symbol": "BTCUSD",
                            "start": end - seconds * 300, "end": end},
                    timeout=10).json()
        candles = sorted(r.get("result") or [], key=lambda c: c.get("time", 0))
        current_bucket = end - end % seconds
        if (not spec["include_live"] and candles
                and candles[-1].get("time", 0) >= current_bucket):
            candles = candles[:-1]
        closes = [float(c["close"]) for c in candles]
        highs = [float(c.get("high", c["close"])) for c in candles]
        lows = [float(c.get("low", c["close"])) for c in candles]
        frames[key] = _trend_metrics(
            closes, candles[-1].get("time") if candles else None,
            highs, lows, filters["rsi_up"], filters["rsi_down"],
            filters["ema_gap_pct"])
        frames[key]["live_candle"] = bool(spec["include_live"])

        if key == "15m":
            slope = _ema21_slope_pct(closes, filters["slope_bars"])
            frames[key]["ema21_slope_pct"] = round(slope, 4)
            reasons = []
            if frames[key]["trend"] == "up" and slope < filters["min_slope_pct"]:
                reasons.append("15M EMA21 slope is not rising enough")
            elif frames[key]["trend"] == "down" and slope > -filters["min_slope_pct"]:
                reasons.append("15M EMA21 slope is not falling enough")
            adx = float(frames[key].get("adx") or 0)
            if frames[key]["trend"] in ("up", "down") and adx < filters["adx_min"]:
                reasons.append(f"15M ADX {adx:.1f} is below {filters['adx_min']:.1f}")
            if reasons:
                frames[key]["unfiltered_trend"] = frames[key]["trend"]
                frames[key]["trend"] = "neutral"
                frames[key]["filter_reasons"] = reasons

    hour_raw = frames["1h"]["trend"]
    hour_confirmed, debounce = _debounced_hourly_trend(
        user, hour_raw, frames["1h"].get("candle_time"),
        filters["hour_confirm_samples"])
    frames["1h"]["unfiltered_trend"] = hour_raw
    frames["1h"]["trend"] = hour_confirmed
    frames["1h"]["debounce"] = debounce
    frames["1h"]["debounce_pending"] = hour_raw in ("up", "down") and hour_confirmed != hour_raw

    directions = [frames[k]["trend"] for k in TREND_TIMEFRAMES]
    combined = directions[0] if len(set(directions)) == 1 and directions[0] in ("up", "down") else "neutral"
    # Preserve the original top-level 1H fields for existing Android/API clients.
    data = {**frames["1h"], "combined": combined, "timeframes": frames,
            "all_aligned": combined in ("up", "down"),
            "filters": filters, "observed_at_utc": datetime.now(timezone.utc).isoformat()}
    _trend_cache[user] = {"ts": time.time(), "data": data}
    return data


@app.route("/api/trend")
def api_trend():
    """5m, 15m and 1h BTC trends. Entry is eligible only when all align."""
    try:
        return jsonify(_trend_snapshot())
    except Exception as e:
        return jsonify({"trend": "na", "combined": "na", "timeframes": {},
                        "error": str(e)}), 502


def _pick_two_step_itm(products: list, spot: float, option_type: str,
                       min_tte_hours: float = 1.0,
                       now: datetime | None = None) -> dict | None:
    """Pick two strike-ladder steps ITM from ATM in the nearest usable expiry.

    CE: ATM index - 2. PE: ATM index + 2. This matches the strategy's
    historical ``itm_strike`` definition while still using live products.
    Products, rather than STRIKE_STEP arithmetic, are authoritative because
    Delta's strike spacing varies by expiry and market conditions.
    """
    prefix = "C-BTC" if option_type == "CE" else "P-BTC"
    now_plus_buffer = (now or datetime.now(timezone.utc)) + timedelta(hours=min_tte_hours)
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


def _as_float(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _ticker_epoch(ticker: dict) -> float:
    ts = _as_float(ticker.get("timestamp"), 0)
    while ts > 100_000_000_000:
        ts /= 1000.0
    if ts > 0:
        return ts
    try:
        return datetime.fromisoformat(str(ticker.get("time", "")).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _trend_quote_snapshot(ticker: dict, now_epoch: float | None = None) -> dict:
    quotes = ticker.get("quotes") or {}
    greeks = ticker.get("greeks") or {}
    bid = _as_float(quotes.get("best_bid"), 0)
    ask = _as_float(quotes.get("best_ask"), 0)
    mid = (bid + ask) / 2 if bid > 0 and ask > 0 else 0.0
    quote_epoch = _ticker_epoch(ticker)
    now_epoch = time.time() if now_epoch is None else now_epoch
    return {
        "symbol": str(ticker.get("symbol", "")),
        "mark": _as_float(ticker.get("mark_price"), 0),
        "bid": bid, "ask": ask, "mid": mid,
        "spread_pct": ((ask - bid) / mid * 100 if mid > 0 else None),
        "ask_size": max(_as_float(quotes.get("ask_size"), 0), 0),
        "bid_size": max(_as_float(quotes.get("bid_size"), 0), 0),
        "delta": _as_float(greeks.get("delta"), 0),
        "spot": _as_float(greeks.get("spot") or ticker.get("spot_price"), 0),
        "mark_iv": _as_float(quotes.get("mark_iv") or ticker.get("mark_vol"), 0),
        "tick_size": max(_as_float(ticker.get("tick_size"), 0.1), 0.00000001),
        "quote_epoch": quote_epoch,
        "quote_age_secs": max(now_epoch - quote_epoch, 0) if quote_epoch else None,
        "trading_status": str(ticker.get("product_trading_status", "")),
        "price_band": ticker.get("price_band") or {},
        "oi_contracts": max(_as_float(ticker.get("oi_contracts"), 0), 0),
    }


def _trend_quote_reasons(quote: dict, config: dict | None = None) -> list[str]:
    config = config or _user_cfg()
    def number(key, default):
        try:
            return float(config.get(key) or default)
        except (TypeError, ValueError):
            return float(default)
    reasons = []
    allow_missing = str(config.get("TREND_ALLOW_MISSING_BOOK") or "false").lower() in {
        "1", "true", "yes", "on"
    }
    if quote.get("trading_status") not in ("", "operational"):
        reasons.append("contract is not operational")
    if quote.get("ask", 0) <= 0 or quote.get("bid", 0) <= 0:
        if not allow_missing:
            reasons.append("two-sided option quote is unavailable")
    spread = quote.get("spread_pct")
    max_spread = max(number("TREND_MAX_SPREAD_PCT", 12), 0)
    if spread is None:
        if not allow_missing:
            reasons.append("option spread cannot be verified")
    elif max_spread and spread > max_spread:
        reasons.append(f"spread {spread:.2f}% exceeds {max_spread:.2f}%")
    min_depth = max(number("TREND_MIN_BOOK_DEPTH_LOTS", 10), 0)
    if quote.get("ask_size", 0) < min_depth and not allow_missing:
        reasons.append(f"ask depth {quote.get('ask_size', 0):.0f} is below {min_depth:.0f} lots")
    max_age = max(number("TREND_QUOTE_MAX_AGE_SECS", 20), 0)
    age = quote.get("quote_age_secs")
    if age is None:
        reasons.append("quote timestamp is unavailable")
    elif max_age and age > max_age:
        reasons.append(f"quote is stale ({age:.0f}s)")
    max_iv = max(number("TREND_MAX_MARK_IV", 0), 0)
    if max_iv and quote.get("mark_iv", 0) > max_iv:
        reasons.append(f"mark IV {quote['mark_iv']:.3f} exceeds {max_iv:.3f}")
    return reasons


def _select_trend_option(products: list, tickers: list, spot: float,
                         option_type: str, config: dict | None = None,
                         now: datetime | None = None) -> tuple[dict | None, dict | None, list]:
    """Select liquid ITM option nearest target delta in earliest usable expiry."""
    config = config or _user_cfg()
    now = now or datetime.now(timezone.utc)
    try:
        min_tte = max(float(config.get("TREND_MIN_TTE_HOURS") or 4), 0)
        target_delta = min(max(float(config.get("TREND_TARGET_DELTA") or 0.65), 0.05), 0.99)
    except (TypeError, ValueError):
        min_tte, target_delta = 4.0, 0.65
    prefix = "C-BTC" if option_type == "CE" else "P-BTC"
    ticker_by_symbol = {str(t.get("symbol", "")): t for t in tickers}
    grouped: dict[datetime, list] = {}
    diagnostics = []
    for product in products:
        symbol = str(product.get("symbol", ""))
        if not symbol.startswith(prefix):
            continue
        try:
            expiry = datetime.fromisoformat(str(product.get("settlement_time", "")).replace("Z", "+00:00"))
            strike = float(product.get("strike_price") or 0)
        except (TypeError, ValueError):
            continue
        if expiry <= now + timedelta(hours=min_tte):
            continue
        if not ((option_type == "CE" and strike < spot)
                or (option_type == "PE" and strike > spot)):
            continue
        grouped.setdefault(expiry, []).append(product)

    for expiry in sorted(grouped):
        valid = []
        for product in grouped[expiry]:
            symbol = str(product.get("symbol", ""))
            ticker = ticker_by_symbol.get(symbol)
            if not ticker:
                diagnostics.append(f"{symbol}: ticker unavailable")
                continue
            quote = _trend_quote_snapshot(ticker)
            reasons = _trend_quote_reasons(quote, config)
            if reasons:
                diagnostics.append(f"{symbol}: " + "; ".join(reasons))
                continue
            delta = abs(quote.get("delta", 0))
            # Greeks are preferred. A missing delta remains a lower-priority
            # compatibility fallback, still subject to every liquidity gate.
            score = (0 if delta > 0 else 1,
                     abs(delta - target_delta) if delta > 0 else 99,
                     quote.get("spread_pct") or 999,
                     abs(_as_float(product.get("strike_price")) - spot))
            valid.append((score, product, quote))
        if valid:
            valid.sort(key=lambda row: row[0])
            return valid[0][1], valid[0][2], diagnostics
    return None, None, diagnostics[-10:]


def _current_trend_option_details(direction: str) -> tuple[dict | None, float, dict | None, list]:
    option_type = "CE" if direction == "up" else "PE"
    try:
        spot = float(req.get(f"{API_BASE}/v2/tickers/BTCUSD", timeout=6)
                     .json().get("result", {}).get("mark_price") or 0)
        products = req.get(f"{API_BASE}/v2/products",
                           params={"contract_types": "call_options,put_options",
                                   "underlying_asset_symbols": "BTC",
                                   "states": "live", "page_size": 1000},
                           timeout=12).json().get("result", [])
        tickers = req.get(f"{API_BASE}/v2/tickers",
                          params={"contract_types": "call_options,put_options",
                                  "underlying_asset_symbols": "BTC"},
                          timeout=12).json().get("result", [])
        contract, quote, notes = _select_trend_option(
            products, tickers, spot, option_type, _user_cfg())
        return contract, spot, quote, notes
    except Exception as exc:
        return None, 0.0, None, [str(exc)]


def _current_trend_option(direction: str) -> tuple[dict | None, float]:
    """Back-compatible wrapper retained for existing API/tests."""
    contract, spot, _, _ = _current_trend_option_details(direction)
    return contract, spot


def _trend_auto_mode() -> str:
    # _user_cfg validates the persisted file and deliberately normalizes an
    # absent per-account mode to shadow.  Environment or legacy booleans can
    # therefore never switch a newly created account to live automatically.
    return _user_cfg()["TREND_AUTO_ENTRY_MODE"]


def _state_exit_at(state: dict) -> datetime | None:
    stamp = str(state.get("exit_at_utc") or "")
    if stamp:
        try:
            return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            pass
    date = str(state.get("exit_date") or state.get("entry_date") or "")
    clock = str(state.get("exit_time_utc") or "")
    try:
        dt = datetime.strptime(f"{date} {clock}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        entry_clock = str(state.get("entry_time_utc") or "")
        if entry_clock and clock < entry_clock:
            dt += timedelta(days=1)
        return dt
    except (TypeError, ValueError):
        return None


def _state_has_pending_protection_cleanup(state: dict) -> bool:
    """Whether replacing this slot could lose a live exchange-order identity."""
    if not isinstance(state, dict):
        return False
    return bool(
        state.get("tsl_stop_order_id")
        or state.get("tp_stop_order_id")
        or state.get("pending_stop_protection")
        or state.get("pending_tp_protection")
        or state.get("orphan_protection_order_ids")
        or state.get("pending_close_order_id")
        or state.get("pending_close_client_order_id")
        or state.get("protection_cleanup_pending")
        or state.get("protection_cleanup_errors")
        or state.get("remove_protection_requested")
    )


def _state_has_pending_accounting(state: dict) -> bool:
    """Whether a CLOSED slot still needs authoritative realised accounting."""
    if not isinstance(state, dict):
        return False
    if state.get("history_pending"):
        return True
    if str(state.get("accounting_status") or "").lower() in {
            "pending", "ambiguous", "partial_reduction_unreconciled"}:
        return True
    partial_status = str(
        state.get("partial_exit_accounting_status") or ""
    ).lower()
    if partial_status and partial_status != "complete":
        return True
    if str(state.get("exit_reconciliation_status") or "").lower().startswith(
            "pending"):
        return True
    try:
        return int(state.get("unreconciled_partial_exit_lots") or 0) > 0
    except (TypeError, ValueError, OverflowError):
        return True


def _trend_previous_state_blocker(state: dict) -> str | None:
    """Explain why a non-OPEN Trend record may not be overwritten yet."""
    if not isinstance(state, dict):
        return "Previous Trend state is unreadable; entry fails closed"
    status = str(state.get("status") or "").upper()
    if status == "OPEN":
        return "A trend position is already open"
    if _state_has_pending_protection_cleanup(state):
        return (
            "Previous Trend protection or close cleanup is unresolved; "
            "entry remains blocked until every exchange-order identity is reconciled"
        )
    if _state_has_pending_accounting(state):
        return (
            "Previous Trend realised accounting is still being reconciled; "
            "entry remains blocked until it is complete"
        )
    return None


def _trend_reentry_reason(
    state: dict,
    trend: dict,
    persist: bool = True,
    *,
    dry_run: bool = False,
) -> str | None:
    """One trade per closed 15M candle, then require neutral/opposite rearm."""
    current_direction = str(trend.get("combined") or "neutral")
    current_15m = str((trend.get("timeframes", {}).get("15m") or {}).get("candle_time") or "")
    last_15m = str(state.get("last_entry_15m_candle") or "")
    last_direction = str(state.get("last_entry_direction") or "")
    rearmed = bool(state.get("trend_rearmed", False))
    if last_direction and current_direction in ("neutral", "up", "down"):
        if current_direction == "neutral" or current_direction != last_direction:
            if not rearmed and state.get("status") != "OPEN":
                state["trend_rearmed"] = True
                rearmed = True
                if persist:
                    _atomic_write_json(
                        _slot_file("trend", dry_run=dry_run), state)

    if not current_15m:
        return "Completed 15M candle identity is unavailable"
    if last_15m and current_15m == last_15m:
        return "This completed 15M candle has already triggered a Trend entry"
    if last_direction and current_direction == last_direction and not rearmed:
        return "Trend must turn neutral or opposite before the same direction can rearm"

    cooldown = max(_as_float(_cfg("TREND_REENTRY_COOLDOWN_MIN", "30"), 30), 0)
    try:
        was_loss = float(state.get("pnl_usd")) < 0
    except (TypeError, ValueError):
        was_loss = False
    exited = _state_exit_at(state)
    if was_loss and cooldown and exited:
        remaining = int((exited + timedelta(minutes=cooldown)
                         - datetime.now(timezone.utc)).total_seconds())
        if remaining > 0:
            return f"Trend loss cooldown active for {math.ceil(remaining / 60)} more minute(s)"
    return None


def _account_unrealized_pnl() -> float | None:
    """One authenticated snapshot for the shared daily-loss governor."""
    try:
        r = req.get(f"{API_BASE}/v2/positions/margined",
                    headers=_sign("GET", "/v2/positions/margined"), timeout=8).json()
        if not r.get("success"):
            return None
        return sum(_as_float(p.get("unrealized_pnl"), 0)
                   for p in r.get("result", []) if _as_float(p.get("size"), 0) != 0)
    except Exception:
        return None


def _open_long_premium_usd(*, dry_run: bool = False) -> float:
    total = 0.0
    for slot in SLOTS:
        state = _load_json(_slot_file(slot, dry_run=dry_run), {})
        if state.get("status") == "OPEN" and state.get("side", "long") != "short":
            total += max(_as_float(state.get("total_cost_usd"), 0), 0)
    return total


def _trend_lot_plan(
    contract: dict,
    quote: dict,
    *,
    dry_run: bool = False,
) -> dict:
    config = _user_cfg()
    configured = max(int(_as_float(config.get("TREND_LOTS") or 100, 100)), 1)
    max_order = max(int(_as_float(config.get("MAX_ORDER_LOTS") or 1000, 1000)), 1)
    chunk_cap = max(int(_as_float(config.get("TREND_ORDER_CHUNK_LOTS") or 1000, 1000)), 1)
    max_order = min(max_order, chunk_cap)
    ask = _as_float(quote.get("ask") or quote.get("mark"), 0)
    cv = _as_float(contract.get("contract_value"), 0.001)
    strike = _as_float(contract.get("strike_price"), 0)
    notional_reference = _as_float(quote.get("spot"), 0) or strike
    # Simulations are backed by virtual capital, not the authenticated
    # account's USD wallet.  Configured lots become the paper affordability
    # ceiling while premium, risk and order caps remain mandatory.
    affordable = (configured if dry_run
                  else _affordable_option_lots(ask, cv, notional_reference))
    affordability_source = "paper_configured_cap" if dry_run else "exchange_wallet"
    observed_ask_depth = max(int(_as_float(quote.get("ask_size"), 0)), 0)
    risk_budget = max(_as_float(config.get("TREND_RISK_BUDGET_USD") or 100, 100), 0)
    max_slippage_pct = max(_as_float(config.get("TREND_MAX_SLIPPAGE_PCT") or 1, 1), 0)
    premium_per_lot = ask * cv
    round_trip_fee = 2 * _option_fee_per_lot(ask, cv, notional_reference)
    slippage_per_lot = premium_per_lot * max_slippage_pct / 100
    _, _, sl_target, _ = _tp_env("trend")

    premium_limit = max(_as_float(config.get("MAX_ACCOUNT_PREMIUM_AT_RISK_USD") or 500, 500), 0)
    premium_remaining = max(
        premium_limit - _open_long_premium_usd(dry_run=dry_run), 0
    ) if premium_limit else math.inf
    premium_cap = (int(premium_remaining / premium_per_lot)
                   if premium_per_lot > 0 and math.isfinite(premium_remaining) else max_order)
    lots = risk_based_lots(
        configured=configured,
        affordable=affordable or 0,
        # Trend depth still controls contract eligibility and each LIVE IOC
        # execution chunk, but never reduces the planned strategy lots.
        liquidity_cap=premium_cap,
        max_order_lots=max_order,
        risk_budget_usd=risk_budget,
        stop_loss_usd=sl_target,
        premium_per_lot=premium_per_lot,
        round_trip_fee_per_lot=round_trip_fee,
        slippage_per_lot=slippage_per_lot,
    )
    proposed_risk = max(sl_target,
                        lots * (premium_per_lot + round_trip_fee + slippage_per_lot)) if lots else 0
    return {
        "lots": lots, "configured": configured,
        "affordable": affordable,
        "affordability_source": affordability_source,
        "observed_ask_depth_lots": observed_ask_depth,
        "book_depth_applied_to_sizing": False,
        "premium_cap": premium_cap, "max_order_cap": max_order,
        "risk_budget_usd": round(risk_budget, 2),
        "stop_loss_usd": round(sl_target, 2),
        "premium_per_lot": round(premium_per_lot, 8),
        "round_trip_fee_per_lot": round(round_trip_fee, 8),
        "slippage_per_lot": round(slippage_per_lot, 8),
        "proposed_risk_usd": round(proposed_risk, 2),
    }


def _trend_entry_preview_data(
    *,
    dry_run: bool | None = None,
) -> tuple[dict, int]:
    mode = _trading_mode_payload()
    # Direct helper callers retain the historical LIVE default. HTTP routes
    # and the auto worker always pass the server-authoritative current mode.
    is_dry_run = False if dry_run is None else bool(dry_run)
    if is_dry_run:
        try:
            _import_legacy_dry_records()
        except Exception:
            pass
    else:
        _sync_states_from_exchange()
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
    state = _load_json(_slot_file("trend", dry_run=is_dry_run), {})
    previous_state_blocker = _trend_previous_state_blocker(state)
    if previous_state_blocker:
        return {"ok": True, "can_enter": False, "reason": previous_state_blocker,
                "direction": direction, "option_type": option_type,
                "signal_key": signal_key}, 200
    external = _external_options.get(_active_user(), []) if not is_dry_run else []
    if external and not _cfg_bool("ALLOW_EXTERNAL_POSITIONS_WITH_BOT", False):
        return {"ok": True, "can_enter": False,
                "reason": f"{len(external)} external/manual option position(s) are open; "
                          "portfolio risk is unowned and entries fail closed",
                "external_positions": external, "signal_key": signal_key}, 200
    reentry_reason = _trend_reentry_reason(
        state, trend, dry_run=is_dry_run)
    if not option_type:
        pending = bool((trend.get("timeframes", {}).get("1h") or {}).get("debounce_pending"))
        return {"ok": True, "can_enter": False,
                "reason": ("Live 1H direction is awaiting persistence confirmation" if pending
                           else "5M, 15M and 1H trends are not aligned or filters are not met"),
                "direction": direction, "signal_key": signal_key,
                "timeframes": trend.get("timeframes", {}),
                "filters": trend.get("filters", {})}, 200
    if reentry_reason:
        return {"ok": True, "can_enter": False, "reason": reentry_reason,
                "direction": direction, "option_type": option_type,
                "signal_key": signal_key}, 200
    contract, spot, quote, diagnostics = _current_trend_option_details(direction)
    if not contract:
        return {"ok": True, "can_enter": False,
                "reason": f"No liquid target-delta ITM {option_type} contract passed the gates",
                "market_diagnostics": diagnostics}, 200
    symbol = contract["symbol"]
    cv = float(contract.get("contract_value") or 0.001)
    quote = quote or {}
    entry_price = _as_float(quote.get("ask") or quote.get("mark"), 0)
    if entry_price <= 0:
        return {"ok": True, "can_enter": False,
                "reason": f"Executable ask unavailable for {symbol}"}, 200
    sizing = _trend_lot_plan(contract, quote, dry_run=is_dry_run)
    lots = int(sizing["lots"])
    if lots < 1:
        return {"ok": True, "can_enter": False,
                "reason": "Configured, affordable, risk, premium, or order cap permits zero lots",
                "sizing": sizing}, 200

    unrealized = 0.0 if is_dry_run else _account_unrealized_pnl()
    if (not is_dry_run and unrealized is None
            and _cfg_bool("RISK_FAIL_CLOSED", True)):
        return {"ok": True, "can_enter": False,
                "reason": "Account unrealized P&L could not be verified (risk checks fail closed)",
                "sizing": sizing}, 200
    decision = evaluate_entry(
        _mode_data_dir(is_dry_run), sizing["proposed_risk_usd"], _user_cfg(),
        unrealized_pnl_usd=unrealized or 0.0, dry_run=is_dry_run)
    if not decision.allowed:
        return {"ok": True, "can_enter": False,
                "reason": decision.reason, "risk": decision_dict(decision),
                "sizing": sizing}, 200
    return {"ok": True, "can_enter": True, "direction": direction,
            "option_type": option_type, "symbol": symbol, "product_id": int(contract["id"]),
            "strike": float(contract.get("strike_price") or 0), "spot": round(spot, 2),
            "mark": round(_as_float(quote.get("mark"), entry_price), 4),
            "bid": round(_as_float(quote.get("bid")), 4),
            "ask": round(_as_float(quote.get("ask")), 4),
            "spread_pct": round(_as_float(quote.get("spread_pct")), 3),
            "delta": round(_as_float(quote.get("delta")), 4),
            "mark_iv": round(_as_float(quote.get("mark_iv")), 4),
            "quote": quote, "lots": lots,
            "est_value": round(entry_price * cv * lots, 2),
            "settlement": contract.get("settlement_time", ""),
            "contract_value": cv, "dry_run": is_dry_run,
            "execution_mode": "dry_run" if is_dry_run else "live",
            "mode_revision": mode["mode_revision"],
            "signal_key": signal_key, "timeframes": trend.get("timeframes", {}),
            "signal_snapshot": trend, "sizing": sizing,
            "risk": decision_dict(decision),
            "auto_mode": _trend_auto_mode()}, 200


@app.route("/api/trend-entry/preview")
def api_trend_entry_preview():
    current_mode = _trading_mode_payload()
    data, status = _trend_entry_preview_data(
        dry_run=current_mode["dry_run_mode"])
    for key, value in _trading_mode_payload().items():
        data.setdefault(key, value)
    data.setdefault("dry_run", data.get("dry_run_mode", False))
    return jsonify(data), status


def _trend_audit(event: str, details: dict) -> None:
    try:
        audit_event(_user_dir(), event, details)
    except Exception:
        # An audit disk problem must be visible through auto health, but it
        # must not turn a confirmed exchange fill into an HTTP failure.
        health = _trend_auto_health.setdefault(_active_user(), {})
        health["last_audit_error"] = "strategy audit write failed"


def _ceil_to_tick(price: float, tick: float) -> float:
    if tick <= 0:
        return price
    steps = math.ceil((price - 1e-12) / tick)
    decimals = max(0, min(12, int(math.ceil(-math.log10(tick))) + 2)) if tick < 1 else 4
    return round(steps * tick, decimals)


def _trend_client_id(user: str, chunk_index: int, market=False) -> str:
    safe_user = re.sub(r"[^a-z0-9]", "", user.lower())[:7] or "acct"
    suffix = "m" if market else "l"
    return f"trend-{safe_user}-{int(time.time() * 1000):x}-{chunk_index}{suffix}"[:32]


def _filled_order_size(order: dict, requested: int) -> int:
    for key in ("filled_size", "filled_quantity", "executed_size"):
        if order.get(key) not in (None, ""):
            return min(max(int(_as_float(order.get(key), 0)), 0), requested)
    if order.get("unfilled_size") not in (None, ""):
        return min(max(requested - int(_as_float(order.get("unfilled_size"), requested)), 0), requested)
    # An average price alone does not prove quantity: IOC orders may be
    # partially filled. Never infer requested size without an explicit filled
    # or unfilled field; the caller verifies the live exchange position next.
    return 0


def _order_commission_optional_usd(order: dict) -> float | None:
    for key in ("paid_commission", "commission", "commission_usd", "total_commission"):
        if order.get(key) not in (None, ""):
            return max(_as_float(order.get(key), 0), 0)
    meta = order.get("meta_data") or {}
    if meta.get("paid_commission") not in (None, ""):
        return max(_as_float(meta.get("paid_commission"), 0), 0)
    return None


def _order_commission_usd(order: dict) -> float:
    """Compatibility helper for entry execution; missing remains zero there."""
    value = _order_commission_optional_usd(order)
    return float(value or 0.0)


def _refresh_order(order: dict, product_id: int, requested: int) -> dict:
    if not order.get("id") or _filled_order_size(order, requested) > 0:
        return order
    latest = order
    for _ in range(3):
        try:
            path = f"/v2/orders/{order['id']}"
            query = f"?product_id={product_id}"
            result = req.get(f"{API_BASE}{path}", params={"product_id": product_id},
                             headers=_sign("GET", path, query), timeout=8).json()
            if result.get("success") and isinstance(result.get("result"), dict):
                latest = result["result"]
                if (_filled_order_size(latest, requested) > 0
                        or latest.get("unfilled_size") not in (None, "")):
                    break
        except Exception:
            pass
        time.sleep(0.25)
    return latest


def _exchange_option_position(product_id: int) -> dict | None:
    try:
        result = req.get(f"{API_BASE}/v2/positions/margined",
                         headers=_sign("GET", "/v2/positions/margined"), timeout=8).json()
        if not result.get("success"):
            return None
        for position in result.get("result", []):
            if int(position.get("product_id", 0) or 0) == product_id:
                return position
    except Exception:
        pass
    return None


def _journal_order_intent(payload: dict, preview: dict, chunk_index: int) -> bool:
    """Durably record client identity before an irreversible exchange call."""
    client_id = str(payload.get("client_order_id") or "").strip()
    safe_client_id = re.sub(r"[^a-zA-Z0-9_-]", "_", client_id)[:80]
    pending_path = (_user_dir() / f"pending_trend_order_{safe_client_id}.json"
                    if safe_client_id else None)
    try:
        if pending_path is None:
            raise RuntimeError("Trend order intent has no client identity")
        _atomic_write_json(pending_path, {
            "status": "PENDING",
            "client_order_id": client_id,
            "product_id": payload.get("product_id"),
            "symbol": preview.get("symbol"),
            "size": payload.get("size"),
            "order_type": payload.get("order_type"),
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "chunk_index": chunk_index,
            "signal_key": preview.get("signal_key"),
        })
        audit_event(_user_dir(), "trend_order_intent", {
            "client_order_id": client_id,
            "product_id": payload.get("product_id"), "symbol": preview.get("symbol"),
            "size": payload.get("size"), "order_type": payload.get("order_type"),
            "limit_price": payload.get("limit_price"), "time_in_force": payload.get("time_in_force"),
            "chunk_index": chunk_index, "signal_key": preview.get("signal_key"),
        })
        return True
    except Exception as exc:
        _trend_auto_health.setdefault(_active_user(), {})["last_audit_error"] = str(exc)
        return False


def _clear_pending_trend_intents(client_ids) -> None:
    for client_id in client_ids or ():
        safe_client_id = re.sub(r"[^a-zA-Z0-9_-]", "_", str(client_id or "").strip())[:80]
        if not safe_client_id:
            continue
        try:
            (_user_dir() / f"pending_trend_order_{safe_client_id}.json").unlink(
                missing_ok=True)
        except OSError:
            # Retaining the marker is fail-closed: a later mode check will keep
            # the selector locked until the operator can verify the intent.
            pass


def _submit_trend_order(payload: dict) -> tuple[dict | None, dict]:
    body = json.dumps(payload, separators=(",", ":"))
    result = req.post(f"{API_BASE}/v2/orders", data=body,
                      headers=_sign("POST", "/v2/orders", "", body), timeout=15).json()
    order = result.get("result") if result.get("success") else None
    return (order if isinstance(order, dict) and order.get("id") else None), result


def _execute_trend_chunks(preview: dict, requested_lots: int) -> dict:
    """Buy using bounded IOC limits; depth sizes chunks, not planned lots."""
    symbol, pid = preview["symbol"], int(preview["product_id"])
    max_slippage = max(_as_float(_cfg("TREND_MAX_SLIPPAGE_PCT", "1"), 1), 0)
    chunk_size = max(int(_as_float(_cfg("TREND_ORDER_CHUNK_LOTS", "1000"), 1000)), 1)
    market_fallback = _cfg_bool("TREND_MARKET_FALLBACK_ENABLED", False)
    participation = min(max(_as_float(_cfg("TREND_BOOK_PARTICIPATION_PCT", "25"), 25), 0), 100)
    reference_ask = _as_float(preview.get("ask"), 0)
    remaining, filled_total, weighted = requested_lots, 0, 0.0
    executions, error = [], None
    pending_intent_ids, unresolved_intent_ids = [], []
    chunk_index = 0
    while remaining > 0:
        chunk_index += 1
        chunk = min(remaining, chunk_size)
        ticker_data = req.get(f"{API_BASE}/v2/tickers/{symbol}", timeout=7).json().get("result", {})
        quote = _trend_quote_snapshot(ticker_data)
        reasons = _trend_quote_reasons(quote)
        if reasons:
            error = "Fresh execution quote failed: " + "; ".join(reasons)
            break
        ask, tick = _as_float(quote.get("ask"), 0), _as_float(quote.get("tick_size"), 0.1)
        execution_depth_cap = int(
            _as_float(quote.get("ask_size"), 0) * participation / 100)
        if execution_depth_cap <= 0:
            error = "Fresh ask depth permits zero lots at configured participation"
            break
        chunk = min(chunk, execution_depth_cap)
        limit_price = _ceil_to_tick((reference_ask or ask) * (1 + max_slippage / 100), tick)
        if ask > limit_price:
            error = f"Fresh ask {ask} exceeds entry slippage cap {limit_price}"
            break
        upper = _as_float((quote.get("price_band") or {}).get("upper_limit"), 0)
        if upper and limit_price > upper:
            error = f"Slippage-capped limit {limit_price} exceeds exchange upper band {upper}"
            break
        client_id = _trend_client_id(_active_user(), chunk_index)
        payload = {"product_id": pid, "size": chunk, "side": "buy",
                   "order_type": "limit_order", "limit_price": str(limit_price),
                   "time_in_force": "ioc", "client_order_id": client_id}
        if not _journal_order_intent(payload, preview, chunk_index):
            error = "Durable order-intent journal failed; no exchange order was sent"
            break
        pending_intent_ids.append(client_id)
        order, result = _submit_trend_order(payload)
        if not order:
            err = result.get("error") or {}
            resized = (_downsized_lots(chunk, err.get("context") or {})
                       if str(err.get("code")) in BALANCE_REJECTIONS else None)
            if resized and filled_total == 0:
                remaining = min(remaining, resized)
                continue
            error = str(result.get("error") or result)
            break
        order = _refresh_order(order, pid, chunk)
        limit_filled = _filled_order_size(order, chunk)
        fill_price = _as_float(order.get("average_fill_price"), ask)
        quantity_proven = order.get("unfilled_size") not in (None, "") or limit_filled > 0
        if not quantity_proven:
            position = _exchange_option_position(pid)
            if position is not None:
                live_size = max(int(_as_float(position.get("size"), 0)), 0)
                limit_filled = min(max(live_size - filled_total, 0), chunk)
                quantity_proven = True
                position_entry = _as_float(position.get("entry_price"), fill_price)
                if limit_filled > 0:
                    fill_price = max((position_entry * live_size - weighted) / limit_filled, 0)
        filled = limit_filled
        weighted += fill_price * limit_filled
        executions.append({"kind": "ioc_limit", "client_order_id": client_id,
                           "order_id": order.get("id"), "requested": chunk,
                           "filled": limit_filled, "limit_price": limit_price,
                           "average_fill_price": fill_price,
                           "paid_commission_usd": _order_commission_optional_usd(order)})

        if not quantity_proven:
            unresolved_intent_ids.append(client_id)
            error = (f"Order {order.get('id')} quantity could not be verified; "
                     "intent is journaled for reconciliation and no fallback was sent")
            break

        unfilled = max(chunk - limit_filled, 0)
        if unfilled and market_fallback:
            fallback_id = _trend_client_id(_active_user(), chunk_index, market=True)
            fallback_payload = {"product_id": pid, "size": unfilled, "side": "buy",
                                "order_type": "market_order",
                                "client_order_id": fallback_id}
            if not _journal_order_intent(fallback_payload, preview, chunk_index):
                error = "Market fallback journal failed; unfilled quantity was aborted"
                filled_total += filled
                remaining -= filled
                break
            pending_intent_ids.append(fallback_id)
            fallback, fallback_result = _submit_trend_order(fallback_payload)
            if fallback:
                fallback = _refresh_order(fallback, pid, unfilled)
                fallback_filled = _filled_order_size(fallback, unfilled)
                fallback_price = _as_float(fallback.get("average_fill_price"), ask)
                fallback_proven = (fallback.get("unfilled_size") not in (None, "")
                                   or fallback_filled > 0)
                if not fallback_proven:
                    position = _exchange_option_position(pid)
                    if position is not None:
                        live_size = max(int(_as_float(position.get("size"), 0)), 0)
                        fallback_filled = min(
                            max(live_size - filled_total - limit_filled, 0), unfilled)
                        fallback_proven = True
                        position_entry = _as_float(position.get("entry_price"), fallback_price)
                        if fallback_filled > 0:
                            fallback_price = max(
                                (position_entry * live_size - weighted) / fallback_filled, 0)
                executions.append({"kind": "configured_market_fallback",
                                   "client_order_id": fallback_id,
                                   "order_id": fallback.get("id"), "requested": unfilled,
                                   "filled": fallback_filled,
                                   "average_fill_price": fallback_price,
                                   "paid_commission_usd": _order_commission_optional_usd(fallback)})
                weighted += fallback_price * fallback_filled
                filled += fallback_filled
                if not fallback_proven:
                    unresolved_intent_ids.append(fallback_id)
                    error = (f"Fallback order {fallback.get('id')} quantity is unverified; "
                             "no further order was sent")
            else:
                error = str(fallback_result.get("error") or fallback_result)
        filled_total += filled
        remaining -= filled
        # Never chase an IOC partial with repeated price changes. The filled
        # exposure is persisted and protected; the unfilled intent is aborted.
        if filled < chunk:
            error = error or f"IOC filled {filled}/{chunk}; remaining order was aborted"
            break
        if remaining > 0:
            # Make the verified slice visible to the caller immediately so it
            # can persist state and establish protection before any more risk.
            error = (f"Filled protected IOC slice {filled_total}/{requested_lots}; "
                     "remaining lots were not submitted")
            break
    filled_executions = [
        item for item in executions if int(item.get("filled") or 0) > 0
    ]
    fee_complete = all(
        item.get("paid_commission_usd") is not None
        for item in filled_executions
    )
    paid_commission = round(sum(
        _as_float(item.get("paid_commission_usd"), 0)
        for item in filled_executions
    ), 8)
    return {"filled_lots": filled_total,
            "fill_price": weighted / filled_total if filled_total else 0.0,
            "executions": executions, "unfilled_lots": requested_lots - filled_total,
            "paid_commission_usd": paid_commission if fee_complete else None,
            "entry_fees_complete": fee_complete,
            "error": error, "market_fallback_enabled": market_fallback,
            "pending_intent_ids": pending_intent_ids,
            "unresolved_intent_ids": unresolved_intent_ids}


def _execute_trend_entry(
    auto: bool = False,
    *,
    expected: dict | None = None,
):
    """Buy the server-derived contract under the cross-process risk lock."""
    user = _active_user()
    with account_entry_lock(_user_dir(), f"trend:{user}") as acquired:
        if not acquired:
            return jsonify({"ok": False, "error": "Another strategy entry is in progress"}), 409
        expectation_error = _mode_expectation_error(expected)
        if expectation_error:
            return jsonify({"ok": False, "error": expectation_error}), 409
        mode = _trading_mode_payload()
        # HTTP and auto callers are mode-bound. Direct internal invocations
        # retain the legacy LIVE default used by low-level safety tests/tools.
        mode_bound = auto or expected is not None
        is_dry_run = mode["dry_run_mode"] if mode_bound else False
        state_dir = _mode_data_dir(is_dry_run)
        state_guard = ExitStack()
        close_state_acquired = state_guard.enter_context(account_file_lock(
            state_dir, "close-trend", f"dashboard-trend-entry-{os.getpid()}",
            stale_after_sec=30, wait_sec=5,
        ))
        if not close_state_acquired:
            state_guard.close()
            return jsonify({
                "ok": False,
                "error": "Trend protection/cleanup state is busy; entry was not submitted",
            }), 409
        if not is_dry_run:
            _sync_states_from_exchange()
        preview, status = (
            _trend_entry_preview_data(dry_run=is_dry_run)
            if mode_bound else _trend_entry_preview_data()
        )
        if status != 200 or not preview.get("can_enter"):
            state_guard.close()
            _trend_audit("trend_entry_blocked", {"auto": auto, **preview})
            return jsonify(preview), 400 if status == 200 else status
        # Preview and execution are separate safety boundaries. A CLOSED
        # reconciliation worker can publish a pending order/accounting identity
        # after the preview was built; reload immediately before any exchange
        # submission so the new OPEN state can never overwrite that identity.
        if mode_bound and bool(preview.get("dry_run")) != is_dry_run:
            state_guard.close()
            return jsonify({
                "ok": False,
                "error": "Trading Mode changed while building the Trend preview",
            }), 409
        latest_previous_state = _load_json(
            _slot_file("trend", dry_run=is_dry_run), {})
        previous_state_blocker = _trend_previous_state_blocker(
            latest_previous_state
        )
        if previous_state_blocker:
            blocked = {
                "ok": False, "can_enter": False,
                "reason": previous_state_blocker,
                "error": previous_state_blocker,
            }
            state_guard.close()
            _trend_audit("trend_entry_blocked", {"auto": auto, **blocked})
            return jsonify(blocked), 409
        key, secret = _active_creds()
        if not is_dry_run and (not key or not secret):
            state_guard.close()
            return jsonify({"ok": False, "error": "API credentials not configured"}), 400

        pid, requested = int(preview["product_id"]), int(preview["lots"])
        cv = float(preview["contract_value"])
        try:
            if is_dry_run:
                fill = float(preview.get("ask") or preview["mark"])
                execution = {"filled_lots": requested, "fill_price": fill,
                             "unfilled_lots": 0, "error": None,
                             "paid_commission_usd": 0.0,
                             "entry_fees_complete": True,
                             "market_fallback_enabled": False,
                             "executions": [{"kind": "dry_run", "requested": requested,
                                             "filled": requested,
                                             "average_fill_price": fill,
                                             "client_order_id": None, "order_id": 0}]}
            else:
                execution = _execute_trend_chunks(preview, requested)
                if execution["filled_lots"] <= 0:
                    resolved_intents = set(execution.get("pending_intent_ids") or ()) - set(
                        execution.get("unresolved_intent_ids") or ())
                    _clear_pending_trend_intents(resolved_intents)
                    _trend_audit("trend_entry_failed", {
                        "auto": auto, "signal_key": preview.get("signal_key"),
                        "symbol": preview["symbol"], "requested_lots": requested,
                        "execution": execution, "quote": preview.get("quote"),
                        "sizing": preview.get("sizing"), "risk": preview.get("risk")})
                    if "could not be verified" in str(execution.get("error") or ""):
                        _send_telegram(
                            f"🚨 <b>TREND ORDER RECONCILIATION REQUIRED ({user.upper()})</b>\n"
                            f"<code>{preview['symbol']}</code> order intent was accepted but fill quantity "
                            f"could not be verified. Client IDs are durably journaled; inspect the exchange now."
                        )
                    state_guard.close()
                    return jsonify({"ok": False,
                                    "error": execution.get("error") or "IOC order did not fill"}), 400
                fill = float(execution["fill_price"])
            lots = int(execution["filled_lots"])
            orders = execution["executions"]
            client_ids = [o.get("client_order_id") for o in orders if o.get("client_order_id")]
            order_ids = [o.get("order_id") for o in orders if o.get("order_id") is not None]
            now = datetime.now(timezone.utc)
            # Record the exact signal, sizing, risk and execution policy at fill.
            protection_policy = _tp_policy("trend")
            new_state = {
                "slot": "trend", "status": "OPEN", "side": "long",
                "option_type": preview["option_type"], "trend_signal": preview["direction"],
                "entry_date": now.strftime("%Y-%m-%d"),
                "entry_time_utc": now.strftime("%H:%M:%S"),
                "symbol": preview["symbol"], "product_id": pid,
                "strike": preview["strike"], "settlement": preview["settlement"],
                "contract_value": cv, "lots": lots, "entry_mark": round(fill, 4),
                "owned_entry_lots": lots, "original_owned_entry_lots": lots,
                "protection_lots": lots, "max_protected_lots": lots,
                "protection_revision": 0, "continuity_revision": 0,
                "position_cycle_id": _trend_position_cycle_id(
                    pid, now.isoformat(), order_ids),
                "continuity_anchor_utc": now.isoformat(),
                "continuity_verified": False,
                "continuity_status": "awaiting_monitor_verification",
                "original_bot_entry_mark": round(fill, 4),
                "original_bot_entry_fee_usd": execution.get("paid_commission_usd"),
                "original_bot_entry_fee_source": (
                    "exchange" if execution.get("entry_fees_complete")
                    else "fee_pending"
                ),
                "cycle_entry_lots_total": lots, "cycle_exit_lots_total": 0,
                "partial_exit_accounting_status": "complete",
                "position_composition": "bot_only",
                "btc_at_entry": preview["spot"],
                "total_cost_usd": round(fill * cv * lots, 2),
                "entry_fees_usd": execution.get("paid_commission_usd"),
                "fees_usd": execution.get("paid_commission_usd"),
                "entry_fee_source": (
                    "exchange" if execution.get("entry_fees_complete")
                    else "fee_pending"
                ),
                "order_id": order_ids[0] if order_ids else 0,
                "order_ids": order_ids,
                "client_order_id": client_ids[0] if client_ids else None,
                "client_order_ids": client_ids,
                "ownership": "trend_bot",
                "entry_trigger": "trend_auto" if auto else "trend_alignment",
                "auto_signal_key": preview.get("signal_key"),
                "trend_timeframes": preview.get("timeframes", {}),
                "signal_snapshot": preview.get("signal_snapshot", {}),
                "quote_snapshot": preview.get("quote", {}),
                "sizing_snapshot": preview.get("sizing", {}),
                "risk_decision": preview.get("risk", {}),
                "risk_at_entry_usd": preview.get("sizing", {}).get("proposed_risk_usd"),
                "trading_date": preview.get("risk", {}).get("trading_date"),
                "execution_snapshot": execution,
                "last_entry_15m_candle": str((preview.get("timeframes", {}).get("15m") or {}).get("candle_time") or ""),
                "last_entry_direction": preview["direction"],
                "trend_rearmed": False,
                "protection_config": protection_policy,
                "dry_run": is_dry_run,
                "execution_mode": "dry_run" if is_dry_run else "live",
            }
            if is_dry_run:
                new_state["simulation_id"] = _simulation_identity(
                    new_state, "trend")
                simulated_entry_fee = _option_fee_per_lot(
                    fill, cv, float(preview.get("strike") or 0)
                ) * lots
                new_state.update({
                    "entry_fees_usd": round(simulated_entry_fee, 8),
                    "fees_usd": round(simulated_entry_fee, 8),
                    "entry_fee_source": "configured_simulation",
                    "original_bot_entry_fee_usd": round(
                        simulated_entry_fee, 8),
                    "original_bot_entry_fee_source": "configured_simulation",
                })
            _atomic_write_json(
                _slot_file("trend", dry_run=is_dry_run), new_state)
            resolved_intents = set(execution.get("pending_intent_ids") or ()) - set(
                execution.get("unresolved_intent_ids") or ())
            _clear_pending_trend_intents(resolved_intents)
            # The new generation is durable. Release close-trend before the
            # monitor starts, because its first protection cycle takes this
            # same lock.
            state_guard.close()
            monitor_started = False
            if not is_dry_run and not _tp_running(_active_user(), "trend"):
                monitor_started = _spawn_tp(_active_user(), "trend") is not None
            protection_verified, protection_health = (True, {}) if is_dry_run else \
                _wait_for_protection(user, "trend", now, timeout_secs=10)
            with account_file_lock(
                    state_dir, "close-trend",
                    f"dashboard-trend-entry-health-{os.getpid()}",
                    stale_after_sec=30, wait_sec=2) as health_state_lock:
                if health_state_lock:
                    latest_state = _load_json(
                        _slot_file("trend", dry_run=is_dry_run), {})
                    if (
                        latest_state.get("status") == "OPEN"
                        and str(latest_state.get("position_cycle_id") or "")
                        == str(new_state.get("position_cycle_id") or "")
                    ):
                        latest_state.update({
                            "protection_verified_at_entry": protection_verified,
                            "protection_health_at_entry": protection_health,
                        })
                        _atomic_write_json(
                            _slot_file("trend", dry_run=is_dry_run),
                            latest_state)
            if not is_dry_run and not protection_verified:
                _send_telegram(
                    f"🚨 <b>TREND PROTECTION ALERT ({user.upper()})</b>\n"
                    f"<code>{preview['symbol']}</code> filled, but protection was not verified "
                    f"within 10 seconds. Further exposure is blocked while this position is open."
                )
            mode = " — DRY-RUN (simulated)" if is_dry_run else ""
            _send_telegram(
                f"📈 <b>TREND ENTRY — {preview['option_type']} ({_active_user().upper()})</b>{mode}\n"
                f"<code>{'━' * 24}</code>\nSymbol  » <code>{preview['symbol']}</code>\n"
                f"Signal  » <code>5M + 15M + 1H {preview['direction'].upper()}</code>\n"
                f"Lots    » <code>{lots:,}</code>\nFill    » <code>${fill:.4f}</code>\n"
                f"Value   » <code>${fill * cv * lots:,.2f}</code>\n"
                f"Execution » <code>IOC limit{(' · PARTIAL ' + str(lots) + '/' + str(requested)) if lots < requested else ''}</code>"
            )
            _trend_audit("trend_entry_filled", {
                "auto": auto, "symbol": preview["symbol"], "option_type": preview["option_type"],
                "signal_key": preview.get("signal_key"), "signal": preview.get("signal_snapshot"),
                "quote": preview.get("quote"), "sizing": preview.get("sizing"),
                "risk": preview.get("risk"), "execution": execution,
                "monitor_started": monitor_started,
                "protection_verified": protection_verified,
                "protection_health": protection_health})
            return jsonify({"ok": True, "slot": "trend", "side": "long",
                            "option_type": preview["option_type"], "symbol": preview["symbol"],
                            "lots": lots, "requested_lots": requested, "fill": fill,
                            "order_id": order_ids[0] if order_ids else 0,
                            "order_ids": order_ids,
                            "dry_run": is_dry_run, "monitor_started": monitor_started,
                            "protection_verified": protection_verified,
                            "partial_fill": lots < requested,
                            "execution_warning": execution.get("error"), "auto": auto})
        except Exception as e:
            state_guard.close()
            _trend_audit("trend_entry_exception", {"auto": auto, "error": str(e),
                                                   "preview": preview})
            if not is_dry_run:
                _send_telegram(
                    f"🚨 <b>TREND ENTRY EXCEPTION ({user.upper()})</b>\n"
                    f"<code>{preview.get('symbol', '')}</code> submission raised an exception. "
                    f"Order intents are journaled; reconcile the exchange before retrying.\n"
                    f"<code>{str(e)[:300]}</code>"
                )
            return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/trend-entry", methods=["POST"])
def api_trend_entry():
    if not _trend_entry_lock.acquire(blocking=False):
        return jsonify({"ok": False, "error": "Another Trend entry is being processed"}), 409
    try:
        return _execute_trend_entry(
            auto=False, expected=request.get_json(silent=True) or {})
    finally:
        _trend_entry_lock.release()


def _maybe_auto_trend_entry() -> bool:
    """Shadow or live-auto entry, with explicit health and blocked reasons."""
    user = _active_user()
    mode = _trend_auto_mode()
    health = _trend_auto_health.setdefault(user, {})
    health.update({"user": user, "mode": mode,
                   "last_cycle_utc": datetime.now(timezone.utc).isoformat(),
                   "last_error": None})
    if mode == "disabled":
        health.update(status="disabled", last_action="none")
        return False
    now = time.time()
    if now - _trend_auto_last_attempt.get(user, 0.0) < 30:
        health["status"] = "throttled"
        return False
    if not _trend_entry_lock.acquire(blocking=False):
        health.update(status="busy", last_action="entry lock busy")
        return False
    try:
        _trend_auto_last_attempt[user] = now
        dry_run = _trading_mode_payload()["dry_run_mode"]
        if not dry_run:
            _sync_states_from_exchange()
        state = _load_json(_slot_file("trend", dry_run=dry_run), {})
        if state.get("status") == "OPEN":
            health.update(status="position_open", last_action="waiting for open Trend position")
            return False
        preview, status = _trend_entry_preview_data(dry_run=dry_run)
        if status != 200 or not preview.get("can_enter"):
            health.update(status="blocked", last_action=preview.get("reason") or preview.get("error"),
                          last_signal_key=preview.get("signal_key"))
            return False
        signal_key = str(preview.get("signal_key") or "")
        health.update(last_signal_key=signal_key, last_preview={
            "direction": preview.get("direction"), "symbol": preview.get("symbol"),
            "lots": preview.get("lots"), "risk": preview.get("risk"),
        })
        if mode == "shadow":
            if _trend_shadow_seen.get(user) != signal_key:
                _trend_shadow_seen[user] = signal_key
                _trend_audit("trend_shadow_signal", {
                    "signal_key": signal_key, "signal": preview.get("signal_snapshot"),
                    "symbol": preview.get("symbol"), "quote": preview.get("quote"),
                    "sizing": preview.get("sizing"), "risk": preview.get("risk")})
            health.update(status="shadow_ready", last_action="eligible signal recorded; no order sent",
                          last_shadow_utc=datetime.now(timezone.utc).isoformat())
            return False
        response = _execute_trend_entry(auto=True)
        raw_response = response[0] if isinstance(response, tuple) else response
        response_status = (response[1] if isinstance(response, tuple)
                           else getattr(raw_response, "status_code", 500))
        ok = int(response_status) < 300
        health.update(status="filled" if ok else "entry_failed",
                      last_action=(
                          ("dry-run auto simulation opened" if dry_run
                           else "live auto entry submitted")
                          if ok else
                          ("dry-run auto simulation failed" if dry_run
                           else "live auto entry failed")
                      ),
                      last_entry_utc=datetime.now(timezone.utc).isoformat() if ok else health.get("last_entry_utc"))
        return ok
    except Exception as exc:
        health.update(status="error", last_error=str(exc), last_action="auto loop exception")
        _trend_audit("trend_auto_error", {"error": str(exc)})
        return False
    finally:
        _trend_entry_lock.release()


@app.route("/api/trend-auto/status")
def api_trend_auto_status():
    user = _active_user()
    return jsonify({"user": user, "mode": _trend_auto_mode(),
                    **_trend_auto_health.get(user, {})})


def _trend_auto_loop() -> None:
    """Background per-account trigger; works even when no browser is open."""
    while True:
        try:
            _ensure_open_monitors()
            for acct in _load_accounts():
                user = _safe_user(acct.get("username", ""))
                if not user:
                    continue
                try:
                    with app.test_request_context("/api/trend-entry"):
                        g.basic_user = user
                        _maybe_auto_trend_entry()
                except Exception as exc:
                    _trend_auto_health.setdefault(user, {}).update(
                        status="error", last_error=str(exc),
                        last_cycle_utc=datetime.now(timezone.utc).isoformat())
                    try:
                        with app.test_request_context("/api/trend-entry"):
                            g.basic_user = user
                            _trend_audit("trend_auto_error", {"error": str(exc)})
                    except Exception:
                        pass
        except Exception as exc:
            print(f"Trend auto supervisor error: {exc}")
        time.sleep(15)


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
    try:
        return jsonify(_user_cfg())
    except AccountConfigError as exc:
        return jsonify({"ok": False, "config_valid": False,
                        "error": str(exc)}), 409


@app.route("/api/trading-mode-availability", methods=["GET"])
def trading_mode_availability():
    try:
        return jsonify(_trading_mode_change_status())
    except AccountConfigError as exc:
        return jsonify({
            "ok": False,
            "mode_change_allowed": False,
            "mode_selection_enabled": False,
            "verification_ok": False,
            "error": str(exc),
            "mode_lock_reason": (
                "Trading Mode is locked because account configuration could "
                "not be verified."
            ),
        }), 409


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
            pid = int(pid_file.read_text().strip())
            if _pid_alive(pid) and not _pid_is_monitor(pid, user, slot):
                print(f"WARNING: refusing to signal unproven PID {pid} for {user}/{slot}")
                return False
            if _pid_alive(pid):
                os.kill(pid, 15)
        except Exception:
            pass
        pid_file.unlink(missing_ok=True)
    state = _load_json(USERS_DIR / user / SLOT_STATE_FILES[slot], {})
    if state.get("status") != "OPEN":
        return False
    return _spawn_tp(user, slot) is not None


_TP_KEYS_BY_SLOT = {
    "evening": {"TP_TARGET_PNL", "TP_POLL_SECS", "SL_TARGET_PNL", "TSL_TARGET_PNL",
                "TSL_ARM_PNL", "TSL_TRAIL_PNL", "TSL_LOCK_MIN_PNL"},
    "morning": {"TP_TARGET_PNL_MORNING", "TP_POLL_SECS_MORNING",
                "SL_TARGET_PNL_MORNING", "TSL_TARGET_PNL_MORNING",
                "TSL_ARM_PNL_MORNING", "TSL_TRAIL_PNL_MORNING",
                "TSL_LOCK_MIN_PNL_MORNING"},
    "trend": {"TP_TARGET_PNL_TREND", "TP_POLL_SECS_TREND",
              "SL_TARGET_PNL_TREND", "TSL_TARGET_PNL_TREND",
              "TSL_ARM_PNL_TREND", "TSL_TRAIL_PNL_TREND",
              "TSL_LOCK_MIN_PNL_TREND"},
}

_CONFIG_NUMERIC_BOUNDS = {
    "TREND_EMA_GAP_PCT": (0, 5), "TREND_RSI_UP": (50, 100),
    "TREND_RSI_DOWN": (0, 50), "TREND_15M_SLOPE_BARS": (1, 20),
    "TREND_MIN_15M_SLOPE_PCT": (0, 5), "TREND_ADX_MIN": (0, 100),
    "TREND_1H_CONFIRM_SAMPLES": (1, 10), "TREND_MIN_TTE_HOURS": (1, 168),
    "TREND_TARGET_DELTA": (0.05, 0.99), "TREND_MAX_SPREAD_PCT": (0.1, 100),
    "TREND_MIN_BOOK_DEPTH_LOTS": (0, 10_000_000),
    "TREND_BOOK_PARTICIPATION_PCT": (0.1, 100),
    "TREND_QUOTE_MAX_AGE_SECS": (1, 300), "TREND_MAX_MARK_IV": (0, 10),
    "TREND_RISK_BUDGET_USD": (1, 10_000_000),
    "TREND_MAX_SLIPPAGE_PCT": (0.01, 20), "TREND_ORDER_CHUNK_LOTS": (1, 5000),
    "MAX_ORDER_LOTS": (1, 5000),
    "TREND_REENTRY_COOLDOWN_MIN": (0, 1440),
    "MAX_TRADES_PER_DAY_GLOBAL": (1, 100), "MAX_DAILY_LOSS_USD": (0, 10_000_000),
    "MAX_OPEN_RISK_USD": (0, 10_000_000), "MAX_CONSECUTIVE_LOSSES": (0, 100),
    "LOSS_COOLDOWN_MINUTES": (0, 1440),
    "MAX_ACCOUNT_PREMIUM_AT_RISK_USD": (0, 10_000_000),
    "OPTION_FEE_RATE": (0, 0.01), "OPTION_FEE_CAP_PCT": (0, 1),
    "RISK_PER_TRADE_USD_MORNING": (1, 10_000_000),
    "RISK_PER_TRADE_USD_EVENING": (1, 10_000_000),
    "SHORT_MAX_RISK_USD": (0, 10_000_000), "MAX_SPREAD_PCT": (0.1, 100),
    "MAX_SLIPPAGE_PCT": (0.01, 20), "MIN_BOOK_DEPTH_MULTIPLE": (0.01, 100),
    "MAX_QUOTE_AGE_SEC": (1, 300), "ORDER_CHUNK_LOTS": (1, 5000),
    "MOVE_MIN_EDGE_PCT": (0, 1000), "MOVE_MIN_TTE_MINUTES": (1, 1440),
    "MOVE_MAX_TTE_HOURS": (1, 168), "MOVE_VOL_LOOKBACK": (30, 1000),
    "MAX_CONCURRENT_MOVE_POSITIONS": (1, 2),
    "MOVE_MIN_LONG_EDGE_ABS_USD": (0, 10_000),
    "MOVE_MIN_SHORT_EDGE_ABS_USD": (0, 10_000),
    "MOVE_MIN_LONG_EDGE_PCT": (0, 100),
    "MOVE_MIN_SHORT_EDGE_PCT": (0, 100),
    "MOVE_MAX_MODEL_AGE_SEC": (1, 3600),
    "MOVE_MIN_BID_SIZE": (0, 10_000_000),
    "MOVE_MIN_ASK_SIZE": (0, 10_000_000),
    "MOVE_MAX_JUMP_SCORE_SHORT": (0, 1),
    "MOVE_MAX_LONG_PREMIUM_RISK_USD": (1, 10_000_000),
    "MOVE_MAX_SHORT_MARGIN_USAGE_PCT": (0, 100),
    "MOVE_MIN_LIQUIDATION_BUFFER_PCT": (0, 100),
    "MOVE_NO_ENTRY_BEFORE_SETTLEMENT_SEC": (0, 86_400),
    "MOVE_DRY_RUN_CAPITAL_USD": (1000, 1000),
    "MOVE_FORECAST_LOOKBACK_DAYS": (7, 30),
    "MOVE_FORECAST_OUTER_SCENARIOS": (8, 128),
    "MOVE_FORECAST_PATHS_PER_SCENARIO": (32, 1024),
}


def _validate_config_update(data: dict, current: dict) -> str | None:
    if "DRY_RUN" in data:
        raw_value = data.get("DRY_RUN")
        raw_dry_run = str(raw_value if raw_value is not None else "").strip().lower()
        if raw_dry_run not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
            return "DRY_RUN must be enabled or disabled"
    mode = str(data.get("TREND_AUTO_ENTRY_MODE", current.get("TREND_AUTO_ENTRY_MODE", ""))).lower()
    if mode and mode not in {"disabled", "shadow", "live"}:
        return "TREND_AUTO_ENTRY_MODE must be disabled, shadow, or live"
    move_mode = str(data.get(
        "MOVE_AUTO_ENTRY_MODE",
        current.get("MOVE_AUTO_ENTRY_MODE") or "shadow",
    ) or "shadow").lower()
    if move_mode not in {"disabled", "shadow", "live"}:
        return "MOVE_AUTO_ENTRY_MODE must be disabled, shadow, or live"
    for key in (
        "MOVE_ALLOW_LONG", "MOVE_REQUIRE_NO_OPEN_ORDERS", "MOVE_REQUIRE_FLAT",
    ):
        if key not in data:
            continue
        raw = str(data.get(key) if data.get(key) is not None else "").lower()
        if raw not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
            return f"{key} must be enabled or disabled"
    for key, (low, high) in _CONFIG_NUMERIC_BOUNDS.items():
        if key not in data:
            continue
        try:
            value = float(data[key])
        except (TypeError, ValueError):
            return f"{key} must be numeric"
        if not math.isfinite(value) or not low <= value <= high:
            return f"{key} must be between {low} and {high}"
    merged = {**current, **data}
    try:
        if float(merged.get("TREND_RSI_DOWN") or 45) >= float(merged.get("TREND_RSI_UP") or 55):
            return "TREND_RSI_DOWN must be below TREND_RSI_UP"
    except (TypeError, ValueError):
        return "Trend RSI thresholds must be numeric"
    try:
        if (float(merged.get("MOVE_MIN_TTE_MINUTES") or 90) / 60
                >= float(merged.get("MOVE_MAX_TTE_HOURS") or 30)):
            return "MOVE minimum TTE must be below maximum TTE"
    except (TypeError, ValueError):
        return "MOVE TTE limits must be numeric"
    try:
        if (float(merged.get("MOVE_MIN_SHORT_EDGE_PCT") or 10)
                < float(merged.get("MOVE_MIN_LONG_EDGE_PCT") or 5)):
            return (
                "MOVE short edge % must be at least the long edge % "
                "because short MOVE has greater tail risk"
            )
        if (float(merged.get("MOVE_MIN_SHORT_EDGE_ABS_USD") or 0.02)
                < float(merged.get("MOVE_MIN_LONG_EDGE_ABS_USD") or 0.01)):
            return (
                "MOVE short absolute edge must be at least the long "
                "absolute edge"
            )
    except (TypeError, ValueError):
        return "MOVE edge thresholds must be numeric"
    if str(merged.get("ALLOW_SHORT_MOVE") or "false").lower() in {"1", "true", "yes", "on"}:
        try:
            if float(merged.get("SHORT_MAX_RISK_USD") or 0) <= 0:
                return ("Short MOVE is enabled. Enter a positive Maximum short risk $, "
                        "or select Disabled under Short MOVE entries.")
        except (TypeError, ValueError):
            return "SHORT_MAX_RISK_USD must be numeric"
    return None


def _save_config_data(data: dict):
    """Save strategy settings for the ACTIVE account only — written to
    users/<name>/config.json, never to the shared .env (which now serves
    purely as the global default for keys an account hasn't set). The
    account's bot instance watches its config.json and self-reloads."""
    user_dir = _user_dir()
    with account_file_lock(
        user_dir,
        "config",
        f"dashboard-config-save:{os.getpid()}:{time.time_ns()}",
        stale_after_sec=30,
        wait_sec=5,
    ) as acquired:
        if not acquired:
            return jsonify({
                "ok": False,
                "config_saved": False,
                "error": (
                    "Configuration is busy in another request. No settings "
                    "were changed; retry Save."
                ),
            }), 409
        try:
            saved, _ = _saved_user_cfg()
            current = _user_cfg()
        except AccountConfigError as exc:
            return jsonify({
                "ok": False,
                "config_saved": False,
                "error": str(exc),
            }), 409
        error = _validate_config_update(data, current)
        if error:
            return jsonify({"ok": False, "config_saved": False,
                            "error": error}), 400
        if "MAX_TRADES_PER_DAY_GLOBAL" in data:
            # The scheduler still reads this legacy alias in one pre-entry check.
            # Keep both caps identical so the visible setting is the effective one.
            data["MAX_TRADES_PER_DAY"] = data["MAX_TRADES_PER_DAY_GLOBAL"]
        if "TREND_AUTO_ENTRY_MODE" in data:
            # Keep old Android clients meaningful: only live mode maps to true.
            data["TREND_AUTO_ENTRY_ENABLED"] = (
                str(data["TREND_AUTO_ENTRY_MODE"]).lower() == "live")
        elif "TREND_AUTO_ENTRY_ENABLED" in data:
            legacy_on = str(data["TREND_AUTO_ENTRY_ENABLED"]).lower() in {
                "1", "true", "yes", "on",
            }
            data["TREND_AUTO_ENTRY_MODE"] = (
                "live" if legacy_on else "disabled")
        saved.update({k: str(v) for k, v in data.items() if k in CONFIG_KEYS})
        _atomic_write_json(_cfg_file(), saved)
    # Running TP monitors don't watch config — bounce any of the active
    # user's monitors whose targets just changed.
    user = _active_user()
    _trend_cache.pop(user, None)
    tp_restarted = []
    snapshot_slots = []
    snapshot_errors = []
    for slot, keys in _TP_KEYS_BY_SLOT.items():
        if not (keys & set(data)):
            continue
        for dry_run in (False, True):
            # Protection is attached to each position in its own namespace.
            # Updating a paper policy never restarts or touches a LIVE monitor.
            data_dir = _mode_data_dir(dry_run)
            state_path = data_dir / SLOT_STATE_FILES[slot]
            if dry_run and not state_path.exists():
                continue
            with account_file_lock(
                    data_dir, f"close-{slot}",
                    f"dashboard-config-{os.getpid()}-{slot}-"
                    f"{'dry' if dry_run else 'live'}",
                    stale_after_sec=30, wait_sec=5) as acquired:
                if not acquired:
                    snapshot_errors.append(
                        f"{slot} ({'DRY RUN' if dry_run else 'LIVE'})")
                    continue
                state = _load_json(state_path, {})
                if (state.get("status") == "OPEN"
                        and _is_dry_record(state) is dry_run):
                    state["protection_config"] = _tp_policy(slot)
                    if dry_run:
                        # A saved paper policy must become effective on the
                        # next scheduler tick, not after the old interval.
                        state["dry_next_protection_check_utc"] = (
                            datetime.now(timezone.utc).isoformat())
                        state["dry_protection_last_error"] = ""
                    _atomic_write_json(state_path, state)
                    if not dry_run:
                        snapshot_slots.append(slot)

    # Process termination/spawn must happen after every state lock is released.
    # A new monitor takes the same close-{slot} lock on its first protection
    # cycle, so restarting inside the context can deadlock or time out.
    for slot in snapshot_slots:
        if _tp_running(user, slot) and _restart_tp_monitor(user, slot):
            tp_restarted.append(slot)
    if snapshot_errors:
        return jsonify({
            "ok": False,
            "config_saved": True,
            "error": (
                "Configuration was saved, but active protection state is busy "
                "for: " + ", ".join(snapshot_errors)
                + ". Retry Save before relying on the new protection values."
            ),
            "tp_restarted": tp_restarted,
            "protection_snapshot_pending": snapshot_errors,
        }), 409
    return jsonify({"ok": True, "tp_restarted": tp_restarted})


@app.route("/api/config", methods=["POST"])
def set_config():
    data = dict(request.json or {})
    if "DRY_RUN" not in data:
        return _save_config_data(data)
    # Mode changes serialize with every manual/scheduled MOVE and Trend entry.
    # If an entry already owns the mutex, Save fails without changing mode.
    with account_entry_lock(
        _user_dir(), f"config-mode:{_active_user()}"
    ) as acquired:
        if not acquired:
            return jsonify({
                "ok": False,
                "error": (
                    "Trading Mode cannot change while an entry is being "
                    "processed. Retry Save after the entry finishes."
                ),
            }), 409
        raw_value = data.get("DRY_RUN")
        raw_requested = str(raw_value if raw_value is not None else "").strip().lower()
        if raw_requested in {"true", "1", "yes", "on"}:
            requested_dry_run = True
        elif raw_requested in {"false", "0", "no", "off"}:
            requested_dry_run = False
        else:
            # Preserve the normal validation response for malformed clients.
            return _save_config_data(data)
        try:
            current_dry_run, _ = _trading_mode()
        except AccountConfigError as exc:
            return jsonify({
                "ok": False,
                "config_saved": False,
                "error": str(exc),
            }), 409
        if requested_dry_run != current_dry_run:
            availability = _trading_mode_change_status()
            if not availability["mode_change_allowed"]:
                return jsonify({
                    **availability,
                    "ok": False,
                    "config_saved": False,
                    "error": availability["mode_lock_reason"],
                }), 409
        return _save_config_data(data)


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


_monitor_last_restart: dict[str, float] = {}


def _ensure_open_monitors(force: bool = False) -> int:
    """Supervise OPEN protection and one-shot CLOSED reconciliation workers."""
    if not USERS_DIR.exists():
        return 0
    started = 0
    for udir in USERS_DIR.iterdir():
        if not (udir / "account.json").exists():
            continue
        user = udir.name
        for slot in SLOTS:
            state = _load_json(udir / SLOT_STATE_FILES[slot], {})
            status = str(state.get("status") or "").upper()
            open_protection = status == "OPEN" and not state.get("dry_run")
            pending_closed_accounting = (
                status == "CLOSED"
                and _state_has_pending_accounting(state)
                and not state.get("dry_run")
            )
            pending_closed_cleanup = (
                status == "CLOSED"
                and _state_has_pending_protection_cleanup(state)
                and not state.get("dry_run")
            )
            if not (open_protection or pending_closed_accounting
                    or pending_closed_cleanup):
                continue
            if slot == "trend" and not _is_owned_trend_state(state):
                continue
            key = f"{user}:{slot}"
            running = _tp_running(user, slot)
            health = _tp_health(user, slot)
            healthy = _tp_health_fresh(health) and _tp_health_matches(
                health, state, user, slot)
            # A just-spawned process needs one poll interval to create its
            # first heartbeat; don't mistake startup for a hang.
            pid_file = _pid_file(user, slot)
            grace = False
            try:
                grace = pid_file.exists() and time.time() - pid_file.stat().st_mtime < 120
            except OSError:
                pass
            if running and (healthy or grace):
                continue
            if not force and time.time() - _monitor_last_restart.get(key, 0) < 90:
                continue
            _monitor_last_restart[key] = time.time()
            proc = (_restart_tp_monitor(user, slot) if running else _spawn_tp(user, slot))
            if proc:
                started += 1
                pid = proc.pid if hasattr(proc, "pid") else "?"
                print(f"Supervised {user}/{slot} TP monitor (pid {pid})")
            else:
                print(f"WARNING: could not supervise {user}/{slot} TP monitor")
    return started


def _revive_tp_monitors():
    """Back-compatible startup/status hook."""
    return _ensure_open_monitors(force=True)


def _dry_run_protection_cycle(
    *,
    now: datetime | None = None,
) -> int:
    """Evaluate TP/SL/TSL for all isolated simulations of the active user."""
    closed = 0
    cycle_at = now or datetime.now(timezone.utc)
    if cycle_at.tzinfo is None:
        cycle_at = cycle_at.replace(tzinfo=timezone.utc)
    cycle_at = cycle_at.astimezone(timezone.utc)
    try:
        _import_legacy_dry_records()
    except Exception:
        pass
    for slot in SLOTS:
        state_path = _slot_file(slot, dry_run=True)
        state = _load_json(state_path, {})
        if (str(state.get("status") or "").upper() != "OPEN"
                or not _is_dry_record(state)):
            continue
        policy = _dry_protection_policy(state)
        next_check = _parse_utc_stamp(
            state.get("dry_next_protection_check_utc"))
        if next_check and cycle_at < next_check:
            continue
        attempted_at = cycle_at.isoformat()
        poll_secs = policy["poll_secs"]
        try:
            _, pnl, _, _ = _dry_run_mark_and_pnl(state)
        except Exception as exc:
            with account_file_lock(
                _mode_data_dir(True), f"close-{slot}",
                f"dry-protection-error:{os.getpid()}",
                stale_after_sec=30, wait_sec=0,
            ) as acquired:
                if not acquired:
                    continue
                latest = _load_json(state_path, {})
                if (str(latest.get("status") or "").upper() != "OPEN"
                        or not _is_dry_record(latest)
                        or _simulation_identity(latest, slot)
                        != _simulation_identity(state, slot)):
                    continue
                retry_secs = min(poll_secs, 10)
                latest.update({
                    "dry_last_protection_attempt_utc": attempted_at,
                    "dry_next_protection_check_utc": (
                        cycle_at + timedelta(seconds=retry_secs)
                    ).isoformat(),
                    "dry_protection_last_error": str(exc)[:300],
                })
                _atomic_write_json(state_path, latest)
            continue
        previous_peak = _as_float(
            state.get("dry_peak_pnl_usd"), max(pnl, 0))
        peak = max(previous_peak, pnl)
        tp = policy["tp_target_pnl"]
        sl = policy["sl_target_pnl"]
        arm = policy["tsl_arm_pnl"]
        trail = policy["tsl_trail_pnl"]
        locked = policy["tsl_lock_min_pnl"]
        tsl_armed = bool(arm and trail and peak >= arm)
        tsl_floor = max(peak - trail, locked) if tsl_armed else None
        trigger = None
        if tp and pnl >= tp:
            trigger = "take_profit_simulated"
        elif sl and pnl <= -sl:
            trigger = "stop_loss_simulated"
        elif tsl_armed and pnl <= tsl_floor:
            trigger = "trailing_stop_simulated"
        if not trigger:
            settlement = str(state.get("settlement") or "")
            try:
                settles_at = datetime.fromisoformat(
                    settlement.replace("Z", "+00:00"))
                if settles_at.tzinfo is None:
                    settles_at = settles_at.replace(tzinfo=timezone.utc)
                if cycle_at >= settles_at.astimezone(timezone.utc):
                    trigger = "settlement_simulated"
            except (TypeError, ValueError):
                pass

        with account_file_lock(
            _mode_data_dir(True), f"close-{slot}",
            f"dry-protection:{os.getpid()}", stale_after_sec=30, wait_sec=0,
        ) as acquired:
            if not acquired:
                continue
            latest = _load_json(state_path, {})
            if (str(latest.get("status") or "").upper() != "OPEN"
                    or not _is_dry_record(latest)
                    or _simulation_identity(latest, slot)
                    != _simulation_identity(state, slot)):
                continue
            latest.update({
                "dry_last_protection_attempt_utc": attempted_at,
                "dry_last_protection_check_utc": attempted_at,
                "dry_next_protection_check_utc": (
                    cycle_at + timedelta(seconds=poll_secs)
                ).isoformat(),
                "dry_protection_last_error": "",
                "dry_peak_pnl_usd": round(peak, 8),
                "dry_tsl_armed": tsl_armed,
                "dry_tsl_floor_usd": (
                    round(tsl_floor, 8) if tsl_floor is not None else None),
            })
            if trigger:
                _close_dry_simulation_locked(slot, latest, trigger=trigger)
                closed += 1
            else:
                _atomic_write_json(state_path, latest)
    return closed


def _dry_run_protection_loop() -> None:
    """Public-data-only due-time scheduler; never calls an order endpoint."""
    while True:
        try:
            for account in _load_accounts():
                user = _safe_user(account.get("username", ""))
                if not user:
                    continue
                try:
                    with app.test_request_context("/api/dry-run/status"):
                        g.basic_user = user
                        _dry_run_protection_cycle()
                except Exception as exc:
                    print(f"Dry-run protection error for {user}: {exc}")
        except Exception as exc:
            print(f"Dry-run protection supervisor error: {exc}")
        # Each OPEN state carries its own next-check time. A short scheduler
        # tick keeps configured intervals accurate without polling the market
        # until that individual slot is due.
        time.sleep(1)


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
    threading.Thread(
        target=_dry_run_protection_loop,
        name="dry-run-protection",
        daemon=True,
    ).start()
    app.run(host="0.0.0.0", port=5001, debug=False)
