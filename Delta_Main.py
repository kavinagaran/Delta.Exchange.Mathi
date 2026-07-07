"""
Delta Exchange BTC Futures Bot — Flawless Trend Strategy

Signal logic (all three must agree on 15-min candles):
  Bullish : Supertrend(ATR=10, mult=3.0) = bullish
            AND close > EMA(200)
            AND RSI(14) > 50
  Bearish : Supertrend = bearish
            AND close < EMA(200)
            AND RSI(14) < 50

Instrument : BTCUSD perpetual futures (not options)
Sides      : Long AND Short
Exit       : Signal reversal only — no fixed stop-loss
             Bullish → Bearish : close long, open short
             Bearish → Bullish : close short, open long
One trade per signal — no re-entry while the same signal persists
"""

import ctypes
import hashlib
import hmac
import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from urllib.parse import urlencode

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

# ─────────────────────────────────────────────
# ENVIRONMENT + CONFIG
# ─────────────────────────────────────────────
load_dotenv()

BASE_URL         = os.getenv("BASE_URL", "https://api.india.delta.exchange")
API_KEY          = os.getenv("API_KEY", "")
API_SECRET       = os.getenv("API_SECRET", "")
PERPETUAL_SYMBOL = os.getenv("PERPETUAL_SYMBOL", "BTCUSD")
ORDER_SIZE       = int(os.getenv("ORDER_SIZE", 1000))       # USD notional (1 contract = $1)
RSI_PERIOD       = int(os.getenv("RSI_PERIOD", 14))
RSI_BULL         = float(os.getenv("RSI_BULL", 55))         # RSI must be > this for longs
RSI_BEAR         = float(os.getenv("RSI_BEAR", 45))         # RSI must be < this for shorts
EMA_PERIOD       = int(os.getenv("EMA_PERIOD", 200))
SUPERTREND_ATR   = int(os.getenv("SUPERTREND_ATR", 10))
SUPERTREND_MULT  = float(os.getenv("SUPERTREND_MULT", 3.0))
ADX_PERIOD       = int(os.getenv("ADX_PERIOD", 14))
ADX_THRESHOLD    = float(os.getenv("ADX_THRESHOLD", 30))    # skip entry if ADX ≤ this
CANDLE_RES       = os.getenv("CANDLE_RES", "1h")
CANDLES_NEEDED   = int(os.getenv("CANDLES_NEEDED", 300))    # must be > EMA_PERIOD
POLL_INTERVAL    = int(os.getenv("POLL_INTERVAL", 60))
FAST2SMS_API_KEY = os.getenv("FAST2SMS_API_KEY", "")
ALERT_PHONE      = os.getenv("ALERT_PHONE", "")

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────
ROOT_DIR = Path(__file__).parent
LOG_DIR  = ROOT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

_fmt = logging.Formatter(
    "%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
_fh = TimedRotatingFileHandler(
    LOG_DIR / "delta_bot.log",
    when="midnight", interval=1, backupCount=30, utc=True
)
_fh.setFormatter(_fmt)
_ch = logging.StreamHandler(sys.stdout)
_ch.setFormatter(_fmt)

log = logging.getLogger("delta_bot")
log.setLevel(logging.INFO)
log.addHandler(_fh)
log.addHandler(_ch)

# ─────────────────────────────────────────────
# STARTUP GUARD
# ─────────────────────────────────────────────
if not API_KEY or not API_SECRET:
    log.critical("API_KEY and API_SECRET must be set in .env — aborting.")
    sys.exit(1)

# ─────────────────────────────────────────────
# SINGLE-INSTANCE LOCK
# ─────────────────────────────────────────────
LOCK_FILE = ROOT_DIR / "delta_bot.lock"

def _pid_alive(pid: int) -> bool:
    import subprocess
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True
        ).stdout
        return str(pid) in out
    except Exception:
        return False

def acquire_lock() -> bool:
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
            if _pid_alive(pid):
                log.warning("Another instance is already running (PID %d). Exiting.", pid)
                return False
        except (ValueError, OSError):
            pass
    LOCK_FILE.write_text(str(os.getpid()))
    return True

def release_lock() -> None:
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass

# ─────────────────────────────────────────────
# AUTHENTICATION HELPER
# ─────────────────────────────────────────────
def generate_signature(secret: str, message: str) -> str:
    return hmac.new(
        bytes(secret, "utf-8"),
        bytes(message, "utf-8"),
        hashlib.sha256
    ).hexdigest()

