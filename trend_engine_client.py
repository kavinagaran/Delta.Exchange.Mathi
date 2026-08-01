"""Fail-closed client for the btc_trend_engine service (ADR 0001).

The only path by which ``dashboard.py`` talks to the engine.  Synchronous and
``requests``-based to match the existing dashboard, and deliberately incapable
of raising into a trading loop: every failure mode is converted into a
schema-shaped snapshot carrying ``regime=DEGRADED`` and
``entry_allowed=False``.

There is no caching of the last good snapshot.  A stale-but-recent value is
exactly the failure mode Trend_Engine.md §3.2 forbids: the caller must be able
to distinguish "the engine currently says no" from "the engine is not
answering", and both must block entry.

TTL is re-checked here as well as in the engine, so contract invariant 12
(an expired signal cannot create an order) holds even if the engine
misreports.
"""

from __future__ import annotations

import math
import os
import queue
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

EXPECTED_MAJOR = 1  # docs/trend-snapshot-contract.md v1.0.0
MAX_CLOCK_SKEW_SECONDS = 5.0

# Substituted by this client; never produced by the engine.
ENGINE_UNREACHABLE = "ENGINE_UNREACHABLE"
SCHEMA_MISMATCH = "SCHEMA_MISMATCH"
SIGNAL_EXPIRED = "SIGNAL_EXPIRED"
CONTRACT_VERSION_MISMATCH = "CONTRACT_VERSION_MISMATCH"
CLOCK_SKEW = "CLOCK_SKEW"

_REQUIRED_FIELDS = (
    "schema_version", "symbol", "timestamp", "candle_close_utc", "signal_id",
    "regime", "direction", "trend_score", "confidence", "entry_allowed",
    "signal_ttl_seconds", "data_quality", "reason_codes",
)


def engine_base_url() -> str:
    host = os.getenv("ENGINE_HOST", "127.0.0.1")
    port = os.getenv("ENGINE_PORT", "5055")
    return f"http://{host}:{port}"


def _timeout() -> float:
    try:
        return float(os.getenv("ENGINE_TIMEOUT_SECS", "1.5"))
    except (TypeError, ValueError):
        return 1.5


def degraded_snapshot(symbol: str, data_quality: str, detail: str = "",
                      now: datetime | None = None) -> dict[str, Any]:
    """A schema-shaped snapshot that blocks entry, for every failure path."""
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    stamp = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "schema_version": "1.0.0",
        "symbol": symbol,
        "timestamp": stamp,
        "candle_close_utc": stamp,
        "signal_id": "",
        "regime": "DEGRADED",
        "regime_since": stamp,
        "direction": 0,
        "trend_score": 0.0,
        "confidence": 0.0,
        "forecast_horizon_seconds": 0,
        "expected_return_bps": None,
        "expected_absolute_move_bps": None,
        "forecast_volatility_bps": None,
        "jump_probability": None,
        "invalidation_price": None,
        "suggested_stop_bps": None,
        "entry_allowed": False,
        "signal_ttl_seconds": 0,
        "components": [],
        "timeframes": [],
        "gates": [{"name": "engine_available", "passed": False,
                   "detail": detail or data_quality}],
        "reason_codes": [data_quality],
        "data_quality": data_quality,
        "feature_set_version": "",
        "model_version": "",
        "client_detail": detail,
    }


