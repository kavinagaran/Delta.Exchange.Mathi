"""Fail-closed execution primitives for LIVE Trend-score entries.

The score controller itself lives in :mod:`dashboard`.  This module contains
the irreversible-order seam so it can be tested without Flask, account globals,
or a real exchange connection.

The important invariants are:

* every submitted entry requests exactly 1,000 lots;
* entries are bounded IOC limit orders (never an implicit market fallback);
* a deterministic client id and ``ENTRY_PENDING`` state are durable before
  the POST;
* response-loss recovery uses an exact, conclusive order lookup;
* explicit IOC partial fills are accepted once, persisted, and protected;
* exchange order/fill prices are bound to the durable IOC limit;
* a fill is never called OPEN until the real-time exchange position agrees;
* recovered fills retain the exchange/durable entry time and original signal;
* a protection failure immediately invokes the supplied reduce-only flatten
  routine, and an unverified flatten remains visibly OPEN;
* protection setup can never overwrite a same-cycle monitor close with stale
  OPEN state.

Callers own account/config locks and supply strict exchange adapters.  In
particular, ``lookup_order`` must return ``conclusive=False`` for a timeout,
rejected lookup, or partial order-history scan.  Only an exact order-id or
client-order-id endpoint may prove absence.  ``protect_position`` is a
start-and-verify callback only: it must not flatten.  ``flatten_position`` owns
the one emergency close path and must return a durable result containing an
explicit ``flat_verified`` boolean.
"""

from __future__ import annotations

import copy
import hashlib
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from typing import Any, Callable, Mapping


LIVE_SCORE_LOTS = 1_000

_ACTIVE_ORDER_STATES = {
    "open",
    "pending",
    "partially_filled",
    "partially-filled",
    "untriggered",
    "triggered",
}
_TERMINAL_ORDER_STATES = {
    "closed",
    "filled",
    "cancelled",
    "canceled",
    "rejected",
    "expired",
    "failed",
}


class LiveScoreExecutionError(RuntimeError):
    """The requested LIVE mutation is invalid or cannot be proven safe."""


@dataclass(frozen=True)
class ExactOrderLookup:
    """Result of an exact exchange-order identity lookup."""

    order: dict[str, Any] | None
    conclusive: bool
    error: str = ""


def _finite(value: Any, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise LiveScoreExecutionError(f"{label} is invalid")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise LiveScoreExecutionError(f"{label} is invalid") from exc
    if not math.isfinite(result) or (positive and result <= 0):
        raise LiveScoreExecutionError(f"{label} is invalid")
    return result


def _positive_int(value: Any, label: str) -> int:
    number = _finite(value, label, positive=True)
    integer = int(number)
    if number != integer:
        raise LiveScoreExecutionError(f"{label} must be an integer")
    return integer


def _positive_decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool):
        raise LiveScoreExecutionError(f"{label} is invalid")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise LiveScoreExecutionError(f"{label} is invalid") from exc
    if not number.is_finite() or number <= 0:
        raise LiveScoreExecutionError(f"{label} is invalid")
    return number