def get_auth_headers(method: str, path: str, query: str = "", body: str = "") -> dict:
    timestamp = str(int(time.time()))
    sig_data  = method + timestamp + path + query + body
    signature = generate_signature(API_SECRET, sig_data)
    return {
        "api-key":      API_KEY,
        "timestamp":    timestamp,
        "signature":    signature,
        "Content-Type": "application/json",
        "User-Agent":   "python-btc-futures-bot",
    }

# ─────────────────────────────────────────────
# API HELPERS
# ─────────────────────────────────────────────
class DeltaAPIError(RuntimeError):
    def __init__(self, code: str, context: dict, raw: str):
        super().__init__(code)
        self.code    = code
        self.context = context
        self.raw     = raw

def api_get(path: str, params: dict = None, auth: bool = False) -> dict:
    url     = BASE_URL + path
    headers = get_auth_headers("GET", path, "") if auth else {}
    resp    = requests.get(url, params=params, headers=headers, timeout=(3, 27))
    resp.raise_for_status()
    return resp.json()

def api_post(path: str, payload: dict) -> dict:
    body    = json.dumps(payload)
    headers = get_auth_headers("POST", path, "", body)
    resp    = requests.post(url=BASE_URL + path, data=body, headers=headers, timeout=(3, 27))
    if not resp.ok:
        try:
            err  = resp.json().get("error", {})
            code = err.get("code", "unknown")
            ctx  = err.get("context", {})
            raise DeltaAPIError(code, ctx, resp.text)
        except (ValueError, AttributeError):
            pass
        log.error("API POST %s  status=%d  body=%s", path, resp.status_code, resp.text)
        resp.raise_for_status()
    return resp.json()

def api_delete(path: str, params: dict = None, body: str = "") -> dict:
    query_str = ("?" + urlencode(params)) if params else ""
    headers   = get_auth_headers("DELETE", path, query_str, body)
    resp      = requests.delete(
        BASE_URL + path,
        params=params,
        data=body or None,
        headers=headers,
        timeout=(3, 27),
    )
    if not resp.ok:
        log.error("API DELETE %s  status=%d  body=%s", path, resp.status_code, resp.text)
    resp.raise_for_status()
    return resp.json()

# ─────────────────────────────────────────────
# STARTUP: RESOLVE PRODUCT ID
# ─────────────────────────────────────────────
_product_id: int | None = None

def resolve_product_id() -> int:
    data = api_get(f"/v2/tickers/{PERPETUAL_SYMBOL}")
    if not data.get("success"):
        raise RuntimeError(f"Could not resolve product_id for {PERPETUAL_SYMBOL}: {data}")
    pid = int(data["result"]["product_id"])
    log.info("Resolved %s → product_id=%d", PERPETUAL_SYMBOL, pid)
    return pid

# ─────────────────────────────────────────────
# STEP 1 – FETCH CANDLES
# ─────────────────────────────────────────────
_RES_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900,
    "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400,
}

def fetch_candles(symbol: str, resolution: str, count: int) -> pd.DataFrame:
    end   = int(time.time())
    start = end - _RES_SECONDS[resolution] * count
    data  = api_get("/v2/history/candles", params={
        "symbol": symbol, "resolution": resolution, "start": start, "end": end,
    })
    if not data.get("success") or not data.get("result"):
        raise RuntimeError(f"Failed to fetch candles for {symbol}: {data}")
    df = pd.DataFrame(data["result"])
    df.rename(columns={"time": "timestamp"}, inplace=True)
    df = df[["timestamp", "open", "high", "low", "close", "volume"]].copy()
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df.sort_values("timestamp", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df

# ─────────────────────────────────────────────
# STEP 2 – FLAWLESS TREND INDICATORS
# ─────────────────────────────────────────────
def compute_ema(series: pd.Series, period: int) -> float:
    return float(series.ewm(span=period, adjust=False).mean().iloc[-1])

def compute_rsi(series: pd.Series, period: int) -> float:
    delta = series.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_g = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_l = loss.ewm(com=period - 1, min_periods=period).mean()
    rs    = avg_g / avg_l
    return float((100 - (100 / (1 + rs))).iloc[-1])

def compute_supertrend(df: pd.DataFrame, atr_period: int = 10, multiplier: float = 3.0) -> str:
    high  = df["high"].copy()
    low   = df["low"].copy()
    close = df["close"].copy()

    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)

    atr = tr.ewm(com=atr_period - 1, min_periods=atr_period).mean()
    hl2 = (high + low) / 2
    ub  = (hl2 + multiplier * atr).copy()
    lb  = (hl2 - multiplier * atr).copy()

    st  = pd.Series(index=df.index, dtype=float)
    direction = pd.Series(index=df.index, dtype=str)

    for i in range(1, len(df)):
        if close.iloc[i - 1] <= ub.iloc[i - 1]:
            ub.iloc[i] = min(ub.iloc[i], ub.iloc[i - 1])
        if close.iloc[i - 1] >= lb.iloc[i - 1]:
            lb.iloc[i] = max(lb.iloc[i], lb.iloc[i - 1])

        if i == 1:
            st.iloc[i]        = ub.iloc[i]
            direction.iloc[i] = "bearish"
        elif st.iloc[i - 1] == ub.iloc[i - 1]:
            if close.iloc[i] <= ub.iloc[i]:
                st.iloc[i]        = ub.iloc[i]
                direction.iloc[i] = "bearish"
            else:
                st.iloc[i]        = lb.iloc[i]
                direction.iloc[i] = "bullish"
        else:
            if close.iloc[i] >= lb.iloc[i]:
                st.iloc[i]        = lb.iloc[i]
                direction.iloc[i] = "bullish"
            else:
                st.iloc[i]        = ub.iloc[i]
                direction.iloc[i] = "bearish"

    return str(direction.iloc[-1])