def _validate(payload: Any, symbol: str, now: datetime) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return degraded_snapshot(symbol, SCHEMA_MISMATCH,
                                 "response is not an object", now)
    missing = [field for field in _REQUIRED_FIELDS if field not in payload]
    if missing:
        return degraded_snapshot(symbol, SCHEMA_MISMATCH,
                                 f"missing fields: {', '.join(missing)}", now)

    version = str(payload.get("schema_version") or "")
    try:
        major = int(version.split(".")[0])
    except (ValueError, IndexError):
        return degraded_snapshot(symbol, CONTRACT_VERSION_MISMATCH,
                                 f"unparseable schema_version {version!r}", now)
    if major != EXPECTED_MAJOR:
        return degraded_snapshot(
            symbol, CONTRACT_VERSION_MISMATCH,
            f"engine speaks v{version}, client expects v{EXPECTED_MAJOR}.x", now)

    if payload.get("symbol") != symbol:
        return degraded_snapshot(
            symbol, SCHEMA_MISMATCH,
            f"snapshot is for {payload.get('symbol')!r}, requested {symbol!r}", now)

    stamp = _parse_iso(payload.get("timestamp"))
    if stamp is None:
        return degraded_snapshot(symbol, SCHEMA_MISMATCH,
                                 "unparseable timestamp", now)

    # Only *future* dating is clock skew. A snapshot legitimately ages between
    # production (every closed 5m candle) and this poll, so age in the past is
    # what TTL governs — checking abs() here would reject nearly every healthy
    # snapshot and make the TTL branch unreachable.
    age = (now - stamp).total_seconds()
    if age < -MAX_CLOCK_SKEW_SECONDS:
        return degraded_snapshot(
            symbol, CLOCK_SKEW,
            f"snapshot is dated {-age:.1f}s in the future", now)

    try:
        ttl = int(payload.get("signal_ttl_seconds") or 0)
    except (TypeError, ValueError):
        ttl = 0
    if ttl <= 0 or stamp + timedelta(seconds=ttl) < now:
        return degraded_snapshot(
            symbol, SIGNAL_EXPIRED,
            f"signal issued {stamp.isoformat()} with ttl {ttl}s", now)

    # Invariant 1, re-asserted client-side: a non-OK engine reading can never
    # arrive with entry_allowed true, whatever the engine claims.
    if payload.get("data_quality") != "OK" and payload.get("entry_allowed"):
        return degraded_snapshot(
            symbol, SCHEMA_MISMATCH,
            "engine set entry_allowed with data_quality "
            f"{payload.get('data_quality')!r}", now)
    return payload


def get_snapshot(symbol: str = "BTCUSD", *,
                 now: datetime | None = None) -> dict[str, Any]:
    """Always returns a snapshot-shaped dict. Never raises."""
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    token = os.getenv("ENGINE_TOKEN", "")
    try:
        response = requests.get(
            f"{engine_base_url()}/trend/latest",
            params={"symbol": symbol},
            headers={"X-Engine-Token": token},
            timeout=_timeout(),
        )
    except Exception as exc:
        return degraded_snapshot(symbol, ENGINE_UNREACHABLE,
                                 f"{type(exc).__name__}: {exc}", moment)
    if response.status_code != 200:
        return degraded_snapshot(symbol, ENGINE_UNREACHABLE,
                                 f"HTTP {response.status_code}", moment)
    try:
        payload = response.json()
    except ValueError:
        return degraded_snapshot(symbol, SCHEMA_MISMATCH,
                                 "response body is not JSON", moment)
    return _validate(payload, symbol, moment)


def get_health() -> dict[str, Any]:
    """Liveness for the dashboard's engine tile. Never raises."""
    try:
        response = requests.get(f"{engine_base_url()}/health",
                                timeout=_timeout())
        if response.status_code != 200:
            return {"ok": False, "detail": f"HTTP {response.status_code}"}
        body = response.json()
    except Exception as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    return {"ok": bool(body.get("ok")), "version": body.get("version")}


def get_status() -> dict[str, Any]:
    """Full engine status for the Trend Engine page. Never raises."""
    try:
        response = requests.get(
            f"{engine_base_url()}/status",
            headers={"X-Engine-Token": os.getenv("ENGINE_TOKEN", "")},
            timeout=_timeout(),
        )
        if response.status_code != 200:
            return {"available": False, "detail": f"HTTP {response.status_code}"}
        return {"available": True, **response.json()}
    except Exception as exc:
        return {"available": False, "detail": f"{type(exc).__name__}: {exc}"}


# ── shadow reporting (Phase 8a) ─────────────────────────────────────────
# Reporting the legacy decision to the engine is the one place this module
# WRITES. It is attached to a loop that places real orders, so it is
# fire-and-forget through a bounded queue and a daemon worker rather than an
# inline HTTP call: a hung engine must cost the trading loop exactly zero
# latency, and an 8-second timeout is not zero. See tests/test_shadow_survival.py.

SHADOW_QUEUE_MAX = 256

_shadow_queue: "queue.Queue[dict[str, Any]] | None" = None
_shadow_worker: threading.Thread | None = None
_shadow_lock = threading.Lock()
_shadow_dropped = 0


def _shadow_send(payload: dict[str, Any]) -> None:
    """The actual HTTP write. Patched wholesale in tests."""
    requests.post(
        f"{engine_base_url()}/shadow/legacy-decision",
        json=payload,
        headers={"X-Engine-Token": os.getenv("ENGINE_TOKEN", "")},
        timeout=_timeout(),
    )