def _utc_now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if not isinstance(value, datetime):
        raise LiveScoreExecutionError("execution clock returned an invalid value")
    if value.tzinfo is None:
        raise LiveScoreExecutionError("execution clock must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _exchange_timestamp(value: Any) -> datetime | None:
    """Normalize Delta Unix-second/millisecond/microsecond or ISO timestamps."""

    if value in (None, "") or isinstance(value, bool):
        return None
    raw = str(value).strip()
    try:
        numeric = Decimal(raw)
        if numeric.is_finite():
            magnitude = abs(numeric)
            if magnitude >= Decimal("100000000000000"):
                seconds = numeric / Decimal(1_000_000)
            elif magnitude >= Decimal("100000000000"):
                seconds = numeric / Decimal(1_000)
            else:
                seconds = numeric
            return datetime.fromtimestamp(float(seconds), tz=timezone.utc)
    except (InvalidOperation, ValueError, OSError, OverflowError):
        pass
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _entry_timestamp(
    pending: Mapping[str, Any],
    order: Mapping[str, Any],
) -> tuple[datetime, str]:
    """Return the authoritative fill/order time, never recovery wall time."""

    for key in (
        "filled_at",
        "fill_time",
        "fill_timestamp",
        "last_fill_at",
        "created_at",
        "created_at_utc",
    ):
        entered = _exchange_timestamp(order.get(key))
        if entered is not None:
            return entered, f"exchange_order.{key}"
    entered = _exchange_timestamp(pending.get("pending_entry_started_at_utc"))
    if entered is None:
        raise LiveScoreExecutionError(
            "filled LIVE entry has no authoritative or durable entry timestamp"
        )
    return entered, "durable_pending_entry_started_at_utc"


def score_entry_client_id(user: str, transition_id: str) -> str:
    """Return one stable Delta-compatible entry identity for a transition."""

    clean_user = re.sub(r"[^a-z0-9]", "", str(user or "").lower())[:5] or "acct"
    transition = str(transition_id or "").strip()
    if not transition:
        raise LiveScoreExecutionError("transition_id is required")
    digest = hashlib.sha256(
        f"live-entry|{clean_user}|{transition}".encode("utf-8")
    ).hexdigest()[:16]
    # Keep the existing ``trend-`` ownership prefix and remain below Delta's
    # documented 32-character client-id limit.
    return f"trend-{clean_user}-{digest}-e"[:32]


def score_close_client_id(user: str, transition_id: str, sequence: int = 0) -> str:
    """Stable identity for one reduce-only switch-close attempt."""

    clean_user = re.sub(r"[^a-z0-9]", "", str(user or "").lower())[:5] or "acct"
    transition = str(transition_id or "").strip()
    if not transition:
        raise LiveScoreExecutionError("transition_id is required")
    if sequence < 0:
        raise LiveScoreExecutionError("close sequence cannot be negative")
    digest = hashlib.sha256(
        f"live-close|{clean_user}|{transition}|{sequence}".encode("utf-8")
    ).hexdigest()[:15]
    return f"trend-{clean_user}-{digest}-x"[:32]


def validate_fixed_entry(prepared: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the selected contract and normalize its LIVE order direction."""

    if not isinstance(prepared, Mapping):
        raise LiveScoreExecutionError("prepared entry must be an object")
    row = copy.deepcopy(dict(prepared))
    lots = _positive_int(row.get("lots"), "prepared lots")
    if lots != LIVE_SCORE_LOTS:
        raise LiveScoreExecutionError(
            "LIVE Trend score entry must request exactly 1,000 lots"
        )
    product_id = _positive_int(row.get("product_id"), "product_id")
    symbol = str(row.get("symbol") or "").strip()
    zone = str(row.get("zone") or "").strip().upper()
    side = str(row.get("side") or "").strip().lower()
    instrument = str(row.get("instrument_kind") or "").strip().upper()
    option_type = str(row.get("option_type") or "").strip().upper()

    if zone == "CE_2_ITM":
        expected = ("BTC_OPTION", "CE", "long", "C-BTC-")
    elif zone == "PE_3_ITM":
        expected = ("BTC_OPTION", "PE", "long", "P-BTC-")
    elif zone == "SHORT_MOVE":
        expected = ("BTC_MOVE", "MOVE", "short", "MV-BTC-")
    else:
        raise LiveScoreExecutionError("prepared score zone is unsupported")
    if (instrument, option_type, side) != expected[:3] or not symbol.startswith(
        expected[3]
    ):
        raise LiveScoreExecutionError(
            "prepared contract identity does not match its score zone"
        )

    contract_value = _finite(
        row.get("contract_value"), "contract_value", positive=True
    )
    reference_price = _finite(
        row.get("entry_price"), "reference entry price", positive=True
    )
    strike = _finite(row.get("strike"), "strike", positive=True)
    order_limit_value = row.get("max_order_lots")
    if order_limit_value in (None, ""):
        raw_product = row.get("raw_product")
        if isinstance(raw_product, Mapping):
            order_limit_value = raw_product.get("position_size_limit")
    max_order_lots = _positive_int(order_limit_value, "contract order limit")
    if max_order_lots < LIVE_SCORE_LOTS:
        raise LiveScoreExecutionError(
            "selected contract cannot accept the fixed 1,000-lot order"
        )

    normalized = {
        **row,
        "lots": lots,
        "product_id": product_id,
        "symbol": symbol,
        "zone": zone,
        "side": side,
        "exchange_side": "sell" if side == "short" else "buy",
        "instrument_kind": instrument,
        "option_type": option_type,
        "contract_value": contract_value,
        "entry_price": reference_price,
        "strike": strike,
        "max_order_lots": max_order_lots,
    }
    return normalized


def _inward_tick(boundary: float, tick: float, side: str) -> float:
    try:
        boundary_decimal = Decimal(str(boundary))
        tick_decimal = Decimal(str(tick))
        if not boundary_decimal.is_finite() or not tick_decimal.is_finite():
            raise InvalidOperation
        if tick_decimal <= 0:
            raise InvalidOperation
        rounding = ROUND_FLOOR if side == "buy" else ROUND_CEILING
        units = (boundary_decimal / tick_decimal).to_integral_value(
            rounding=rounding
        )
        price = units * tick_decimal
    except (InvalidOperation, ValueError) as exc:
        raise LiveScoreExecutionError("tick-bounded price is invalid") from exc
    if price <= 0:
        raise LiveScoreExecutionError("tick-bounded price is not positive")
    return float(price)


def bounded_ioc_payload(
    prepared: Mapping[str, Any],
    quote: Mapping[str, Any],
    *,
    client_order_id: str,
    max_slippage_pct: float,
    max_spread_pct: float,
    max_quote_age_sec: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the one permitted fixed-size, slippage-bounded IOC payload."""

    entry = validate_fixed_entry(prepared)
    if not isinstance(quote, Mapping):
        raise LiveScoreExecutionError("fresh execution quote is unavailable")
    quote = copy.deepcopy(dict(quote))
    client_id = str(client_order_id or "").strip()
    if not client_id or len(client_id) > 32:
        raise LiveScoreExecutionError("client_order_id is invalid")

    bid = _finite(quote.get("bid"), "fresh bid", positive=True)
    ask = _finite(quote.get("ask"), "fresh ask", positive=True)
    if ask < bid:
        raise LiveScoreExecutionError("fresh quote is crossed")
    mid = (bid + ask) / 2.0
    spread = (ask - bid) / mid * 100.0
    spread_cap = _finite(max_spread_pct, "maximum spread")
    if spread_cap < 0 or spread > spread_cap:
        raise LiveScoreExecutionError(
            f"fresh spread {spread:.6f}% exceeds {spread_cap:.6f}%"
        )
    age = _finite(quote.get("quote_age_secs"), "fresh quote age")
    age_cap = _finite(max_quote_age_sec, "maximum quote age", positive=True)
    if age < 0 or age > age_cap:
        raise LiveScoreExecutionError(
            f"fresh quote is stale ({age:.3f}s > {age_cap:.3f}s)"
        )
    status = str(quote.get("trading_status") or "").strip().lower()
    if status != "operational":
        raise LiveScoreExecutionError("selected contract is not operational")

    side = entry["exchange_side"]
    depth_key = "ask_size" if side == "buy" else "bid_size"
    depth = _finite(quote.get(depth_key), f"fresh {depth_key}", positive=True)
    if depth < LIVE_SCORE_LOTS:
        raise LiveScoreExecutionError(
            f"fresh {depth_key} cannot cover the fixed 1,000-lot IOC"
        )

    slippage = _finite(max_slippage_pct, "maximum slippage")
    if slippage < 0:
        raise LiveScoreExecutionError("maximum slippage cannot be negative")
    reference = entry["entry_price"]
    boundary = (
        reference * (1 + slippage / 100.0)
        if side == "buy"
        else reference * (1 - slippage / 100.0)
    )
    tick = _finite(quote.get("tick_size"), "tick size", positive=True)
    limit = _inward_tick(boundary, tick, side)
    if side == "buy" and ask > limit + tick * 1e-9:
        raise LiveScoreExecutionError(
            f"fresh ask {ask} exceeds bounded buy limit {limit}"
        )
    if side == "sell" and bid < limit - tick * 1e-9:
        raise LiveScoreExecutionError(
            f"fresh bid {bid} is below bounded sell limit {limit}"
        )

    band = quote.get("price_band")
    if not isinstance(band, Mapping):
        band = {}
    if side == "buy":
        upper = _finite(band.get("upper_limit"), "upper price band") \
            if band.get("upper_limit") not in (None, "") else 0.0
        if upper and limit > upper:
            raise LiveScoreExecutionError(
                "bounded buy limit exceeds the exchange price band"
            )
    else:
        lower = _finite(band.get("lower_limit"), "lower price band") \
            if band.get("lower_limit") not in (None, "") else 0.0
        if lower and limit < lower:
            raise LiveScoreExecutionError(
                "bounded sell limit is below the exchange price band"
            )

    payload = {
        "product_id": entry["product_id"],
        "size": LIVE_SCORE_LOTS,
        "side": side,
        "order_type": "limit_order",
        "limit_price": str(limit),
        "time_in_force": "ioc",
        "post_only": False,
        "client_order_id": client_id,
    }
    snapshot = {
        **quote,
        "bid": bid,
        "ask": ask,
        "spread_pct": spread,
        "entry_depth": depth,
        "reference_price": reference,
        "slippage_boundary": boundary,
        "limit_price": limit,
        "side": side,
    }
    return payload, snapshot


def _entry_times(now: datetime, risk_day_offset_minutes: int) -> dict[str, str]:
    local_date = (
        now.astimezone(timezone.utc)
        + timedelta(minutes=int(risk_day_offset_minutes))
    ).date().isoformat()
    return {
        "entry_date": now.strftime("%Y-%m-%d"),
        "entry_time_utc": now.strftime("%H:%M:%S"),
        "entry_at_utc": _iso(now),
        "trading_date": local_date,
    }


def build_pending_entry_state(
    *,
    user: str,
    signal: Mapping[str, Any],
    prepared: Mapping[str, Any],
    transition_id: str,
    quote_snapshot: Mapping[str, Any],
    payload: Mapping[str, Any],
    protection_config: Mapping[str, Any],
    risk_snapshot: Mapping[str, Any] | None,
    ownership: str = "trend_score_auto_live",
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    risk_day_offset_minutes: int = 330,
) -> dict[str, Any]:
    """Create the complete durable record written before an entry POST."""

    entry = validate_fixed_entry(prepared)
    now = _utc_now(clock)
    transition = str(transition_id or "").strip()
    if not transition:
        raise LiveScoreExecutionError("transition_id is required")
    client_id = score_entry_client_id(user, transition)
    if str(payload.get("client_order_id") or "") != client_id:
        raise LiveScoreExecutionError(
            "entry payload does not use the deterministic transition identity"
        )
    if int(payload.get("size") or 0) != LIVE_SCORE_LOTS:
        raise LiveScoreExecutionError("entry payload is not exactly 1,000 lots")
    if not isinstance(protection_config, Mapping):
        raise LiveScoreExecutionError("protection configuration is required")
    for key in (
        "tp_target_pnl",
        "sl_target_pnl",
        "tsl_arm_pnl",
        "tsl_trail_pnl",
    ):
        _finite(protection_config.get(key), key, positive=True)

    decision = signal.get("decision")
    if not isinstance(decision, Mapping):
        decision = {}
    signal_key = str(signal.get("signal_key") or "").strip()
    if not signal_key:
        raise LiveScoreExecutionError("signal_key is required")
    score = _finite(signal.get("score"), "direction score")
    direction = (
        "up"
        if entry["zone"] == "CE_2_ITM"
        else "down"
        if entry["zone"] == "PE_3_ITM"
        else "neutral"
    )
    policy_decision = (
        "BUY_CE"
        if entry["zone"] == "CE_2_ITM"
        else "BUY_PE"
        if entry["zone"] == "PE_3_ITM"
        else "SELL_MOVE"
    )
    proposed_risk = None
    if isinstance(risk_snapshot, Mapping):
        for key in ("proposed_risk_usd", "risk_at_entry_usd"):
            if risk_snapshot.get(key) not in (None, ""):
                proposed_risk = _finite(
                    risk_snapshot.get(key), "proposed risk", positive=True
                )
                break
    state = {
        "slot": "trend",
        "status": "ENTRY_PENDING",
        "side": entry["side"],
        "option_type": entry["option_type"],
        "instrument_kind": entry["instrument_kind"],
        "trend_signal": direction,
        **_entry_times(now, risk_day_offset_minutes),
        "symbol": entry["symbol"],
        "product_id": entry["product_id"],
        "strike": entry["strike"],
        "settlement": entry.get("settlement") or entry.get("expiry"),
        "contract_value": entry["contract_value"],
        "lots": LIVE_SCORE_LOTS,
        "requested_lots": LIVE_SCORE_LOTS,
        "owned_entry_lots": 0,
        "protection_lots": 0,
        "entry_mark": None,
        "ownership": str(ownership or "trend_score_auto_live"),
        "entry_trigger": "trend_engine_score_zone_auto",
        "entry_classification": "rules_based_score_auto",
        "strategy": "trend_engine_score_zone",
        "dry_run": False,
        "execution_mode": "live",
        "position_cycle_id": transition,
        "transition_id": transition,
        "trend_score_zone": entry["zone"],
        "engine_zone": entry["zone"],
        "score_auto_signal_key": signal_key,
        "signal_bar_close_utc": signal.get("signal_bar_close_utc"),
        "engine_signal_fingerprint": signal_key,
        "engine_policy_decision": policy_decision,
        "engine_entry_decision": policy_decision,
        "entry_decision_id": decision.get("decision_id"),
        "model_version": decision.get("model_version"),
        "schema_version": decision.get("schema_version"),
        "direction_score_at_entry": score,
        "market_regime_at_entry": str(
            signal.get("market_regime") or "UNCLEAR"
        ),
        "btc_at_entry": (
            (signal.get("snapshot") or {}).get("market", {}).get("spot")
            if isinstance(signal.get("snapshot"), Mapping)
            else None
        ),
        "risk_at_entry_usd": proposed_risk,
        "risk_decision": copy.deepcopy(dict(risk_snapshot or {})),
        "protection_config": copy.deepcopy(dict(protection_config)),
        "protection_revision": 0,
        "continuity_revision": 0,
        "continuity_anchor_utc": _iso(now),
        "continuity_verified": False,
        "continuity_status": "entry_pending",
        "selected_contract_snapshot": copy.deepcopy(entry),
        "quote_snapshot": copy.deepcopy(dict(quote_snapshot)),
        "entry_decision_snapshot": copy.deepcopy(dict(decision)),
        "signal_snapshot": {
            "signal_key": signal_key,
            "signal_bar_close_utc": signal.get("signal_bar_close_utc"),
            "direction_score": score,
            "market_regime": signal.get("market_regime"),
            "zone": entry["zone"],
        },
        "pending_entry_client_order_id": client_id,
        "pending_entry_order_id": None,
        "pending_entry_requested_lots": LIVE_SCORE_LOTS,
        "pending_entry_side": entry["exchange_side"],
        "pending_entry_payload": copy.deepcopy(dict(payload)),
        "pending_entry_submission_state": "prepared",
        "pending_entry_post_boundary": False,
        "pending_entry_attempts": 0,
        "pending_entry_started_at_utc": _iso(now),
        "execution_snapshot": {
            "kind": "bounded_ioc_limit",
            "requested": LIVE_SCORE_LOTS,
            "filled": 0,
            "unfilled": LIVE_SCORE_LOTS,
            "client_order_id": client_id,
            "order_id": None,
            "limit_price": payload.get("limit_price"),
            "order_submitted": False,
            "exchange_api_called": False,
        },
    }
    return state


def _order_state(order: Mapping[str, Any] | None) -> str:
    return str(
        (order or {}).get("state") or (order or {}).get("status") or ""
    ).strip().lower()


def _validate_order_identity(
    order: Mapping[str, Any],
    *,
    product_id: int,
    client_order_id: str,
    side: str,
    expected_limit_price: Any,
) -> dict[str, Any]:
    if not isinstance(order, Mapping) or not order.get("id"):
        raise LiveScoreExecutionError(
            "exchange order acknowledgement has no order identity"
        )
    result = dict(order)
    returned_client = str(result.get("client_order_id") or "")
    if returned_client != str(client_order_id):
        raise LiveScoreExecutionError("exchange client-order identity mismatch")
    if (
        result.get("product_id") not in (None, "")
        and _positive_int(
            result.get("product_id"), "exchange order product_id"
        )
        != int(product_id)
    ):
        raise LiveScoreExecutionError("exchange order product mismatch")
    returned_side = str(result.get("side") or "").lower()
    if returned_side and returned_side != side:
        raise LiveScoreExecutionError("exchange order side mismatch")
    reduce_only = result.get("reduce_only")
    if reduce_only not in (None, "", False, 0, "0", "false", "False"):
        raise LiveScoreExecutionError("entry order unexpectedly has reduce_only")
    order_type = str(result.get("order_type") or "").lower()
    if order_type and order_type != "limit_order":
        raise LiveScoreExecutionError("entry order is not a limit order")
    tif = str(result.get("time_in_force") or "").lower()
    if tif and tif != "ioc":
        raise LiveScoreExecutionError("entry order is not IOC")
    if result.get("size") not in (None, ""):
        if _positive_int(result.get("size"), "exchange order size") != LIVE_SCORE_LOTS:
            raise LiveScoreExecutionError(
                "exchange order size differs from fixed 1,000 lots"
            )
    _validate_returned_limit_price(result, expected_limit_price)
    return result


def _validate_returned_limit_price(
    order: Mapping[str, Any],
    expected_limit_price: Any,
) -> None:
    """Bind an exchange-reported limit exactly to the durable order intent."""

    expected = _positive_decimal(
        expected_limit_price, "persisted entry limit price"
    )
    returned = order.get("limit_price")
    if returned in (None, ""):
        return
    actual = _positive_decimal(returned, "exchange order limit price")
    if actual != expected:
        raise LiveScoreExecutionError(
            "exchange order limit price differs from durable entry intent"
        )


def _validate_persisted_entry_payload(
    value: Any,
    *,
    product_id: int,
    client_order_id: str,
    side: str,
) -> dict[str, Any]:
    """Verify the immutable order intent without needing a current quote."""

    if not isinstance(value, Mapping):
        raise LiveScoreExecutionError(
            "pending LIVE entry has no durable order payload"
        )
    payload = copy.deepcopy(dict(value))
    if str(payload.get("client_order_id") or "") != client_order_id:
        raise LiveScoreExecutionError(
            "pending LIVE entry payload has a different client identity"
        )
    if (
        _positive_int(payload.get("product_id"), "persisted product_id")
        != product_id
    ):
        raise LiveScoreExecutionError(
            "pending LIVE entry payload has a different product"
        )
    if _positive_int(payload.get("size"), "persisted order size") != LIVE_SCORE_LOTS:
        raise LiveScoreExecutionError(
            "pending LIVE entry payload is not exactly 1,000 lots"
        )
    if str(payload.get("side") or "").strip().lower() != side:
        raise LiveScoreExecutionError(
            "pending LIVE entry payload has a different side"
        )
    if str(payload.get("order_type") or "").strip().lower() != "limit_order":
        raise LiveScoreExecutionError(
            "pending LIVE entry payload is not a limit order"
        )
    if str(payload.get("time_in_force") or "").strip().lower() != "ioc":
        raise LiveScoreExecutionError(
            "pending LIVE entry payload is not IOC"
        )
    if payload.get("post_only") is not False:
        raise LiveScoreExecutionError(
            "pending LIVE entry payload has an invalid post-only flag"
        )
    _positive_decimal(payload.get("limit_price"), "persisted limit price")
    return payload


def terminal_filled_lots(
    order: Mapping[str, Any] | None,
    requested: int = LIVE_SCORE_LOTS,
) -> int | None:
    """Return an explicitly proven terminal fill, never an inferred full fill."""

    def bounded_lots(value: Any) -> int | None:
        try:
            number = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return None
        if (
            not number.is_finite()
            or number != number.to_integral_value()
            or number < 0
            or number > requested
        ):
            return None
        return int(number)

    state = _order_state(order)
    if state not in _TERMINAL_ORDER_STATES:
        return None
    for key in ("filled_size", "filled_quantity", "executed_size"):
        value = (order or {}).get(key)
        if value not in (None, ""):
            return bounded_lots(value)
    unfilled = (order or {}).get("unfilled_size")
    if unfilled not in (None, ""):
        remainder = bounded_lots(unfilled)
        if remainder is None:
            return None
        return requested - remainder
    if state in {"rejected", "failed"}:
        return 0
    return None


def _normalize_lookup(value: Any) -> ExactOrderLookup:
    if isinstance(value, ExactOrderLookup):
        return value
    if isinstance(value, tuple) and len(value) >= 2:
        order, conclusive = value[:2]
        error = value[2] if len(value) > 2 else ""
        return ExactOrderLookup(
            dict(order) if isinstance(order, Mapping) and order else None,
            bool(conclusive),
            str(error or ""),
        )
    raise LiveScoreExecutionError(
        "exact order lookup returned an invalid result"
    )


def _wait_terminal(
    initial: Mapping[str, Any],
    *,
    product_id: int,
    client_order_id: str,
    side: str,
    expected_limit_price: Any,
    lookup_order: Callable[[Any, str, int], Any],
    timeout_sec: float,
    poll_sec: float,
    monotonic: Callable[[], float],
    sleeper: Callable[[float], None],
) -> tuple[dict[str, Any], int | None, bool]:
    latest = dict(initial)
    timeout = _finite(timeout_sec, "terminal timeout")
    interval = _finite(poll_sec, "terminal poll interval")
    if timeout < 0 or interval < 0:
        raise LiveScoreExecutionError(
            "terminal timeout and poll interval cannot be negative"
        )
    deadline = monotonic() + timeout
    lookup_conclusive = True
    while True:
        filled = terminal_filled_lots(latest)
        if filled is not None:
            fill_price_valid = filled <= 0
            if filled > 0:
                try:
                    _finite(
                        latest.get("average_fill_price"),
                        "average fill price",
                        positive=True,
                    )
                except LiveScoreExecutionError:
                    fill_price_valid = False
                else:
                    fill_price_valid = True
            if fill_price_valid:
                return latest, filled, lookup_conclusive
        if monotonic() >= deadline:
            return latest, None, lookup_conclusive
        try:
            lookup = _normalize_lookup(
                lookup_order(latest.get("id"), client_order_id, product_id)
            )
        except Exception:
            return latest, None, False
        lookup_conclusive = lookup.conclusive
        if lookup.order:
            latest = _validate_order_identity(
                lookup.order,
                product_id=product_id,
                client_order_id=client_order_id,
                side=side,
                expected_limit_price=expected_limit_price,
            )
        elif not lookup.conclusive:
            return latest, None, False
        sleeper(interval)


def _commission(order: Mapping[str, Any]) -> tuple[float | None, str]:
    for key in (
        "paid_commission",
        "commission",
        "commission_usd",
        "total_commission",
    ):
        if order.get(key) not in (None, ""):
            value = _finite(order.get(key), "entry commission")
            if value < 0:
                raise LiveScoreExecutionError("entry commission is negative")
            return value, "exchange"
    return None, "fee_pending"


def _position_size(position: Mapping[str, Any], product_id: int) -> int:
    returned = position.get("product_id")
    if returned not in (None, "") and int(returned) != int(product_id):
        raise LiveScoreExecutionError(
            "real-time position returned a different product"
        )
    try:
        value = Decimal(str(position.get("size")))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise LiveScoreExecutionError(
            "real-time position size is invalid"
        ) from exc
    if not value.is_finite() or value != value.to_integral_value():
        raise LiveScoreExecutionError(
            "real-time position size is not an integer"
        )
    return int(value)


def _open_state_from_fill(
    pending: Mapping[str, Any],
    order: Mapping[str, Any],
    filled: int,
    position: Mapping[str, Any],
) -> dict[str, Any]:
    product_id = int(pending["product_id"])
    expected = filled if pending.get("pending_entry_side") == "buy" else -filled
    actual = _position_size(position, product_id)
    if actual != expected:
        raise LiveScoreExecutionError(
            f"entry fill/position mismatch: order proves {expected}, exchange reports {actual}"
        )
    exchange_entry = _finite(
        position.get("entry_price"), "real-time entry price", positive=True
    )
    order_entry = _finite(
        order.get("average_fill_price"), "average fill price", positive=True
    )
    payload = pending.get("pending_entry_payload")
    if not isinstance(payload, Mapping):
        raise LiveScoreExecutionError(
            "filled LIVE entry has no durable order payload"
        )
    durable_limit = _positive_decimal(
        payload.get("limit_price"), "persisted entry limit price"
    )
    average_fill = _positive_decimal(
        order.get("average_fill_price"), "average fill price"
    )
    execution_side = str(
        pending.get("pending_entry_side") or ""
    ).strip().lower()
    price_tolerance = max(
        Decimal("1e-12"),
        abs(durable_limit) * Decimal("1e-12"),
    )
    if (
        execution_side == "buy"
        and average_fill > durable_limit + price_tolerance
    ):
        raise LiveScoreExecutionError(
            "average buy fill exceeds the durable entry limit"
        )
    if (
        execution_side == "sell"
        and average_fill < durable_limit - price_tolerance
    ):
        raise LiveScoreExecutionError(
            "average sell fill is below the durable entry limit"
        )
    if execution_side not in {"buy", "sell"}:
        raise LiveScoreExecutionError(
            "filled LIVE entry has an invalid durable side"
        )
    tolerance = max(0.02, abs(exchange_entry) * 0.0001)
    if abs(exchange_entry - order_entry) > tolerance:
        raise LiveScoreExecutionError(
            "entry fill price does not match the real-time position basis"
        )
    entered, entry_time_source = _entry_timestamp(pending, order)
    fee, fee_source = _commission(order)
    state = copy.deepcopy(dict(pending))
    state.update(
        {
            "status": "OPEN",
            "lots": filled,
            "requested_lots": LIVE_SCORE_LOTS,
            "owned_entry_lots": filled,
            "original_owned_entry_lots": filled,
            "protection_lots": filled,
            "max_protected_lots": filled,
            "entry_mark": round(exchange_entry, 8),
            "entry_at_utc": _iso(entered),
            "entry_date": entered.strftime("%Y-%m-%d"),
            "entry_time_utc": entered.strftime("%H:%M:%S"),
            "entry_time_source": entry_time_source,
            "total_cost_usd": round(
                exchange_entry * float(state["contract_value"]) * filled, 8
            ),
            "entry_fees_usd": fee,
            "entry_fee_usd": fee,
            "fees_usd": fee,
            "entry_fee_source": fee_source,
            "original_bot_entry_mark": round(exchange_entry, 8),
            "original_bot_entry_fee_usd": fee,
            "original_bot_entry_fee_source": fee_source,
            "pnl_includes_fees": False,
            "order_id": order.get("id"),
            "order_ids": [order.get("id")],
            "client_order_id": pending.get("pending_entry_client_order_id"),
            "client_order_ids": [pending.get("pending_entry_client_order_id")],
            "pending_entry_client_order_id": None,
            "pending_entry_order_id": None,
            "pending_entry_submission_state": None,
            "cycle_entry_lots_total": filled,
            "cycle_exit_lots_total": 0,
            "partial_exit_accounting_status": "complete",
            "accounting_status": (
                "complete" if fee is not None else "fee_pending"
            ),
            "position_composition": "bot_only",
            "continuity_verified": False,
            "continuity_status": "awaiting_monitor_verification",
            "execution_snapshot": {
                **copy.deepcopy(dict(state.get("execution_snapshot") or {})),
                "requested": LIVE_SCORE_LOTS,
                "filled": filled,
                "unfilled": LIVE_SCORE_LOTS - filled,
                "partial_fill": filled < LIVE_SCORE_LOTS,
                "order_submitted": True,
                "exchange_api_called": True,
                "order_id": order.get("id"),
                "order_state": _order_state(order),
                "average_fill_price": order_entry,
                "position_entry_price": exchange_entry,
                "paid_commission_usd": fee,
                "entry_fees_complete": fee is not None,
            },
        }
    )
    return state


def switch_entry_gate(
    state: Mapping[str, Any],
    position: Mapping[str, Any] | None,
) -> tuple[bool, str]:
    """Require a fully reconciled flat close before the switch entry."""

    if not isinstance(state, Mapping):
        return False, "previous Trend state is unreadable"
    if position is None:
        return False, "previous Trend exchange position is unverified"
    try:
        live_size = _position_size(position, int(state.get("product_id") or 0))
    except Exception as exc:
        return False, str(exc)
    if live_size != 0:
        return False, f"previous Trend position still has {abs(live_size)} lots"
    if str(state.get("status") or "").upper() != "CLOSED":
        return False, "previous Trend state is not CLOSED"
    if any(
        state.get(key) not in (None, "", False)
        for key in (
            "pending_entry_client_order_id",
            "pending_entry_order_id",
            "pending_entry_submission_state",
            "pending_close_client_order_id",
            "pending_close_order_id",
            "pending_close_submission_state",
            "pending_stop_protection",
            "pending_tp_protection",
            "protection_failure_flatten_pending",
        )
    ):
        return False, "previous Trend close or protection identity is unresolved"
    if state.get("protection_cleanup_pending"):
        return False, "previous Trend exchange protection cleanup is unresolved"
    if state.get("history_pending"):
        return False, "previous Trend history is pending"
    if str(state.get("accounting_status") or "").lower() != "complete":
        return False, "previous Trend accounting is incomplete"
    if (
        str(
            state.get("partial_exit_accounting_status") or ""
        ).lower()
        != "complete"
    ):
        return False, "previous Trend partial-exit accounting is incomplete"
    return True, ""


def execute_or_recover_entry(
    *,
    user: str,
    signal: Mapping[str, Any],
    prepared: Mapping[str, Any] | None,
    transition_id: str,
    fresh_quote: Mapping[str, Any] | None,
    protection_config: Mapping[str, Any],
    risk_snapshot: Mapping[str, Any] | None,
    existing_state: Mapping[str, Any] | None,
    persist_state: Callable[[dict[str, Any]], None],
    load_state: Callable[[], Mapping[str, Any] | None],
    final_preflight: Callable[[Mapping[str, Any]], None],
    submit_order: Callable[[dict[str, Any]], Any],
    lookup_order: Callable[[Any, str, int], Any],
    get_position: Callable[[int], Mapping[str, Any] | None],
    protect_position: Callable[
        [Mapping[str, Any], datetime], tuple[bool, Mapping[str, Any]]
    ],
    flatten_position: Callable[
        [Mapping[str, Any], str], Mapping[str, Any]
    ],
    max_slippage_pct: float,
    max_spread_pct: float,
    max_quote_age_sec: float,
    ownership: str = "trend_score_auto_live",
    audit: Callable[[str, Mapping[str, Any]], None] | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    terminal_timeout_sec: float = 8.0,
    terminal_poll_sec: float = 0.25,
) -> dict[str, Any]:
    """Submit or recover one deterministic LIVE score entry.

    The returned ``consume_signal`` is deliberately true after any conclusive
    exchange submission outcome, including a zero or partial IOC fill.  A
    pre-submit gate failure remains retryable; an ambiguous identity remains
    pending and must never be duplicated.

    ``protect_position`` must only install/start protection and verify it; it
    must not close the position.  If it returns false, this function invokes
    ``flatten_position`` exactly once.  The flatten adapter must perform a
    durable reduce-only close, reconcile the real-time position, and return
    ``flat_verified=True`` only after exchange size is proven zero.
    ``load_state`` must bypass caches and read the authoritative durable slot.
    Post-protection handling never writes an OPEN snapshot: it re-reads that
    slot, honors a same-cycle CLOSED state, and refuses to act on a different
    position generation.
    """

    now = _utc_now(clock)
    existing = copy.deepcopy(dict(existing_state or {}))
    submitted_this_call = False
    may_submit = False
    recovering_pending = (
        str(existing.get("status") or "").upper() == "ENTRY_PENDING"
    )
    if recovering_pending:
        durable_signal = existing.get("signal_snapshot")
        if not isinstance(durable_signal, Mapping):
            durable_signal = {}
        signal_identities = {
            str(value).strip()
            for value in (
                existing.get("score_auto_signal_key"),
                existing.get("engine_signal_fingerprint"),
                durable_signal.get("signal_key"),
            )
            if str(value or "").strip()
        }
        if len(signal_identities) != 1:
            raise LiveScoreExecutionError(
                "pending LIVE entry signal identity is missing or inconsistent"
            )
        handled_signal_key = next(iter(signal_identities))
        handled_transition_id = str(
            existing.get("transition_id") or ""
        ).strip()
        if not handled_transition_id:
            raise LiveScoreExecutionError(
                "pending LIVE entry has no durable transition identity"
            )
    else:
        handled_signal_key = str(signal.get("signal_key") or "").strip()
        handled_transition_id = str(transition_id or "").strip()
    handled_identity = {
        "handled_signal_key": handled_signal_key,
        "handled_transition_id": handled_transition_id,
    }

    if recovering_pending:
        persisted_contract = existing.get("selected_contract_snapshot")
        if not isinstance(persisted_contract, Mapping):
            raise LiveScoreExecutionError(
                "pending LIVE entry has no durable selected-contract snapshot"
            )
        entry = validate_fixed_entry(persisted_contract)
        active_transition_id = handled_transition_id
        client_id = score_entry_client_id(user, active_transition_id)
        persisted_payload = _validate_persisted_entry_payload(
            existing.get("pending_entry_payload"),
            product_id=entry["product_id"],
            client_order_id=client_id,
            side=entry["exchange_side"],
        )
        durable_limit_price = persisted_payload["limit_price"]
        identity = (
            str(existing.get("pending_entry_client_order_id") or "")
            == client_id
            and int(existing.get("product_id") or 0) == entry["product_id"]
            and int(existing.get("pending_entry_requested_lots") or 0)
            == LIVE_SCORE_LOTS
            and str(existing.get("pending_entry_side") or "")
            == entry["exchange_side"]
        )
        if not identity:
            raise LiveScoreExecutionError(
                "pending LIVE entry identity is internally inconsistent"
            )
        state = existing
        submission_state = str(
            state.get("pending_entry_submission_state") or ""
        ).strip().lower()
        execution_snapshot = state.get("execution_snapshot")
        if not isinstance(execution_snapshot, Mapping):
            execution_snapshot = {}
        definitely_pre_post = bool(
            submission_state == "prepared"
            and not state.get("pending_entry_order_id")
            and state.get("pending_entry_post_boundary") is False
            and state.get("pending_entry_last_attempt_at_utc") in (None, "")
            and type(state.get("pending_entry_attempts")) is int
            and state.get("pending_entry_attempts") == 0
            and execution_snapshot.get("order_submitted") is False
            and execution_snapshot.get("exchange_api_called") is False
        )
        if definitely_pre_post:
            # ``prepared`` is written before ``submitting`` and the latter is
            # itself made durable before POST.  A surviving prepared record is
            # therefore proof that this identity has not crossed the network
            # boundary and may proceed without an exchange absence lookup.
            if active_transition_id != str(transition_id or "").strip():
                raise LiveScoreExecutionError(
                    "a prepared entry for an older transition must be explicitly "
                    "cancelled before a different signal can be submitted"
                )
            if not isinstance(fresh_quote, Mapping):
                raise LiveScoreExecutionError(
                    "a fresh execution quote is required before submission"
                )
            payload, quote_snapshot = bounded_ioc_payload(
                entry,
                fresh_quote,
                client_order_id=client_id,
                max_slippage_pct=max_slippage_pct,
                max_spread_pct=max_spread_pct,
                max_quote_age_sec=max_quote_age_sec,
            )
            durable_limit_price = payload["limit_price"]
            state.update(
                quote_snapshot=copy.deepcopy(quote_snapshot),
                pending_entry_payload=copy.deepcopy(payload),
                execution_snapshot={
                    **dict(execution_snapshot),
                    "limit_price": payload["limit_price"],
                },
            )
            order = None
            may_submit = True
        else:
            try:
                lookup = _normalize_lookup(
                    lookup_order(
                        state.get("pending_entry_order_id"),
                        client_id,
                        entry["product_id"],
                    )
                )
            except Exception as exc:
                lookup = ExactOrderLookup(
                    None,
                    False,
                    f"exact entry identity lookup failed: {exc}",
                )
            if lookup.order:
                order = _validate_order_identity(
                    lookup.order,
                    product_id=entry["product_id"],
                    client_order_id=client_id,
                    side=entry["exchange_side"],
                    expected_limit_price=durable_limit_price,
                )
            else:
                # Once ``submitting`` is durable, even a conclusive immediate
                # absence is not permission to POST again.  Delta client-id
                # idempotency/strong-consistency is not assumed.  A paginated
                # order scan must also report ``conclusive=False``.
                reason = (
                    lookup.error
                    or (
                        "exact entry identity is not visible after the "
                        "submission boundary"
                        if lookup.conclusive
                        else "exact entry identity lookup is inconclusive"
                    )
                )
                state.update(
                    pending_entry_submission_state=(
                        "post_boundary_order_absent"
                        if lookup.conclusive
                        else "lookup_inconclusive"
                    ),
                    pending_entry_last_error=reason,
                    pending_entry_last_reconciled_at_utc=_iso(now),
                )
                persist_state(state)
                return {
                    **handled_identity,
                    "ok": False,
                    "status": "ENTRY_PENDING",
                    "state": state,
                    "order_submitted": False,
                    "consume_signal": False,
                    "error": state["pending_entry_last_error"],
                }
    elif existing and str(existing.get("status") or "").upper() not in {
        "",
        "IDLE",
        "CLOSED",
    }:
        raise LiveScoreExecutionError(
            "existing LIVE Trend state is not eligible for an entry"
        )
    else:
        entry = validate_fixed_entry(prepared or {})
        active_transition_id = str(transition_id or "").strip()
        client_id = score_entry_client_id(user, active_transition_id)
        if not isinstance(fresh_quote, Mapping):
            raise LiveScoreExecutionError(
                "a fresh execution quote is required before submission"
            )
        payload, quote_snapshot = bounded_ioc_payload(
            entry,
            fresh_quote,
            client_order_id=client_id,
            max_slippage_pct=max_slippage_pct,
            max_spread_pct=max_spread_pct,
            max_quote_age_sec=max_quote_age_sec,
        )
        durable_limit_price = payload["limit_price"]
        state = build_pending_entry_state(
            user=user,
            signal=signal,
            prepared=entry,
            transition_id=active_transition_id,
            quote_snapshot=quote_snapshot,
            payload=payload,
            protection_config=protection_config,
            risk_snapshot=risk_snapshot,
            ownership=ownership,
            clock=lambda: now,
        )
        persist_state(state)
        order = None
        may_submit = True

    if order is None:
        if not may_submit:
            raise LiveScoreExecutionError(
                "post-boundary LIVE entry cannot be resubmitted"
            )
        # The pre-submit callback is the final mode revision, account risk,
        # external exposure and open-order boundary.  It intentionally runs
        # after the durable identity exists.
        final_preflight(state)
        state.update(
            pending_entry_submission_state="submitting",
            pending_entry_post_boundary=True,
            pending_entry_attempts=(
                int(state.get("pending_entry_attempts") or 0) + 1
            ),
            pending_entry_last_attempt_at_utc=_iso(_utc_now(clock)),
            pending_entry_last_error="",
        )
        persist_state(state)
        if audit:
            audit(
                "trend_score_live_entry_intent",
                {
                    "transition_id": active_transition_id,
                    "signal_key": handled_signal_key,
                    "client_order_id": client_id,
                    "product_id": entry["product_id"],
                    "symbol": entry["symbol"],
                    "side": entry["exchange_side"],
                    "size": LIVE_SCORE_LOTS,
                    "limit_price": payload["limit_price"],
                    "time_in_force": "ioc",
                },
            )
        try:
            response = submit_order(payload)
            submitted_this_call = True
        except Exception as exc:
            # Never retry in the same call.  Query the exact durable identity;
            # an inconclusive/absent immediate read remains pending for the
            # next controller cycle.
            try:
                recovered = _normalize_lookup(
                    lookup_order(None, client_id, entry["product_id"])
                )
            except Exception as lookup_exc:
                recovered = ExactOrderLookup(
                    None, False, f"{exc}; exact lookup failed: {lookup_exc}"
                )
            if not recovered.order:
                state.update(
                    pending_entry_submission_state="submission_unknown",
                    pending_entry_last_error=str(
                        recovered.error or exc
                    )[:500],
                    pending_entry_last_reconciled_at_utc=_iso(_utc_now(clock)),
                    execution_snapshot={
                        **dict(state.get("execution_snapshot") or {}),
                        "order_submitted": True,
                        "exchange_api_called": True,
                    },
                )
                persist_state(state)
                return {
                    **handled_identity,
                    "ok": False,
                    "status": "ENTRY_PENDING",
                    "state": state,
                    "order_submitted": True,
                    "consume_signal": False,
                    "error": state["pending_entry_last_error"],
                }
            order = _validate_order_identity(
                recovered.order,
                product_id=entry["product_id"],
                client_order_id=client_id,
                side=entry["exchange_side"],
                expected_limit_price=durable_limit_price,
            )
        else:
            if isinstance(response, tuple) and len(response) >= 2:
                acknowledged, raw = response[:2]
            elif isinstance(response, Mapping):
                raw = dict(response)
                acknowledged = (
                    raw.get("result") if raw.get("success") is True else None
                )
            else:
                acknowledged, raw = None, {}
            if not acknowledged:
                explicit_rejection = (
                    isinstance(raw, Mapping)
                    and raw.get("success") is False
                    and raw.get("error") not in (None, "", {})
                )
                # The POST boundary is irreversible.  Even an explicit
                # ``success:false`` response is not accepted as proof that
                # Delta did not create/fill the deterministic client order:
                # reconcile the authoritative identity first.
                try:
                    exact = _normalize_lookup(
                        lookup_order(None, client_id, entry["product_id"])
                    )
                except Exception as exc:
                    exact = ExactOrderLookup(
                        None,
                        False,
                        f"exact acknowledgement lookup failed: {exc}",
                    )
                if exact.order:
                    acknowledged = _validate_order_identity(
                        exact.order,
                        product_id=entry["product_id"],
                        client_order_id=client_id,
                        side=entry["exchange_side"],
                        expected_limit_price=durable_limit_price,
                    )
                else:
                    rejection = (
                        copy.deepcopy(raw.get("error"))
                        if explicit_rejection else None
                    )
                    if explicit_rejection and exact.conclusive:
                        try:
                            position = get_position(entry["product_id"])
                            if not isinstance(position, Mapping):
                                raise LiveScoreExecutionError(
                                    "real-time position is unavailable"
                                )
                            position_size = _position_size(
                                position, entry["product_id"]
                            )
                        except Exception as exc:
                            state.update(
                                pending_entry_submission_state=(
                                    "rejection_position_unverified"
                                ),
                                pending_entry_last_error=(
                                    "entry rejection could not be finalized "
                                    f"because flat exposure is unverified: {exc}"
                                )[:500],
                                pending_entry_last_reconciled_at_utc=_iso(
                                    _utc_now(clock)
                                ),
                                execution_snapshot={
                                    **dict(
                                        state.get("execution_snapshot") or {}
                                    ),
                                    "order_submitted": True,
                                    "exchange_api_called": True,
                                },
                            )
                            persist_state(state)
                            return {
                                **handled_identity,
                                "ok": False,
                                "status": "ENTRY_PENDING",
                                "state": state,
                                "order_submitted": True,
                                "consume_signal": False,
                                "error": state["pending_entry_last_error"],
                            }
                        if position_size == 0:
                            idle = {
                                "slot": "trend",
                                "status": "IDLE",
                                "dry_run": False,
                                "execution_mode": "live",
                                "last_entry_transition_id": active_transition_id,
                                "last_entry_signal_key": handled_signal_key,
                                "last_entry_client_order_id": client_id,
                                "last_entry_rejection": rejection,
                                "last_entry_rejection_exact_absence": True,
                                "last_entry_position_verified_flat": True,
                                "last_entry_attempt_at_utc": _iso(
                                    _utc_now(clock)
                                ),
                            }
                            persist_state(idle)
                            return {
                                **handled_identity,
                                "ok": False,
                                "status": "REJECTED",
                                "state": idle,
                                "order_submitted": True,
                                "consume_signal": True,
                                "error": str(rejection),
                            }
                        state.update(
                            pending_entry_submission_state=(
                                "rejection_position_mismatch"
                            ),
                            pending_entry_last_error=(
                                "entry rejection cannot be finalized because "
                                f"the target product has position size "
                                f"{position_size}"
                            ),
                            pending_entry_last_reconciled_at_utc=_iso(
                                _utc_now(clock)
                            ),
                            execution_snapshot={
                                **dict(state.get("execution_snapshot") or {}),
                                "order_submitted": True,
                                "exchange_api_called": True,
                            },
                        )
                        persist_state(state)
                        return {
                            **handled_identity,
                            "ok": False,
                            "status": "ENTRY_PENDING",
                            "state": state,
                            "order_submitted": True,
                            "consume_signal": False,
                            "error": state["pending_entry_last_error"],
                        }

                    lookup_error = exact.error or (
                        "authoritative client-order absence is not proven"
                    )
                    state.update(
                        pending_entry_submission_state=(
                            "rejection_lookup_inconclusive"
                            if explicit_rejection
                            else "acknowledgement_ambiguous"
                        ),
                        pending_entry_last_error=(
                            (
                                f"entry response reported {rejection}; "
                                if explicit_rejection else
                                "entry acknowledgement is ambiguous; "
                            )
                            + lookup_error
                        )[:500],
                        pending_entry_last_reconciled_at_utc=_iso(
                            _utc_now(clock)
                        ),
                        execution_snapshot={
                            **dict(state.get("execution_snapshot") or {}),
                            "order_submitted": True,
                            "exchange_api_called": True,
                        },
                    )
                    persist_state(state)
                    return {
                        **handled_identity,
                        "ok": False,
                        "status": "ENTRY_PENDING",
                        "state": state,
                        "order_submitted": True,
                        "consume_signal": False,
                        "error": state["pending_entry_last_error"],
                    }
            if isinstance(acknowledged, Mapping):
                # A returned limit mismatch is an execution-invariant breach,
                # not an incomplete acknowledgement that may be papered over
                # by a second lookup which omits the limit field.
                _validate_returned_limit_price(
                    acknowledged, durable_limit_price
                )
            try:
                order = _validate_order_identity(
                    acknowledged,
                    product_id=entry["product_id"],
                    client_order_id=client_id,
                    side=entry["exchange_side"],
                    expected_limit_price=durable_limit_price,
                )
            except LiveScoreExecutionError:
                # A response that omits/garbles the client id is not accepted
                # merely because it carries an order id.  Recover exact.
                try:
                    exact = _normalize_lookup(
                        lookup_order(
                            acknowledged.get("id")
                            if isinstance(acknowledged, Mapping)
                            else None,
                            client_id,
                            entry["product_id"],
                        )
                    )
                except Exception as exc:
                    exact = ExactOrderLookup(
                        None,
                        False,
                        f"exact acknowledgement lookup failed: {exc}",
                    )
                if not exact.order:
                    state.update(
                        pending_entry_submission_state="identity_ambiguous",
                        pending_entry_last_error=exact.error
                        or "entry acknowledgement identity is unverified",
                    )
                    persist_state(state)
                    return {
                        **handled_identity,
                        "ok": False,
                        "status": "ENTRY_PENDING",
                        "state": state,
                        "order_submitted": True,
                        "consume_signal": False,
                        "error": state["pending_entry_last_error"],
                    }
                order = _validate_order_identity(
                    exact.order,
                    product_id=entry["product_id"],
                    client_order_id=client_id,
                    side=entry["exchange_side"],
                    expected_limit_price=durable_limit_price,
                )

        state.update(
            pending_entry_order_id=order.get("id"),
            pending_entry_submission_state="acknowledged",
            pending_entry_last_error="",
            execution_snapshot={
                **dict(state.get("execution_snapshot") or {}),
                "order_id": order.get("id"),
                "order_submitted": True,
                "exchange_api_called": True,
            },
        )
        persist_state(state)

    order, filled, lookup_conclusive = _wait_terminal(
        order,
        product_id=entry["product_id"],
        client_order_id=client_id,
        side=entry["exchange_side"],
        expected_limit_price=durable_limit_price,
        lookup_order=lookup_order,
        timeout_sec=terminal_timeout_sec,
        poll_sec=terminal_poll_sec,
        monotonic=monotonic,
        sleeper=sleeper,
    )
    if filled is None:
        state.update(
            pending_entry_order_id=order.get("id"),
            pending_entry_submission_state=(
                "active"
                if _order_state(order) in _ACTIVE_ORDER_STATES
                else "terminal_or_fill_ambiguous"
            ),
            pending_entry_last_error=(
                "exact order lookup became inconclusive"
                if not lookup_conclusive
                else "entry order is not terminal with a proven fill"
            ),
        )
        persist_state(state)
        return {
            **handled_identity,
            "ok": False,
            "status": "ENTRY_PENDING",
            "state": state,
            "order_submitted": submitted_this_call,
            "consume_signal": False,
            "error": state["pending_entry_last_error"],
        }

    if filled == 0:
        position = get_position(entry["product_id"])
        if not isinstance(position, Mapping):
            state.update(
                pending_entry_submission_state="zero_fill_position_unverified",
                pending_entry_last_error=(
                    "IOC reports zero fill but the real-time position is unavailable"
                ),
            )
            persist_state(state)
            return {
                **handled_identity,
                "ok": False,
                "status": "ENTRY_PENDING",
                "state": state,
                "order_submitted": submitted_this_call,
                "consume_signal": False,
                "error": state["pending_entry_last_error"],
            }
        if _position_size(position, entry["product_id"]) != 0:
            state.update(
                pending_entry_submission_state="zero_fill_position_mismatch",
                pending_entry_last_error=(
                    "IOC reports zero fill but target-product exposure exists"
                ),
            )
            persist_state(state)
            return {
                **handled_identity,
                "ok": False,
                "status": "ENTRY_PENDING",
                "state": state,
                "order_submitted": submitted_this_call,
                "consume_signal": False,
                "error": state["pending_entry_last_error"],
            }
        idle = {
            "slot": "trend",
            "status": "IDLE",
            "dry_run": False,
            "execution_mode": "live",
            "last_entry_transition_id": active_transition_id,
            "last_entry_signal_key": handled_signal_key,
            "last_entry_client_order_id": client_id,
            "last_entry_order_id": order.get("id"),
            "last_entry_order_state": _order_state(order),
            "last_entry_filled_lots": 0,
            "last_entry_attempt_at_utc": _iso(_utc_now(clock)),
        }
        persist_state(idle)
        return {
            **handled_identity,
            "ok": False,
            "status": "NO_FILL",
            "state": idle,
            "order_submitted": submitted_this_call,
            "consume_signal": True,
            "filled_lots": 0,
        }

    position = get_position(entry["product_id"])
    if not isinstance(position, Mapping):
        state.update(
            pending_entry_submission_state="filled_position_unverified",
            pending_entry_order_id=order.get("id"),
            pending_entry_proven_filled_lots=filled,
            pending_entry_last_error=(
                "entry fill is proven but the real-time position is unavailable"
            ),
        )
        persist_state(state)
        return {
            **handled_identity,
            "ok": False,
            "status": "ENTRY_PENDING",
            "state": state,
            "order_submitted": submitted_this_call,
            "consume_signal": False,
            "filled_lots": filled,
            "error": state["pending_entry_last_error"],
        }
    try:
        opened = _open_state_from_fill(state, order, filled, position)
    except LiveScoreExecutionError as exc:
        state.update(
            pending_entry_submission_state="filled_position_mismatch",
            pending_entry_order_id=order.get("id"),
            pending_entry_proven_filled_lots=filled,
            pending_entry_last_error=str(exc),
        )
        persist_state(state)
        return {
            **handled_identity,
            "ok": False,
            "status": "ENTRY_PENDING",
            "state": state,
            "order_submitted": submitted_this_call,
            "consume_signal": False,
            "filled_lots": filled,
            "error": str(exc),
        }

    try:
        persist_state(opened)
    except Exception:
        # The durable ENTRY_PENDING record still contains the exact identity.
        # Attempt immediate risk reduction with the fully proven in-memory
        # state; recovery can subsequently reconcile either outcome.
        try:
            flattened = dict(
                flatten_position(opened, "entry_state_persist_failure")
            )
        except Exception:
            raise
        return {
            **handled_identity,
            "ok": False,
            "status": "FLATTENED_AFTER_STATE_FAILURE",
            "state": flattened,
            "order_submitted": submitted_this_call,
            "consume_signal": True,
            "filled_lots": filled,
            "flat_verified": bool(flattened.get("flat_verified")),
        }

    try:
        protected, protection = protect_position(opened, _utc_now(clock))
    except Exception as exc:
        protected, protection = False, {"error": str(exc)}
    protection_view = (
        copy.deepcopy(dict(protection))
        if isinstance(protection, Mapping)
        else {"error": "protection callback returned an invalid result"}
    )

    def reload_open_cycle() -> tuple[dict[str, Any] | None, str]:
        try:
            loaded = load_state()
        except Exception as exc:
            return None, f"durable Trend state reload failed: {exc}"
        if not isinstance(loaded, Mapping):
            return None, "durable Trend state is unavailable"
        current = copy.deepcopy(dict(loaded))
        expected_cycle = str(opened.get("position_cycle_id") or "").strip()
        current_cycle = str(current.get("position_cycle_id") or "").strip()
        if not expected_cycle or current_cycle != expected_cycle:
            return current, "durable Trend position generation changed"
        expected_transition = str(opened.get("transition_id") or "").strip()
        current_transition = str(current.get("transition_id") or "").strip()
        if (
            not expected_transition
            or current_transition != expected_transition
        ):
            return current, "durable Trend transition generation changed"
        try:
            same_product = int(current.get("product_id") or 0) == int(
                opened.get("product_id") or 0
            )
        except (TypeError, ValueError, OverflowError):
            same_product = False
        if not same_product:
            return current, "durable Trend product generation changed"
        status = str(current.get("status") or "").strip().upper()
        if status not in {"OPEN", "CLOSED"}:
            return current, (
                f"durable same-cycle Trend state is {status or 'unreadable'}"
            )
        return current, ""

    def state_conflict_result(
        current: Mapping[str, Any] | None,
        error: str,
    ) -> dict[str, Any]:
        result_status = (
            "POST_PROTECTION_GENERATION_CHANGED"
            if "generation changed" in error
            else "POST_PROTECTION_STATE_UNVERIFIED"
        )
        return {
            **handled_identity,
            "ok": False,
            "status": result_status,
            "state": copy.deepcopy(dict(current or opened)),
            "order_submitted": submitted_this_call,
            "consume_signal": True,
            "filled_lots": filled,
            "partial_fill": filled < LIVE_SCORE_LOTS,
            "protection_verified": bool(protected),
            "error": error,
        }

    def closed_during_setup_result(
        current: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            **handled_identity,
            "ok": True,
            "status": "CLOSED_DURING_PROTECTION_SETUP",
            "state": copy.deepcopy(dict(current)),
            "order_submitted": submitted_this_call,
            "consume_signal": True,
            "filled_lots": filled,
            "partial_fill": filled < LIVE_SCORE_LOTS,
            "protection_verified": bool(protected),
            "closed_during_protection_setup": True,
        }

    current, reload_error = reload_open_cycle()
    if reload_error:
        return state_conflict_result(current, reload_error)
    assert current is not None
    if str(current.get("status") or "").upper() == "CLOSED":
        return closed_during_setup_result(current)

    if protected:
        latest = {
            **current,
            "protection_verified_at_entry": True,
            "protection_health_at_entry": protection_view,
        }
        if audit:
            audit(
                "trend_score_live_entry_opened",
                {
                    "transition_id": active_transition_id,
                    "signal_key": handled_signal_key,
                    "client_order_id": client_id,
                    "order_id": order.get("id"),
                    "symbol": entry["symbol"],
                    "requested_lots": LIVE_SCORE_LOTS,
                    "filled_lots": filled,
                    "partial_fill": filled < LIVE_SCORE_LOTS,
                    "protection_verified": True,
                },
            )
        return {
            **handled_identity,
            "ok": True,
            "status": "OPEN",
            "state": latest,
            "order_submitted": submitted_this_call,
            "consume_signal": True,
            "filled_lots": filled,
            "partial_fill": filled < LIVE_SCORE_LOTS,
            "protection_verified": True,
        }

    # Re-read immediately before the emergency close.  The monitor can finish
    # a TP/SL/TSL close after its failed/late protection response but before
    # this branch executes.
    current, reload_error = reload_open_cycle()
    if reload_error:
        return state_conflict_result(current, reload_error)
    assert current is not None
    if str(current.get("status") or "").upper() == "CLOSED":
        return closed_during_setup_result(current)

    failed = {
        **current,
        "protection_verified_at_entry": False,
        "protection_health_at_entry": protection_view,
        "protection_failure_at_utc": _iso(_utc_now(clock)),
    }
    try:
        flattened = dict(
            flatten_position(failed, "protection_failure_flatten")
        )
    except Exception as exc:
        current, reload_error = reload_open_cycle()
        if reload_error:
            return state_conflict_result(current, reload_error)
        assert current is not None
        if str(current.get("status") or "").upper() == "CLOSED":
            return {
                **handled_identity,
                "ok": False,
                "status": "FLATTENED_UNPROTECTED",
                "state": current,
                "order_submitted": submitted_this_call,
                "consume_signal": True,
                "filled_lots": filled,
                "partial_fill": filled < LIVE_SCORE_LOTS,
                "protection_verified": False,
                "flat_verified": True,
            }
        still_open = {
            **current,
            "protection_verified_at_entry": False,
            "protection_health_at_entry": protection_view,
            "protection_failure_flatten_error": str(exc),
            "protection_failure_flatten_pending": True,
        }
        return {
            **handled_identity,
            "ok": False,
            "status": "UNPROTECTED_OPEN",
            "state": still_open,
            "order_submitted": submitted_this_call,
            "consume_signal": True,
            "filled_lots": filled,
            "partial_fill": filled < LIVE_SCORE_LOTS,
            "protection_verified": False,
            "error": str(exc),
        }

    # The flatten callback owns the durable reduce-only close.  Its returned
    # flag is not accepted until a fresh durable read shows this same cycle
    # CLOSED; otherwise no stale OPEN/CLOSED write is made here.
    current, reload_error = reload_open_cycle()
    if reload_error:
        return state_conflict_result(current, reload_error)
    assert current is not None
    if str(current.get("status") or "").upper() == "CLOSED":
        return {
            **handled_identity,
            "ok": False,
            "status": "FLATTENED_UNPROTECTED",
            "state": current,
            "order_submitted": submitted_this_call,
            "consume_signal": True,
            "filled_lots": filled,
            "partial_fill": filled < LIVE_SCORE_LOTS,
            "protection_verified": False,
            "flat_verified": True,
        }

    if not flattened.get("flat_verified"):
        still_open = {
            **current,
            **flattened,
            "status": "OPEN",
            "protection_failure_flatten_pending": True,
        }
        return {
            **handled_identity,
            "ok": False,
            "status": "UNPROTECTED_OPEN",
            "state": still_open,
            "order_submitted": submitted_this_call,
            "consume_signal": True,
            "filled_lots": filled,
            "partial_fill": filled < LIVE_SCORE_LOTS,
            "protection_verified": False,
            "error": "emergency flatten is not verified flat",
        }
    still_open = {
        **current,
        "protection_verified_at_entry": False,
        "protection_health_at_entry": protection_view,
        "protection_failure_flatten_pending": True,
    }
    return {
        **handled_identity,
        "ok": False,
        "status": "UNPROTECTED_OPEN",
        "state": still_open,
        "order_submitted": submitted_this_call,
        "consume_signal": True,
        "filled_lots": filled,
        "partial_fill": filled < LIVE_SCORE_LOTS,
        "protection_verified": False,
        "flat_verified": False,
        "error": (
            "emergency flatten reported flat but the durable same-cycle "
            "state is still OPEN"
        ),
    }


__all__ = [
    "ExactOrderLookup",
    "LIVE_SCORE_LOTS",
    "LiveScoreExecutionError",
    "bounded_ioc_payload",
    "build_pending_entry_state",
    "execute_or_recover_entry",
    "score_close_client_id",
    "score_entry_client_id",
    "switch_entry_gate",
    "terminal_filled_lots",
    "validate_fixed_entry",
]