def compute_adx(df: pd.DataFrame, period: int) -> float:
    high  = df["high"]
    low   = df["low"]
    close = df["close"]

    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)

    up   = high.diff()
    down = -low.diff()
    plus_dm  = pd.Series(
        np.where((up > down) & (up > 0), up, 0.0), index=df.index, dtype=float
    )
    minus_dm = pd.Series(
        np.where((down > up) & (down > 0), down, 0.0), index=df.index, dtype=float
    )

    atr14     = tr.ewm(com=period - 1, min_periods=period, adjust=False).mean()
    plus_di   = 100 * plus_dm.ewm(com=period - 1, min_periods=period, adjust=False).mean() / atr14
    minus_di  = 100 * minus_dm.ewm(com=period - 1, min_periods=period, adjust=False).mean() / atr14
    dx        = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx       = dx.ewm(com=period - 1, min_periods=period, adjust=False).mean()
    return float(adx.iloc[-1])

def get_signal(df: pd.DataFrame) -> str:
    """Flawless Trend: Supertrend(10,3) + EMA(200) + RSI 55/45 + ADX(14) > 30."""
    close = df["close"]
    price = float(close.iloc[-1])
    ema   = compute_ema(close, EMA_PERIOD)
    rsi   = compute_rsi(close, RSI_PERIOD)
    st    = compute_supertrend(df, SUPERTREND_ATR, SUPERTREND_MULT)
    adx   = compute_adx(df, ADX_PERIOD)

    log.info(
        "Price=%.2f  EMA%d=%.2f  RSI=%.2f  ST=%s  ADX=%.1f",
        price, EMA_PERIOD, ema, rsi, st, adx,
    )

    if adx <= ADX_THRESHOLD:
        log.info("ADX=%.1f ≤ %.0f — market ranging, skipping entry.", adx, ADX_THRESHOLD)
        return "neutral"

    if st == "bullish" and price > ema and rsi > RSI_BULL:
        return "bullish"
    if st == "bearish" and price < ema and rsi < RSI_BEAR:
        return "bearish"
    return "neutral"

# ─────────────────────────────────────────────
# STEP 3 – MARK PRICE
# ─────────────────────────────────────────────
def get_mark_price() -> float:
    data = api_get(f"/v2/tickers/{PERPETUAL_SYMBOL}")
    return float(data["result"]["mark_price"])

# ─────────────────────────────────────────────
# STEP 4 – ORDER MANAGEMENT
# ─────────────────────────────────────────────
def place_market_order(side: str, size: int) -> dict:
    payload = {
        "product_id": _product_id,
        "order_type": "market_order",
        "side":       side,
        "size":       size,
    }
    result = api_post("/v2/orders", payload)
    if not result.get("success"):
        raise RuntimeError(f"Market order failed: {result}")
    log.info("Market order: %s %d contracts  id=%s", side.upper(), size, result["result"]["id"])
    return result["result"]

def get_position_size() -> int:
    """Returns signed position size: positive = long, negative = short, 0 = flat."""
    data = api_get("/v2/positions", params={"product_id": _product_id}, auth=True)
    if data.get("success") and data.get("result"):
        return int(data["result"].get("size", 0))
    return 0

