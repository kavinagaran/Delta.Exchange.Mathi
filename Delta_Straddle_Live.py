"""
Delta_Straddle_Live.py
BTC MOVE Options (MV-BTC) Forecast-Selected LONG/SHORT — Delta Exchange India

Schedule (IST):
  Entry : 5:35 PM IST  = 12:05 UTC
  Exit  : 1:00 AM IST  = 19:30 UTC  (next morning IST, same UTC day)

Guarantees:
  - Portfolio-wide daily loss/trade/open-risk controls shared with Trend
  - Lots capped by configured, affordable, aggregate risk and exchange limits
  - Spread/slippage-bounded IOC entry execution with crash recovery journals
  - Crash-safe state and automatic protection-monitor recovery
"""

import hashlib
import hmac
import json
import logging
import os
import subprocess
import sys
import time
import math
import statistics
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv

from risk_controls import (
    account_entry_lock,
    account_file_lock,
    audit_event,
    decision_dict,
    evaluate_entry,
    load_states,
    risk_based_lots,
    trading_date,
)
from move_decision import (
    LONG_MOVE,
    MANAGE_EXISTING_POSITION,
    NO_TRADE,
    SHORT_MOVE,
    MoveInputError,
    aggregate_risk_lot_caps,
    evaluate_move_decision,
    forecast_move_distribution,
)

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

def _strict_config_bool(value, *, key: str = "boolean setting") -> bool:
    """Parse a persisted safety toggle without silently treating junk as false."""
    if isinstance(value, bool):
        return value
    raw = str(value if value is not None else "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{key} must be true or false")


_ENV_DRY_RUN_DEFAULT = _strict_config_bool(
    os.getenv("DRY_RUN", "false"), key="DRY_RUN")

# ── Account identity & per-user data (must come before strategy config:
#    every os.getenv below may be overridden by this user's config.json) ──
BOT_USER = os.getenv("BOT_USER", os.getenv("DASH_USER", "mathi"))
ACCOUNT_DIR = Path(__file__).parent / "users" / BOT_USER
LIVE_DATA_DIR = ACCOUNT_DIR
DRY_DATA_DIR = ACCOUNT_DIR / "dry_run"
TREND_SIGNAL_SNAPSHOT_FILE = ACCOUNT_DIR / "trend_signal_snapshot.json"
MORNING_SIDEWAYS_SNAPSHOT_MAX_AGE_SEC = 45.0
ACCOUNT_DIR.mkdir(parents=True, exist_ok=True)
DRY_DATA_DIR.mkdir(parents=True, exist_ok=True)

ENV_FILE           = Path(__file__).parent / ".env"
CFG_FILE           = ACCOUNT_DIR / "config.json"
ENTRY_CONFIGURATION_ERRORS: list[str] = []

# One-time migration: these files used to live in the repo root — move them
# into the bot account's LIVE folder if they're still there.  DRY-RUN data is
# deliberately stored under users/<user>/dry_run and is migrated by the
# dashboard's account-storage migration under its cross-process lock.
import shutil as _shutil
for _f in ("straddle_state.json", "morning_state.json", "trade_history.json"):
    _src = Path(__file__).parent / _f
    _dst = LIVE_DATA_DIR / _f
    if _src.exists() and not _dst.exists():
        _shutil.move(str(_src), str(_dst))

# Each bot instance trades ITS user's own credentials (account.json),
# falling back to the .env keys — one `mathi-bot@<user>` service per account.
_account_path = ACCOUNT_DIR / "account.json"
if _account_path.exists():
    try:
        _acct = json.loads(_account_path.read_text(encoding="utf-8"))
        if not isinstance(_acct, dict):
            raise ValueError("account document is not an object")
        if not _acct.get("api_key") or not _acct.get("api_secret"):
            raise ValueError("account API credentials are missing")
        API_KEY = str(_acct["api_key"])
        API_SECRET = str(_acct["api_secret"])
    except (OSError, ValueError, TypeError) as exc:
        # Never fall back to another account's global credentials when this
        # account file exists but is corrupt. Exits will fail visibly instead
        # of mutating the wrong account; all new entries are blocked below.
        API_KEY = API_SECRET = ""
        ENTRY_CONFIGURATION_ERRORS.append(f"invalid account.json: {exc}")

# Per-account strategy config: users/<name>/config.json overrides the .env
# defaults key by key, so every os.getenv below is account-scoped.
_config_document: dict = {}
if CFG_FILE.exists():
    try:
        _config_document = json.loads(CFG_FILE.read_text(encoding="utf-8"))
        if not isinstance(_config_document, dict):
            raise ValueError("config document is not an object")
        for _k, _v in _config_document.items():
            os.environ[str(_k)] = str(_v)
    except (OSError, ValueError, TypeError) as exc:
        _config_document = {}
        ENTRY_CONFIGURATION_ERRORS.append(f"invalid config.json: {exc}")

try:
    PROCESS_DRY_RUN = _strict_config_bool(
        os.getenv("DRY_RUN", str(_ENV_DRY_RUN_DEFAULT)),
        key="DRY_RUN",
    )
except ValueError as exc:
    PROCESS_DRY_RUN = True
    ENTRY_CONFIGURATION_ERRORS.append(str(exc))

# New entries use exactly one mode namespace.  The LIVE namespace remains the
# legacy account root for backward compatibility; paper state can therefore
# never overwrite or block a real slot.
DRY_RUN = PROCESS_DRY_RUN
DATA_DIR = DRY_DATA_DIR if PROCESS_DRY_RUN else LIVE_DATA_DIR
STATE_FILE         = DATA_DIR / "straddle_state.json"
MORNING_STATE_FILE = DATA_DIR / "morning_state.json"
HISTORY_FILE       = DATA_DIR / "trade_history.json"

TAG = BOT_USER.upper()   # identifies this instance in alerts and logs


@contextmanager
def _storage_namespace(dry_run: bool):
    """Temporarily address one mode's files in this single-threaded engine.

    The process mode still governs NEW exposure.  This scoped path switch is
    only for supervising, reconciling, and closing positions that were opened
    before a later config-mode change.
    """
    global DATA_DIR, STATE_FILE, MORNING_STATE_FILE, HISTORY_FILE
    previous = (DATA_DIR, STATE_FILE, MORNING_STATE_FILE, HISTORY_FILE)
    data_dir = DRY_DATA_DIR if dry_run else LIVE_DATA_DIR
    DATA_DIR = data_dir
    STATE_FILE = data_dir / "straddle_state.json"
    MORNING_STATE_FILE = data_dir / "morning_state.json"
    HISTORY_FILE = data_dir / "trade_history.json"
    try:
        yield
    finally:
        DATA_DIR, STATE_FILE, MORNING_STATE_FILE, HISTORY_FILE = previous

# One per-order ceiling for every strategy; risk/affordability/liquidity caps
# normally bind first.  Do not maintain a different hidden Evening ceiling.
# Preserve the historical 1,000-lot ceiling unless an account explicitly
# opts into a larger per-order limit.  A software upgrade must never enlarge
# live exposure merely because a new setting was introduced.
MAX_ORDER_LOTS = int(os.getenv("MAX_ORDER_LOTS", 1000))
_env_lots = int(os.getenv("STRADDLE_LOTS", 1000))
LOTS      = min(_env_lots, MAX_ORDER_LOTS)
assert 1 <= LOTS <= MAX_ORDER_LOTS, f"LOTS must be between 1 and {MAX_ORDER_LOTS}, got {LOTS}"

# Evening slot toggles (defaults on — this is the core trade).
# EVENING_EXIT_ENABLED=false skips only the scheduled exit: the position then
# closes via TP monitor, square-off, or settlement. The exit deliberately does
# NOT also depend on EVENING_ENABLED, so switching off new entries never
# strands an already-open position without its scheduled close.
EVENING_ENABLED      = os.getenv("EVENING_ENABLED", "true").lower() in ("1", "true", "yes")
EVENING_EXIT_ENABLED = os.getenv("EVENING_EXIT_ENABLED", "true").lower() in ("1", "true", "yes")

# Scheduled MOVE direction is selected by the normalized forecast/decision
# engine. Legacy MORNING_SIDE/EVENING_SIDE settings are intentionally ignored;
# discretionary dashboard MOVE entry is no longer part of the strategy.
MOVE_AUTO_ENTRY_MODE = str(
    _config_document.get("MOVE_AUTO_ENTRY_MODE") or "shadow").strip().lower()
if MOVE_AUTO_ENTRY_MODE not in {"disabled", "shadow", "live"}:
    ENTRY_CONFIGURATION_ERRORS.append(
        "MOVE_AUTO_ENTRY_MODE must be disabled, shadow, or live")
    MOVE_AUTO_ENTRY_MODE = "disabled"

# Timing in UTC — configurable via .env / dashboard (defaults: 12:05 entry, 19:30 exit)
ENTRY_H_UTC = int(os.getenv("ENTRY_H_UTC", 12))
ENTRY_M_UTC = int(os.getenv("ENTRY_M_UTC", 5))
EXIT_H_UTC  = int(os.getenv("EXIT_H_UTC",  19))
EXIT_M_UTC  = int(os.getenv("EXIT_M_UTC",  30))
assert 0 <= ENTRY_H_UTC <= 23 and 0 <= ENTRY_M_UTC <= 59, "invalid entry time"
assert 0 <= EXIT_H_UTC  <= 23 and 0 <= EXIT_M_UTC  <= 59, "invalid exit time"

# Execution windows (minutes). Entries fire only inside a 10-min window (a
# stale late entry is worse than no entry); exits fire from their start time
# ONWARD (catch-up), so bot downtime can't strand a position past its exit.
ENTRY_WIN_START = ENTRY_M_UTC
ENTRY_WIN_END   = min(ENTRY_M_UTC + 10, 60)  # 10-min window, capped at hour end
EXIT_WIN_START  = EXIT_M_UTC

POLL_SEC = 30

# Morning trade — buys TODAY's contract (settles 12:00 UTC same day)
MORNING_ENABLED = os.getenv("MORNING_ENABLED", "true").lower() in ("1", "true", "yes")
MORNING_H_UTC   = int(os.getenv("MORNING_H_UTC", 0))     # 00:15 UTC = 5:45 AM IST
MORNING_M_UTC   = int(os.getenv("MORNING_M_UTC", 15))
_morning_lots   = int(os.getenv("MORNING_LOTS", 2000))
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
MORNING_EXIT_WIN_START = MORNING_EXIT_M_UTC   # fires from this time ONWARD (catch-up)

# Account-wide risk and execution rules.  Defaults intentionally fail toward
# smaller/no entries; every value can be overridden per account in config.json.
MAX_TRADES_GLOBAL = int(os.getenv("MAX_TRADES_PER_DAY_GLOBAL",
                                  os.getenv("MAX_TRADES_PER_DAY", "3")))
MAX_OPEN_RISK_USD = float(os.getenv("MAX_OPEN_RISK_USD", "500"))
MAX_DAILY_LOSS_USD = float(os.getenv("MAX_DAILY_LOSS_USD", "500"))
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3"))
LOSS_COOLDOWN_MINUTES = int(os.getenv("LOSS_COOLDOWN_MINUTES", "30"))
RISK_DAY_TZ_OFFSET_MIN = int(os.getenv("RISK_DAY_TZ_OFFSET_MIN", "330"))
RISK_PER_TRADE_MORNING = float(os.getenv("RISK_PER_TRADE_USD_MORNING", "200"))
RISK_PER_TRADE_EVENING = float(os.getenv("RISK_PER_TRADE_USD_EVENING", "200"))
ALLOW_SHORT_MOVE = os.getenv("ALLOW_SHORT_MOVE", "false").lower() in ("1", "true", "yes")
SHORT_MAX_RISK_USD = float(os.getenv("SHORT_MAX_RISK_USD", "0"))

SAFE_EXECUTION_ENABLED = os.getenv("SAFE_EXECUTION_ENABLED", "true").lower() in ("1", "true", "yes")
ALLOW_MARKET_ENTRY_FALLBACK = os.getenv("ALLOW_MARKET_ENTRY_FALLBACK", "false").lower() in ("1", "true", "yes")
MAX_SPREAD_PCT = float(os.getenv("MAX_SPREAD_PCT", "3.0"))
MAX_SLIPPAGE_PCT = float(os.getenv("MAX_SLIPPAGE_PCT", "1.0"))
MIN_BOOK_DEPTH_MULTIPLE = float(os.getenv("MIN_BOOK_DEPTH_MULTIPLE", "1.0"))
MAX_QUOTE_AGE_SEC = int(os.getenv("MAX_QUOTE_AGE_SEC", "20"))
ORDER_CHUNK_LOTS = max(int(os.getenv("ORDER_CHUNK_LOTS", "1000")), 1)

# Keep the fee model configurable and conservative. Actual paid commissions
# from order fills are persisted and supersede this estimate in reporting.
OPTION_FEE_RATE = float(os.getenv("OPTION_FEE_RATE", "0.00010"))
OPTION_FEE_CAP_PCT = float(os.getenv("OPTION_FEE_CAP_PCT", "0.035"))

MOVE_VALUE_FILTER_ENABLED = os.getenv("MOVE_VALUE_FILTER_ENABLED", "true").lower() in ("1", "true", "yes")
MOVE_MIN_EDGE_PCT = float(os.getenv("MOVE_MIN_EDGE_PCT", "5.0"))
MOVE_MIN_TTE_MINUTES = int(os.getenv("MOVE_MIN_TTE_MINUTES", "90"))
MOVE_MAX_TTE_HOURS = float(os.getenv("MOVE_MAX_TTE_HOURS", "30"))
MOVE_VOL_LOOKBACK = max(int(os.getenv("MOVE_VOL_LOOKBACK", "96")), 30)
MAX_CONCURRENT_MOVE_POSITIONS = max(int(os.getenv("MAX_CONCURRENT_MOVE_POSITIONS", "1")), 1)
MOVE_ALLOW_LONG = os.getenv(
    "MOVE_ALLOW_LONG", "true").lower() in ("1", "true", "yes")
MOVE_MIN_LONG_EDGE_ABS_USD = float(os.getenv(
    "MOVE_MIN_LONG_EDGE_ABS_USD", "0.01"))
MOVE_MIN_SHORT_EDGE_ABS_USD = float(os.getenv(
    "MOVE_MIN_SHORT_EDGE_ABS_USD", "0.02"))
MOVE_MIN_LONG_EDGE_PCT = float(os.getenv(
    "MOVE_MIN_LONG_EDGE_PCT", os.getenv("MOVE_MIN_EDGE_PCT", "5.0")))
MOVE_MIN_SHORT_EDGE_PCT = float(os.getenv(
    "MOVE_MIN_SHORT_EDGE_PCT", "10.0"))
MOVE_MAX_MODEL_AGE_SEC = int(os.getenv("MOVE_MAX_MODEL_AGE_SEC", "600"))
MOVE_MIN_BID_SIZE = float(os.getenv("MOVE_MIN_BID_SIZE", "1"))
MOVE_MIN_ASK_SIZE = float(os.getenv("MOVE_MIN_ASK_SIZE", "1"))
MOVE_MAX_JUMP_SCORE_SHORT = float(os.getenv(
    "MOVE_MAX_JUMP_SCORE_SHORT", "0.30"))
MOVE_MAX_LONG_PREMIUM_RISK_USD = float(os.getenv(
    "MOVE_MAX_LONG_PREMIUM_RISK_USD", "1000"))
MOVE_MAX_SHORT_MARGIN_USAGE_PCT = float(os.getenv(
    "MOVE_MAX_SHORT_MARGIN_USAGE_PCT", "30"))
MOVE_MIN_LIQUIDATION_BUFFER_PCT = float(os.getenv(
    "MOVE_MIN_LIQUIDATION_BUFFER_PCT", "50"))
MOVE_NO_ENTRY_BEFORE_SETTLEMENT_SEC = int(os.getenv(
    "MOVE_NO_ENTRY_BEFORE_SETTLEMENT_SEC", "3600"))
MOVE_REQUIRE_NO_OPEN_ORDERS = os.getenv(
    "MOVE_REQUIRE_NO_OPEN_ORDERS", "true").lower() in ("1", "true", "yes")
MOVE_REQUIRE_FLAT = os.getenv(
    "MOVE_REQUIRE_FLAT", "true").lower() in ("1", "true", "yes")
try:
    _configured_move_dry_capital = float(os.getenv(
        "MOVE_DRY_RUN_CAPITAL_USD", "1000"))
except (TypeError, ValueError):
    _configured_move_dry_capital = 0.0
if not math.isclose(_configured_move_dry_capital, 1000.0):
    ENTRY_CONFIGURATION_ERRORS.append(
        "MOVE_DRY_RUN_CAPITAL_USD must remain fixed at 1000")
MOVE_DRY_RUN_CAPITAL_USD = 1000.0
MOVE_FORECAST_LOOKBACK_DAYS = max(int(os.getenv(
    "MOVE_FORECAST_LOOKBACK_DAYS", "30")), 7)
MOVE_FORECAST_OUTER_SCENARIOS = max(int(os.getenv(
    "MOVE_FORECAST_OUTER_SCENARIOS", "32")), 8)
MOVE_FORECAST_PATHS_PER_SCENARIO = max(int(os.getenv(
    "MOVE_FORECAST_PATHS_PER_SCENARIO", "128")), 32)
ALLOW_EXTERNAL_POSITIONS_WITH_BOT = os.getenv(
    "ALLOW_EXTERNAL_POSITIONS_WITH_BOT", "false").lower() in ("1", "true", "yes")

# Telegram alerts
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHATID = os.getenv("TELEGRAM_CHAT_ID",   "")
TELEGRAM_ON     = os.getenv("TELEGRAM_ALERTS", "true").lower() in ("1", "true", "yes")

# ─────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

_fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s",
                         datefmt="%Y-%m-%d %H:%M:%S")
