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

import os
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


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