# ─────────────────────────────────────────────
# STEP 5 – BOT STATE
# ─────────────────────────────────────────────
class BotState:
    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.direction:   str | None = None   # "long" | "short"
        self.entry_price: float      = 0.0
        self.last_signal: str        = "neutral"

    def has_position(self) -> bool:
        return self.direction is not None

state = BotState()

# ─────────────────────────────────────────────
# STEP 6 – SYNC STATE WITH EXCHANGE
# ─────────────────────────────────────────────
def sync_position_state() -> None:
    """Detect positions closed externally (liquidation) and reset state."""
    if not state.has_position():
        return
    try:
        size = get_position_size()
        if size == 0:
            log.warning(
                "%s position is no longer open on exchange (liquidated?). Going flat.",
                state.direction.upper(),
            )
            state.reset()
    except Exception as e:
        log.warning("Could not verify open position size: %s", e)

# ─────────────────────────────────────────────
# STEP 7 – ENTER / EXIT POSITION
# ─────────────────────────────────────────────
def exit_current_position() -> None:
    if not state.has_position():
        return

    close_side = "sell" if state.direction == "long" else "buy"
    log.info("Closing %s position (signal reversal)...", state.direction.upper())
    try:
        payload = {
            "product_id":  _product_id,
            "order_type":  "market_order",
            "side":        close_side,
            "size":        ORDER_SIZE,
            "reduce_only": True,
        }
        result = api_post("/v2/orders", payload)
        if not result.get("success"):
            raise RuntimeError(f"Exit failed: {result}")
        log.info("Closed %s position", state.direction.upper())
    except Exception as e:
        log.error("Error closing position: %s", e)

    state.reset()

def enter_position(signal: str) -> None:
    if state.has_position():
        log.warning("enter_position() called while %s is already active. Skipping.",
                    state.direction)
        return

    try:
        mark = get_mark_price()
    except Exception as e:
        log.error("Could not fetch mark price: %s", e)
        return

    direction  = "long" if signal == "bullish" else "short"
    entry_side = "buy"  if signal == "bullish" else "sell"

    log.info("Entering %s: mark=%.2f", direction.upper(), mark)

    try:
        place_market_order(entry_side, ORDER_SIZE)
    except DeltaAPIError as e:
        if e.code == "insufficient_margin":
            avail  = float(e.context.get("available_balance", 0))
            needed = float(e.context.get("required_additional_balance", 0))
            log.error(
                "INSUFFICIENT MARGIN — %s  Available: $%.2f  Needed: $%.2f  "
                "Reduce ORDER_SIZE in .env or deposit funds.",
                direction.upper(), avail, needed,
            )
            _alert_insufficient_margin(direction, avail, needed)
        else:
            log.error("Order failed [%s]: %s", e.code, e.raw)
        return
    except Exception as e:
        log.error("Order failed: %s", e)
        return

    state.direction   = direction
    state.entry_price = mark
    state.last_signal = signal

    log.info("Position active: %s  size=%d  entry=%.2f", direction.upper(), ORDER_SIZE, mark)
    show_trade_alert(direction, mark)

# ─────────────────────────────────────────────
# STEP 8 – ALERTS
# ─────────────────────────────────────────────
_MB_OK           = 0x00
_MB_ICON_INFO    = 0x40
_MB_ICON_WARN    = 0x30
_MB_SYSTEM_MODAL = 0x1000