# Per-instance log — multiple bots must never interleave one file
_fh  = TimedRotatingFileHandler(LOG_DIR / f"straddle_{BOT_USER}.log",
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
    last = None
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if 400 <= status < 500:
                raise   # client rejection — an identical retry can never succeed,
                        # and callers need the response body (e.g. order rejection
                        # context) intact to react to it
            last = exc
        except Exception as exc:
            last = exc
        log.warning("Attempt %d/%d — %s: %s", attempt + 1, retries, fn.__name__, last)
        if attempt < retries - 1:
            time.sleep(delay)
    raise RuntimeError(f"All {retries} retries exhausted: {fn.__name__}") from last

# ─────────────────────────────────────────────────────────────
# STATE PERSISTENCE
# ─────────────────────────────────────────────────────────────
def _atomic_json(path: Path, value) -> None:
    if path.exists():
        raw = path.read_text(encoding="utf-8")
        try:
            json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise RuntimeError(f"refusing to overwrite corrupt JSON state: {path}") from exc
        backup = path.with_suffix(path.suffix + ".bak")
        backup_tmp = backup.with_suffix(backup.suffix + ".tmp")
        backup_tmp.write_text(raw, encoding="utf-8")
        os.replace(backup_tmp, backup)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _state_flag(value) -> bool:
    return value is True or str(value or "").strip().lower() in {
        "1", "true", "yes", "on",
    }


class StateNamespaceMismatch(RuntimeError):
    """A paper state is in REAL storage, or vice versa."""


def _dry_namespace(path: Path) -> bool:
    """Storage identity, independent of the process's current config mode."""
    return Path(path).parent.name.lower() == "dry_run"


def _validate_state_namespace(state: dict, path: Path) -> dict:
    """Reject a state whose recorded mode disagrees with its directory.

    Missing ``dry_run`` remains a backward-compatible REAL record.  A legacy
    paper record left in the real account root must fail closed so it can
    never be monitored, reconciled, or closed as exchange exposure.
    """
    if not isinstance(state, dict):
        raise ValueError("state is not an object")
    expected_dry = _dry_namespace(path)
    actual_dry = _state_flag(state.get("dry_run", False))
    raw_mode = str(state.get("execution_mode") or "").strip().lower()
    if raw_mode:
        if raw_mode not in {"real", "live", "dry_run", "dry-run", "paper"}:
            raise RuntimeError(f"state has invalid execution_mode: {raw_mode}")
        mode_dry = raw_mode in {"dry_run", "dry-run", "paper"}
        if mode_dry != actual_dry:
            raise StateNamespaceMismatch(
                "state dry_run and execution_mode markers disagree")
    if actual_dry != expected_dry:
        location = "DRY-RUN" if expected_dry else "REAL"
        recorded = "DRY-RUN" if actual_dry else "REAL"
        raise StateNamespaceMismatch(
            f"{recorded} state is stored in the {location} namespace: {path}")
    return state


def save_state(state: dict):
    _validate_state_namespace(state, STATE_FILE)
    _atomic_json(STATE_FILE, state)
    log.info("State saved → %s", STATE_FILE.name)

def load_state() -> dict | None:
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return _validate_state_namespace(state, STATE_FILE)
        except StateNamespaceMismatch as exc:
            log.critical("%s; treating it as non-current until migration", exc)
            return None
        except (OSError, ValueError, TypeError) as exc:
            backup = STATE_FILE.with_suffix(STATE_FILE.suffix + ".bak")
            try:
                state = json.loads(backup.read_text(encoding="utf-8"))
                _validate_state_namespace(state, STATE_FILE)
                log.critical("Primary state corrupt (%s); using validated backup %s", exc, backup)
                return state
            except (OSError, ValueError, TypeError):
                raise RuntimeError(f"state and backup are unreadable: {STATE_FILE}") from exc
    return None

def clear_state():
    STATE_FILE.unlink(missing_ok=True)

def save_morning_state(state: dict):
    _validate_state_namespace(state, MORNING_STATE_FILE)
    _atomic_json(MORNING_STATE_FILE, state)
    log.info("State saved → %s", MORNING_STATE_FILE.name)

def load_morning_state() -> dict | None:
    if MORNING_STATE_FILE.exists():
        try:
            state = json.loads(MORNING_STATE_FILE.read_text(encoding="utf-8"))
            return _validate_state_namespace(state, MORNING_STATE_FILE)
        except StateNamespaceMismatch as exc:
            log.critical("%s; treating it as non-current until migration", exc)
            return None
        except (OSError, ValueError, TypeError) as exc:
            backup = MORNING_STATE_FILE.with_suffix(MORNING_STATE_FILE.suffix + ".bak")
            try:
                state = json.loads(backup.read_text(encoding="utf-8"))
                _validate_state_namespace(state, MORNING_STATE_FILE)
                log.critical("Primary morning state corrupt (%s); using validated backup %s", exc, backup)
                return state
            except (OSError, ValueError, TypeError):
                raise RuntimeError(f"state and backup are unreadable: {MORNING_STATE_FILE}") from exc
    return None

_MOVE_HISTORY_ACCOUNTING_FIELDS = {
    "exit_mark", "pnl_usd", "gross_pnl_usd", "fees_usd",
    "entry_fee_usd", "exit_fee_usd", "pnl_includes_fees",
    "fees_complete", "fees_available", "fees_estimated",
    "entry_fee_source", "exit_fee_source", "exit_price_source",
    "exit_order_id", "exit_client_order_id", "exit_time",
    "exit_reconciliation_status", "accounting_status",
}


def _move_bool(value) -> bool:
    return value is True or str(value or "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _move_finite_number(record: dict, key: str) -> bool:
    value = record.get(key)
    if value is None or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _move_external_accounting_required(record: dict) -> bool:
    if _move_bool(record.get("dry_run")):
        return False
    trigger = str(record.get("exit_trigger") or "").strip().lower()
    return (
        "external" in trigger
        or trigger in {"exchange_already_flat", "reconciled_stale"}
        or str(record.get("exit_price_source") or "").lower() == "mark_after_exchange_flat"
        or str(record.get("exit_reconciliation_status") or "").startswith("pending")
        or str(record.get("accounting_status") or "").lower() == "pending"
    )


def _move_history_accounting_complete(record: dict) -> bool:
    """Whether a MOVE history row contains final realised accounting."""
    if not isinstance(record, dict):
        return False
    if not (_move_finite_number(record, "exit_mark")
            and _move_finite_number(record, "pnl_usd")):
        return False
    if not _move_external_accounting_required(record):
        # Legacy and dry-run MOVE rows predate detailed fee fields.
        return True
    if str(record.get("exit_price_source") or "").lower() == "mark_after_exchange_flat":
        return False
    if not _move_bool(record.get("pnl_includes_fees")):
        return False
    return all(_move_finite_number(record, key) for key in (
        "gross_pnl_usd", "fees_usd", "entry_fee_usd", "exit_fee_usd",
    ))


def _move_history_authority(record: dict) -> int:
    """Rank partial rows so a retry cannot erase stronger exchange evidence."""
    if _move_history_accounting_complete(record):
        return 10_000
    score = 0
    if record.get("exit_order_id") not in (None, ""):
        score += 100
    if record.get("exit_client_order_id") not in (None, ""):
        score += 50
    if _move_bool(record.get("pnl_includes_fees")):
        score += 25
    if str(record.get("exit_fee_source") or "").lower() == "exchange":
        score += 20
    if str(record.get("entry_fee_source") or "").lower() == "exchange":
        score += 20
    score += sum(1 for key in (
        "exit_mark", "pnl_usd", "gross_pnl_usd", "fees_usd",
        "entry_fee_usd", "exit_fee_usd",
    ) if _move_finite_number(record, key))
    if str(record.get("exit_price_source") or "").lower() == "mark_after_exchange_flat":
        score -= 50
    return score


def _move_first_present(state: dict, *keys):
    for key in keys:
        value = state.get(key)
        if value not in (None, ""):
            return value
    return None


def log_trade(state: dict) -> bool:
    """Safely upsert a MOVE trade; return true only for final accounting."""
    # HISTORY_FILE shares the active mode directory with both slot states.
    # Key validation to that directory so isolated history writers/tests do
    # not depend on an unrelated module-level slot path.
    _validate_state_namespace(state, HISTORY_FILE)
    entry_fee = _move_first_present(
        state, "entry_fee_usd", "entry_commission_usd", "entry_fees_usd",
    )
    exit_fee = _move_first_present(
        state, "exit_fee_usd", "exit_commission_usd", "exit_fees_usd",
    )
    fees_complete = state.get("fees_complete")
    if fees_complete in (None, ""):
        fees_complete = (
            _move_bool(state.get("pnl_includes_fees"))
            and entry_fee is not None and exit_fee is not None
            and state.get("fees_usd") is not None
        )
    record = {
        "slot":         state.get("slot", "evening"),
        "date":         state.get("entry_date", ""),
        "entry_date":   state.get("entry_date", ""),
        "trading_date": state.get("trading_date", state.get("entry_date", "")),
        "symbol":       state.get("symbol", ""),
        "strike":       state.get("strike", 0),
        "lots":         state.get("owned_entry_lots") or state.get("entry_lots")
                        or state.get("lots", 0),
        "exit_lots":    state.get("closed_lots") or state.get("lots", 0),
        "entry_mark":   state.get("entry_mark", 0),
        # Unknown realised fields stay JSON null.  Zero is a valid result, not
        # an acceptable placeholder for a close whose fill is unresolved.
        "exit_mark":    state.get("exit_mark"),
        "btc_entry":    state.get("btc_at_entry", 0),
        "btc_exit":     state.get("btc_at_exit", 0),
        "btc_move_pct": state.get("btc_move_pct", 0),
        "pnl_usd":      state.get("pnl_usd"),
        "gross_pnl_usd": state.get("gross_pnl_usd"),
        "fees_usd":     state.get("fees_usd"),
        "entry_fee_usd": entry_fee,
        "exit_fee_usd": exit_fee,
        "entry_fee_source": state.get("entry_fee_source"),
        "exit_fee_source": state.get("exit_fee_source"),
        "fees_available": _move_bool(state.get("fees_available")),
        "fees_complete": _move_bool(fees_complete),
        "fees_estimated": _move_bool(state.get("fees_estimated")),
        "pnl_includes_fees": _move_bool(state.get("pnl_includes_fees")),
        "cost_usd":     state.get("total_cost_usd", 0),
        "entry_time":   state.get("entry_time_utc", ""),
        "exit_time":    state.get("exit_time_utc", ""),
        "dry_run":      state.get("dry_run", False),
        "execution_mode": ("dry_run" if _move_bool(state.get("dry_run"))
                           else "real"),
        "side":         state.get("side", "long"),
        "order_id":     state.get("order_id"),
        "order_ids":    state.get("order_ids", []),
        "client_order_id": state.get("client_order_id"),
        "client_order_ids": state.get("client_order_ids", []),
        "exit_order_id": state.get("exit_order_id"),
        "exit_client_order_id": state.get("exit_client_order_id"),
        "exit_price_source": state.get("exit_price_source"),
        "exit_reconciliation_status": state.get("exit_reconciliation_status"),
        "entry_trigger": state.get("entry_trigger"),
        "exit_trigger": state.get("exit_trigger"),
        "risk_at_entry_usd": state.get("risk_at_entry_usd"),
        "move_value_signal": state.get("move_value_signal"),
        "execution_snapshot": state.get("execution_snapshot"),
    }
    # The old exchange-flat path valued the exit at a later ticker mark.  That
    # is not a realised fill and its zero/derived P&L must not become durable.
    if (str(record.get("exit_price_source") or "").lower()
            == "mark_after_exchange_flat"):
        for key in (
            "exit_mark", "pnl_usd", "gross_pnl_usd", "fees_usd",
            "exit_fee_usd",
        ):
            record[key] = None
        record.update({
            "pnl_includes_fees": False,
            "fees_complete": False,
            "fees_available": False,
        })
    record["accounting_status"] = (
        "complete" if _move_history_accounting_complete(record) else "pending"
    )
    owner = f"move-history-{state.get('slot', 'unknown')}"
    with account_file_lock(DATA_DIR, "history", owner, stale_after_sec=30,
                           wait_sec=5) as acquired:
        if not acquired:
            raise RuntimeError("trade-history lock unavailable; refusing an unsafe overwrite")
        history = []
        if HISTORY_FILE.exists():
            try:
                history = json.loads(HISTORY_FILE.read_text())
            except (OSError, ValueError, TypeError) as exc:
                raise RuntimeError(
                    f"existing trade history is unreadable; refusing to overwrite {HISTORY_FILE}") from exc
            if not isinstance(history, list) or any(not isinstance(row, dict) for row in history):
                raise RuntimeError(
                    f"existing trade history has invalid schema; refusing to overwrite {HISTORY_FILE}")
        # Dedupe on date+symbol+entry_time so multiple trades per day are kept,
        # but never replace a complete or more authoritative row with defaults.
        duplicate_index = next((
            index for index, row in enumerate(history)
            if (row.get("entry_date") or row.get("date")) == record["date"]
            and row.get("symbol") == record["symbol"]
            and (row.get("entry_time") or row.get("entry_time_utc")) == record["entry_time"]
        ), None)
        if duplicate_index is None:
            history.append(record)
            stored = record
        else:
            existing = history[duplicate_index]
            if _move_history_accounting_complete(existing):
                stored = existing
            else:
                existing_authority = _move_history_authority(existing)
                incoming_authority = _move_history_authority(record)
                stored = dict(existing)
                for key, value in record.items():
                    if value is None:
                        continue
                    if (key in _MOVE_HISTORY_ACCOUNTING_FIELDS
                            and existing.get(key) is not None
                            and existing_authority >= incoming_authority):
                        continue
                    stored[key] = value
                stored["accounting_status"] = (
                    "complete" if _move_history_accounting_complete(stored) else "pending"
                )
                history[duplicate_index] = stored
        history.sort(key=lambda r: (
            str(r.get("date") or r.get("entry_date") or ""),
            str(r.get("entry_time") or r.get("entry_time_utc") or ""),
        ))
        _atomic_json(HISTORY_FILE, history)
    if _move_finite_number(stored, "pnl_usd"):
        log.info("Trade logged → %s  P&L=$%.2f", record["date"], float(stored["pnl_usd"]))
    else:
        log.warning("Trade history pending → %s  realised P&L is unresolved", record["date"])
    return _move_history_accounting_complete(stored)


def _flush_pending_move_history() -> bool:
    """Drain CLOSED-state history outboxes without ever overwriting them."""
    complete = True
    for slot, load_fn, save_fn in (
        ("morning", load_morning_state, save_morning_state),
        ("evening", load_state, save_state),
    ):
        state = load_fn() or {}
        if not state.get("history_pending"):
            continue
        try:
            final = log_trade(state)
            if final:
                state["history_pending"] = False
                state["history_logged"] = True
                state["history_logged_at_utc"] = datetime.now(timezone.utc).isoformat()
                save_fn(state)
            else:
                complete = False
                log.warning("%s history outbox remains pending: realised accounting incomplete", slot)
        except Exception as exc:
            complete = False
            log.error("%s history outbox remains pending: %s", slot, exc)
    return complete

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

    if state.get("history_pending"):
        if not _flush_pending_move_history():
            log.error("Previous evening trade history is not durable — new entry blocked.")
            return True
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
        if _move_bool(state.get("dry_run")):
            log.info("Stale DRY-RUN evening state reached settlement; valuing it from public ticker.")
            _close_dry_run_position_job(
                state, save_state, load_state, "EVENING",
                "settlement_simulated",
            )
            state = load_state() or {}
            if state.get("history_pending"):
                return True
        else:
            log.info("Stale OPEN state — position settled/closed externally. Marking CLOSED.")
            state["status"]       = "CLOSED"
            state["exit_trigger"] = "settlement_or_external"
            state["history_pending"] = True
            save_state(state)
            if not _flush_pending_move_history():
                return True

    # Count today's completed trades (history uses 'date' from the bot,
    # 'entry_date' from dashboard square-offs)
    trades = []
    if HISTORY_FILE.exists():
        try:
            trades = json.loads(HISTORY_FILE.read_text())
        except (OSError, ValueError, TypeError) as exc:
            raise RuntimeError("trade history is unreadable; entry blocked") from exc
        if not isinstance(trades, list) or any(not isinstance(row, dict) for row in trades):
            raise RuntimeError("trade history schema is invalid; entry blocked")
    count = sum(1 for t in trades
                if (t.get("entry_date") or t.get("date", "")) == today
                and _move_bool(t.get("dry_run")) == bool(DRY_RUN))

    # Include state's trade if it closed today but never reached history
    # (e.g. a manual position that settled on the exchange)
    if state.get("entry_date") == today and state.get("status") != "OPEN":
        in_history = any(
            (t.get("entry_date") or t.get("date", "")) == today
            and t.get("symbol") == state.get("symbol")
            and _move_bool(t.get("dry_run")) == bool(DRY_RUN)
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
    Never substitutes a different settlement when tomorrow's target is absent;
    silently changing the volatility horizon changes the strategy itself.
    """
    now      = datetime.now(timezone.utc)
    tmrw_str = (now.date() + timedelta(days=1)).strftime("%Y-%m-%d")

    contract = get_mv_contract(tmrw_str)
    if contract:
        log.info("MV contract (tomorrow): %s  id=%s  strike=%s  settles=%s",
                 contract["symbol"], contract["id"],
                 contract.get("strike_price"), contract.get("settlement_time"))
        return contract

    raise LookupError(f"No live BTC MV contract found for target settlement {tmrw_str}.")

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


def get_execution_snapshot(symbol: str, side: str) -> dict:
    """Return a validated top-of-book snapshot and executable IOC depth.

    Entries are priced from the side we actually consume (ask for buys, bid
    for sells), never from mark.  Executable depth only sizes each LIVE IOC
    chunk inside the configured slippage envelope; it does not reduce the
    strategy's planned total lots.
    """
    ticker = _retry(_get, f"/v2/tickers/{symbol}").get("result") or {}
    book = _retry(_get, f"/v2/l2orderbook/{symbol}").get("result") or {}
    quotes = ticker.get("quotes") or {}
    try:
        bid = float(quotes.get("best_bid") or (book.get("buy") or [{}])[0].get("price") or 0)
        ask = float(quotes.get("best_ask") or (book.get("sell") or [{}])[0].get("price") or 0)
    except (TypeError, ValueError, IndexError):
        bid = ask = 0.0
    if bid <= 0 or ask <= 0 or ask < bid:
        raise RuntimeError(f"invalid order book for {symbol}: bid={bid}, ask={ask}")
    mid = (bid + ask) / 2
    spread_pct = (ask - bid) / mid * 100
    if spread_pct > MAX_SPREAD_PCT:
        raise RuntimeError(
            f"spread gate blocked {symbol}: {spread_pct:.2f}% > {MAX_SPREAD_PCT:.2f}%")

    # A two-sided price without a trustworthy timestamp is not a verified
    # executable quote. Delta has exposed both numeric ``timestamp`` (seconds
    # or milliseconds) and ISO ``time`` fields, so accept either but fail
    # closed when neither can be parsed.
    try:
        raw_timestamp = float(ticker.get("timestamp") or 0)
    except (TypeError, ValueError):
        raw_timestamp = 0.0
    while raw_timestamp > 100_000_000_000:
        raw_timestamp /= 1000.0
    if raw_timestamp > 0:
        quoted_at = datetime.fromtimestamp(raw_timestamp, timezone.utc)
    else:
        quote_time = str(ticker.get("time") or "")
        try:
            quoted_at = datetime.fromisoformat(quote_time.replace("Z", "+00:00"))
            if quoted_at.tzinfo is None:
                quoted_at = quoted_at.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            raise RuntimeError(f"quote timestamp unavailable for {symbol}")
    age = (datetime.now(timezone.utc) - quoted_at.astimezone(timezone.utc)).total_seconds()
    if age < -MAX_QUOTE_AGE_SEC:
        raise RuntimeError(f"quote timestamp is {abs(age):.1f}s in the future for {symbol}")
    if age > MAX_QUOTE_AGE_SEC:
        raise RuntimeError(f"stale quote for {symbol}: {age:.1f}s old")

    levels = book.get("sell" if side == "buy" else "buy") or []
    best = ask if side == "buy" else bid
    limit = (best * (1 + MAX_SLIPPAGE_PCT / 100)
             if side == "buy" else best * (1 - MAX_SLIPPAGE_PCT / 100))
    depth = 0
    for level in levels:
        try:
            price, size = float(level["price"]), int(float(level["size"]))
        except (KeyError, TypeError, ValueError):
            continue
        inside = price <= limit if side == "buy" else price >= limit
        if inside:
            depth += max(size, 0)
    liquidity_cap = int(depth / max(MIN_BOOK_DEPTH_MULTIPLE, 0.01))
    if liquidity_cap < 1:
        raise RuntimeError(f"no executable depth inside slippage gate for {symbol}")
    return {
        "bid": bid, "ask": ask, "mid": mid, "spread_pct": spread_pct,
        "limit_price": limit, "liquidity_cap": liquidity_cap,
        "quote_age_sec": age, "mark": float(ticker.get("mark_price") or mid),
        "spot": float(ticker.get("spot_price") or 0),
        "bid_size": int(float(quotes.get("bid_size") or 0)),
        "ask_size": int(float(quotes.get("ask_size") or 0)),
        "tick_size": float(ticker.get("tick_size") or 0.1),
        "quote_timestamp_ms": int(quoted_at.timestamp() * 1000),
    }


_MOVE_CANDLE_CACHE: dict = {}


def _ticker_timestamp_ms(ticker: dict) -> int:
    try:
        raw = float(ticker.get("timestamp") or 0)
    except (TypeError, ValueError, OverflowError):
        raw = 0.0
    while raw and raw < 100_000_000_000:
        raw *= 1000
    while raw > 100_000_000_000_000:
        raw /= 1000
    if raw > 0:
        return int(raw)
    text = str(ticker.get("time") or "")
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return int(stamp.timestamp() * 1000)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("MOVE quote timestamp is unavailable") from exc


def get_move_decision_market(contract: dict) -> dict:
    """Fetch one side-neutral top-of-book snapshot for AUTO direction."""
    symbol = str(contract.get("symbol") or "")
    ticker = _retry(_get, f"/v2/tickers/{symbol}").get("result") or {}
    quotes = ticker.get("quotes") or {}
    try:
        bid = float(quotes.get("best_bid") or 0)
        ask = float(quotes.get("best_ask") or 0)
        bid_size = float(quotes.get("bid_size") or 0)
        ask_size = float(quotes.get("ask_size") or 0)
        mark = float(ticker.get("mark_price") or 0)
        spot = float(ticker.get("spot_price") or 0)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError("MOVE ticker contains invalid numeric fields") from exc
    if bid <= 0 or ask <= 0 or ask < bid or spot <= 0:
        raise RuntimeError(
            f"MOVE decision requires a fresh two-sided market: "
            f"bid={bid}, ask={ask}, spot={spot}")
    return {
        "bid": bid,
        "ask": ask,
        "bid_size": max(bid_size, 0),
        "ask_size": max(ask_size, 0),
        "quote_timestamp_ms": _ticker_timestamp_ms(ticker),
        "mark_price": mark if mark > 0 else (bid + ask) / 2,
        "spot_price": spot,
        "trading_status": str(
            ticker.get("trading_status")
            or contract.get("trading_status")
            or ""),
    }


def _fetch_move_index_candles(
    index_symbol: str,
    now: datetime,
) -> list[dict]:
    """Fetch completed 5m index history in bounded chunks and cache one bar."""
    end = int(now.timestamp())
    completed_end = end - end % 300
    key = (index_symbol, completed_end, MOVE_FORECAST_LOOKBACK_DAYS)
    cached = _MOVE_CANDLE_CACHE.get(key)
    if isinstance(cached, list):
        return cached
    start = completed_end - (MOVE_FORECAST_LOOKBACK_DAYS + 1) * 86_400
    rows: dict[int, dict] = {}
    cursor = start
    chunk_seconds = 6 * 86_400
    while cursor < completed_end:
        chunk_end = min(cursor + chunk_seconds, completed_end)
        response = _retry(
            _get,
            "/v2/history/candles",
            params={
                "resolution": "5m",
                "symbol": index_symbol,
                "start": cursor,
                "end": chunk_end,
            },
        )
        batch = response.get("result")
        if not isinstance(batch, list):
            raise RuntimeError("BTC index candle response is invalid")
        for row in batch:
            if not isinstance(row, dict):
                continue
            try:
                timestamp = int(float(row.get("time") or 0))
            except (TypeError, ValueError, OverflowError):
                continue
            if timestamp > 0:
                rows[timestamp] = row
        cursor = chunk_end
    ordered = [rows[timestamp] for timestamp in sorted(rows)]
    if not ordered:
        raise RuntimeError("BTC index candle history is empty")
    _MOVE_CANDLE_CACHE.clear()
    _MOVE_CANDLE_CACHE[key] = ordered
    return ordered


def _current_settlement_twap(
    index_symbol: str,
    *,
    now: datetime,
    settlement: datetime,
) -> tuple[float, float]:
    """Return the completed portion of Delta's final 30-minute index TWAP."""
    window_start = settlement - timedelta(minutes=30)
    if now <= window_start:
        return 0.0, 0.0
    end = min(now, settlement)
    response = _retry(
        _get,
        "/v2/history/candles",
        params={
            "resolution": "1m",
            "symbol": index_symbol,
            "start": int(window_start.timestamp()),
            "end": int(end.timestamp()),
        },
    )
    rows = response.get("result")
    if not isinstance(rows, list):
        raise RuntimeError("settlement TWAP index response is invalid")
    completed_before = int(end.timestamp()) - int(end.timestamp()) % 60
    closes = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            timestamp = int(float(row.get("time") or 0))
            close = float(row.get("close") or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if (timestamp < int(window_start.timestamp())
                or timestamp >= completed_before
                or timestamp in seen or close <= 0):
            continue
        seen.add(timestamp)
        closes.append(close)
    fixed_minutes = min(len(closes), 30)
    if fixed_minutes <= 0:
        return 0.0, 0.0
    return statistics.mean(closes), fixed_minutes / 30.0


def _move_account_decision_snapshot(contract: dict) -> dict:
    """Account inputs for one AUTO cycle; paper capital never reads LIVE funds."""
    if DRY_RUN:
        open_states = [
            state for name, state in load_states(DATA_DIR).items()
            if name in {"morning", "evening"}
            and str(state.get("status") or "").upper() == "OPEN"
            and _move_bool(state.get("dry_run"))
        ]
        position = sum(
            (-1 if state.get("side") == "short" else 1)
            * int(state.get("lots") or 0)
            for state in open_states
        )
        average = (
            float(open_states[0].get("entry_mark") or 0)
            if len(open_states) == 1 else 0.0)
        return {
            "current_position_qty": position,
            "average_entry_price": average,
            "available_margin": MOVE_DRY_RUN_CAPITAL_USD,
            "account_equity": MOVE_DRY_RUN_CAPITAL_USD,
            "liquidation_buffer": 1.0,
            "open_orders_count": 0,
            "capital_source": "isolated_virtual_capital",
        }

    positions_response = _retry(
        _get, "/v2/positions/margined", auth=True)
    positions = positions_response.get("result")
    if not positions_response.get("success") or not isinstance(positions, list):
        raise RuntimeError("account positions are unavailable for MOVE decision")
    move_positions = [
        position for position in positions
        if str(position.get("product_symbol") or "").startswith("MV-BTC-")
        and float(position.get("size") or 0) != 0
    ]
    position_qty = sum(float(position.get("size") or 0)
                       for position in move_positions)
    average_entry = (
        float(move_positions[0].get("entry_price") or 0)
        if len(move_positions) == 1 else 0.0)

    wallet_response = _retry(_get, "/v2/wallet/balances", auth=True)
    wallets = wallet_response.get("result")
    if not wallet_response.get("success") or not isinstance(wallets, list):
        raise RuntimeError("wallet is unavailable for MOVE decision")
    wallet = next(
        (row for row in wallets if row.get("asset_symbol") == "USD"), None)
    if not isinstance(wallet, dict):
        raise RuntimeError("USD wallet is unavailable for MOVE decision")
    available = float(wallet.get("available_balance") or 0)
    equity = float(
        wallet.get("balance")
        or wallet.get("asset_balance")
        or available
        or 0
    )
    liquidation_buffer = (
        max(min(available / equity, 1.0), 0.0) if equity > 0 else 0.0)

    orders_response = _retry(
        _get,
        "/v2/orders",
        params={"states": "open", "page_size": 100},
        auth=True,
    )
    orders = orders_response.get("result")
    if not orders_response.get("success") or not isinstance(orders, list):
        raise RuntimeError("open orders are unavailable for MOVE decision")
    return {
        "current_position_qty": position_qty,
        "average_entry_price": average_entry,
        "available_margin": max(available, 0.0),
        "account_equity": max(equity, 0.0),
        "liquidation_buffer": liquidation_buffer,
        "open_orders_count": len(orders),
        "capital_source": "verified_exchange_wallet",
    }


def _move_strategy_config(
    slot: str,
    contract: dict,
    configured_lots: int,
) -> dict:
    risk_budget, _ = _slot_risk(slot)
    long_risk = min(
        max(MOVE_MAX_LONG_PREMIUM_RISK_USD, 0),
        max(risk_budget, 0),
        MOVE_DRY_RUN_CAPITAL_USD if DRY_RUN else float("inf"),
    )
    short_risk = (
        min(max(SHORT_MAX_RISK_USD, 0), max(risk_budget, 0))
        if SHORT_MAX_RISK_USD > 0 else 0.0)
    product_limit = max(int(float(
        contract.get("position_size_limit") or MAX_ORDER_LOTS)), 1)
    return {
        "allow_long": MOVE_ALLOW_LONG,
        "allow_short": ALLOW_SHORT_MOVE,
        "min_long_edge_absolute": max(MOVE_MIN_LONG_EDGE_ABS_USD, 0),
        "min_short_edge_absolute": max(MOVE_MIN_SHORT_EDGE_ABS_USD, 0),
        "min_long_edge_pct": max(MOVE_MIN_LONG_EDGE_PCT, 0) / 100,
        "min_short_edge_pct": max(MOVE_MIN_SHORT_EDGE_PCT, 0) / 100,
        "max_spread_pct": max(MAX_SPREAD_PCT, 0) / 100,
        "max_quote_age_ms": max(MAX_QUOTE_AGE_SEC, 0) * 1000,
        "max_model_age_ms": max(MOVE_MAX_MODEL_AGE_SEC, 0) * 1000,
        "min_bid_size": max(MOVE_MIN_BID_SIZE, 0),
        "min_ask_size": max(MOVE_MIN_ASK_SIZE, 0),
        "max_jump_event_score_for_short": max(
            min(MOVE_MAX_JUMP_SCORE_SHORT, 1), 0),
        "max_long_premium_risk": long_risk,
        "max_short_p99_loss": short_risk,
        "max_short_margin_usage": max(
            min(MOVE_MAX_SHORT_MARGIN_USAGE_PCT / 100, 1), 0),
        "max_contracts": min(max(configured_lots, 1), MAX_ORDER_LOTS),
        "max_total_position": min(product_limit, MAX_ORDER_LOTS),
        "no_new_entry_seconds_before_settlement": max(
            MOVE_NO_ENTRY_BEFORE_SETTLEMENT_SEC,
            MOVE_MIN_TTE_MINUTES * 60,
        ),
        "max_open_loss": max(risk_budget, 0),
        "require_no_existing_orders": MOVE_REQUIRE_NO_OPEN_ORDERS,
        "require_flat_before_entry": MOVE_REQUIRE_FLAT,
        "required_liquidation_buffer": max(
            min(MOVE_MIN_LIQUIDATION_BUFFER_PCT / 100, 1), 0),
    }


def _move_decision_path(slot: str) -> Path:
    if slot not in {"morning", "evening"}:
        raise ValueError("MOVE decision slot must be morning or evening")
    return DATA_DIR / f"move_decision_{slot}.json"


def _load_morning_sideways_signal(
    *,
    now: datetime | None = None,
) -> dict:
    """Read the dashboard-published MTF signal without recalculating it.

    The published snapshot is the source of truth for what the operator sees:
    completed 5M, completed 15M and live/debounced 1H. Missing, malformed,
    future-dated or stale data never authorises a short.
    """
    checked_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    unavailable = {
        "available": False,
        "all_sideways": False,
        "checked_at_utc": checked_at.isoformat(),
    }
    try:
        document = json.loads(
            TREND_SIGNAL_SNAPSHOT_FILE.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("snapshot is not an object")
        observed_raw = str(document.get("observed_at_utc") or "")
        observed_at = datetime.fromisoformat(
            observed_raw.replace("Z", "+00:00"))
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)
        observed_at = observed_at.astimezone(timezone.utc)
        age = (checked_at - observed_at).total_seconds()
        if age < -5:
            raise ValueError("snapshot timestamp is in the future")
        if age > MORNING_SIDEWAYS_SNAPSHOT_MAX_AGE_SEC:
            raise ValueError(
                f"snapshot is stale ({age:.1f}s > "
                f"{MORNING_SIDEWAYS_SNAPSHOT_MAX_AGE_SEC:.0f}s)")

        source_frames = document.get("timeframes")
        if not isinstance(source_frames, dict):
            raise ValueError("snapshot timeframes are unavailable")
        expected_live = {"5m": False, "15m": False, "1h": True}
        frames = {}
        for timeframe, live_candle in expected_live.items():
            row = source_frames.get(timeframe)
            if not isinstance(row, dict):
                raise ValueError(f"{timeframe} snapshot is unavailable")
            trend = str(row.get("trend") or "").strip().lower()
            if trend not in {"up", "down", "neutral"}:
                raise ValueError(f"{timeframe} trend is invalid")
            if bool(row.get("live_candle")) is not live_candle:
                raise ValueError(f"{timeframe} candle policy is invalid")
            if row.get("candle_time") in (None, ""):
                raise ValueError(f"{timeframe} candle time is unavailable")
            frames[timeframe] = {
                "trend": trend,
                "display": "sideways" if trend == "neutral" else trend,
                "candle_time": row.get("candle_time"),
                "live_candle": live_candle,
                "unfiltered_trend": row.get("unfiltered_trend"),
                "debounce_pending": bool(row.get("debounce_pending")),
            }
        return {
            "available": True,
            "all_sideways": all(
                row["trend"] == "neutral" for row in frames.values()),
            "observed_at_utc": observed_at.isoformat(),
            "checked_at_utc": checked_at.isoformat(),
            "age_seconds": round(max(age, 0), 3),
            "timeframes": frames,
        }
    except Exception as exc:
        return {**unavailable, "reason": str(exc)}


def _apply_morning_sideways_short_decision(
    decision: dict,
    signal: dict,
) -> tuple[dict, dict | None]:
    """Give the all-SIDEWAYS Morning rule priority over value/event gates.

    Only ``edge`` and ``jump_event_risk`` are replaced by this explicit
    strategy signal. Common exchange gates and the independent short-side
    permission, liquidity, p99, margin and liquidation gates must still pass.
    """
    if not signal.get("available") or not signal.get("all_sideways"):
        return decision, None

    updated = json.loads(json.dumps(decision))
    original = json.loads(json.dumps(decision))
    short_gates = updated.get("gates", {}).get("short", {})
    required_short_gates = (
        "allowed",
        "bid_liquidity",
        "p99_risk_per_contract",
        "available_margin",
        "liquidation_buffer",
    )
    blockers = []
    if updated.get("common_passed") is not True:
        blockers.append("common")
    if (original.get("action") == MANAGE_EXISTING_POSITION
            or float((original.get("metrics") or {}).get(
                "current_position_qty") or 0) != 0):
        blockers.append("existing_position")
    blockers.extend(
        gate for gate in required_short_gates
        if short_gates.get(gate) is not True
    )
    override = {
        "kind": "morning_all_sideways_short",
        "applied": not blockers,
        "signal_observed_at_utc": signal.get("observed_at_utc"),
        "timeframes": signal.get("timeframes", {}),
        "replaced_gates": ["edge", "jump_event_risk"],
        "preserved_safety_blockers": blockers,
        "original_action": original.get("action"),
        "original_side": original.get("side"),
        "original_failed_gates": original.get("failed_gates"),
    }
    if blockers:
        # All-SIDEWAYS owns the Morning direction choice. If its SHORT cannot
        # pass the preserved safety gates, do not fall back to a forecast LONG.
        updated.update({
            "action": NO_TRADE,
            "side": None,
            "long_signal": False,
            "short_signal": False,
            "conflict": False,
            "strategy_override": override,
        })
        return updated, override

    failed = updated.setdefault("failed_gates", {})
    failed["short"] = [
        name for name in failed.get("short", [])
        if name not in {"edge", "jump_event_risk"}
    ]
    updated.update({
        "action": SHORT_MOVE,
        "side": "sell",
        "long_signal": False,
        "short_signal": True,
        "conflict": False,
        "strategy_override": override,
    })
    return updated, override


def _persist_move_decision(slot: str, context: dict) -> None:
    _atomic_json(_move_decision_path(slot), context)
    decision = context.get("decision") or {}
    audit_event(DATA_DIR, "move_auto_decision", {
        "slot": slot,
        "decision_id": context.get("decision_id"),
        "action": decision.get("action"),
        "side": decision.get("side"),
        "failed_gates": decision.get("failed_gates"),
        "forecast": context.get("forecast"),
        "strategy_override": context.get("strategy_override"),
        "morning_sideways_signal": context.get("morning_sideways_signal"),
        "auto_mode": MOVE_AUTO_ENTRY_MODE,
        "dry_run": DRY_RUN,
    })


def build_move_auto_decision(
    contract: dict,
    slot: str,
    configured_lots: int,
    *,
    now: datetime | None = None,
) -> dict:
    """Build, evaluate and persist one normalized AUTO decision cycle."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    settlement = _settlement_time(contract).astimezone(timezone.utc)
    market = get_move_decision_market(contract)
    index_symbol = str(
        (contract.get("spot_index") or {}).get("symbol") or ".DEXBTUSD")
    candles = _fetch_move_index_candles(index_symbol, now)
    current_twap, fixed_fraction = _current_settlement_twap(
        index_symbol, now=now, settlement=settlement)
    forecast = forecast_move_distribution(
        candles,
        current_index_price=market["spot_price"],
        strike=float(contract.get("strike_price") or 0),
        now_ms=int(now.timestamp() * 1000),
        settlement_end_ts_ms=int(settlement.timestamp() * 1000),
        current_30m_twap=current_twap,
        fraction_of_final_twap_fixed=fixed_fraction,
        # No trusted calendar source is configured.  The forecast module marks
        # event risk unknown/high and automatic shorts fail closed.
        scheduled_event_score=None,
        outer_scenarios=MOVE_FORECAST_OUTER_SCENARIOS,
        paths_per_scenario=MOVE_FORECAST_PATHS_PER_SCENARIO,
        minimum_history_days=MOVE_FORECAST_LOOKBACK_DAYS,
    )
    account = _move_account_decision_snapshot(contract)
    cv = float(contract.get("contract_value") or 0.001)
    strike = float(contract.get("strike_price") or 0)
    long_fee = min(
        OPTION_FEE_RATE * market["spot_price"],
        OPTION_FEE_CAP_PCT * market["ask"],
    ) * cv
    short_fee = min(
        OPTION_FEE_RATE * market["spot_price"],
        OPTION_FEE_CAP_PCT * market["bid"],
    ) * cv
    settlement_end_ms = int(settlement.timestamp() * 1000)
    normalized = {
        "timestamp": {"now_ms": int(now.timestamp() * 1000)},
        "contract": {
            "symbol": str(contract.get("symbol") or ""),
            "strike": strike,
            "expiry_ts_ms": settlement_end_ms,
            "settlement_window_start_ts_ms": settlement_end_ms - 1_800_000,
            "settlement_window_end_ts_ms": settlement_end_ms,
            "contract_multiplier": cv,
            "tick_size": float(contract.get("tick_size") or 0.1),
            "lot_size": 1,
            "min_order_size": 1,
            "max_position_size": float(
                contract.get("position_size_limit") or MAX_ORDER_LOTS),
        },
        "market": {
            "bid": market["bid"],
            "ask": market["ask"],
            "bid_size": market["bid_size"],
            "ask_size": market["ask_size"],
            "quote_timestamp_ms": market["quote_timestamp_ms"],
            "mark_price": market["mark_price"],
        },
        "underlying": {
            "btc_index_price": market["spot_price"],
            "current_30m_twap": current_twap,
            "fraction_of_final_twap_fixed": fixed_fraction,
            "spot_index_symbol": index_symbol,
        },
        "forecast": {
            key: forecast[key] for key in (
                "expected_payoff_low",
                "expected_payoff_mid",
                "expected_payoff_high",
                "payoff_p99",
                "jump_event_score",
                "model_timestamp_ms",
            )
        },
        "costs": {
            "long_round_trip_cost_per_contract": long_fee * 2,
            "short_round_trip_cost_per_contract": short_fee * 2,
            "long_slippage_per_contract": (
                market["ask"] * cv * MAX_SLIPPAGE_PCT / 100),
            "short_slippage_per_contract": (
                market["bid"] * cv * MAX_SLIPPAGE_PCT / 100),
        },
        "account": {
            "current_position_qty": account["current_position_qty"],
            "average_entry_price": account["average_entry_price"],
            "available_margin": account["available_margin"],
            "liquidation_buffer": account["liquidation_buffer"],
            "open_orders_count": account["open_orders_count"],
        },
        "exchange": {
            "system_operational": True,
            "product_operational": (
                str(contract.get("state") or "live").lower() == "live"
                and str(contract.get("trading_status") or "").lower()
                == "operational"
            ),
            "trading_enabled": not _move_bool(
                (contract.get("product_specs") or {}).get(
                    "only_reduce_only_orders_allowed")),
        },
    }
    strategy = _move_strategy_config(slot, contract, configured_lots)
    decision = evaluate_move_decision(normalized, strategy)
    morning_sideways_signal = None
    strategy_override = None
    if slot == "morning":
        morning_sideways_signal = _load_morning_sideways_signal(now=now)
        decision, strategy_override = _apply_morning_sideways_short_decision(
            decision, morning_sideways_signal)
    decision_id = hashlib.sha256(json.dumps(
        {
            "slot": slot,
            "symbol": contract.get("symbol"),
            "model_timestamp_ms": forecast["model_timestamp_ms"],
            "quote_timestamp_ms": market["quote_timestamp_ms"],
            "action": decision["action"],
            "strategy_override": (
                strategy_override.get("kind")
                if strategy_override and strategy_override.get("applied")
                else None
            ),
            "trend_observed_at_utc": (
                morning_sideways_signal.get("observed_at_utc")
                if morning_sideways_signal else None
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()[:24]
    context = {
        "schema_version": 1,
        "slot": slot,
        "decision_id": decision_id,
        "recorded_at_utc": now.isoformat().replace("+00:00", "Z"),
        "auto_mode": MOVE_AUTO_ENTRY_MODE,
        "dry_run": DRY_RUN,
        "execution_mode": "dry_run" if DRY_RUN else "real",
        "virtual_capital_usd": MOVE_DRY_RUN_CAPITAL_USD if DRY_RUN else None,
        "normalized_input": normalized,
        "forecast": forecast,
        "account_snapshot": account,
        "strategy_config": strategy,
        "decision": decision,
        "morning_sideways_signal": morning_sideways_signal,
        "strategy_override": strategy_override,
    }
    _persist_move_decision(slot, context)
    return context


def _settlement_time(contract: dict) -> datetime:
    try:
        return datetime.fromisoformat(
            str(contract.get("settlement_time") or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(f"invalid settlement time for {contract.get('symbol')}") from exc


def move_value_signal(contract: dict, snapshot: dict, side: str) -> dict:
    """Estimate MOVE fair absolute movement from realised volatility and ATR.

    This is a transparent gating model, not a price oracle.  Its inputs and
    output are persisted so the threshold can be walk-forward calibrated.
    """
    now = datetime.now(timezone.utc)
    settlement = _settlement_time(contract)
    tte_sec = (settlement - now).total_seconds()
    if tte_sec < MOVE_MIN_TTE_MINUTES * 60:
        raise RuntimeError(
            f"MOVE expiry gate: only {tte_sec / 60:.0f}m remains; minimum is {MOVE_MIN_TTE_MINUTES}m")
    if tte_sec > MOVE_MAX_TTE_HOURS * 3600:
        raise RuntimeError(
            f"MOVE expiry gate: {tte_sec / 3600:.1f}h exceeds {MOVE_MAX_TTE_HOURS:.1f}h")

    end = int(now.timestamp())
    response = _retry(
        _get, "/v2/history/candles",
        params={"resolution": "15m", "symbol": PERPETUAL_SYMBOL,
                "start": end - (MOVE_VOL_LOOKBACK + 5) * 900, "end": end},
    )
    candles = sorted(response.get("result") or [], key=lambda c: c.get("time", 0))
    current_bucket = end - end % 900
    if candles and int(candles[-1].get("time") or 0) >= current_bucket:
        candles = candles[:-1]
    candles = candles[-MOVE_VOL_LOOKBACK:]
    if len(candles) < 30:
        raise RuntimeError("not enough completed candles for MOVE value filter")
    closes = [float(c["close"]) for c in candles]
    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    log_returns = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(log_returns) < 20:
        raise RuntimeError("not enough valid returns for MOVE value filter")
    sigma_15m = statistics.stdev(log_returns)
    remaining_bars = max(tte_sec / 900, 1)
    current_spot = float(snapshot.get("spot") or closes[-1])
    strike = float(contract.get("strike_price") or current_spot)
    displacement = current_spot - strike
    future_sigma_usd = closes[-1] * sigma_15m * math.sqrt(remaining_bars)
    # E|N(mu, sigma)|: current movement from the fixed MOVE strike matters,
    # especially for the morning slot when half the measurement window is gone.
    if future_sigma_usd > 0:
        z = displacement / future_sigma_usd
        expected_rv = (
            future_sigma_usd * math.sqrt(2 / math.pi) * math.exp(-0.5 * z * z)
            + displacement * math.erf(z / math.sqrt(2))
        )
    else:
        expected_rv = abs(displacement)
    true_ranges = []
    for i in range(max(1, len(candles) - 14), len(candles)):
        prev = closes[i - 1]
        true_ranges.append(max(highs[i] - lows[i], abs(highs[i] - prev), abs(lows[i] - prev)))
    atr14 = statistics.mean(true_ranges)
    expected_atr = math.sqrt(displacement * displacement
                             + (atr14 * math.sqrt(remaining_bars)) ** 2)
    forecast = (expected_rv + expected_atr) / 2
    premium = snapshot["ask"] if side == "buy" else snapshot["bid"]
    raw_edge = forecast - premium if side == "buy" else premium - forecast
    edge_pct = raw_edge / premium * 100 if premium > 0 else -100.0
    result = {
        "forecast_abs_move": round(forecast, 2),
        "forecast_rv_move": round(expected_rv, 2),
        "forecast_atr_move": round(expected_atr, 2),
        "atr14": round(atr14, 2),
        "sigma_15m": round(sigma_15m, 8),
        "current_displacement": round(displacement, 2),
        "premium": premium,
        "edge_usd": round(raw_edge, 2),
        "edge_pct": round(edge_pct, 2),
        "tte_minutes": round(tte_sec / 60, 1),
        "passed": edge_pct >= MOVE_MIN_EDGE_PCT,
    }
    if MOVE_VALUE_FILTER_ENABLED and not result["passed"]:
        raise RuntimeError(
            f"MOVE value gate blocked entry: edge {edge_pct:.2f}% < {MOVE_MIN_EDGE_PCT:.2f}%")
    return result

# ─────────────────────────────────────────────────────────────
# ORDER MANAGEMENT
# ─────────────────────────────────────────────────────────────
def place_market_order(product_id: int, symbol: str, side: str, size: int,
                       force_real: bool = False, client_order_id: str | None = None,
                       reduce_only: bool = False) -> dict:
    """force_real=True bypasses DRY_RUN. Used for CLOSING an existing position —
    DRY_RUN must only ever gate opening NEW exposure, never faking the close of
    a position that may be real. Faking a close (as the old exit code did)
    marks state CLOSED and sends a "closed" alert while the real exchange
    position sits open and unmanaged — the exact bug that caused a live
    2395-lot position to go untracked for 6+ hours on 2026-07-09."""
    # Hard safety check — never exceed the configured per-order cap
    if size < 1:
        raise ValueError("Order size must be positive.")
    if size > MAX_ORDER_LOTS and not reduce_only:
        raise ValueError(f"Order size {size} exceeds hard cap of {MAX_ORDER_LOTS} lots. Aborting.")

    if DRY_RUN and not force_real:
        log.info("[DRY-RUN] %s %d lots  %s  id=%d", side.upper(), size, symbol, product_id)
        return {"result": {"id": 0, "state": "dry_run"}}

    log.info("ORDER: %s  %d lots  %s  (product_id=%d)%s",
             side.upper(), size, symbol, product_id,
             "  [force_real: DRY_RUN bypassed for close]" if (DRY_RUN and force_real) else "")
    payload = {
        "product_id": product_id,
        "size":        size,
        "side":        side,
        "order_type": "market_order",
        "reduce_only": bool(reduce_only),
    }
    if client_order_id:
        payload["client_order_id"] = client_order_id[:32]
    # Order creation is non-idempotent at the HTTP layer.  The caller journals
    # ``client_order_id`` before reaching here and owns response-loss recovery;
    # retrying this POST internally would submit the same close again before
    # the exchange can be queried for that exact durable identity.
    resp = _post("/v2/orders", payload)
    order = resp.get("result", {})
    # A 200 response with success:false (or no order id) is NOT a fill — never
    # let a caller silently treat a rejected order as a completed one.
    if not resp.get("success") or not order.get("id"):
        raise RuntimeError(f"Order rejected or incomplete for {symbol}: {resp}")
    log.info("  Filled: order_id=%s  state=%s  avg_price=%s",
             order.get("id"), order.get("state"),
             order.get("average_fill_price", "pending"))
    return resp


def _entry_client_id(slot: str, sequence: int = 0) -> str:
    stamp = datetime.now(timezone.utc).strftime("%y%m%d%H%M%S")
    clean_user = "".join(c for c in BOT_USER.lower() if c.isalnum())[:6] or "bot"
    return f"mb-{clean_user}-{slot[:1]}-{stamp}-{sequence}"[:32]


def _close_client_id(slot: str) -> str:
    """Unique durable identity for each separate close attempt."""
    clean_user = "".join(c for c in BOT_USER.lower() if c.isalnum())[:6] or "bot"
    return f"mc-{clean_user}-{slot[:1]}-{time.time_ns():x}"[:32]


def _write_entry_journal(slot: str, data: dict | None) -> Path:
    path = DATA_DIR / f"pending_{slot}_entry.json"
    if data is None:
        path.unlink(missing_ok=True)
    else:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    return path


_ACTIVE_ORDER_STATES = {
    "open", "pending", "partially_filled", "partially-filled", "untriggered", "triggered",
}
_TERMINAL_ORDER_STATES = {
    "closed", "filled", "cancelled", "canceled", "rejected", "expired", "failed",
}


def _order_state(order: dict | None) -> str:
    return str((order or {}).get("state") or (order or {}).get("status") or "").lower()


def _lookup_owned_order(order_id=None, client_order_id=None, product_id=None) -> dict | None:
    """Find an order using exchange-owned identity, never product alone.

    The history fallback is essential when the POST reached Delta but its HTTP
    response was lost.  In that case the pre-journalled client order ID is the
    only safe way to distinguish our fill from a manual trade in the product.
    """
    if order_id:
        try:
            params = {"product_id": product_id} if product_id else None
            response = _retry(_get, f"/v2/orders/{order_id}", params=params, auth=True,
                              retries=2, delay=0.25)
            result = response.get("result") or {}
            if response.get("success") and isinstance(result, dict):
                return result
        except Exception:
            pass
    if not client_order_id:
        return None
    for path, params in (
        ("/v2/orders", {"states": "open", "page_size": 100}),
        ("/v2/orders/history", {"page_size": 100}),
    ):
        try:
            response = _retry(_get, path, params=params, auth=True, retries=2, delay=0.25)
            for order in response.get("result") or []:
                if str(order.get("client_order_id") or "") != str(client_order_id):
                    continue
                if product_id and int(order.get("product_id") or 0) != int(product_id):
                    continue
                return order
        except Exception:
            continue
    return None


def _verified_terminal_fill(order: dict | None, requested: int) -> int | None:
    """Return a proven fill count, or None while execution is ambiguous."""
    state = _order_state(order)
    if state not in _TERMINAL_ORDER_STATES:
        return None
    for key in ("filled_size", "filled_quantity", "executed_size"):
        value = (order or {}).get(key)
        if value not in (None, ""):
            try:
                return min(max(int(float(value)), 0), requested)
            except (TypeError, ValueError):
                return None
    unfilled = (order or {}).get("unfilled_size")
    if unfilled not in (None, ""):
        try:
            return min(max(requested - int(float(unfilled)), 0), requested)
        except (TypeError, ValueError):
            return None
    # A rejected/failed order never entered the book. Cancelled/expired orders
    # may have partial fills, so they still require an explicit size field.
    if state == "rejected":
        return 0
    return None


def _wait_for_terminal_fill(initial: dict, requested: int, product_id: int,
                            client_order_id: str, timeout_sec: float | None = None
                            ) -> tuple[dict, int | None]:
    timeout = (float(os.getenv("ENTRY_ORDER_VERIFY_TIMEOUT_SEC", "8"))
               if timeout_sec is None else max(float(timeout_sec), 0))
    order = dict(initial or {})
    deadline = time.monotonic() + timeout
    while True:
        filled = _verified_terminal_fill(order, requested)
        if filled is not None:
            if filled > 0 and float(order.get("average_fill_price") or 0) <= 0:
                filled = None
            else:
                return order, filled
        if time.monotonic() >= deadline:
            return order, None
        refreshed = _lookup_owned_order(order.get("id"), client_order_id, product_id)
        if refreshed:
            order = refreshed
        time.sleep(0.25)


def _place_controlled_entry_impl(product_id: int, symbol: str, side: str, size: int,
                                 slot: str, initial_snapshot: dict | None = None,
                                 entry_context: dict | None = None,
                                 ) -> tuple[dict, int]:
    """Fill an entry with bounded IOC chunks and a crash-recovery journal."""
    if size < 1 or size > MAX_ORDER_LOTS:
        raise ValueError(f"invalid controlled entry size {size}")
    if DRY_RUN:
        cid = _entry_client_id(slot)
        log.info("[DRY-RUN] controlled %s %d lots %s", side.upper(), size, symbol)
        return {"result": {"id": 0, "client_order_id": cid, "state": "dry_run",
                           "average_fill_price": (initial_snapshot or {}).get(
                               "ask" if side == "buy" else "bid", 0),
                           "paid_commission": 0}}, size

    remaining = size
    fills: list[dict] = []
    journal = {
        "slot": slot, "symbol": symbol, "product_id": product_id, "side": side,
        "requested_lots": size, "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "fills": fills, "orders": [],
        "protection_config": (entry_context or {}).get("protection_config"),
        "risk_at_entry_usd": (entry_context or {}).get("risk_at_entry_usd"),
        "move_value_signal": (entry_context or {}).get("value_signal"),
    }
    _write_entry_journal(slot, journal)
    sequence = 0
    while remaining > 0:
        snap = initial_snapshot if sequence == 0 and initial_snapshot else get_execution_snapshot(symbol, side)
        available = min(int(snap["liquidity_cap"]), remaining, ORDER_CHUNK_LOTS)
        if available < 1:
            break
        tick = max(float(snap.get("tick_size") or 0.1), 1e-8)
        raw_limit = float(snap["limit_price"])
        # Round inward, never beyond the configured slippage boundary. If the
        # inward tick is no longer marketable there is no valid bounded price.
        units = math.floor(raw_limit / tick) if side == "buy" else math.ceil(raw_limit / tick)
        limit_price = units * tick
        market_price = float(snap["ask"] if side == "buy" else snap["bid"])
        if ((side == "buy" and limit_price + tick * 1e-9 < market_price)
                or (side == "sell" and limit_price - tick * 1e-9 > market_price)):
            raise RuntimeError(
                f"no marketable tick inside slippage boundary for {symbol}: "
                f"market={market_price}, boundary={raw_limit}, tick={tick}")
        client_id = _entry_client_id(slot, sequence)
        payload = {
            "product_id": product_id, "size": available, "side": side,
            "order_type": "limit_order", "limit_price": f"{limit_price:.8f}".rstrip("0").rstrip("."),
            "time_in_force": "ioc", "post_only": False,
            "client_order_id": client_id,
        }
        intent = {
            "client_order_id": client_id, "requested_lots": available,
            "limit_price": limit_price, "side": side, "status": "submitting",
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        journal["orders"].append(intent)
        _write_entry_journal(slot, journal)  # durable identity before network I/O
        try:
            response = _post("/v2/orders", payload)
        except Exception:
            intent["status"] = "submission_unknown"
            _write_entry_journal(slot, journal)
            raise
        result = response.get("result") or {}
        if not response.get("success") or not result.get("id"):
            error = response.get("error") or {}
            intent.update({"status": "rejected", "error": error, "verified_filled_lots": 0})
            _write_entry_journal(slot, journal)
            new = (downsized_lots(available, error.get("context") or {})
                   if str(error.get("code")) in BALANCE_REJECTIONS else None)
            if new and not fills:
                remaining = min(remaining, new)
                initial_snapshot = None
                sequence += 1  # never reuse a pre-journalled client order ID
                continue
            if fills:
                log.error("Later entry chunk rejected; preserving %d already-filled lots: %s",
                          sum(f["lots"] for f in fills), error)
                break
            _write_entry_journal(slot, None)  # explicit exchange rejection: no ambiguous fill
            raise RuntimeError(f"controlled entry rejected for {symbol}: {response}")
        intent.update({"status": "acknowledged", "order_id": result.get("id")})
        _write_entry_journal(slot, journal)
        verified_order, filled = _wait_for_terminal_fill(
            result, available, product_id, client_id)
        if filled is None:
            intent["status"] = "ambiguous"
            _write_entry_journal(slot, journal)
            raise RuntimeError(
                f"IOC order {result.get('id')} did not reach a verifiable terminal state")
        price = float(verified_order.get("average_fill_price") or 0)
        intent.update({
            "status": "terminal", "order_id": verified_order.get("id") or result.get("id"),
            "order_state": _order_state(verified_order), "verified_filled_lots": filled,
        })
        _write_entry_journal(slot, journal)
        if filled <= 0:
            if fills:
                break
            _write_entry_journal(slot, None)  # completed IOC with zero fill is unambiguous
            raise RuntimeError(f"IOC entry produced no verified fill for {symbol}: {verified_order}")
        fill = {
            "order_id": verified_order.get("id") or result.get("id"),
            "client_order_id": client_id,
            "lots": filled, "average_fill_price": price,
            "paid_commission": float(verified_order.get("paid_commission")
                                     or verified_order.get("commission")
                                     or verified_order.get("total_commission") or 0),
            "commission_reported": any(
                verified_order.get(key) not in (None, "")
                for key in ("paid_commission", "commission", "total_commission")
            ),
        }
        fills.append(fill)
        remaining -= filled
        journal["fills"] = fills
        journal["remaining_lots"] = remaining
        _write_entry_journal(slot, journal)
        sequence += 1
        initial_snapshot = None
        if filled < available:
            log.warning("IOC chunk partially filled %d/%d; not chasing remaining liquidity.",
                        filled, available)
            break

    total = sum(f["lots"] for f in fills)
    if total < 1:
        raise RuntimeError(f"controlled entry did not fill any lots for {symbol}")
    avg = sum(f["average_fill_price"] * f["lots"] for f in fills) / total
    paid = sum(f["paid_commission"] for f in fills)
    aggregate = {
        "id": fills[0]["order_id"], "order_ids": [f["order_id"] for f in fills],
        "client_order_id": fills[0]["client_order_id"],
        "client_order_ids": [f["client_order_id"] for f in fills],
        "average_fill_price": avg, "paid_commission": paid,
        "commission_reported": all(f.get("commission_reported") for f in fills),
        "size": total, "unfilled_size": size - total, "state": "closed",
    }
    log.info("Controlled entry filled %d/%d lots in %d chunk(s) @ %.4f.",
             total, size, len(fills), avg)
    return {"success": True, "result": aggregate}, total


def place_controlled_entry(product_id: int, symbol: str, side: str, size: int,
                           slot: str, initial_snapshot: dict | None = None,
                           entry_context: dict | None = None) -> tuple[dict, int]:
    """Execute an IOC entry and immediately protect any exposure on error."""
    try:
        return _place_controlled_entry_impl(
            product_id, symbol, side, size, slot, initial_snapshot, entry_context)
    except Exception:
        # A timeout/transport failure can occur after Delta accepted an order.
        # Reconcile now (not only at next service restart), persist any proven
        # exposure, and start its monitor before propagating the failure.
        try:
            if (DATA_DIR / f"pending_{slot}_entry.json").exists():
                recover_pending_entries((slot,))
        except Exception:
            log.exception("Immediate %s partial-entry recovery failed", slot)
        raise


# Rejection codes that mean "the balance doesn't cover this size" — the fix
# is fewer lots, not a retry of the same order.
BALANCE_REJECTIONS = ("insufficient_commission", "insufficient_margin",
                      "insufficient_balance")


def _rejection(exc) -> tuple[str, dict]:
    """(error_code, context) from a Delta order-rejection HTTPError."""
    try:
        err = exc.response.json().get("error") or {}
        return str(err.get("code", "")), err.get("context") or {}
    except Exception:
        return "", {}


def downsized_lots(size: int, ctx: dict) -> int | None:
    """The exchange's rejection context states available_balance and
    required_additional_balance — together they give the TRUE total cost of
    the rejected order (margin + premium + commission, whatever Delta's
    formula is), so the per-lot cost and the truly affordable size follow
    exactly. None = can't downsize (context unusable or already at 1 lot)."""
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


def place_entry_order(product_id: int, symbol: str, side: str, size: int,
                      slot: str = "evening", snapshot: dict | None = None,
                      entry_context: dict | None = None) -> tuple[dict, int]:
    """Place an ENTRY order, auto-downsizing when the exchange rejects it for
    balance/commission/margin. Client-side sizing estimates cannot perfectly
    model Delta's margin+fee formulas (SELL/short entries especially — margin
    is a different formula from a long's premium+fee) — the exchange's own
    rejection context is authoritative, so resize from it and retry.
    Returns (order_response, actual_lots). Closes must NEVER go through this:
    a close must be for the full position size or not at all."""
    if SAFE_EXECUTION_ENABLED:
        return place_controlled_entry(
            product_id, symbol, side, size, slot, snapshot, entry_context)
    raise RuntimeError(
        "scheduled market entry is disabled: SAFE_EXECUTION_ENABLED must remain true")


def get_mv_position(product_id: int) -> dict | None:
    data = _retry(_get, "/v2/positions/margined", auth=True)
    positions = data.get("result")
    if not data.get("success") or not isinstance(positions, list):
        raise RuntimeError(f"exchange position check failed: {data}")
    for pos in positions:
        if not isinstance(pos, dict):
            raise RuntimeError("exchange position response contained a malformed row")
        if str(pos.get("product_id")) != str(product_id):
            continue
        try:
            size = float(pos.get("size", 0))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"invalid exchange position size for product {product_id}") from exc
        if size != 0:
            return pos
    return None

def get_available_usd() -> float:
    data = _retry(_get, "/v2/wallet/balances", auth=True)
    for w in data.get("result", []):
        if w.get("asset_symbol") == "USD":
            return float(w.get("available_balance") or 0)
    return 0.0

def _effective_lots(configured: int, mark: float, contract_val: float, label: str) -> int:
    """Return the configured lots after applying the applicable funding cap.

    LIVE sizing requires a verified wallet and fails closed when affordability
    cannot be established.  DRY RUN uses virtual capital, so its paper funding
    ceiling is the configured size.  The caller still applies liquidity,
    strategy-risk, SL, fee, slippage and order-size caps.
    """
    if DRY_RUN:
        lots = max(min(configured, MAX_ORDER_LOTS), 0)
        log.info(
            "%s paper lot sizing: configured=%d  virtual_cap=%d  -> using %d",
            label, configured, configured, lots,
        )
        return lots

    # Affordability is a mandatory safety cap for scheduled automation.  The
    # legacy DYNAMIC_LOTS toggle remains readable for old clients but can no
    # longer authorise an order larger than the verified wallet can fund.
    try:
        bal  = get_available_usd()
        spot = get_btc_price()
        fee  = min(OPTION_FEE_RATE * spot, OPTION_FEE_CAP_PCT * mark) * contract_val
        cost = mark * contract_val + fee
        afford = int((bal * 0.98) / cost) if cost > 0 else 0
    except Exception as e:
        log.error("%s: balance check failed (%s) — entry blocked.", label, e)
        return 0
    lots = max(min(configured, afford, MAX_ORDER_LOTS), 0)
    log.info("%s lot sizing: configured=%d  affordable=%d  (bal $%.2f, $%.4f/lot incl. fee)  -> using %d",
             label, configured, afford, bal, cost, lots)
    if afford < configured:
        log.info("%s: sized DOWN to %d lots — balance covers fewer than the configured %d.",
                 label, lots, configured)
    return lots


def _risk_config() -> dict:
    return {
        "MAX_TRADES_PER_DAY_GLOBAL": MAX_TRADES_GLOBAL,
        "MAX_TRADES_PER_DAY": MAX_TRADES_PER_DAY,
        "MAX_OPEN_RISK_USD": MAX_OPEN_RISK_USD,
        "MAX_DAILY_LOSS_USD": MAX_DAILY_LOSS_USD,
        "MAX_CONSECUTIVE_LOSSES": MAX_CONSECUTIVE_LOSSES,
        "LOSS_COOLDOWN_MINUTES": LOSS_COOLDOWN_MINUTES,
        "RISK_DAY_TZ_OFFSET_MIN": RISK_DAY_TZ_OFFSET_MIN,
        "SHORT_MAX_RISK_USD": SHORT_MAX_RISK_USD,
    }


def _assert_entry_configuration() -> None:
    errors = list(ENTRY_CONFIGURATION_ERRORS)
    if not API_KEY or not API_SECRET:
        errors.append("account API credentials are unavailable")
    if errors:
        raise RuntimeError("new entries disabled: " + "; ".join(errors))


def _configured_dry_run_now() -> bool:
    """Re-read the authoritative entry mode immediately before a new entry."""
    if not CFG_FILE.exists():
        return _ENV_DRY_RUN_DEFAULT
    try:
        document = json.loads(CFG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError(
            "new entries disabled: config.json is unreadable during mode check"
        ) from exc
    if not isinstance(document, dict):
        raise RuntimeError(
            "new entries disabled: config.json is not an object during mode check")
    raw = document.get("DRY_RUN")
    if raw in (None, ""):
        return _ENV_DRY_RUN_DEFAULT
    try:
        return _strict_config_bool(raw, key="DRY_RUN")
    except ValueError as exc:
        raise RuntimeError(f"new entries disabled: {exc}") from exc


def _assert_entry_mode_current() -> None:
    """Close the config-save/reload race at the irreversible entry boundary."""
    persisted = _configured_dry_run_now()
    if persisted != PROCESS_DRY_RUN:
        running = "DRY-RUN" if PROCESS_DRY_RUN else "REAL"
        selected = "DRY-RUN" if persisted else "REAL"
        raise RuntimeError(
            f"trading mode changed from {running} to {selected}; "
            "bot reload is required before any new entry")


def _slot_risk(slot: str) -> tuple[float, float]:
    if slot == "morning":
        return RISK_PER_TRADE_MORNING, abs(float(os.getenv("SL_TARGET_PNL_MORNING", "0") or 0))
    return RISK_PER_TRADE_EVENING, abs(float(os.getenv("SL_TARGET_PNL", "0") or 0))


def _protection_snapshot(slot: str) -> dict:
    suffix = "_MORNING" if slot == "morning" else ""
    tp_default = "300" if slot == "morning" else "105"
    legacy = float(os.getenv(f"TSL_TARGET_PNL{suffix}", "0") or 0)
    snapshot = {
        "tp_target_pnl": float(os.getenv(f"TP_TARGET_PNL{suffix}", tp_default) or tp_default),
        "sl_target_pnl": abs(float(os.getenv(f"SL_TARGET_PNL{suffix}", "0") or 0)),
        "tsl_target_pnl": abs(legacy),
        "tsl_arm_pnl": abs(float(os.getenv(f"TSL_ARM_PNL{suffix}", str(legacy)) or legacy)),
        "tsl_trail_pnl": abs(float(os.getenv(f"TSL_TRAIL_PNL{suffix}", str(legacy)) or legacy)),
        "tsl_lock_min_pnl": abs(float(os.getenv(f"TSL_LOCK_MIN_PNL{suffix}", "0") or 0)),
        "poll_secs": max(int(float(os.getenv(f"TP_POLL_SECS{suffix}", "30") or 30)), 10),
    }
    numeric = [value for value in snapshot.values() if isinstance(value, (int, float))]
    if any(not math.isfinite(float(value)) for value in numeric):
        raise ValueError(f"{slot} protection configuration contains a non-finite value")
    if snapshot["tp_target_pnl"] <= 0:
        raise ValueError(f"{slot} take-profit must be positive")
    if bool(snapshot["tsl_arm_pnl"]) != bool(snapshot["tsl_trail_pnl"]):
        raise ValueError(f"{slot} TSL arm and trail must both be positive or both be zero")
    return snapshot


def _entry_fee_accounting(order: dict, plan: dict) -> tuple[float, str]:
    """Prefer exchange commission, otherwise persist the configured estimate."""
    result = order.get("result") or {}
    if DRY_RUN:
        return 0.0, "dry_run"
    marker_present = "commission_reported" in result
    if ((marker_present and bool(result.get("commission_reported")))
            or (not marker_present
                and any(result.get(key) not in (None, "")
                        for key in ("paid_commission", "commission", "total_commission")))):
        return abs(float(result.get("paid_commission") or result.get("commission")
                         or result.get("total_commission") or 0)), "exchange"
    return abs(float(plan.get("estimated_entry_fee_usd") or 0)), "configured_estimate"


def _persist_entry_state(slot: str, state: dict, save_fn) -> None:
    """Close the final fill-to-state crash gap with immediate reconciliation."""
    try:
        save_fn(state)
    except Exception:
        log.exception("Could not persist filled %s entry; flattening immediately", slot)
        # We still have authoritative in-memory fill identity. Do not make a
        # second disk write a prerequisite to the reduce-only fail-safe.
        _close_position_job(
            state, save_fn, slot.upper(), exit_trigger_override="entry_state_persist_failure")
        raise


def _account_unrealized_pnl() -> float:
    """Use the exchange's whole-account snapshot, including manual exposure."""
    response = _retry(_get, "/v2/positions/margined", auth=True)
    if not response.get("success"):
        raise RuntimeError("account positions unavailable for unrealized-risk check")
    positions = [p for p in response.get("result", []) if float(p.get("size") or 0) != 0]
    owned_pids = {
        int(state.get("product_id") or 0)
        for state in load_states(DATA_DIR).values()
        if str(state.get("status", "")).upper() == "OPEN"
    }
    external = [str(p.get("product_symbol") or p.get("product_id")) for p in positions
                if int(p.get("product_id") or 0) not in owned_pids]
    if external and not ALLOW_EXTERNAL_POSITIONS_WITH_BOT:
        raise RuntimeError(
            "external/manual position blocks new automated exposure: " + ", ".join(external[:5]))
    total = 0.0
    for position in positions:
        value = position.get("unrealized_pnl")
        if value in (None, ""):
            raise RuntimeError(
                f"unrealized P&L unavailable for {position.get('product_symbol') or position.get('product_id')}")
        total += float(value)
    return round(total, 2)


def _refresh_move_auto_context_for_execution(
    auto_context: dict,
    contract: dict,
    slot: str,
    configured_lots: int,
    execution_snapshot: dict,
) -> dict:
    """Re-evaluate AUTO direction with the irreversible-boundary snapshot."""
    context = json.loads(json.dumps(auto_context))
    normalized = context.get("normalized_input") or {}
    market = normalized.get("market") or {}
    market.update({
        "bid": execution_snapshot["bid"],
        "ask": execution_snapshot["ask"],
        "bid_size": execution_snapshot.get("bid_size", 0),
        "ask_size": execution_snapshot.get("ask_size", 0),
        "quote_timestamp_ms": execution_snapshot["quote_timestamp_ms"],
        "mark_price": execution_snapshot.get("mark"),
    })
    normalized["market"] = market
    normalized["timestamp"] = {
        "now_ms": int(datetime.now(timezone.utc).timestamp() * 1000)}
    account = _move_account_decision_snapshot(contract)
    normalized["account"] = {
        "current_position_qty": account["current_position_qty"],
        "average_entry_price": account["average_entry_price"],
        "available_margin": account["available_margin"],
        "liquidation_buffer": account["liquidation_buffer"],
        "open_orders_count": account["open_orders_count"],
    }
    strategy = _move_strategy_config(slot, contract, configured_lots)
    decision = evaluate_move_decision(normalized, strategy)
    previous_override = context.get("strategy_override") or {}
    if (slot == "morning"
            and previous_override.get("kind") == "morning_all_sideways_short"
            and previous_override.get("applied") is True):
        current_signal = _load_morning_sideways_signal()
        decision, current_override = _apply_morning_sideways_short_decision(
            decision, current_signal)
        if not current_override or current_override.get("applied") is not True:
            raise RuntimeError(
                "Morning all-SIDEWAYS signal changed or a preserved SHORT "
                "safety gate failed before execution; no order was placed")
        context["morning_sideways_signal"] = current_signal
        context["strategy_override"] = current_override
    expected_side = str(
        (auto_context.get("decision") or {}).get("side") or "")
    if decision.get("side") != expected_side or decision.get("action") not in {
            LONG_MOVE, SHORT_MOVE}:
        raise RuntimeError(
            "MOVE AUTO decision changed before execution; no order was placed")
    context.update({
        "recorded_at_utc": datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"),
        "normalized_input": normalized,
        "account_snapshot": account,
        "strategy_config": strategy,
        "decision": decision,
        "execution_revalidated": True,
    })
    _persist_move_decision(slot, context)
    return context


def _short_initial_margin_per_contract(
    contract: dict,
    *,
    spot: float,
    premium: float,
    contract_value: float,
) -> float:
    """Conservative base isolated margin estimate before exchange validation."""
    raw = max(float(contract.get("initial_margin") or 0), 0)
    ratio = raw / 100 if raw > 0.10 else raw
    base = (ratio * spot + premium) * contract_value
    return max(base, premium * contract_value, 0.00000001)


def build_move_entry_plan(
    contract: dict,
    configured_lots: int,
    side: str,
    slot: str,
    *,
    auto_context: dict | None = None,
) -> dict:
    """Validate AUTO value, affordability, risk sizing and portfolio limits."""
    _assert_entry_configuration()
    protection = _protection_snapshot(slot)  # validate before any order is submitted
    unresolved = [p.name for p in DATA_DIR.glob("pending_*_entry.json")]
    if unresolved:
        raise RuntimeError(f"unresolved entry journal blocks new exposure: {', '.join(unresolved)}")
    open_move = [s for name, s in load_states(DATA_DIR).items()
                 if name in ("morning", "evening")
                 and str(s.get("status", "")).upper() == "OPEN"
                 and _move_bool(s.get("dry_run")) == bool(DRY_RUN)]
    if len(open_move) >= MAX_CONCURRENT_MOVE_POSITIONS:
        raise RuntimeError(
            f"concurrent MOVE cap reached ({len(open_move)}/{MAX_CONCURRENT_MOVE_POSITIONS})")
    if side == "sell" and not ALLOW_SHORT_MOVE:
        raise RuntimeError("short MOVE entries are disabled; set ALLOW_SHORT_MOVE only with an explicit SL")
    symbol = contract["symbol"]
    if not DRY_RUN and get_mv_position(int(contract["id"])):
        raise RuntimeError(
            f"target product {symbol} already has net exposure; automated ownership would be ambiguous")
    cv = float(contract.get("contract_value") or 0.001)
    snapshot = get_execution_snapshot(symbol, side)
    if auto_context is not None:
        auto_context = _refresh_move_auto_context_for_execution(
            auto_context, contract, slot, configured_lots, snapshot)
        auto_decision = auto_context["decision"]
        if auto_decision.get("side") != side:
            raise RuntimeError("MOVE AUTO side does not match the entry plan")
        value_signal = {
            "automatic": True,
            "decision_id": auto_context.get("decision_id"),
            "forecast": auto_context.get("forecast"),
            "decision": auto_decision,
        }
    else:
        # Retained only for recovery-compatible direct callers. Scheduled
        # jobs always supply an AUTO context; no UI route uses this path.
        auto_decision = None
        value_signal = move_value_signal(contract, snapshot, side)
    premium = snapshot["ask"] if side == "buy" else snapshot["bid"]
    if auto_context is not None:
        strategy = auto_context["strategy_config"]
        account_snapshot = auto_context["account_snapshot"]
        spot_for_margin = float(
            snapshot.get("spot")
            or (auto_context.get("normalized_input", {}).get("underlying") or {})
            .get("btc_index_price")
            or 0
        )
        initial_margin_per_contract = _short_initial_margin_per_contract(
            contract,
            spot=spot_for_margin,
            premium=premium,
            contract_value=cv,
        )
        lot_caps = aggregate_risk_lot_caps(
            auto_decision,
            strategy,
            available_margin=float(
                account_snapshot.get("available_margin") or 0),
            short_initial_margin_per_contract=initial_margin_per_contract,
            current_position_qty=float(
                account_snapshot.get("current_position_qty") or 0),
        )
        if side == "buy":
            per_contract_funding = max(float(
                auto_decision["metrics"].get(
                    "long_premium_risk_per_contract") or 0), 0)
            available_capital = float(
                account_snapshot.get("available_margin") or 0)
            lot_caps["funding"] = (
                int((available_capital * 0.98) / per_contract_funding)
                if per_contract_funding > 0 else 0)
            lot_caps["effective"] = min(lot_caps.values())
        affordable = min(
            configured_lots,
            int(lot_caps.get("effective") or 0),
            MAX_ORDER_LOTS,
        )
    else:
        initial_margin_per_contract = 0.0
        lot_caps = {}
        affordable = _effective_lots(
            configured_lots, premium, cv, slot.upper())
    if affordable < 1:
        raise RuntimeError(
            "AUTO aggregate premium/p99/margin lot cap produced zero lots"
            if auto_context is not None else
            "verified affordable lots is zero")
    risk_budget, stop_loss = _slot_risk(slot)
    configured_stop_loss = stop_loss
    paper_short_risk_assumption = 0.0
    if side == "sell":
        if SHORT_MAX_RISK_USD <= 0:
            raise RuntimeError(
                "short MOVE simulation requires a positive short-risk cap"
                if DRY_RUN else
                "short MOVE requires a positive SL and short-risk cap")
        if stop_loss <= 0:
            if not DRY_RUN:
                raise RuntimeError(
                    "short MOVE requires a positive SL and short-risk cap")
            # Paper entries have no exchange exposure or exchange stop order.
            # Size and account for their risk using the mandatory short cap.
            stop_loss = SHORT_MAX_RISK_USD
            paper_short_risk_assumption = SHORT_MAX_RISK_USD
        risk_budget = min(risk_budget, SHORT_MAX_RISK_USD)
    spot = snapshot.get("spot") or get_btc_price()
    fee_one_way = min(OPTION_FEE_RATE * spot, OPTION_FEE_CAP_PCT * premium) * cv
    slippage_per_lot = premium * cv * MAX_SLIPPAGE_PCT / 100
    lots = risk_based_lots(
        configured=configured_lots,
        affordable=affordable,
        # MOVE depth controls bounded LIVE IOC chunks in
        # _place_controlled_entry_impl; it is not a strategy sizing cap.
        liquidity_cap=MAX_ORDER_LOTS,
        max_order_lots=MAX_ORDER_LOTS,
        risk_budget_usd=risk_budget,
        stop_loss_usd=stop_loss,
        premium_per_lot=premium * cv,
        round_trip_fee_per_lot=fee_one_way * 2,
        slippage_per_lot=slippage_per_lot,
        short=side == "sell",
    )
    if lots < 1:
        raise RuntimeError(
            "risk sizing produced zero lots; verify risk budget, SL and affordability")
    if auto_decision is not None and side == "sell":
        catastrophe = lots * max(
            float(auto_decision["metrics"].get(
                "short_p99_loss_per_contract") or 0),
            initial_margin_per_contract,
        )
    elif auto_decision is not None:
        catastrophe = lots * float(auto_decision["metrics"].get(
            "long_premium_risk_per_contract") or 0)
    else:
        catastrophe = lots * (
            premium * cv + fee_one_way * 2 + slippage_per_lot)
    # A configured SL is a trigger, not a guarantee of fill.  Long premium at
    # risk remains the catastrophe bound and must never be understated.
    proposed_risk = max(stop_loss, catastrophe)
    # Paper results have their own state/history risk ledger.  Pulling the
    # exchange account's live P&L into a simulation would mix the two modes
    # and could make a harmless paper entry depend on unrelated real exposure.
    unrealized = 0.0 if DRY_RUN else _account_unrealized_pnl()
    decision = evaluate_entry(DATA_DIR, proposed_risk, _risk_config(),
                              unrealized_pnl_usd=unrealized,
                              dry_run=DRY_RUN)
    audit_event(DATA_DIR, "move_entry_evaluated", {
        "slot": slot, "symbol": symbol, "side": side,
        "configured_lots": configured_lots, "affordable_lots": affordable,
        "observed_executable_depth_lots": snapshot["liquidity_cap"],
        "book_depth_applied_to_sizing": False, "risk_lots": lots,
        "risk_budget_usd": risk_budget, "stop_loss_usd": stop_loss,
        "configured_stop_loss_usd": configured_stop_loss,
        "paper_short_risk_assumption_usd": paper_short_risk_assumption,
        "auto_decision_id": (
            auto_context.get("decision_id") if auto_context else None),
        "aggregate_lot_caps": lot_caps,
        "short_initial_margin_per_contract": initial_margin_per_contract,
        "proposed_risk_usd": round(proposed_risk, 2),
        "unrealized_pnl_usd": unrealized,
        "snapshot": snapshot, "value_signal": value_signal,
        "risk_decision": decision_dict(decision),
    })
    if not decision.allowed:
        raise RuntimeError(f"portfolio risk blocked entry: {decision.reason}")
    return {
        "lots": lots, "snapshot": snapshot, "value_signal": value_signal,
        "observed_executable_depth_lots": snapshot["liquidity_cap"],
        "book_depth_applied_to_sizing": False,
        "risk_budget_usd": risk_budget, "stop_loss_usd": stop_loss,
        "configured_stop_loss_usd": configured_stop_loss,
        "paper_short_risk_assumption_usd": paper_short_risk_assumption,
        "auto_context": auto_context,
        "aggregate_lot_caps": lot_caps,
        "short_initial_margin_per_contract": initial_margin_per_contract,
        "risk_at_entry_usd": round(proposed_risk, 2),
        "risk_decision": decision_dict(decision),
        "estimated_entry_fee_usd": round(fee_one_way * lots, 4),
        "protection_config": protection,
    }


def recover_pending_entries(slots=("morning", "evening")) -> None:
    """Resolve crash journals using bot-owned order identity.

    A same-product exchange position is never sufficient evidence by itself:
    it may have been opened manually. At least one terminal fill bearing an
    order/client ID from our pre-submit journal must account for the remaining
    exposure before this strategy adopts and protects it.
    """
    for slot in slots:
        path = DATA_DIR / f"pending_{slot}_entry.json"
        if not path.exists():
            continue
        try:
            journal = json.loads(path.read_text(encoding="utf-8"))
            pid = int(journal.get("product_id") or 0)
            if not pid:
                raise RuntimeError("pending entry journal has no product_id")

            fills = list(journal.get("fills") or [])
            intents = list(journal.get("orders") or [])
            # Compatibility with journals written immediately before this
            # release: their verified-looking fills still get re-queried by
            # exact order/client ID before being trusted.
            if not intents and fills:
                intents = [{
                    "order_id": fill.get("order_id"),
                    "client_order_id": fill.get("client_order_id"),
                    "requested_lots": fill.get("lots"),
                    "status": "legacy_requires_verification",
                } for fill in fills]
                fills = []

            unresolved = []
            verified_fills: list[dict] = []
            seen_order_ids: set[str] = set()
            seen_client_ids: set[str] = set()
            for intent in intents:
                order_identity = str(intent.get("order_id") or "")
                client_identity = str(intent.get("client_order_id") or "")
                if ((order_identity and order_identity in seen_order_ids)
                        or (client_identity and client_identity in seen_client_ids)):
                    intent["status"] = "duplicate_journal_identity_ignored"
                    continue
                if order_identity:
                    seen_order_ids.add(order_identity)
                if client_identity:
                    seen_client_ids.add(client_identity)
                requested = int(float(intent.get("requested_lots") or 0))
                if requested < 1:
                    unresolved.append(str(intent.get("client_order_id") or "invalid-intent"))
                    continue
                order = _lookup_owned_order(
                    intent.get("order_id"), intent.get("client_order_id"), pid)
                if not order:
                    if (intent.get("status") == "rejected"
                            and int(intent.get("verified_filled_lots") or 0) == 0):
                        continue
                    intent["status"] = "ownership_unresolved"
                    unresolved.append(str(intent.get("client_order_id") or intent.get("order_id")))
                    continue
                returned_client = str(order.get("client_order_id") or "")
                returned_side = str(order.get("side") or "").lower()
                if (str(order.get("product_id") or "") != str(pid)
                        or (client_identity and returned_client != client_identity)
                        or (returned_side and returned_side != str(journal.get("side") or "").lower())):
                    intent["status"] = "ownership_mismatch"
                    unresolved.append(client_identity or order_identity)
                    continue
                filled = _verified_terminal_fill(order, requested)
                if filled is None:
                    intent["status"] = ("active" if _order_state(order) in _ACTIVE_ORDER_STATES
                                        else "ambiguous")
                    intent["order_id"] = order.get("id") or intent.get("order_id")
                    unresolved.append(str(intent.get("client_order_id") or intent.get("order_id")))
                    continue
                intent.update({
                    "status": "terminal", "order_id": order.get("id") or intent.get("order_id"),
                    "order_state": _order_state(order), "verified_filled_lots": filled,
                })
                if filled:
                    price = float(order.get("average_fill_price") or 0)
                    if price <= 0:
                        intent["status"] = "ambiguous_fill_price"
                        unresolved.append(str(intent.get("client_order_id") or intent.get("order_id")))
                        continue
                    verified_fills.append({
                        "order_id": order.get("id") or intent.get("order_id"),
                        "client_order_id": (order.get("client_order_id")
                                            or intent.get("client_order_id")),
                        "lots": filled, "average_fill_price": price,
                        "paid_commission": float(order.get("paid_commission")
                                                 or order.get("commission") or 0),
                    })

            journal["orders"] = intents
            journal["fills"] = verified_fills
            journal["recovery_unresolved_orders"] = unresolved
            journal["last_reconciled_at_utc"] = datetime.now(timezone.utc).isoformat()
            _write_entry_journal(slot, journal)

            position = get_mv_position(pid) if pid else None
            actual_size = int(float(position.get("size") or 0)) if position else 0
            if actual_size == 0:
                if unresolved:
                    log.error("Pending %s order identity remains unresolved; journal retained.", slot)
                    audit_event(DATA_DIR, "pending_entry_unresolved", {
                        "slot": slot, "product_id": pid, "orders": unresolved,
                        "exchange_size": 0,
                    })
                    continue
                log.warning("Clearing resolved pending %s journal; exchange has no position.", slot)
                _write_entry_journal(slot, None)
                audit_event(DATA_DIR, "pending_entry_cleared_no_position", {
                    "slot": slot, "product_id": pid,
                    "verified_filled_lots": sum(f["lots"] for f in verified_fills),
                })
                continue
            proven_lots = sum(int(fill.get("lots") or 0) for fill in verified_fills)
            expected_sign = -1 if str(journal.get("side")) == "sell" else 1
            ownership_proven = (proven_lots > 0
                                and actual_size * expected_sign > 0
                                and abs(actual_size) <= proven_lots)
            if not ownership_proven:
                journal["ownership_status"] = "unproven_exchange_position"
                _write_entry_journal(slot, journal)
                audit_event(DATA_DIR, "pending_entry_ownership_blocked", {
                    "slot": slot, "product_id": pid, "exchange_size": actual_size,
                    "proven_bot_fills": proven_lots, "orders": unresolved,
                })
                send_telegram(
                    f"🚨 <b>UNRESOLVED ENTRY OWNERSHIP — {slot.upper()} ({TAG})</b>\n"
                    f"Product <code>{pid}</code> has <code>{actual_size}</code> live lots but only "
                    f"<code>{proven_lots}</code> lots are proven bot fills.\n"
                    "The journal is retained and all new entries are blocked; inspect immediately."
                )
                continue

            try:
                product_response = _retry(
                    _get, f"/v2/products/{pid}", retries=2, delay=0.25)
                product = product_response.get("result") or {}
            except Exception as exc:
                # Public metadata enrichment is never a prerequisite to
                # protecting/flattening proven exchange exposure.
                log.warning("Product metadata unavailable during recovery: %s", exc)
                product = {}
            lots = abs(actual_size)
            side = "sell" if actual_size < 0 else "buy"
            fill = float(position.get("entry_price") or 0)
            if fill <= 0 and verified_fills:
                total = sum(int(f.get("lots") or 0) for f in verified_fills)
                fill = (sum(float(f.get("average_fill_price") or 0) * int(f.get("lots") or 0)
                            for f in verified_fills) / total) if total else 0
            if fill <= 0:
                raise RuntimeError("owned live position has no verifiable entry price")
            cv = float(product.get("contract_value") or 0.001)
            recovered_protection = journal.get("protection_config")
            try:
                protection_is_valid = (
                    isinstance(recovered_protection, dict)
                    and math.isfinite(float(recovered_protection.get("tp_target_pnl") or 0))
                    and float(recovered_protection.get("tp_target_pnl") or 0) > 0
                )
            except (TypeError, ValueError):
                protection_is_valid = False
            started = str(journal.get("started_at_utc") or datetime.now(timezone.utc).isoformat())
            try:
                began = datetime.fromisoformat(started.replace("Z", "+00:00"))
            except ValueError:
                began = datetime.now(timezone.utc)
            existing = (load_morning_state() if slot == "morning" else load_state()) or {}
            if (existing.get("status") == "OPEN"
                    and int(existing.get("product_id") or 0) not in (0, pid)):
                raise RuntimeError(
                    f"cannot recover product {pid}; {slot} already tracks product "
                    f"{existing.get('product_id')}")
            state = {
                "slot": slot, "status": "OPEN", "side": "short" if side == "sell" else "long",
                "entry_date": began.strftime("%Y-%m-%d"),
                "trading_date": trading_date(began, RISK_DAY_TZ_OFFSET_MIN),
                "entry_time_utc": began.strftime("%H:%M:%S"),
                "symbol": product.get("symbol") or journal.get("symbol"),
                "product_id": pid, "strike": float(product.get("strike_price") or 0),
                "settlement": product.get("settlement_time", ""), "contract_value": cv,
                "lots": lots, "owned_entry_lots": min(lots, proven_lots),
                "entry_mark": fill, "btc_at_entry": 0,
                "total_cost_usd": round(fill * cv * lots, 2),
                "order_id": (verified_fills[0].get("order_id") if verified_fills else None),
                "order_ids": [f.get("order_id") for f in verified_fills if f.get("order_id")],
                "client_order_id": (verified_fills[0].get("client_order_id")
                                    if verified_fills else None),
                "client_order_ids": [f.get("client_order_id") for f in verified_fills
                                     if f.get("client_order_id")],
                "entry_commission_usd": sum(float(f.get("paid_commission") or 0)
                                            for f in verified_fills),
                "entry_trigger": "recovered_pending_journal", "dry_run": False,
                "execution_mode": "real",
                "ownership": "move_bot", "recovery_unresolved_orders": unresolved,
                "protection_config": recovered_protection if protection_is_valid else {},
            }
            # Preserve monitor bookkeeping when recovery is re-run after the
            # state was already saved but before every order became terminal.
            if existing.get("status") == "OPEN" and int(existing.get("product_id") or 0) == pid:
                state = {**existing, **state}
            save_fn = save_morning_state if slot == "morning" else save_state
            try:
                save_fn(state)
            except Exception:
                # Proven bot exposure exists but cannot be made durable. Use
                # the in-memory state to reduce it before propagating failure.
                _close_position_job(
                    state, save_fn, slot.upper(),
                    exit_trigger_override="recovery_state_persist_failure")
                raise
            if not protection_is_valid:
                _close_position_job(
                    state, save_fn, slot.upper(),
                    exit_trigger_override="missing_recovery_protection_snapshot")
                if not unresolved:
                    _write_entry_journal(slot, None)
                continue
            protected = start_tp_monitor(slot)
            if not protected:
                # Reduce risk before any nonessential journal annotation.
                _emergency_flatten_unprotected(slot)
                if not unresolved:
                    _write_entry_journal(slot, None)
                continue
            if not unresolved:
                _write_entry_journal(slot, None)
            else:
                journal["position_adopted"] = True
                journal["protection_confirmed"] = True
                _write_entry_journal(slot, journal)
            try:
                audit_event(DATA_DIR, "pending_entry_recovered", {
                    "slot": slot, "symbol": state["symbol"], "lots": lots,
                    "entry_mark": fill, "unresolved_orders": unresolved,
                })
            except Exception:
                log.exception("Recovered-entry audit failed after protection was confirmed")
            send_telegram(
                f"⚠️ <b>RECOVERED INTERRUPTED {slot.upper()} ENTRY — {TAG}</b>\n"
                f"<code>{state['symbol']}</code> · {lots:,} lots @ ${fill:.4f}\n"
                "Protection monitor is confirmed."
            )
        except Exception as exc:
            log.exception("Pending %s entry recovery failed", slot)
            send_telegram(f"🚨 <b>PENDING ENTRY RECOVERY FAILED — {slot.upper()} ({TAG})</b>\n"
                          f"<code>{exc}</code>\nNew entries remain blocked by the journal.")

# ─────────────────────────────────────────────────────────────
# TP MONITOR SPAWN + STALE-STOP SWEEP
# ─────────────────────────────────────────────────────────────
def start_tp_monitor(slot: str):
    """Spawn tp_monitor.py for this slot right after an entry, so TP/SL/TSL
    protection never depends on someone pressing Start in the dashboard.
    Uses the same users/<user>/tp_<slot>.pid file the dashboard tracks."""
    current_state = (
        load_morning_state() if slot == "morning" else load_state()
    ) or {}
    if _move_bool(current_state.get("dry_run")):
        log.info("DRY-RUN %s state uses local simulated exits; no exchange monitor spawned.",
                 slot)
        return True
    health_file = DATA_DIR / f"tp_{slot}_health.json"
    timeout_sec = max(min(int(os.getenv("PROTECTION_START_TIMEOUT_SEC", "30")), 55), 5)
    heartbeat_max_age = max(
        int(os.getenv(f"TP_POLL_SECS_{slot.upper()}",
                      os.getenv("TP_POLL_SECS", "30"))) * 3,
        60,
    )

    def ready(expected_pid=None, newer_than=0.0):
        try:
            health = json.loads(health_file.read_text(encoding="utf-8"))
            if expected_pid and int(health.get("pid") or 0) != int(expected_pid):
                return False
            if str(health.get("user") or "").lower() != BOT_USER.lower():
                return False
            if str(health.get("slot") or "").lower() != slot.lower():
                return False
            current_state = (load_morning_state() if slot == "morning" else load_state()) or {}
            if str(health.get("product_id") or "") != str(current_state.get("product_id") or ""):
                return False
            expected_order = current_state.get("order_id") or current_state.get("entry_order_id")
            if expected_order and str(health.get("entry_order_id") or "") != str(expected_order):
                return False
            expected_client = current_state.get("client_order_id")
            if (expected_client
                    and str(health.get("entry_client_order_id") or "") != str(expected_client)):
                return False
            if health_file.stat().st_mtime < newer_than:
                return False
            heartbeat = datetime.fromisoformat(
                str(health.get("heartbeat_utc") or "").replace("Z", "+00:00"))
            if heartbeat.tzinfo is None:
                heartbeat = heartbeat.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - heartbeat.astimezone(timezone.utc)).total_seconds()
            if age < -30 or age > heartbeat_max_age:
                return False
            proven_mode = bool(
                health.get("exchange_protection_complete")
                or health.get("local_fallback_active")
            )
            return (bool(health.get("protection_established")) and proven_mode
                    and health.get("status") in {"healthy", "degraded"})
        except (OSError, ValueError, TypeError):
            return False
    try:
        pf = DATA_DIR / f"tp_{slot}.pid"
        if pf.exists():
            try:
                # POSIX liveness probe; the live bot runs on Linux (Windows
                # dev copy is DRY_RUN and returns above — os.kill(pid, 0)
                # is NOT safe as a liveness check on Windows).
                existing_pid = int(pf.read_text().strip())
                os.kill(existing_pid, 0)
                if ready(existing_pid):
                    log.info("TP monitor for %s already running and healthy.", slot)
                    return True
                # A stale pidfile is not authority to signal an arbitrary
                # reused PID. Require both OS command identity and the monitor
                # health identity before terminating it.
                try:
                    cmdline = Path(f"/proc/{existing_pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
                    health_identity = json.loads(health_file.read_text(encoding="utf-8"))
                    identity_ok = (
                        "tp_monitor.py" in cmdline
                        and f"--slot {slot}" in cmdline
                        and f"--user {BOT_USER}" in cmdline
                        and int(health_identity.get("pid") or 0) == existing_pid
                        and str(health_identity.get("slot") or "").lower() == slot.lower()
                        and str(health_identity.get("user") or "").lower() == BOT_USER.lower()
                    )
                except Exception:
                    identity_ok = False
                if not identity_ok:
                    log.critical("Refusing to signal unproven PID %d from %s", existing_pid, pf)
                    return False
                log.error("TP monitor for %s is alive but has no healthy protection heartbeat; restarting.",
                          slot)
                os.kill(existing_pid, 15)
                for _ in range(20):
                    try:
                        os.kill(existing_pid, 0)
                    except OSError:
                        break
                    time.sleep(0.1)
                try:
                    os.kill(existing_pid, 0)
                except OSError:
                    pass
                else:
                    log.critical("Old %s monitor PID %d did not terminate; duplicate spawn blocked.",
                                 slot, existing_pid)
                    return False
                pf.unlink(missing_ok=True)
            except (OSError, ValueError):
                pf.unlink(missing_ok=True)
        script = Path(__file__).parent / "tp_monitor.py"
        spawned_at = time.time()
        proc = subprocess.Popen(
            [sys.executable, str(script), "--slot", slot, "--user", BOT_USER],
            cwd=str(Path(__file__).parent),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        pf.write_text(str(proc.pid))
        log.info("TP monitor spawned for %s slot (pid %d).", slot, proc.pid)
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            if ready(proc.pid, spawned_at):
                log.info("TP monitor for %s confirmed protection healthy.", slot)
                audit_event(DATA_DIR, "protection_confirmed", {"slot": slot, "pid": proc.pid})
                return True
            time.sleep(0.5)
        message = f"monitor did not confirm protection within {timeout_sec}s"
        log.error("%s for %s.", message, slot)
        audit_event(DATA_DIR, "protection_unconfirmed", {"slot": slot, "pid": proc.pid,
                                                          "reason": message})
        state = load_morning_state() if slot == "morning" else load_state()
        if state and state.get("status") == "OPEN":
            state["protection_start_failure"] = message
            state["protection_established"] = False
            (save_morning_state if slot == "morning" else save_state)(state)
        send_telegram(f"🚨 <b>PROTECTION NOT CONFIRMED — {slot.upper()} ({TAG})</b>\n"
                      f"<code>{message}</code>\nNo further slot entry is allowed while this position is open.")
        return False
    except Exception as exc:
        log.warning("TP monitor spawn failed for %s: %s", slot, exc)
        send_telegram(f"⚠️ <b>TP MONITOR SPAWN FAILED — {slot.upper()} ({TAG})</b>\n"
                      f"<code>{exc}</code>\nProtection failed; entry fail-safe will flatten the position.")
        return False


def _emergency_flatten_unprotected(slot: str) -> bool:
    """Fail-safe for a real fill whose protection could not be confirmed."""
    state = load_morning_state() if slot == "morning" else load_state()
    if not state or state.get("status") != "OPEN":
        return True
    save_fn = save_morning_state if slot == "morning" else save_state
    _close_position_job(
        state, save_fn, slot.upper(), exit_trigger_override="protection_start_failure")
    try:
        audit_event(DATA_DIR, "unprotected_position_flatten_verified", {
            "slot": slot, "symbol": state.get("symbol"), "lots": state.get("lots"),
        })
    except Exception:
        log.exception("Unprotected-position flatten audit failed after verified close")
    return True


def _protect_or_flatten_entry(slot: str) -> bool:
    """Make protection confirmation part of entry completion.

    Returns True when the filled position remains open and protected. Returns
    False when the fail-safe flattened it. Raises when neither outcome could
    be verified, leaving the journal/state in place for periodic recovery.
    """
    if start_tp_monitor(slot):
        _write_entry_journal(slot, None)
        return True
    # The original entry journal and OPEN state already exist. Do no further
    # disk I/O before the risk-reducing order; a full disk must not suppress
    # the emergency close.
    _emergency_flatten_unprotected(slot)
    send_telegram(
        f"🚨 <b>{slot.upper()} ENTRY PROTECTION FAILED — {TAG}</b>\n"
        "The position was immediately flattened with a verified reduce-only close."
    )
    _write_entry_journal(slot, None)
    return False


def cancel_product_stops(product_id: int):
    """Cancel only protection orders this account's state explicitly owns.

    A product-level sweep used to cancel manual orders as collateral damage.
    State order IDs and our client-order prefix are the ownership boundary.
    """
    if DRY_RUN:
        return
    owned_ids = set()
    for state in (load_state() or {}, load_morning_state() or {}):
        if int(state.get("product_id") or 0) != int(product_id):
            continue
        for key in ("tsl_stop_order_id", "tp_stop_order_id"):
            if state.get(key):
                owned_ids.add(str(state[key]))
        for order_id in state.get("orphan_protection_order_ids") or []:
            if order_id:
                owned_ids.add(str(order_id))

    data = _retry(
        _get, "/v2/orders",
        params={"product_ids": str(product_id), "states": "open"}, auth=True)
    if not data.get("success"):
        raise RuntimeError(f"could not verify open orders for product {product_id}: {data}")
    orders = data.get("result")
    if not isinstance(orders, list):
        raise RuntimeError(f"invalid open-order response for product {product_id}")
    non_stops = [order for order in orders if not order.get("stop_order_type")]
    if non_stops:
        ids = ", ".join(str(order.get("id")) for order in non_stops)
        raise RuntimeError(
            f"open non-protection order(s) block automated entry on product {product_id}: {ids}")
    stops = [order for order in orders if order.get("stop_order_type")]
    unowned = [order for order in stops if str(order.get("id")) not in owned_ids]
    if unowned:
        ids = ", ".join(str(order.get("id")) for order in unowned)
        # Client-ID prefixes are diagnostic, not cancellation authority. If a
        # crash lost the state ownership record, an operator must reconcile it
        # rather than letting a new entry delete an order on a guess.
        raise RuntimeError(
            f"unowned resting protection order(s) block entry on product {product_id}: {ids}")

    for order in stops:
        order_id = order.get("id")
        body = json.dumps({"id": order_id, "product_id": product_id},
                          separators=(",", ":"))
        hdrs = _sign("DELETE", "/v2/orders", "", body)
        response = requests.delete(BASE_URL + "/v2/orders", data=body, headers=hdrs,
                                   timeout=15)
        response.raise_for_status()
        result = response.json()
        if not result.get("success"):
            # Cancellation responses can be lost/racy; only an independently
            # verified terminal order permits entry to continue.
            refreshed = _lookup_owned_order(order_id, order.get("client_order_id"), product_id)
            if not refreshed or _order_state(refreshed) not in _TERMINAL_ORDER_STATES:
                raise RuntimeError(
                    f"could not confirm cancellation of owned protection order {order_id}: {result}")
        log.info("Cancelled owned stale resting order %s on product %d.",
                 order_id, product_id)

    # DELETE can be accepted asynchronously. Re-read the book and require the
    # target product to be completely clear before opening new exposure.
    verified = _retry(
        _get, "/v2/orders",
        params={"product_ids": str(product_id), "states": "open"}, auth=True)
    remaining = verified.get("result")
    if not verified.get("success") or not isinstance(remaining, list):
        raise RuntimeError(f"could not verify post-cancel order state for product {product_id}")
    if remaining:
        ids = ", ".join(str(order.get("id")) for order in remaining)
        raise RuntimeError(
            f"open order(s) remain after protection cleanup on product {product_id}: {ids}")


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
            send_telegram(f"✅ <b>API ACCESS RESTORED — {TAG}</b>\nOrders can be placed again.")
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
        f"🚨 <b>API KEY BLOCKED — IP CHANGED ({TAG})</b>\n"
        "Delta is rejecting authenticated calls: IP not whitelisted.\n"
        "Whitelist these in Delta → Account → API Keys:\n"
        f"IPv4 » <code>{ip4}</code>\n"
        f"IPv6 » <code>{ip6}</code>\n"
        "⚠️ TP monitor, exit and entry orders will FAIL until fixed."
    )

# ─────────────────────────────────────────────────────────────
# MORNING ENTRY JOB  —  00:15 UTC  (5:45 AM IST)
# AUTO-selects LONG/SHORT/NO_TRADE for today's 12:00 UTC settlement.
# ─────────────────────────────────────────────────────────────
def morning_entry_job():
    # The mutex stays at the account root so a config transition can never
    # allow simultaneous REAL and DRY-RUN entry workers.
    with account_entry_lock(ACCOUNT_DIR, "scheduled-morning") as acquired:
        if not acquired:
            log.warning("Another account entry is in progress — morning entry skipped.")
            return
        _assert_entry_mode_current()
        return _morning_entry_job_locked()


def _morning_entry_job_locked():
    log.info("=" * 64)
    log.info("MORNING ENTRY  %s UTC  (%s)",
             datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
             _ist_label(MORNING_H_UTC, MORNING_M_UTC))

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Once-per-day guard for the morning slot
    ms = load_morning_state()
    if ms and ms.get("history_pending"):
        if not _flush_pending_move_history():
            raise RuntimeError("previous morning trade history is not durable; entry blocked")
        ms = load_morning_state()
    if ms and ms.get("entry_date") == today:
        log.info("Morning trade already recorded today (status=%s). Skipping.",
                 ms.get("status", ""))
        return {"terminal": True, "status": "already_recorded"}

    if MOVE_AUTO_ENTRY_MODE == "disabled":
        log.info("Morning MOVE AUTO is disabled.")
        return {"terminal": True, "status": "auto_disabled"}

    contract = get_mv_contract(today)
    if not contract:
        raise LookupError(f"No live BTC MV contract found for {today}.")
    product_id   = contract["id"]
    symbol       = contract["symbol"]
    strike       = float(contract.get("strike_price",    0))
    contract_val = float(contract.get("contract_value", 0.001))
    settlement   = contract.get("settlement_time", "")

    auto_context = build_move_auto_decision(
        contract, "morning", MORNING_LOTS)
    auto_decision = auto_context["decision"]
    action = auto_decision["action"]
    log.info(
        "Morning AUTO decision: %s  long_edge=$%.4f  short_edge=$%.4f",
        action,
        float(auto_decision["metrics"]["long_edge_per_contract"]),
        float(auto_decision["metrics"]["short_edge_per_contract"]),
    )
    if MOVE_AUTO_ENTRY_MODE == "shadow":
        log.info("Morning AUTO shadow recorded; no position was opened.")
        return {
            "terminal": True, "status": "shadow_recorded",
            "decision": auto_decision,
        }
    if action not in {LONG_MOVE, SHORT_MOVE}:
        log.info(
            "Morning AUTO placed no trade: %s",
            auto_decision.get("failed_gates"),
        )
        return {
            "terminal": True, "status": action.lower(),
            "decision": auto_decision,
        }
    side = str(auto_decision["side"])
    btc_price = float(
        auto_context["normalized_input"]["underlying"]["btc_index_price"])
    plan = build_move_entry_plan(
        contract, MORNING_LOTS, side, "morning",
        auto_context=auto_context)
    snap       = plan["snapshot"]
    entry_mark = snap["ask"] if side == "buy" else snap["bid"]
    lots       = plan["lots"]
    total_cost = entry_mark * contract_val * lots

    log.info("Symbol      : %s", symbol)
    log.info("Strike      : $%.0f  |  BTC mark: $%.2f", strike, btc_price)
    log.info("Settlement  : %s", settlement)
    log.info("Entry mark  : $%.4f/BTC", entry_mark)
    log.info("Lots        : %d  |  Total premium: $%.2f", lots, total_cost)

    cancel_product_stops(product_id)
    order, lots = place_entry_order(product_id, symbol, side, lots,
                                    slot="morning", snapshot=snap, entry_context=plan)
    fill  = float(order.get("result", {}).get("average_fill_price") or entry_mark)
    entry_fee, entry_fee_source = _entry_fee_accounting(order, plan)

    now = datetime.now(timezone.utc)
    strategy_override = auto_context.get("strategy_override") or {}
    sideways_short = (
        strategy_override.get("kind") == "morning_all_sideways_short"
        and strategy_override.get("applied") is True
    )
    _persist_entry_state("morning", {
        "slot":           "morning",
        "status":         "OPEN",
        "side":           "long" if side == "buy" else "short",
        "entry_date":     today,
        "trading_date":   trading_date(now, RISK_DAY_TZ_OFFSET_MIN),
        "entry_time_utc": now.strftime("%H:%M:%S"),
        "symbol":         symbol,
        "product_id":     product_id,
        "strike":         strike,
        "settlement":     settlement,
        "contract_value": contract_val,
        "lots":           lots,
        "owned_entry_lots": lots,
        "entry_mark":     round(fill, 4),
        "btc_at_entry":   round(btc_price, 2),
        "total_cost_usd": round(fill * contract_val * lots, 2),
        "order_id":       order.get("result", {}).get("id"),
        "order_ids":      order.get("result", {}).get("order_ids", []),
        "client_order_id": order.get("result", {}).get("client_order_id"),
        "client_order_ids": order.get("result", {}).get("client_order_ids", []),
        "entry_commission_usd": entry_fee,
        "entry_fee_source": entry_fee_source,
        "entry_trigger":  (
            "morning_all_sideways_short" if sideways_short else
            f"morning_auto_{'long' if side == 'buy' else 'short'}"
        ),
        "execution_snapshot": snap,
        "move_value_signal": plan["value_signal"],
        "move_auto_decision_id": auto_context.get("decision_id"),
        "move_auto_context": plan.get("auto_context"),
        "move_strategy_override": strategy_override or None,
        "risk_at_entry_usd": plan["risk_at_entry_usd"],
        "risk_decision": plan["risk_decision"],
        "protection_config": plan["protection_config"],
        "dry_run":        DRY_RUN,
        "execution_mode": "dry_run" if DRY_RUN else "real",
    }, save_morning_state)
    if not _protect_or_flatten_entry("morning"):
        log.error("Morning fill was flattened because protection could not be confirmed.")
        return
    try:
        audit_event(DATA_DIR, "move_entry_filled", {
            "slot": "morning", "symbol": symbol, "lots": lots, "fill": fill,
            "order_ids": order.get("result", {}).get("order_ids", []),
        })
    except Exception:
        log.exception("Morning entry audit write failed after protection was confirmed")

    send_telegram(
        f"🌅 <b>MORNING AUTO {'SHORT' if side == 'sell' else 'LONG'} — {TAG}</b>\n"
        f"<code>{'━' * 24}</code>\n"
        f"Symbol  » <code>{symbol}</code>\n"
        f"Side    » <code>{'SHORT — sold to open' if side == 'sell' else 'LONG — bought'}</code>\n"
        f"Strike  » <code>${strike:,.0f}</code>\n"
        f"Lots    » <code>{lots:,}</code>\n"
        f"Entry   » <code>${fill:.4f} / BTC</code>\n"
        f"Cost    » <code>${fill * contract_val * lots:,.2f}</code>\n"
        f"BTC     » <code>${btc_price:,.2f}</code>\n"
        f"Settles » <code>{settlement.replace('T', ' ').replace('Z', ' UTC')}</code>\n"
        f"Mode    » <code>{'DRY-RUN ⚠' if DRY_RUN else 'LIVE ●'}</code>"
    )
    log.info("Morning straddle opened: %d lots %s @ $%.4f", lots, symbol, fill)
    return {
        "terminal": True, "status": "opened", "side": side,
        "lots": lots, "decision": auto_decision,
    }


# ─────────────────────────────────────────────────────────────
# ENTRY JOB  —  12:05 UTC  (5:35 PM IST)
# ─────────────────────────────────────────────────────────────
def entry_job():
    with account_entry_lock(ACCOUNT_DIR, "scheduled-evening") as acquired:
        if not acquired:
            log.warning("Another account entry is in progress — evening entry skipped.")
            return
        _assert_entry_mode_current()
        return _entry_job_locked()


def _entry_job_locked():
    log.info("=" * 64)
    log.info("ENTRY  %s UTC  (5:35 PM IST)",
             datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"))

    # ONE-ORDER-PER-DAY guard — double check before doing anything
    if already_traded_today():
        return {"terminal": True, "status": "already_recorded"}

    if MOVE_AUTO_ENTRY_MODE == "disabled":
        log.info("Evening MOVE AUTO is disabled.")
        return {"terminal": True, "status": "auto_disabled"}

    # Find MV contract
    contract     = find_active_mv_contract()
    product_id   = contract["id"]
    symbol       = contract["symbol"]
    strike       = float(contract.get("strike_price",    0))
    contract_val = float(contract.get("contract_value", 0.001))
    settlement   = contract.get("settlement_time", "")

    auto_context = build_move_auto_decision(contract, "evening", LOTS)
    auto_decision = auto_context["decision"]
    action = auto_decision["action"]
    log.info(
        "Evening AUTO decision: %s  long_edge=$%.4f  short_edge=$%.4f",
        action,
        float(auto_decision["metrics"]["long_edge_per_contract"]),
        float(auto_decision["metrics"]["short_edge_per_contract"]),
    )
    if MOVE_AUTO_ENTRY_MODE == "shadow":
        log.info("Evening AUTO shadow recorded; no position was opened.")
        return {
            "terminal": True, "status": "shadow_recorded",
            "decision": auto_decision,
        }
    if action not in {LONG_MOVE, SHORT_MOVE}:
        log.info(
            "Evening AUTO placed no trade: %s",
            auto_decision.get("failed_gates"),
        )
        return {
            "terminal": True, "status": action.lower(),
            "decision": auto_decision,
        }
    side = str(auto_decision["side"])
    btc_price = float(
        auto_context["normalized_input"]["underlying"]["btc_index_price"])
    plan = build_move_entry_plan(
        contract, LOTS, side, "evening", auto_context=auto_context)
    snap       = plan["snapshot"]
    entry_mark = snap["ask"] if side == "buy" else snap["bid"]
    lots       = plan["lots"]
    total_cost = entry_mark * contract_val * lots

    log.info("Symbol      : %s", symbol)
    log.info("Product ID  : %d", product_id)
    log.info("Strike      : $%.0f  |  BTC mark: $%.2f", strike, btc_price)
    log.info("Settlement  : %s", settlement)
    log.info("Entry mark  : $%.4f/BTC  ($%.4f/lot)", entry_mark, entry_mark * contract_val)
    log.info("Lots        : %d  |  Total premium: $%.2f", lots, total_cost)

    # Place the single entry order in the revalidated AUTO direction.
    cancel_product_stops(product_id)
    order, lots = place_entry_order(product_id, symbol, side, lots,
                                    slot="evening", snapshot=snap, entry_context=plan)
    # Record the REAL fill, not the pre-order snapshot — all downstream P&L
    # (TP/SL triggers included) keys off entry_mark.
    fill  = float(order.get("result", {}).get("average_fill_price") or entry_mark)
    entry_fee, entry_fee_source = _entry_fee_accounting(order, plan)

    # Persist — this is what prevents any second order today
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now = datetime.now(timezone.utc)
    _persist_entry_state("evening", {
        "slot":           "evening",
        "status":         "OPEN",
        "side":           "long" if side == "buy" else "short",
        "entry_date":     today,
        "trading_date":   trading_date(now, RISK_DAY_TZ_OFFSET_MIN),
        "entry_time_utc": now.strftime("%H:%M:%S"),
        "symbol":         symbol,
        "product_id":     product_id,
        "strike":         strike,
        "settlement":     settlement,
        "contract_value": contract_val,
        "lots":           lots,
        "owned_entry_lots": lots,
        "entry_mark":     round(fill, 4),
        "btc_at_entry":   btc_price,
        "total_cost_usd": round(fill * contract_val * lots, 4),
        "order_id":       order.get("result", {}).get("id"),
        "order_ids":      order.get("result", {}).get("order_ids", []),
        "client_order_id": order.get("result", {}).get("client_order_id"),
        "client_order_ids": order.get("result", {}).get("client_order_ids", []),
        "entry_commission_usd": entry_fee,
        "entry_fee_source": entry_fee_source,
        "entry_trigger":  f"evening_auto_{'long' if side == 'buy' else 'short'}",
        "execution_snapshot": snap,
        "move_value_signal": plan["value_signal"],
        "move_auto_decision_id": auto_context.get("decision_id"),
        "move_auto_context": plan.get("auto_context"),
        "risk_at_entry_usd": plan["risk_at_entry_usd"],
        "risk_decision": plan["risk_decision"],
        "protection_config": plan["protection_config"],
        "dry_run":        DRY_RUN,
        "execution_mode": "dry_run" if DRY_RUN else "real",
    }, save_state)
    if not _protect_or_flatten_entry("evening"):
        log.error("Evening fill was flattened because protection could not be confirmed.")
        return
    try:
        audit_event(DATA_DIR, "move_entry_filled", {
            "slot": "evening", "symbol": symbol, "lots": lots, "fill": fill,
            "order_ids": order.get("result", {}).get("order_ids", []),
        })
    except Exception:
        log.exception("Evening entry audit write failed after protection was confirmed")
    log.info("Straddle OPEN. Waiting for exit at %02d:%02d UTC.",
             EXIT_H_UTC, EXIT_M_UTC)

    send_telegram(
        f"{'🔻' if side == 'sell' else '🔺'} <b>AUTO ENTRY CONFIRMED — {TAG}</b>\n"
        f"<code>{'━' * 24}</code>\n"
        f"Symbol  » <code>{symbol}</code>\n"
        f"Side    » <code>{'SHORT — sold to open' if side == 'sell' else 'LONG — bought'}</code>\n"
        f"Strike  » <code>${strike:,.0f}</code>\n"
        f"Lots    » <code>{lots:,}</code>\n"
        f"Premium » <code>${fill:.4f} / BTC</code>\n"
        f"Cost    » <code>${fill * contract_val * lots:.2f}</code>\n"
        f"BTC     » <code>${btc_price:,.2f}</code>\n"
        f"Time    » <code>{datetime.now(timezone.utc).strftime('%H:%M UTC')}  (IST +5:30)</code>\n"
        f"Mode    » <code>{'DRY-RUN ⚠' if DRY_RUN else 'LIVE ●'}</code>"
    )
    return {
        "terminal": True, "status": "opened", "side": side,
        "lots": lots, "decision": auto_decision,
    }

# ─────────────────────────────────────────────────────────────
# EXIT JOBS — shared close logic for both slots
# ─────────────────────────────────────────────────────────────
def _close_dry_run_position_job(state: dict, save_fn, load_fn, label: str,
                                exit_trigger: str) -> dict | None:
    """Close one paper position from public marks and persist its own ledger.

    This path contains no authenticated position lookup, order lookup, order
    submission, or protection-order cleanup.  Its state marker and namespace
    are both verified before a simulated result can be published.
    """
    slot = str(state.get("slot") or label).strip().lower()
    with account_file_lock(
        DATA_DIR, f"close-{slot}", f"scheduled-dry-close-{os.getpid()}",
        stale_after_sec=30, wait_sec=2,
    ) as acquired:
        if not acquired:
            raise RuntimeError(f"another {slot} close/reconciliation is in progress")
        latest = load_fn()
        if latest:
            state = latest
        if not state or state.get("status") != "OPEN":
            log.info("%s paper close already reconciled by another worker.", label)
            return None
        if not _move_bool(state.get("dry_run")):
            raise RuntimeError(
                f"refusing simulated close for non-DRY-RUN {slot} state")

        symbol = str(state.get("symbol") or "")
        lots = int(float(state.get("lots") or 0))
        entry_mark = float(state.get("entry_mark") or 0)
        contract_val = float(state.get("contract_value") or 0.001)
        if not symbol or lots < 1 or entry_mark <= 0 or contract_val <= 0:
            raise RuntimeError("paper state has incomplete entry identity")
        exit_mark = float(get_mv_mark(symbol) or 0)
        if not math.isfinite(exit_mark) or exit_mark <= 0:
            raise RuntimeError(
                f"public ticker has no valid paper exit mark for {symbol}")
        try:
            btc_exit = float(get_btc_price() or state.get("btc_at_entry") or 0)
        except Exception:
            btc_exit = float(state.get("btc_at_entry") or 0)
        btc_entry = float(state.get("btc_at_entry") or 0)
        btc_move = ((btc_exit - btc_entry) / btc_entry * 100
                    if btc_entry and btc_exit else 0.0)
        pnl_sign = -1 if str(state.get("side") or "").lower() == "short" else 1
        gross = (exit_mark - entry_mark) * contract_val * lots * pnl_sign
        # No exchange order exists in DRY-RUN, therefore no actual commission
        # is charged.  Keep the zero explicit so paper accounting is complete
        # rather than masquerading as missing real fill data.
        fees = 0.0
        pnl = gross - fees
        closed_at = datetime.now(timezone.utc)
        state.update({
            "status": "CLOSED",
            "dry_run": True,
            "execution_mode": "dry_run",
            "exit_date": closed_at.strftime("%Y-%m-%d"),
            "exit_time_utc": closed_at.strftime("%H:%M:%S"),
            "exit_at_utc": closed_at.isoformat().replace("+00:00", "Z"),
            "exit_mark": round(exit_mark, 8),
            "btc_at_exit": round(btc_exit, 2),
            "btc_move_pct": round(btc_move, 4),
            "gross_pnl_usd": round(gross, 8),
            "fees_usd": fees,
            "entry_fee_usd": 0.0,
            "exit_fee_usd": 0.0,
            "entry_commission_usd": 0.0,
            "exit_commission_usd": 0.0,
            "entry_fee_source": "dry_run",
            "exit_fee_source": "dry_run",
            "fees_available": True,
            "fees_complete": True,
            "fees_estimated": False,
            "pnl_includes_fees": True,
            "pnl_usd": round(pnl, 4),
            "exit_trigger": exit_trigger,
            "exit_price_source": "public_ticker_simulation",
            "closed_lots": lots,
            "history_pending": True,
            "history_logged": False,
        })
        save_fn(state)
        history_complete = log_trade(state)
        state["history_pending"] = not history_complete
        state["history_logged"] = bool(history_complete)
        if history_complete:
            state["history_logged_at_utc"] = closed_at.isoformat()
        save_fn(state)
        audit_event(DATA_DIR, "dry_run_move_closed", {
            "slot": slot, "symbol": symbol, "lots": lots,
            "entry_mark": entry_mark, "exit_mark": exit_mark,
            "pnl_usd": round(pnl, 4), "exit_trigger": exit_trigger,
        })
        log.info("%s DRY-RUN straddle CLOSED. Simulated P&L: $%.2f",
                 label, pnl)
        return {"pnl": round(pnl, 4), "fill": exit_mark, "dry_run": True}


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
    if _move_bool(state.get("dry_run")):
        return _close_dry_run_position_job(
            state, save_state, load_state, "EVENING",
            "scheduled_exit_evening_simulated",
        )
    _close_position_job(state, save_state, "EVENING", load_fn=load_state)


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
    if _move_bool(state.get("dry_run")):
        return _close_dry_run_position_job(
            state, save_morning_state, load_morning_state, "MORNING",
            "scheduled_exit_morning_simulated",
        )
    _close_position_job(
        state, save_morning_state, "MORNING", load_fn=load_morning_state)


_PENDING_CLOSE_FIELDS = (
    "pending_close_order_id",
    "pending_close_client_order_id",
    "pending_close_requested_lots",
    "pending_close_start_size",
    "pending_close_side",
    "pending_close_submission_state",
    "pending_close_started_at_utc",
    "pending_close_last_error",
    "pending_close_order_state",
    "pending_close_reason",
)


def _clear_pending_close(state: dict) -> None:
    for key in _PENDING_CLOSE_FIELDS:
        state[key] = "" if key in {"pending_close_last_error", "pending_close_reason"} else None


def _validate_recovered_close_order(order: dict, *, product_id: int,
                                    client_order_id: str, side: str,
                                    order_id=None) -> dict:
    """Prove a recovered order belongs to this exact reduce-only close intent."""
    if not isinstance(order, dict) or not order.get("id"):
        raise RuntimeError("recovered close order has no exchange identity")
    if order_id and str(order.get("id")) != str(order_id):
        raise RuntimeError(
            f"recovered close order id mismatch: {order.get('id')} != {order_id}")
    returned_client = str(order.get("client_order_id") or "")
    if returned_client and returned_client != str(client_order_id):
        raise RuntimeError(
            f"recovered close client id mismatch: {returned_client} != {client_order_id}")
    returned_product = order.get("product_id")
    if returned_product not in (None, "") and str(returned_product) != str(product_id):
        raise RuntimeError(
            f"recovered close product mismatch: {returned_product} != {product_id}")
    returned_side = str(order.get("side") or "").lower()
    if returned_side and returned_side != side:
        raise RuntimeError(f"recovered close side mismatch: {returned_side} != {side}")
    if order.get("reduce_only") is False:
        raise RuntimeError("recovered close order is not reduce-only")
    return order


def _close_position_job(state: dict, save_fn, label: str,
                        exit_trigger_override: str | None = None,
                        load_fn=None):
    slot = str(state.get("slot") or label).lower()
    with account_file_lock(
        DATA_DIR, f"close-{slot}", f"scheduled-close-{os.getpid()}",
        stale_after_sec=30, wait_sec=2,
    ) as acquired:
        if not acquired:
            raise RuntimeError(f"another {slot} close/reconciliation is in progress")
        # State may have changed while this caller waited for the account-wide
        # close lock.  Reload inside the critical section so a durable pending
        # close identity is never lost to a stale in-memory copy.
        if load_fn is not None:
            latest = load_fn()
            if latest:
                state = latest
            if not state or state.get("status") != "OPEN":
                log.info("%s close already reconciled by another worker.", label)
                return None
        return _close_position_job_locked(
            state, save_fn, label, exit_trigger_override=exit_trigger_override)


def _close_position_job_locked(state: dict, save_fn, label: str,
                               exit_trigger_override: str | None = None):
    product_id = int(state["product_id"])
    symbol = state["symbol"]
    entry_mark = float(state.get("entry_mark") or 0)
    contract_val = float(state.get("contract_value") or 0.001)
    recorded_lots = int(state.get("lots") or 0)

    # DRY_RUN gates only new exposure. A close always starts with an
    # authenticated exchange-size check and finishes with another one.
    position = get_mv_position(product_id)
    actual_size = int(float(position.get("size") or 0)) if position else 0
    log.info("Exchange position: %d lots of %s", actual_size, symbol)
    if actual_size and abs(actual_size) != recorded_lots:
        log.warning("Position mismatch: expected %d lots, found %d. Using exchange figure.",
                    recorded_lots, actual_size)

    is_short = state.get("side") == "short" or actual_size < 0
    pnl_sign = -1 if is_short else 1
    close_side = "buy" if is_short else "sell"
    close_size = abs(actual_size)
    btc_entry = float(state.get("btc_at_entry") or 0)
    exit_mark = 0.0
    btc_exit = btc_entry
    btc_move = 0.0
    close_order = {}
    close_fee = None
    close_fee_source = "none"
    remaining_size = 0
    filled_lots = 0
    verified_fill = None
    had_close_attempt = False
    pending_id = state.get("pending_close_order_id")
    client_id = str(state.get("pending_close_client_order_id") or "")
    pending_status = str(state.get("pending_close_submission_state") or "").lower()
    requested_size = int(float(state.get("pending_close_requested_lots") or 0))
    attempt_start_signed = int(float(state.get("pending_close_start_size") or 0))
    intent_created = False
    stored_close_side = str(state.get("pending_close_side") or "").lower()
    if stored_close_side and stored_close_side != close_side:
        raise RuntimeError(
            f"pending close side {stored_close_side} conflicts with required {close_side}")

    # A persisted client id is the durable identity of the close. It is
    # reconciled even when the exchange already reports flat, because a lost
    # POST response may be the order that flattened the position.
    if pending_id or client_id:
        if not client_id:
            raise RuntimeError(
                f"pending close order {pending_id} has no durable client identity")
        pending = _lookup_owned_order(pending_id, client_id, product_id)
        if pending:
            close_order = _validate_recovered_close_order(
                pending, product_id=product_id, client_order_id=client_id,
                side=close_side, order_id=pending_id)
            pending_id = close_order.get("id")
            state.update({
                "pending_close_order_id": pending_id,
                "pending_close_order_state": _order_state(close_order),
                "pending_close_submission_state": "acknowledged",
                "pending_close_last_error": "",
            })
            save_fn(state)
        elif pending_status != "prepared" or pending_id:
            state["pending_close_last_error"] = (
                "exact close order identity is not yet visible; duplicate submission blocked")
            save_fn(state)
            raise RuntimeError(
                f"cannot verify pending close identity {client_id}; duplicate close blocked")

    if client_id and not close_order and close_size == 0:
        state["pending_close_last_error"] = (
            "exchange is flat but exact close order identity is not yet visible")
        save_fn(state)
        raise RuntimeError(
            f"close identity {client_id} is unresolved; flat-state accounting deferred")

    if not close_order and close_size > 0 and not client_id:
        client_id = _close_client_id(label.lower())
        requested_size = close_size
        attempt_start_signed = actual_size
        intent_created = True
        state.update({
            "pending_close_order_id": None,
            "pending_close_client_order_id": client_id,
            "pending_close_requested_lots": requested_size,
            "pending_close_start_size": attempt_start_signed,
            "pending_close_side": close_side,
            "pending_close_submission_state": "prepared",
            "pending_close_started_at_utc": datetime.now(timezone.utc).isoformat(),
            "pending_close_last_error": "",
            "pending_close_order_state": None,
            "pending_close_reason": f"scheduled_exit_{label.lower()}",
        })
        # This write is deliberately before network I/O. A response can never
        # be lost without leaving an exact exchange identity for recovery.
        save_fn(state)

    if not close_order and client_id and close_size > 0:
        # A prior response-loss state is never blindly retried. A `prepared`
        # intent may be submitted with the SAME id (including after a crash in
        # the tiny persist-before-POST window); `submission_unknown` must first
        # become visible through exact exchange reconciliation.
        if not intent_created and pending_status not in {"", "prepared"}:
            raise RuntimeError(
                f"close submission {client_id} remains unresolved; duplicate close blocked")
        if requested_size < 1:
            requested_size = close_size
        if not attempt_start_signed:
            attempt_start_signed = actual_size
        try:
            close_response = place_market_order(
                product_id, symbol, close_side, requested_size, force_real=True,
                client_order_id=client_id, reduce_only=True,
            )
            close_order = close_response.get("result") or {}
        except Exception as submit_exc:
            recovered = _lookup_owned_order(None, client_id, product_id)
            if not recovered:
                state.update({
                    "pending_close_submission_state": "submission_unknown",
                    "pending_close_last_error": str(submit_exc),
                })
                save_fn(state)
                raise RuntimeError(
                    f"close response lost for {client_id}; exact recovery pending") from submit_exc
            close_order = _validate_recovered_close_order(
                recovered, product_id=product_id,
                client_order_id=client_id, side=close_side)

        close_order = _validate_recovered_close_order(
            close_order, product_id=product_id,
            client_order_id=client_id, side=close_side)
        pending_id = close_order.get("id")
        state.update({
            "pending_close_order_id": pending_id,
            "pending_close_submission_state": "acknowledged",
            "pending_close_order_state": _order_state(close_order),
            "pending_close_last_error": "",
        })
        save_fn(state)

    if close_order:
        requested_size = (
            requested_size or abs(attempt_start_signed) or close_size or recorded_lots)
        attempt_start_signed = attempt_start_signed or (
            -requested_size if close_side == "buy" else requested_size)
        # Market orders normally close synchronously; refresh an incomplete
        # response so fills and commissions are not guessed from submission.
        verified, verified_fill = _wait_for_terminal_fill(
            close_order, requested_size, product_id, client_id,
            timeout_sec=float(os.getenv("CLOSE_ORDER_VERIFY_TIMEOUT_SEC", "8")),
        )
        if verified:
            close_order = _validate_recovered_close_order(
                verified, product_id=product_id,
                client_order_id=client_id, side=close_side,
                order_id=close_order.get("id"))
        order_state = _order_state(close_order)
        state.update({
            "pending_close_order_id": close_order.get("id") or pending_id,
            "pending_close_order_state": order_state,
        })
        if order_state not in _TERMINAL_ORDER_STATES:
            state["pending_close_submission_state"] = (
                "active" if order_state in _ACTIVE_ORDER_STATES else "ambiguous")
            state["pending_close_last_error"] = (
                f"close order has non-terminal state {order_state or 'unknown'}")
            save_fn(state)
            raise RuntimeError(
                f"close order {close_order.get('id') or client_id} is not terminal; duplicate blocked")

        had_close_attempt = True
        # Give Delta a brief consistency window, then treat position size as
        # the final authority. reduce_only prevents this order from reversing.
        for attempt in range(4):
            live_after = get_mv_position(product_id)
            remaining_size = int(float(live_after.get("size") or 0)) if live_after else 0
            if remaining_size == 0 or attempt == 3:
                break
            time.sleep(0.5)
        if remaining_size and remaining_size * attempt_start_signed < 0:
            raise RuntimeError(
                "reduce-only close produced an impossible side reversal: "
                f"{attempt_start_signed} -> {remaining_size}")
        if abs(remaining_size) > abs(attempt_start_signed):
            raise RuntimeError(
                f"position grew during close: {attempt_start_signed} -> {remaining_size}")
        filled_lots = max(abs(attempt_start_signed) - abs(remaining_size), 0)
        if verified_fill is not None and verified_fill != filled_lots:
            log.warning("Close fill/position delta mismatch (%d vs %d); position delta is authoritative.",
                        verified_fill, filled_lots)
        try:
            real_fill = float(close_order.get("average_fill_price") or 0)
        except (TypeError, ValueError):
            real_fill = 0.0
        if real_fill > 0:
            exit_mark = real_fill
        for fee_key in ("paid_commission", "commission", "total_commission"):
            if close_order.get(fee_key) not in (None, ""):
                close_fee = abs(float(close_order[fee_key]))
                close_fee_source = "exchange"
                break
    elif close_size == 0:
        log.warning("Exchange already reports zero size for %s; no close order placed.", symbol)

    # Market data is accounting-only. A ticker outage must never prevent the
    # authenticated reduce-only order above from reducing exposure.
    if exit_mark <= 0:
        try:
            exit_mark = float(get_mv_mark(symbol) or 0)
        except Exception:
            exit_mark = 0.0
    try:
        btc_exit = float(get_btc_price() or btc_entry)
    except Exception as exc:
        log.warning("BTC accounting snapshot unavailable after close: %s", exc)
        btc_exit = btc_entry
    btc_move = (btc_exit - btc_entry) / btc_entry * 100 if btc_entry else 0
    if had_close_attempt and close_fee is None:
        # With no spot snapshot, the premium cap is the conservative upper
        # bound rather than incorrectly estimating a zero fee.
        fee_basis = (min(OPTION_FEE_RATE * btc_exit,
                         OPTION_FEE_CAP_PCT * max(exit_mark, 0))
                     if btc_exit > 0 else OPTION_FEE_CAP_PCT * max(exit_mark, 0))
        close_fee = fee_basis * contract_val * filled_lots
        close_fee_source = "configured_estimate"
    close_fee = float(close_fee or 0)

    previous_gross = float(state.get("partial_exit_gross_pnl_usd") or 0)
    previous_exit_fees = float(state.get("partial_exit_fees_usd") or 0)
    current_gross = ((exit_mark - entry_mark) * pnl_sign * contract_val * filled_lots
                     if entry_mark > 0 and exit_mark > 0 else 0.0)
    gross_pnl_usd = previous_gross + current_gross
    total_exit_fees = previous_exit_fees + close_fee

    if remaining_size != 0:
        # Never announce/record CLOSED while any exchange exposure remains.
        # The exact order is terminal, so account its fill once and clear the
        # pending identity before a later retry creates a new close intent.
        terminal_order_id = close_order.get("id")
        terminal_client_id = client_id
        terminal_state = _order_state(close_order)
        _clear_pending_close(state)
        state.update({
            "status": "OPEN", "lots": abs(remaining_size),
            "partial_exit_gross_pnl_usd": round(gross_pnl_usd, 8),
            "partial_exit_fees_usd": round(total_exit_fees, 8),
            "last_close_order_id": terminal_order_id,
            "last_close_client_order_id": terminal_client_id,
            "last_close_order_state": terminal_state,
            "last_close_reason": f"scheduled_exit_{label.lower()}",
            "last_partial_exit_mark": exit_mark,
            "last_partial_exit_lots": filled_lots,
            "protection_last_error": f"scheduled close left {abs(remaining_size)} lots open",
        })
        save_fn(state)
        audit_event(DATA_DIR, "move_close_incomplete", {
            "slot": label.lower(), "symbol": symbol, "attempted_lots": requested_size,
            "filled_lots": filled_lots, "remaining_lots": abs(remaining_size),
            "exit_order_id": terminal_order_id,
        })
        send_telegram(
            f"🚨 <b>{label} EXIT INCOMPLETE — {TAG}</b>\n"
            f"<code>{symbol}</code> still has <code>{abs(remaining_size)}</code> lots open.\n"
            "State remains OPEN and exchange protection is retained."
        )
        raise RuntimeError(
            f"scheduled close left {abs(remaining_size)} lots open for {symbol}")

    # Exposure is now independently verified flat. If it was already zero,
    # mark the accounting as estimated rather than inventing a fill order.
    if not had_close_attempt and close_size == 0:
        filled_lots = recorded_lots
        current_gross = ((exit_mark - entry_mark) * pnl_sign * contract_val * filled_lots
                         if entry_mark > 0 and exit_mark > 0 else 0.0)
        gross_pnl_usd = previous_gross + current_gross
        state["exit_price_source"] = "mark_after_exchange_flat"
    entry_fee = float(state.get("entry_commission_usd")
                      or state.get("entry_fees_usd") or 0)
    fees_usd = entry_fee + total_exit_fees
    pnl_usd = gross_pnl_usd - fees_usd
    ret_pct = ((exit_mark - entry_mark) * pnl_sign / entry_mark * 100
               if entry_mark > 0 and exit_mark > 0 else 0)
    log.info("Entry/exit  : $%.4f / $%.4f  (%+.1f%%)", entry_mark, exit_mark, ret_pct)
    log.info("Gross/fees/net: $%.2f / $%.2f / $%.2f", gross_pnl_usd, fees_usd, pnl_usd)

    _clear_pending_close(state)
    state.update({
        "status": "CLOSED",
        "exit_time_utc": datetime.now(timezone.utc).strftime("%H:%M:%S"),
        "btc_at_exit": round(btc_exit, 2),
        "btc_move_pct": round(btc_move, 4),
        "exit_mark": exit_mark,
        "pnl_usd": round(pnl_usd, 4),
        "gross_pnl_usd": round(gross_pnl_usd, 4),
        "fees_usd": round(fees_usd, 4),
        "exit_commission_usd": round(total_exit_fees, 8),
        "exit_fee_source": close_fee_source,
        "pnl_includes_fees": True,
        "exit_trigger": (exit_trigger_override or
                         (f"scheduled_exit_{label.lower()}" if had_close_attempt
                          else "exchange_already_flat")),
        "exit_order_id": close_order.get("id"),
        "exit_client_order_id": client_id or None,
        "closed_lots": int(state.get("owned_entry_lots") or recorded_lots),
        "history_pending": True,
        "history_logged": False,
    })
    save_fn(state)
    history_complete = log_trade(state)
    state["history_pending"] = not history_complete
    state["history_logged"] = bool(history_complete)
    if history_complete:
        state["history_logged_at_utc"] = datetime.now(timezone.utc).isoformat()
    save_fn(state)
    closed_lots = int(state.get("closed_lots") or filled_lots or recorded_lots)
    audit_event(DATA_DIR, "move_position_closed", {
        "slot": label.lower(), "symbol": symbol, "lots": closed_lots,
        "gross_pnl_usd": round(gross_pnl_usd, 4), "fees_usd": round(fees_usd, 4),
        "net_pnl_usd": round(pnl_usd, 4), "exit_order_id": close_order.get("id"),
    })
    log.info("%s straddle CLOSED. P&L: $%.2f", label, pnl_usd)

    _win   = pnl_usd >= 0
    _icon  = "✅" if _win else "❌"
    _label = "WIN" if _win else "LOSS"
    _sign  = "+" if _win else ""
    _arrow = "▲" if btc_move >= 0 else "▼"
    _slot_icon = "🌅" if label == "MORNING" else "🌇"
    send_telegram(
        f"{_icon} <b>{_slot_icon} {label} EXIT — {TAG} · {_label}  {_sign}${abs(pnl_usd):.2f}</b>\n"
        f"<code>{'━' * 24}</code>\n"
        f"Symbol  » <code>{symbol}</code>\n"
        f"Entry   » <code>${entry_mark:.4f} / BTC</code>\n"
        f"Exit    » <code>${exit_mark:.4f} / BTC</code>\n"
        f"BTC Δ   » <code>{_arrow}{abs(btc_move):.2f}%  "
        f"(${btc_entry:,.0f} → ${state.get('btc_at_exit', 0):,.0f})</code>\n"
        f"PnL     » <b>{_sign}${abs(pnl_usd):.2f}</b>\n"
        f"Lots    » <code>{closed_lots:,}</code>\n"
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


def _scheduled_exit_due(state: dict | None, hour_utc: int,
                        minute_utc: int) -> datetime | None:
    """Exit instant belonging to this specific trade, including rollover."""
    if not state or state.get("status") != "OPEN":
        return None
    try:
        entry_date = datetime.strptime(str(state["entry_date"]), "%Y-%m-%d").date()
        entry_clock = datetime.strptime(
            str(state.get("entry_time_utc") or "00:00:00")[:8], "%H:%M:%S").time()
    except (KeyError, TypeError, ValueError):
        # An OPEN state with no trustworthy entry identity is not safe to
        # schedule from; surface it rather than silently using today's date.
        raise RuntimeError("open position has invalid entry date/time for scheduled exit")
    entered = datetime.combine(entry_date, entry_clock, tzinfo=timezone.utc)
    due = datetime.combine(entry_date, datetime.min.time(), tzinfo=timezone.utc).replace(
        hour=hour_utc, minute=minute_utc)
    if due <= entered:
        due += timedelta(days=1)
    return due


def _resume_mode_namespace(dry_run: bool) -> None:
    """Recover one namespace without letting the selected entry mode hide it."""
    label = "DRY-RUN" if dry_run else "REAL"
    with _storage_namespace(dry_run):
        if not dry_run:
            recover_pending_entries()
        _flush_pending_move_history()
        for slot, load_fn in (
            ("evening", load_state),
            ("morning", load_morning_state),
        ):
            state = load_fn()
            if not state or state.get("status") != "OPEN":
                continue
            log.info("Resuming %s %s position: %s (entered %s UTC, lots=%s)",
                     label, slot.upper(), state.get("symbol"),
                     state.get("entry_time_utc"), state.get("lots"))
            if dry_run:
                # Scheduled paper exits are handled by this engine.  A
                # dedicated paper TP/SL worker may also attach independently,
                # but no exchange-protection monitor belongs to this state.
                continue
            if not start_tp_monitor(slot):
                _emergency_flatten_unprotected(slot)


def _flush_all_mode_histories() -> None:
    for dry_run in (False, True):
        try:
            with _storage_namespace(dry_run):
                _flush_pending_move_history()
        except Exception:
            log.exception("%s history flush failed",
                          "DRY-RUN" if dry_run else "REAL")


def main():
    log.info("=" * 64)
    log.info("Delta MV Straddle Bot")
    # A mode switch governs new entries only. Existing real exposure and paper
    # simulations remain independently recoverable/closable.
    _resume_mode_namespace(False)
    _resume_mode_namespace(True)
    log.info("  Morning: %02d:%02d UTC (%s)  lots=%d  side=AUTO  enabled=%s",
             MORNING_H_UTC, MORNING_M_UTC,
             _ist_label(MORNING_H_UTC, MORNING_M_UTC), MORNING_LOTS,
             MORNING_ENABLED)
    log.info("  M-Exit : %s",
             ("%02d:%02d UTC (%s)" % (MORNING_EXIT_H_UTC, MORNING_EXIT_M_UTC,
                                       _ist_label(MORNING_EXIT_H_UTC, MORNING_EXIT_M_UTC)))
             if MORNING_EXIT_ENABLED else "DISABLED (TP/settlement only)")
    log.info("  Entry  : %02d:%02d UTC (%s)  side=AUTO  enabled=%s",
             ENTRY_H_UTC, ENTRY_M_UTC,
             _ist_label(ENTRY_H_UTC, ENTRY_M_UTC), EVENING_ENABLED)
    log.info("  Exit   : %s",
             ("%02d:%02d UTC (%s)" % (EXIT_H_UTC, EXIT_M_UTC,
                                       _ist_label(EXIT_H_UTC, EXIT_M_UTC)))
             if EVENING_EXIT_ENABLED else "DISABLED (TP/settlement only)")
    log.info("  Lots   : %d  |  DRY-RUN: %s  |  MOVE AUTO: %s",
             LOTS, DRY_RUN, MOVE_AUTO_ENTRY_MODE.upper())
    log.info("=" * 64)

    fired_entry        = False
    fired_morning      = False
    next_exit_retry = {
        (False, "morning"): 0.0, (False, "evening"): 0.0,
        (True, "morning"): 0.0, (True, "evening"): 0.0,
    }
    next_pending_recovery = 0.0
    next_history_flush = 0.0
    last_day           = None
    env_mtime          = ENV_FILE.stat().st_mtime if ENV_FILE.exists() else 0
    cfg_mtime          = CFG_FILE.stat().st_mtime if CFG_FILE.exists() else 0

    while True:
        try:
            now     = datetime.now(timezone.utc)
            h, m    = now.hour, now.minute
            today   = now.strftime("%Y-%m-%d")

            # CONFIG WATCH runs before every entry trigger.  The mode is also
            # re-read under the account entry lock, closing the remaining
            # sub-second save/reload race.
            env_mt = ENV_FILE.stat().st_mtime if ENV_FILE.exists() else 0
            cfg_mt = CFG_FILE.stat().st_mtime if CFG_FILE.exists() else 0
            if (env_mt != env_mtime or cfg_mt != cfg_mtime) \
               and time.time() - max(env_mt, cfg_mt) > 2:
                src = "config.json" if cfg_mt != cfg_mtime else ".env"
                log.info("Config change detected in %s — reloading bot with new settings.", src)
                send_telegram(f"🔄 <b>CONFIG CHANGED — BOT RELOADED ({TAG})</b>\n"
                              "New settings are now in effect.")
                os.execv(sys.executable, [sys.executable] + sys.argv)

            if (time.time() >= next_pending_recovery
                    and any(LIVE_DATA_DIR.glob("pending_*_entry.json"))):
                next_pending_recovery = time.time() + 60
                with _storage_namespace(False):
                    recover_pending_entries()
            if time.time() >= next_history_flush:
                next_history_flush = time.time() + 60
                _flush_all_mode_histories()

            # Reset daily flags on new UTC day
            if today != last_day:
                fired_entry        = False
                fired_morning      = False
                for retry_key in next_exit_retry:
                    next_exit_retry[retry_key] = 0.0
                last_day           = today
                log.info("New UTC day: %s — daily flags reset.", today)

            # MORNING ENTRY TRIGGER  00:15–00:24 UTC (5:45 AM IST)
            in_morning_window = (MORNING_ENABLED
                                 and h == MORNING_H_UTC
                                 and MORNING_WIN_START <= m < MORNING_WIN_END)
            if in_morning_window and not fired_morning:
                try:
                    result = morning_entry_job()
                    fired_morning = bool(
                        isinstance(result, dict) and result.get("terminal"))
                except Exception as exc:
                    log.exception("Morning entry job failed")
                    send_telegram(f"⚠️ <b>MORNING ENTRY FAILED — {TAG}</b>\n<code>{exc}</code>")

            # Existing REAL and DRY-RUN positions retain independent scheduled
            # exits after a mode switch.  A paper close is local-only; a real
            # close still uses the authenticated reduce-only path.
            for dry_run in (False, True):
                retry_key = (dry_run, "morning")
                try:
                    with _storage_namespace(dry_run):
                        morning_open = load_morning_state()
                        morning_due = (_scheduled_exit_due(
                            morning_open, MORNING_EXIT_H_UTC, MORNING_EXIT_M_UTC)
                            if MORNING_EXIT_ENABLED and morning_open
                            and morning_open.get("status") == "OPEN" else None)
                        if (morning_due and now >= morning_due
                                and time.time() >= next_exit_retry[retry_key]):
                            morning_exit_job()
                except Exception as exc:
                    next_exit_retry[retry_key] = time.time() + 60
                    mode_label = "DRY-RUN" if dry_run else "REAL"
                    log.exception("%s morning exit job failed", mode_label)
                    send_telegram(
                        f"⚠️ <b>{mode_label} MORNING EXIT FAILED — {TAG}</b>\n"
                        f"<code>{exc}</code>\n"
                        "The bot will retry in 60 seconds while state remains OPEN.")

            # ENTRY TRIGGER  12:05–12:14 UTC (skipped when EVENING_ENABLED=false)
            in_entry_window = (EVENING_ENABLED
                               and h == ENTRY_H_UTC
                               and ENTRY_WIN_START <= m < ENTRY_WIN_END)
            if in_entry_window and not fired_entry:
                try:
                    result = entry_job()
                    fired_entry = bool(
                        isinstance(result, dict) and result.get("terminal"))
                except Exception as exc:
                    log.exception("Entry job failed")
                    send_telegram(f"⚠️ <b>ENTRY FAILED — {TAG}</b>\n<code>{exc}</code>")

            for dry_run in (False, True):
                retry_key = (dry_run, "evening")
                try:
                    with _storage_namespace(dry_run):
                        evening_open = load_state()
                        evening_due = (_scheduled_exit_due(
                            evening_open, EXIT_H_UTC, EXIT_M_UTC)
                            if EVENING_EXIT_ENABLED and evening_open
                            and evening_open.get("status") == "OPEN" else None)
                        if (evening_due and now >= evening_due
                                and time.time() >= next_exit_retry[retry_key]):
                            exit_job()
                except Exception as exc:
                    next_exit_retry[retry_key] = time.time() + 60
                    mode_label = "DRY-RUN" if dry_run else "REAL"
                    log.exception("%s evening exit job failed", mode_label)
                    send_telegram(
                        f"⚠️ <b>{mode_label} EVENING EXIT FAILED — {TAG}</b>\n"
                        f"<code>{exc}</code>\n"
                        "The bot will retry in 60 seconds while state remains OPEN.")

            # Heartbeat every 10 min — reports both slots
            if m % 10 == 0 and now.second < POLL_SEC:
                def _slot_desc(s):
                    if not s or not s.get("status") or s.get("status") == "IDLE":
                        return "idle"
                    return f"{s.get('status', '?')} {s.get('symbol', '?')} x{s.get('lots', '?')}"
                snapshots = {}
                for dry_run in (False, True):
                    with _storage_namespace(dry_run):
                        key = "dry" if dry_run else "real"
                        snapshots[key] = (
                            _slot_desc(load_morning_state()),
                            _slot_desc(load_state()),
                        )
                log.info(
                    "Heartbeat %s UTC  real[morning=%s evening=%s]  "
                    "dry[morning=%s evening=%s]",
                    now.strftime("%H:%M"),
                    snapshots["real"][0], snapshots["real"][1],
                    snapshots["dry"][0], snapshots["dry"][1],
                )
                check_api_access()

        except Exception:
            log.exception("Main loop error")

        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