def _shadow_loop(work_queue: "queue.Queue[dict[str, Any]]") -> None:
    # The queue is bound as an argument, not read from the module global: a
    # reset (or a future re-init) swaps the global while this thread is parked
    # in get(), and reading it back afterwards would hit whatever replaced it.
    while True:
        payload = work_queue.get()
        try:
            if payload is None:  # shutdown sentinel
                return
            _shadow_send(payload)
        except BaseException:
            # Deliberately bare: this thread exists to be unkillable. A
            # transport error, a bad payload, or a bug in _shadow_send must
            # not end the worker, because a dead worker silently stops
            # recording comparisons that the cutover decision depends on.
            pass
        finally:
            work_queue.task_done()


def _ensure_shadow_worker() -> "queue.Queue[dict[str, Any]]":
    global _shadow_queue, _shadow_worker
    with _shadow_lock:
        if _shadow_queue is None:
            _shadow_queue = queue.Queue(maxsize=SHADOW_QUEUE_MAX)
        if _shadow_worker is None or not _shadow_worker.is_alive():
            _shadow_worker = threading.Thread(
                target=_shadow_loop, args=(_shadow_queue,),
                name="shadow-reporter", daemon=True)
            _shadow_worker.start()
        return _shadow_queue


def post_legacy_decision(payload: dict[str, Any]) -> bool:
    """Enqueue one legacy decision for the engine. Never raises, never blocks.

    Returns True if enqueued, False if dropped because the queue is full
    (i.e. the engine is not draining). Dropping is the correct behaviour: the
    alternative is unbounded memory growth or a blocked trading loop, and a
    missing comparison is visible via ``shadow_dropped_count()``.
    """
    global _shadow_dropped
    try:
        work_queue = _ensure_shadow_worker()
        work_queue.put_nowait(dict(payload))
        return True
    except BaseException:
        # Includes queue.Full, and anything a caller passed that dict() chokes
        # on. Either way the trading loop learns nothing about it.
        _shadow_dropped += 1
        return False


def shadow_queue_depth() -> int:
    return 0 if _shadow_queue is None else _shadow_queue.qsize()


def shadow_dropped_count() -> int:
    return _shadow_dropped


def drain_shadow_queue_for_test(timeout: float = 2.0) -> bool:
    """Wait for the worker to finish outstanding work. Test-only."""
    if _shadow_queue is None:
        return True
    deadline = time.monotonic() + timeout
    while _shadow_queue.qsize() > 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    return _shadow_queue.qsize() == 0


def reset_shadow_transport_for_test() -> None:
    """Drop the queue and worker so each test starts clean. Test-only."""
    global _shadow_queue, _shadow_worker, _shadow_dropped
    with _shadow_lock:
        _shadow_queue = None
        _shadow_worker = None
        _shadow_dropped = 0


def get_live_view(symbol: str = "BTCUSD") -> dict[str, Any]:
    """Provisional display score. Never raises.

    Returns ``{"available": False, ...}`` on any failure rather than a
    degraded *snapshot*, deliberately: a degraded snapshot is shaped like a
    decision and something could try to trade it. This is display data and is
    shaped so it cannot be.
    """
    try:
        response = requests.get(
            f"{engine_base_url()}/trend/live",
            params={"symbol": symbol},
            headers={"X-Engine-Token": os.getenv("ENGINE_TOKEN", "")},
            timeout=_timeout(),
        )
        if response.status_code != 200:
            return {"available": False, "detail": f"HTTP {response.status_code}"}
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("provisional"):
            return {"available": False, "detail": "malformed live view"}
        return {"available": True, **payload}
    except Exception as exc:
        return {"available": False, "detail": f"{type(exc).__name__}: {exc}"}


def get_live_history(symbol: str = "BTCUSD", *, limit: int = 288) -> dict[str, Any]:
    """Display-only 5-minute Preview Decision score candles. Never raises.

    This returns a deliberately non-decision-shaped object.  No caller can
    mistake this repainting history for a committed, tradeable signal.
    """
    try:
        response = requests.get(
            f"{engine_base_url()}/trend/live/history",
            params={"symbol": symbol, "limit": max(1, min(int(limit), 288))},
            headers={"X-Engine-Token": os.getenv("ENGINE_TOKEN", "")},
            timeout=_timeout(),
        )
        if response.status_code != 200:
            return {"available": False, "detail": f"HTTP {response.status_code}",
                    "candles": []}
        payload = response.json()
        if (not isinstance(payload, dict)
                or payload.get("display_only") is not True
                or not isinstance(payload.get("candles"), list)):
            return {"available": False, "detail": "malformed live history",
                    "candles": []}
        return {
            "available": True,
            "symbol": payload.get("symbol") or symbol,
            "resolution": payload.get("resolution") or "5m",
            "display_only": True,
            "candles": [item for item in payload["candles"]
                        if isinstance(item, dict)],
        }
    except Exception as exc:
        return {"available": False, "detail": f"{type(exc).__name__}: {exc}",
                "candles": []}