def show_trade_alert(direction: str, entry_price: float) -> None:
    arrow = "▲ LONG  — BUY  FUTURES" if direction == "long" else "▼ SHORT — SELL FUTURES"
    icon  = _MB_ICON_INFO if direction == "long" else _MB_ICON_WARN
    title = f"Delta Bot  |  {arrow}"
    body  = (
        f"Direction  :  {direction.upper()}\n"
        f"Symbol     :  {PERPETUAL_SYMBOL}\n"
        f"Entry      :  ${entry_price:,.2f}\n"
        f"Size       :  {ORDER_SIZE:,} contracts\n"
        f"Exit       :  On signal reversal only\n"
        f"Time       :  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )
    threading.Thread(
        target=ctypes.windll.user32.MessageBoxW,
        args=(0, body, title, _MB_OK | icon | _MB_SYSTEM_MODAL),
        daemon=True,
    ).start()
    sms = (
        f"Delta Bot | {'LONG' if direction == 'long' else 'SHORT'} {PERPETUAL_SYMBOL}\n"
        f"Entry: ${entry_price:,.2f}  Size: {ORDER_SIZE:,} contracts\n"
        f"Exit: signal reversal only"
    )
    send_sms_alert(sms)

def _alert_insufficient_margin(direction: str, avail: float, needed: float) -> None:
    threading.Thread(
        target=ctypes.windll.user32.MessageBoxW,
        args=(
            0,
            f"Direction : {direction.upper()}\n\n"
            f"Available balance  : ${avail:,.2f}\n"
            f"Additional needed  : ${needed:,.2f}\n\n"
            f"Current ORDER_SIZE : {ORDER_SIZE:,} contracts\n\n"
            f"➜  Reduce ORDER_SIZE in .env or deposit funds.",
            "⚠  Insufficient Margin — Trade Skipped",
            _MB_OK | _MB_ICON_WARN | _MB_SYSTEM_MODAL,
        ),
        daemon=True,
    ).start()
    send_sms_alert(
        f"Delta Bot | INSUFFICIENT MARGIN | {direction.upper()}\n"
        f"Available: ${avail:,.2f}  Needed: ${needed:,.2f}\n"
        f"Reduce ORDER_SIZE or deposit funds."
    )

def send_sms_alert(message: str) -> None:
    if not FAST2SMS_API_KEY or not ALERT_PHONE:
        return
    def _send():
        try:
            resp = requests.get(
                "https://www.fast2sms.com/dev/bulkV2",
                headers={"authorization": FAST2SMS_API_KEY},
                params={"route": "q", "message": message, "numbers": ALERT_PHONE, "flash": 0},
                timeout=10,
            )
            if not resp.json().get("return"):
                log.warning("Fast2SMS failed: %s", resp.json())
            else:
                log.info("SMS sent to %s", ALERT_PHONE)
        except Exception as e:
            log.warning("SMS send error: %s", e)
    threading.Thread(target=_send, daemon=True).start()

# ─────────────────────────────────────────────
# STEP 9 – GRACEFUL SHUTDOWN
# ─────────────────────────────────────────────
def _shutdown(*_) -> None:
    log.info("Shutdown signal received. Closing positions and releasing lock...")
    exit_current_position()
    release_lock()
    sys.exit(0)

signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT,  _shutdown)

# ─────────────────────────────────────────────
# STEP 10 – MAIN LOOP
# ─────────────────────────────────────────────
def run_bot() -> None:
    global _product_id
    _product_id = resolve_product_id()

    log.info("=" * 60)
    log.info("Delta Exchange BTC Futures Bot  |  LIVE TRADING")
    log.info(
        "Strategy : Flawless Trend — ST(%d,%.1f) + EMA(%d) + RSI(%d) %.0f/%.0f + ADX(%d)>%.0f",
        SUPERTREND_ATR, SUPERTREND_MULT, EMA_PERIOD, RSI_PERIOD,
        RSI_BULL, RSI_BEAR, ADX_PERIOD, ADX_THRESHOLD,
    )
    log.info("Symbol   : %s  |  Size: %d contracts  |  Exit: signal reversal only",
             PERPETUAL_SYMBOL, ORDER_SIZE)
    log.info("=" * 60)

    while True:
        try:
            now = datetime.now(timezone.utc)
            log.info("[%s UTC] Checking signal...", now.strftime("%Y-%m-%d %H:%M:%S"))

            sync_position_state()

            df  = fetch_candles(PERPETUAL_SYMBOL, CANDLE_RES, CANDLES_NEEDED)
            sig = get_signal(df)
            log.info("Signal: %s", sig.upper())

            if sig == "neutral":
                log.info("No actionable signal. Holding current state.")

            elif state.has_position() and state.last_signal == sig:
                # Same signal still active — stay in the position, no new trade
                log.info("Signal unchanged (%s). Holding %s.", sig.upper(), state.direction.upper())

            else:
                # New signal or reversal: close existing position first, then open opposite
                if state.has_position():
                    log.info("Signal reversed to %s — closing %s.",
                             sig.upper(), state.direction.upper())
                    exit_current_position()
                enter_position(sig)

        except Exception as e:
            log.error("Unhandled error: %s", e, exc_info=True)
            log.info("Retrying in %d seconds...", POLL_INTERVAL)

        time.sleep(POLL_INTERVAL)

# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────
if __name__ == "__main__":
    if not acquire_lock():
        sys.exit(1)
    try:
        run_bot()
    finally:
        release_lock()
