"""Deterministic, probability-free scenario analysis for BTC options.

The Trend Engine's supplied rules allow conservative scenario analysis when a
validated probability model is unavailable.  This module implements that
path without I/O, randomness, option-price extrapolation, or an implied claim
that replay frequencies are calibrated probabilities.

Caller-supplied, completed 5-minute BTC OHLC bars are separated into complete
UTC days.  Non-overlapping paths from each day are scaled to the current spot,
replayed until target, invalidation, or the candidate's time exit, and valued
using option intrinsic value only.  The lower quantile of per-day mean P&L is
the conservative scenario-edge gate; it is not a calibrated expected value.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence


BAR_SECONDS = 300
BARS_PER_UTC_DAY = 86_400 // BAR_SECONDS
METHOD = "btc_historical_replay_intrinsic_floor_v1"
VALUATION_METHOD = "intrinsic_only_zero_residual_time_value"
MAX_HISTORY_AGE_SECONDS = BAR_SECONDS * 2


class _ScenarioInputError(ValueError):
    """Internal validation error converted into a fail-closed result."""

    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def _finite(value: Any, name: str, *, minimum: float | None = None) -> float:
    if value is None or isinstance(value, bool):
        raise _ScenarioInputError("INVALID_INPUT", f"{name} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise _ScenarioInputError(
            "INVALID_INPUT", f"{name} must be numeric"
        ) from exc
    if not math.isfinite(number):
        raise _ScenarioInputError("INVALID_INPUT", f"{name} must be finite")
    if minimum is not None and number < minimum:
        raise _ScenarioInputError(
            "INVALID_INPUT", f"{name} must be at least {minimum}"
        )
    return number


def _utc_datetime(value: Any, name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise _ScenarioInputError(
                "INVALID_INPUT", f"{name} must be an ISO-8601 UTC timestamp"
            ) from exc
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        number = _finite(value, name, minimum=0.00000001)
        while number > 100_000_000_000:
            number /= 1000.0
        try:
            parsed = datetime.fromtimestamp(number, tz=timezone.utc)
        except (OSError, OverflowError, ValueError) as exc:
            raise _ScenarioInputError(
                "INVALID_INPUT", f"{name} is outside the supported timestamp range"
            ) from exc
    else:
        raise _ScenarioInputError(
            "INVALID_INPUT", f"{name} must be a timezone-aware timestamp"
        )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _ScenarioInputError(
            "INVALID_INPUT", f"{name} must include a UTC offset"
        )
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _rounded(value: float) -> float:
    result = round(float(value), 12)
    return 0.0 if result == -0.0 else result


def _quantile(values: Sequence[float], quantile: float) -> float:
    """Return a deterministic linearly interpolated sample quantile."""

    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise _ScenarioInputError("INSUFFICIENT_HISTORY", "no replay values exist")
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _invalid(reason: str, detail: str, audit: Mapping[str, Any] | None = None
             ) -> dict[str, Any]:
    return {
        "valid": False,
        "reason": reason,
        "expected_value_pass": False,
        "net_expected_value_per_lot": None,
        "probability_win": None,
        "probability_validated": False,
        "target_option_price": None,
        "stop_option_price": None,
        "neutral_option_price": None,
        "audit": {
            "method": METHOD,
            "valuation_method": VALUATION_METHOD,
            "validation_error": detail,
            **dict(audit or {}),
        },
    }


def _normalise_history(history: Iterable[Mapping[str, Any]], now: datetime
                       ) -> list[dict[str, Any]]:
    if isinstance(history, (str, bytes, Mapping)):
        raise _ScenarioInputError(
            "INVALID_HISTORY", "history must be an iterable of OHLC rows"
        )
    try:
        source_rows = list(history)
    except TypeError as exc:
        raise _ScenarioInputError(
            "INVALID_HISTORY", "history must be an iterable of OHLC rows"
        ) from exc
    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(source_rows):
        if not isinstance(raw, Mapping):
            raise _ScenarioInputError(
                "INVALID_HISTORY", f"history[{index}] must be an object"
            )
        timestamp = _utc_datetime(
            raw.get("timestamp", raw.get("time")),
            f"history[{index}].timestamp",
        )
        opened = _finite(raw.get("open"), f"history[{index}].open", minimum=0.00000001)
        high = _finite(raw.get("high"), f"history[{index}].high", minimum=0.00000001)
        low = _finite(raw.get("low"), f"history[{index}].low", minimum=0.00000001)
        close = _finite(raw.get("close"), f"history[{index}].close", minimum=0.00000001)
        if high < max(opened, close) or low > min(opened, close) or high < low:
            raise _ScenarioInputError(
                "INVALID_HISTORY", f"history[{index}] has invalid OHLC"
            )
        if timestamp + timedelta(seconds=BAR_SECONDS) > now:
            raise _ScenarioInputError(
                "UNFINISHED_HISTORY", "history contains an unfinished 5-minute bar"
            )
        rows.append({
            "timestamp": timestamp,
            "open": opened,
            "high": high,
            "low": low,
            "close": close,
        })
    if not rows:
        raise _ScenarioInputError("INSUFFICIENT_HISTORY", "history is empty")
    rows.sort(key=lambda row: row["timestamp"])
    for previous, current in zip(rows, rows[1:]):
        spacing = (current["timestamp"] - previous["timestamp"]).total_seconds()
        if spacing != BAR_SECONDS:
            reason = "DUPLICATE_HISTORY" if spacing == 0 else "IRREGULAR_HISTORY"
            raise _ScenarioInputError(
                reason, "history must contain unique, uninterrupted 5-minute bars"
            )
    age = (now - (rows[-1]["timestamp"] + timedelta(seconds=BAR_SECONDS))).total_seconds()
    if age > MAX_HISTORY_AGE_SECONDS:
        raise _ScenarioInputError(
            "STALE_HISTORY",
            f"latest completed history bar is {int(age)} seconds old",
        )
    return rows


def _complete_days(rows: Sequence[Mapping[str, Any]]) -> list[tuple[date, list[dict[str, Any]]]]:
    grouped: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for raw in rows:
        row = dict(raw)
        grouped[row["timestamp"].date()].append(row)
    complete: list[tuple[date, list[dict[str, Any]]]] = []
    for day in sorted(grouped):
        day_rows = grouped[day]
        midnight = datetime.combine(day, time.min, tzinfo=timezone.utc)
        expected = [
            midnight + timedelta(seconds=BAR_SECONDS * index)
            for index in range(BARS_PER_UTC_DAY)
        ]
        if len(day_rows) == BARS_PER_UTC_DAY and [
                row["timestamp"] for row in day_rows] == expected:
            complete.append((day, day_rows))
    return complete


def _history_hash(days: Sequence[tuple[date, Sequence[Mapping[str, Any]]]]) -> str:
    canonical = []
    for _, rows in days:
        for row in rows:
            canonical.append({
                "timestamp": _iso(row["timestamp"]),
                "open": format(float(row["open"]), ".17g"),
                "high": format(float(row["high"]), ".17g"),
                "low": format(float(row["low"]), ".17g"),
                "close": format(float(row["close"]), ".17g"),
            })
    encoded = json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def prepare_scenario_history(
    history: Iterable[Mapping[str, Any]],
    *,
    now: Any,
    min_complete_days: int = 7,
) -> dict[str, Any]:
    """Validate and parse shared replay history once per engine evaluation.

    The private row/day fields are intentionally in-memory only.  Callers pass
    this object back to :func:`conservative_intrinsic_scenario`; they must not
    persist or expose it as decision audit data.
    """

    audit: dict[str, Any] = {}
    try:
        evaluated_now = _utc_datetime(now, "now")
        if isinstance(min_complete_days, bool):
            raise _ScenarioInputError(
                "INVALID_INPUT", "min_complete_days must be an integer"
            )
        minimum_days_number = _finite(
            min_complete_days, "min_complete_days", minimum=1
        )
        if not minimum_days_number.is_integer():
            raise _ScenarioInputError(
                "INVALID_INPUT", "min_complete_days must be an integer"
            )
        minimum_days = int(minimum_days_number)
        rows = _normalise_history(history, evaluated_now)
        days = _complete_days(rows)
        audit = {
            "input_history_start": _iso(rows[0]["timestamp"]),
            "input_history_end": _iso(rows[-1]["timestamp"]),
            "input_bar_count": len(rows),
            "complete_day_count": len(days),
            "min_complete_days": minimum_days,
        }
        if len(days) < minimum_days:
            raise _ScenarioInputError(
                "INSUFFICIENT_COMPLETE_DAYS",
                f"scenario requires {minimum_days} complete UTC days; got {len(days)}",
            )
        audit.update({
            "history_window_start": _iso(days[0][1][0]["timestamp"]),
            "history_window_end": _iso(days[-1][1][-1]["timestamp"]),
            "history_bar_count": sum(len(day_rows) for _, day_rows in days),
            "history_sha256": _history_hash(days),
            "used_utc_days": [day.isoformat() for day, _ in days],
        })
        return {
            "valid": True,
            "reason": "OK",
            "prepared_for_now": _iso(evaluated_now),
            "audit": audit,
            "_rows": rows,
            "_days": days,
        }
    except _ScenarioInputError as exc:
        return {
            "valid": False,
            "reason": exc.reason,
            "validation_error": exc.detail,
            "prepared_for_now": None,
            "audit": audit,
            "_rows": [],
            "_days": [],
        }


def _intrinsic(option_type: str, underlying: float, strike: float) -> float:
    return max(underlying - strike, 0.0) if option_type == "CE" else max(
        strike - underlying, 0.0
    )


def _path_exit(
    rows: Sequence[Mapping[str, Any]],
    *,
    spot: float,
    target: float,
    invalidation: float,
    option_type: str,
) -> tuple[float, str, bool]:
    base = float(rows[0]["open"])
    scale = spot / base
    bullish = option_type == "CE"
    for row in rows:
        opened = float(row["open"]) * scale
        high = float(row["high"]) * scale
        low = float(row["low"]) * scale
        target_hit = high >= target if bullish else low <= target
        stop_hit = low <= invalidation if bullish else high >= invalidation
        if target_hit and stop_hit:
            # OHLC cannot prove intrabar ordering.  Capital protection therefore
            # assumes the adverse barrier was reached first.
            adverse_open = min(opened, invalidation) if bullish else max(
                opened, invalidation
            )
            return adverse_open, "invalidation", True
        if stop_hit:
            adverse_open = min(opened, invalidation) if bullish else max(
                opened, invalidation
            )
            return adverse_open, "invalidation", False
        if target_hit:
            # Capping a favourable gap at the target is intentionally conservative.
            return target, "target", False
    return float(rows[-1]["close"]) * scale, "time_exit", False


def conservative_intrinsic_scenario(
    history: Iterable[Mapping[str, Any]],
    *,
    spot: float,
    target_price: float,
    invalidation_price: float,
    option_type: str,
    strike: float,
    entry_price: float,
    contract_value: float,
    costs_per_lot: float,
    time_exit: Any,
    now: Any,
    min_complete_days: int = 7,
    lower_quantile: float = 0.20,
    prepared_history: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Replay recent complete BTC days and return conservative option EV.

    ``net_expected_value_per_lot`` is retained for schema compatibility; its
    value is the requested lower quantile of per-UTC-day mean replay P&L.  It
    is deliberately *not* a calibrated probability-weighted forecast, so both
    probability fields are always returned as ``None``/``False``.
    """

    initial_audit: dict[str, Any] = {
        "method": METHOD,
        "valuation_method": VALUATION_METHOD,
        "bar_seconds": BAR_SECONDS,
        "history_hash_scope": "complete_utc_days_used",
        "probability_policy": "not_estimated",
        "path_weighting": "equal_weight_within_day_then_equal_weight_by_utc_day",
        "edge_semantics": "uncalibrated_historical_scenario_lower_quantile",
    }
    try:
        evaluated_now = _utc_datetime(now, "now")
        requested_exit = _utc_datetime(time_exit, "time_exit")
        if requested_exit <= evaluated_now:
            raise _ScenarioInputError(
                "INVALID_HORIZON", "time_exit must be later than now"
            )
        requested_horizon = (requested_exit - evaluated_now).total_seconds()
        horizon_bars = int(requested_horizon // BAR_SECONDS)
        if horizon_bars < 1:
            raise _ScenarioInputError(
                "HORIZON_TOO_SHORT", "time_exit must allow one completed 5-minute bar"
            )
        if horizon_bars > BARS_PER_UTC_DAY:
            raise _ScenarioInputError(
                "HORIZON_TOO_LONG",
                "historical replay supports candidate horizons up to 24 hours",
            )
        if isinstance(min_complete_days, bool):
            raise _ScenarioInputError(
                "INVALID_INPUT", "min_complete_days must be an integer"
            )
        try:
            minimum_days_number = float(min_complete_days)
        except (TypeError, ValueError, OverflowError) as exc:
            raise _ScenarioInputError(
                "INVALID_INPUT", "min_complete_days must be an integer"
            ) from exc
        if (not math.isfinite(minimum_days_number)
                or not minimum_days_number.is_integer()
                or minimum_days_number < 1):
            raise _ScenarioInputError(
                "INVALID_INPUT", "min_complete_days must be a positive integer"
            )
        minimum_days = int(minimum_days_number)
        quantile = _finite(lower_quantile, "lower_quantile", minimum=0)
        if not 0 < quantile < 0.5:
            raise _ScenarioInputError(
                "INVALID_INPUT", "lower_quantile must be between zero and 0.5"
            )
        current_spot = _finite(spot, "spot", minimum=0.00000001)
        target = _finite(target_price, "target_price", minimum=0.00000001)
        invalidation = _finite(
            invalidation_price, "invalidation_price", minimum=0.00000001
        )
        option = str(option_type or "").strip().upper()
        if option not in {"CE", "PE"}:
            raise _ScenarioInputError("INVALID_INPUT", "option_type must be CE or PE")
        if option == "CE" and not target > current_spot > invalidation:
            raise _ScenarioInputError(
                "INVALID_BARRIERS", "CE requires target > spot > invalidation"
            )
        if option == "PE" and not target < current_spot < invalidation:
            raise _ScenarioInputError(
                "INVALID_BARRIERS", "PE requires target < spot < invalidation"
            )
        option_strike = _finite(strike, "strike", minimum=0.00000001)
        entry = _finite(entry_price, "entry_price", minimum=0.00000001)
        multiplier = _finite(
            contract_value, "contract_value", minimum=0.0000000001
        )
        costs = _finite(costs_per_lot, "costs_per_lot", minimum=0)

        if prepared_history is not None:
            if not isinstance(prepared_history, Mapping):
                raise _ScenarioInputError(
                    "INVALID_HISTORY", "prepared_history must be an object"
                )
            initial_audit.update(dict(prepared_history.get("audit") or {}))
            if prepared_history.get("valid") is not True:
                raise _ScenarioInputError(
                    str(prepared_history.get("reason") or "INVALID_HISTORY"),
                    str(prepared_history.get("validation_error") or
                        "prepared history is unavailable"),
                )
            if prepared_history.get("prepared_for_now") != _iso(evaluated_now):
                raise _ScenarioInputError(
                    "INVALID_HISTORY", "prepared history belongs to another snapshot"
                )
            rows = list(prepared_history.get("_rows") or [])
            days = list(prepared_history.get("_days") or [])
            if not rows or not days:
                raise _ScenarioInputError(
                    "INVALID_HISTORY", "prepared history is empty"
                )
        else:
            rows = _normalise_history(history, evaluated_now)
            days = _complete_days(rows)
        initial_audit.update({
            "now": _iso(evaluated_now),
            "requested_time_exit": _iso(requested_exit),
            "requested_horizon_seconds": _rounded(requested_horizon),
            "evaluated_horizon_seconds": horizon_bars * BAR_SECONDS,
            "effective_time_exit": _iso(
                evaluated_now + timedelta(seconds=horizon_bars * BAR_SECONDS)
            ),
            "horizon_bars": horizon_bars,
            "min_complete_days": minimum_days,
            "lower_quantile": quantile,
            "input_history_start": initial_audit.get(
                "input_history_start", _iso(rows[0]["timestamp"])
            ),
            "input_history_end": initial_audit.get(
                "input_history_end", _iso(rows[-1]["timestamp"])
            ),
            "input_bar_count": initial_audit.get("input_bar_count", len(rows)),
            "complete_day_count": len(days),
        })
        if len(days) < minimum_days:
            raise _ScenarioInputError(
                "INSUFFICIENT_COMPLETE_DAYS",
                f"scenario requires {minimum_days} complete UTC days; got {len(days)}",
            )

        path_counts: dict[str, int] = {}
        daily_rows: list[dict[str, Any]] = []
        target_exits = 0
        invalidation_exits = 0
        time_exits = 0
        ambiguous_stops = 0
        total_paths = 0
        all_path_pnl: list[float] = []
        for day, day_bars in days:
            day_pnl: list[float] = []
            for start in range(0, len(day_bars) - horizon_bars + 1, horizon_bars):
                path = day_bars[start:start + horizon_bars]
                if len(path) != horizon_bars:
                    continue
                exit_underlying, exit_reason, ambiguous = _path_exit(
                    path,
                    spot=current_spot,
                    target=target,
                    invalidation=invalidation,
                    option_type=option,
                )
                exit_option = _intrinsic(option, exit_underlying, option_strike)
                pnl = (exit_option - entry) * multiplier - costs
                day_pnl.append(pnl)
                all_path_pnl.append(pnl)
                total_paths += 1
                ambiguous_stops += int(ambiguous)
                if exit_reason == "target":
                    target_exits += 1
                elif exit_reason == "invalidation":
                    invalidation_exits += 1
                else:
                    time_exits += 1
            if not day_pnl:
                raise _ScenarioInputError(
                    "INSUFFICIENT_HISTORY", f"no replay paths fit in UTC day {day}"
                )
            label = day.isoformat()
            path_counts[label] = len(day_pnl)
            daily_rows.append({
                "date": label,
                "path_count": len(day_pnl),
                "mean_net_pnl_per_lot": _rounded(sum(day_pnl) / len(day_pnl)),
            })

        daily_means = [row["mean_net_pnl_per_lot"] for row in daily_rows]
        p_lower = _quantile(daily_means, quantile)
        p50 = _quantile(daily_means, 0.5)
        p_upper = _quantile(daily_means, 1.0 - quantile)
        expected_value_pass = p_lower > 0
        used_rows = sum(len(day_rows) for _, day_rows in days)
        initial_audit.update({
            "history_window_start": initial_audit.get(
                "history_window_start", _iso(days[0][1][0]["timestamp"])
            ),
            "history_window_end": initial_audit.get(
                "history_window_end", _iso(days[-1][1][-1]["timestamp"])
            ),
            "history_bar_count": initial_audit.get(
                "history_bar_count", used_rows
            ),
            "history_sha256": (
                initial_audit["history_sha256"]
                if "history_sha256" in initial_audit else _history_hash(days)
            ),
            "used_utc_days": initial_audit.get(
                "used_utc_days", [day.isoformat() for day, _ in days]
            ),
            "path_counts_by_day": path_counts,
            "total_path_count": total_paths,
            "daily_mean_net_pnl_per_lot": daily_rows,
            "net_ev_quantiles_per_lot": {
                f"p{int(round(quantile * 100)):02d}": _rounded(p_lower),
                "p50": _rounded(p50),
                f"p{int(round((1.0 - quantile) * 100)):02d}": _rounded(p_upper),
            },
            "target_exit_path_count": target_exits,
            "invalidation_exit_path_count": invalidation_exits,
            "time_exit_path_count": time_exits,
            "ambiguous_bar_stop_first_count": ambiguous_stops,
            "worst_net_pnl_per_lot": _rounded(min(all_path_pnl)),
            "best_net_pnl_per_lot": _rounded(max(all_path_pnl)),
            "target_option_intrinsic": _rounded(
                _intrinsic(option, target, option_strike)
            ),
            "invalidation_option_intrinsic": _rounded(
                _intrinsic(option, invalidation, option_strike)
            ),
            "neutral_option_intrinsic": _rounded(
                _intrinsic(option, current_spot, option_strike)
            ),
            "entry_price": _rounded(entry),
            "contract_value": _rounded(multiplier),
            "costs_per_lot": _rounded(costs),
        })
        return {
            "valid": True,
            "reason": "OK" if expected_value_pass else "NEGATIVE_EXPECTED_VALUE",
            "expected_value_pass": expected_value_pass,
            "net_expected_value_per_lot": _rounded(p_lower),
            "probability_win": None,
            "probability_validated": False,
            "target_option_price": initial_audit["target_option_intrinsic"],
            "stop_option_price": initial_audit["invalidation_option_intrinsic"],
            "neutral_option_price": initial_audit["neutral_option_intrinsic"],
            "audit": initial_audit,
        }
    except _ScenarioInputError as exc:
        return _invalid(exc.reason, exc.detail, initial_audit)


__all__ = ["conservative_intrinsic_scenario", "prepare_scenario_history"]