def get_decision_history(
    symbol: str = "BTCUSD",
    *,
    limit: int = 288,
) -> dict[str, Any]:
    """Committed five-minute decision scores for display. Never raises.

    The engine's history endpoint returns complete TrendSnapshot objects.
    The dashboard chart needs only the immutable commit timestamp, score, and
    zone, so this client deliberately strips every permission, gate, and
    signal identifier before returning the browser-facing payload.
    """
    try:
        safe_limit = max(1, min(int(limit), 500))
        response = requests.get(
            f"{engine_base_url()}/trend/history",
            params={"symbol": symbol, "limit": safe_limit},
            headers={"X-Engine-Token": os.getenv("ENGINE_TOKEN", "")},
            timeout=_timeout(),
        )
        if response.status_code != 200:
            return {
                "available": False,
                "detail": f"HTTP {response.status_code}",
                "decisions": [],
            }
        payload = response.json()
        snapshots = payload.get("snapshots") if isinstance(payload, dict) else None
        if not isinstance(snapshots, list):
            return {
                "available": False,
                "detail": "malformed decision history",
                "decisions": [],
            }

        decisions_by_time: dict[str, dict[str, Any]] = {}
        for snapshot in snapshots:
            if not isinstance(snapshot, dict):
                continue
            # ``timestamp`` is when the closed-candle snapshot was actually
            # committed. In the current engine contract ``candle_close_utc``
            # retains the trigger Candle's start timestamp, so using it here
            # would draw every decision (and its trade marker) one bar early.
            stamp = _parse_iso(snapshot.get("timestamp"))
            try:
                score = float(snapshot.get("trend_score"))
            except (TypeError, ValueError):
                continue
            if stamp is None or not math.isfinite(score):
                continue
            time_utc = stamp.astimezone(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            decisions_by_time[time_utc] = {
                "time_utc": time_utc,
                "committed_score": score,
                "zone": str(snapshot.get("zone") or "").strip().upper(),
            }

        return {
            "available": True,
            "symbol": payload.get("symbol") or symbol,
            "resolution": "5m",
            "source": "committed_decisions",
            "decisions": [
                decisions_by_time[stamp]
                for stamp in sorted(decisions_by_time)
            ][-safe_limit:],
        }
    except Exception as exc:
        return {
            "available": False,
            "detail": f"{type(exc).__name__}: {exc}",
            "decisions": [],
        }


def get_shadow_summary() -> dict[str, Any]:
    """Agreement statistics for the Trend Engine page. Never raises."""
    try:
        response = requests.get(
            f"{engine_base_url()}/shadow/summary",
            headers={"X-Engine-Token": os.getenv("ENGINE_TOKEN", "")},
            timeout=_timeout(),
        )
        if response.status_code != 200:
            return {"available": False, "detail": f"HTTP {response.status_code}"}
        return {"available": True, **response.json()}
    except Exception as exc:
        return {"available": False, "detail": f"{type(exc).__name__}: {exc}"}


def get_risk_status() -> dict[str, Any]:
    """Kill-switch state for the dashboard. Never raises.

    Fails CLOSED on any error: an unreachable engine cannot *prove* that no
    kill switch is latched, so ``any_active`` is reported True rather than
    False. A caller that treated "unknown" as "clear" would resume trading
    precisely when the safety layer is least observable.
    """
    try:
        response = requests.get(
            f"{engine_base_url()}/risk/status",
            headers={"X-Engine-Token": os.getenv("ENGINE_TOKEN", "")},
            timeout=_timeout(),
        )
        if response.status_code != 200:
            return {"available": False, "any_active": True, "active": [],
                    "detail": f"HTTP {response.status_code}"}
        body = response.json()
    except Exception as exc:
        return {"available": False, "any_active": True, "active": [],
                "detail": f"{type(exc).__name__}: {exc}"}
    return {"available": True, "any_active": bool(body.get("any_active")),
            "active": list(body.get("active") or []),
            "switches": body.get("switches") or {}}


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
