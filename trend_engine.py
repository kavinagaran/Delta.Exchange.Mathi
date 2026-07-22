"""Deterministic, fail-closed market-direction and long-option decision engine.

The module is deliberately pure: it performs no I/O, reads no credentials and
cannot place an order.  Callers provide one complete, point-in-time snapshot
and receive one JSON-serialisable decision object.  Direction is derived from
the underlying before CE/PE contracts are filtered or ranked.

Public entry points are :func:`evaluate_trend` and
:func:`evaluate_trend_json`.  Input errors are represented as a schema-shaped
``NO_TRADE`` decision with ``INVALID_OR_STALE_DATA``; they are never papered
over with estimated market data.
"""

from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from trend_scenario import conservative_intrinsic_scenario, prepare_scenario_history


DECISIONS = {"BUY_CE", "BUY_PE", "HOLD", "EXIT", "NO_TRADE"}
TIMEFRAMES = ("60m", "15m", "5m")
TIMEFRAME_MINUTES = {"5m": 5, "15m": 15, "60m": 60}
TIMEFRAME_WEIGHTS = {"60m": 0.40, "15m": 0.35, "5m": 0.25}

DEFAULT_CONFIG: dict[str, Any] = {
    "underlying": "",
    "entry_timeframe": "5m",
    "setup_timeframe": "15m",
    "regime_timeframe": "60m",
    "holding_period": "INTRADAY_OR_1_TO_3_DAYS",
    "max_risk_per_trade_pct": 0.5,
    "max_daily_loss_pct": 1.5,
    "max_consecutive_losses": 3,
    "min_direction_score": 40,
    "min_price_action_score": 10,
    "min_contract_score": 70,
    "min_trade_score": 65,
    "min_reward_risk": 1.5,
    "max_bid_ask_spread_pct": 3.0,
    # BTC options have daily expiries.  Eligibility is therefore based on the
    # precise settlement timestamp, not a calendar-day DTE band.
    "min_time_to_expiry_hours": 1.5,
    "settlement_exit_buffer_minutes": 30.0,
    "preferred_abs_delta_min": 0.40,
    "preferred_abs_delta_max": 0.65,
    "max_portfolio_positions": 1,
    "allow_event_trading": False,
    # An unavailable calendar remains explicit in the audit trail.  Phase 1
    # may approve that unknown risk for DRY RUN; a known blackout still blocks.
    "allow_unknown_event_risk": False,
    "allow_averaging_down": False,
    # Operational limits intentionally have conservative fixed defaults.  They
    # may be overridden only before evaluation, never learned during trading.
    "model_version": "trend-engine-1.1.0",
    "max_data_latency_seconds": 120,
    "max_timestamp_alignment_seconds": 30,
    "max_candle_age_intervals": 2,
    "min_candles_per_timeframe": 55,
    "event_blackout_minutes": 60,
    "min_option_volume": 1,
    "min_option_open_interest": 1,
    "max_entry_slippage_pct": 1.0,
    "min_underlying_core_score": 30,
    "reversal_score_premium": 15,
    "exit_direction_score": 10,
    "forecast_holding_days": 1.0,
    "max_holding_days": 3.0,
    "target_atr_multiple": 2.0,
    "stop_atr_multiple": 1.0,
    "max_order_lots": 1_000_000,
    "max_exposure_pct": 100.0,
    "scenario_min_complete_days": 7,
    "scenario_lower_quantile": 0.20,
}

_NUMERIC_CONFIG = {
    key for key, value in DEFAULT_CONFIG.items()
    if isinstance(value, (int, float)) and not isinstance(value, bool)
}
_BOOLEAN_CONFIG = {
    key for key, value in DEFAULT_CONFIG.items() if isinstance(value, bool)
}


class TrendInputError(ValueError):
    """A snapshot is incomplete or unsafe to evaluate."""


def _finite(value: Any, name: str, *, minimum: float | None = None,
            maximum: float | None = None) -> float:
    if isinstance(value, bool):
        raise TrendInputError(f"{name} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise TrendInputError(f"{name} must be numeric") from exc
    if not math.isfinite(result):
        raise TrendInputError(f"{name} must be finite")
    if minimum is not None and result < minimum:
        raise TrendInputError(f"{name} must be at least {minimum}")
    if maximum is not None and result > maximum:
        raise TrendInputError(f"{name} must be at most {maximum}")
    return result


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    number = _finite(value, name, minimum=minimum)
    if not number.is_integer():
        raise TrendInputError(f"{name} must be an integer")
    return int(number)


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise TrendInputError(f"{name} must be boolean")
    return value


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TrendInputError(f"{name} must be an object")
    return value


def _list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise TrendInputError(f"{name} must be an array")
    return value


def _parse_time(value: Any, name: str) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        raw = _finite(value, name)
        # Accommodate epoch seconds, milliseconds, microseconds and nanoseconds.
        while abs(raw) > 100_000_000_000:
            raw /= 1000.0
        try:
            result = datetime.fromtimestamp(raw, tz=timezone.utc)
        except (OSError, OverflowError, ValueError) as exc:
            raise TrendInputError(f"{name} is outside the timestamp range") from exc
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            result = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise TrendInputError(f"{name} must be ISO-8601") from exc
    else:
        raise TrendInputError(f"{name} is required")
    if result.tzinfo is None or result.utcoffset() is None:
        raise TrendInputError(f"{name} must include a timezone")
    return result.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _clamp(value: float, low: float, high: float) -> float:
    return min(max(float(value), low), high)


def _round_score(value: float) -> float:
    result = round(float(value), 4)
    return 0.0 if result == -0.0 else result


def _canonical(value: Any) -> str:
    def default(item: Any) -> str:
        if isinstance(item, datetime):
            return _iso(item)
        return repr(item)
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, default=default)


def _decision_id(snapshot: Any, config: Any) -> str:
    digest = hashlib.sha256(
        (_canonical(snapshot) + "|" + _canonical(config)).encode("utf-8")
    ).hexdigest()[:20]
    return f"trend-{digest}"


def _null_contract() -> dict[str, Any]:
    return {
        "symbol": None, "option_type": None, "strike": None,
        "expiry": None, "days_to_expiry": None,
        "time_to_expiry_hours": None, "bid": None,
        "ask": None, "mid": None, "spread_pct": None, "volume": None,
        "open_interest": None, "implied_volatility": None, "delta": None,
        "theta": None, "contract_score": None,
        "contract_components": {
            "liquidity": None, "spread": None, "delta": None,
            "expiry": None, "iv_value": None, "breakeven": None,
            "theta_efficiency": None,
        },
    }


def _null_order() -> dict[str, Any]:
    return {
        "order_type": None, "entry_price": None,
        "maximum_entry_price": None, "quantity_lots": 0,
        "lot_size": None, "stop_option_price": None,
        "underlying_invalidation": None, "target_option_price": None,
        "underlying_target": None, "time_exit": None,
        "estimated_total_costs": None, "maximum_estimated_loss": None,
        "reward_risk": None,
    }


def _fallback_timestamp(snapshot: Any) -> str:
    if isinstance(snapshot, Mapping):
        try:
            return _iso(_parse_time(snapshot.get("timestamp"), "timestamp"))
        except TrendInputError:
            pass
    # A fixed sentinel keeps invalid-input decisions reproducible and avoids
    # pretending that a usable market timestamp was supplied.
    return "1970-01-01T00:00:00Z"


def _base_result(snapshot: Any, config: Mapping[str, Any]) -> dict[str, Any]:
    timestamp = _fallback_timestamp(snapshot)
    underlying = ""
    if isinstance(snapshot, Mapping):
        raw = snapshot.get("underlying")
        if isinstance(raw, str):
            underlying = raw.strip().upper()
        elif isinstance(raw, Mapping):
            underlying = str(raw.get("symbol") or "").strip().upper()
    underlying = underlying or str(config.get("underlying") or "").strip().upper()
    result = {
        "schema_version": "1.0",
        "model_version": str(config.get("model_version") or "trend-engine-1.0.0"),
        "decision_id": _decision_id(snapshot, config),
        "timestamp": timestamp,
        "market_data_timestamp": timestamp,
        "underlying": underlying,
        "decision": "NO_TRADE",
        "directional_bias": "NEUTRAL",
        "confidence": "LOW",
        "direction_score": 0.0,
        "direction_components": {
            "price_action": 0.0, "candlestick": 0.0, "trend": 0.0,
            "momentum": 0.0, "volume_vwap": 0.0,
            "breadth_sector": 0.0, "derivatives_positioning": 0.0,
            "volatility_catalyst": 0.0,
        },
        "timeframe_scores": {"60m": 0.0, "15m": 0.0, "5m": 0.0},
        "market_regime": "UNCLEAR",
        "detected_setup": {
            "market_structure": "UNAVAILABLE",
            "candlestick_pattern": "NONE", "pattern_confirmed": False,
            "support": None, "resistance": None,
            "invalidation_level": None,
        },
        "selected_contract": _null_contract(),
        "trade_score": None,
        "order_plan": _null_order(),
        "risk_state": {
            "account_equity": None, "risk_budget": None,
            "daily_pnl": None, "consecutive_losses": None,
            "kill_switch_active": False,
        },
        "hard_gates": {
            "data_valid": False, "direction_pass": False,
            "price_action_pass": False, "contract_pass": False,
            "spread_pass": False, "expiry_pass": False,
            "event_pass": False, "expected_value_pass": False,
            "reward_risk_pass": False, "portfolio_risk_pass": False,
        },
        "reason_codes": [],
        "decision_summary": "Input has not been evaluated.",
    }
    return result


def _load_config(overrides: Mapping[str, Any] | None) -> dict[str, Any]:
    if overrides is not None and not isinstance(overrides, Mapping):
        raise TrendInputError("config_overrides must be an object")
    config = deepcopy(DEFAULT_CONFIG)
    for key, value in (overrides or {}).items():
        if key not in config:
            raise TrendInputError(f"unapproved configuration key: {key}")
        config[key] = value
    for key in _BOOLEAN_CONFIG:
        _boolean(config[key], f"config.{key}")
    for key in _NUMERIC_CONFIG:
        minimum = 0
        _finite(config[key], f"config.{key}", minimum=minimum)
    for key in ("min_candles_per_timeframe", "max_consecutive_losses",
                "max_portfolio_positions", "max_order_lots",
                "scenario_min_complete_days"):
        _integer(config[key], f"config.{key}", minimum=1)
    for key in ("max_risk_per_trade_pct", "max_daily_loss_pct",
                "max_bid_ask_spread_pct", "max_entry_slippage_pct",
                "max_exposure_pct"):
        _finite(config[key], f"config.{key}", minimum=0, maximum=100)
    if float(config["min_time_to_expiry_hours"]) < 1.5:
        raise TrendInputError("minimum time to expiry cannot be below 1.5 hours")
    if float(config["settlement_exit_buffer_minutes"]) <= 0:
        raise TrendInputError("settlement exit buffer must be positive")
    if not 0 < float(config["scenario_lower_quantile"]) < 0.5:
        raise TrendInputError(
            "scenario lower quantile must be between zero and 0.5"
        )
    if config["preferred_abs_delta_min"] > config["preferred_abs_delta_max"]:
        raise TrendInputError("minimum preferred delta cannot exceed maximum")
    if config["entry_timeframe"] != "5m" or config["setup_timeframe"] != "15m" \
            or config["regime_timeframe"] != "60m":
        raise TrendInputError("this model version requires 5m/15m/60m timeframes")
    return config


def _ema(values: Sequence[float], period: int) -> list[float]:
    if len(values) < period:
        raise TrendInputError(f"at least {period} values are required")
    alpha = 2.0 / (period + 1.0)
    result = [float(values[0])]
    for value in values[1:]:
        result.append(alpha * float(value) + (1.0 - alpha) * result[-1])
    return result


def _atr(candles: Sequence[Mapping[str, Any]], period: int = 14) -> float:
    ranges: list[float] = []
    for index, row in enumerate(candles):
        if index == 0:
            tr = row["high"] - row["low"]
        else:
            previous = candles[index - 1]["close"]
            tr = max(row["high"] - row["low"],
                     abs(row["high"] - previous),
                     abs(row["low"] - previous))
        ranges.append(tr)
    tail = ranges[-period:]
    result = sum(tail) / len(tail)
    if result <= 0:
        raise TrendInputError("ATR is zero")
    return result


def _rsi(values: Sequence[float], period: int = 14) -> float:
    changes = [values[index] - values[index - 1]
               for index in range(1, len(values))]
    tail = changes[-period:]
    gains = sum(max(change, 0.0) for change in tail) / period
    losses = sum(max(-change, 0.0) for change in tail) / period
    if losses == 0:
        return 100.0 if gains > 0 else 50.0
    rs = gains / losses
    return 100.0 - 100.0 / (1.0 + rs)


def _normalise_candles(raw: Any, timeframe: str, now: datetime,
                       config: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = _list(raw, f"candles.{timeframe}")
    minimum = _integer(config["min_candles_per_timeframe"],
                       "config.min_candles_per_timeframe", minimum=55)
    if len(rows) < minimum:
        raise TrendInputError(f"candles.{timeframe} requires at least {minimum} rows")
    interval = timedelta(minutes=TIMEFRAME_MINUTES[timeframe])
    normalised: list[dict[str, Any]] = []
    previous_time: datetime | None = None
    for index, raw_row in enumerate(rows):
        row = _mapping(raw_row, f"candles.{timeframe}[{index}]")
        timestamp = _parse_time(row.get("timestamp", row.get("time")),
                                f"candles.{timeframe}[{index}].timestamp")
        if previous_time is not None and timestamp <= previous_time:
            raise TrendInputError(f"candles.{timeframe} must be chronological")
        if previous_time is not None and timestamp - previous_time != interval:
            raise TrendInputError(f"candles.{timeframe} contains a missing or irregular bar")
        previous_time = timestamp
        opened = _finite(row.get("open"), f"candles.{timeframe}[{index}].open",
                         minimum=0.00000001)
        high = _finite(row.get("high"), f"candles.{timeframe}[{index}].high",
                       minimum=0.00000001)
        low = _finite(row.get("low"), f"candles.{timeframe}[{index}].low",
                      minimum=0.00000001)
        close = _finite(row.get("close"), f"candles.{timeframe}[{index}].close",
                        minimum=0.00000001)
        volume = _finite(row.get("volume"),
                         f"candles.{timeframe}[{index}].volume", minimum=0)
        if high < max(opened, close) or low > min(opened, close) or high < low:
            raise TrendInputError(f"candles.{timeframe}[{index}] has invalid OHLC")
        flag = row.get("complete", row.get("closed", row.get("is_closed")))
        if flag is not None and not _boolean(
                flag, f"candles.{timeframe}[{index}].complete"):
            raise TrendInputError(f"candles.{timeframe} contains an unfinished candle")
        # Timestamps are treated as bar-open timestamps.  An explicit true
        # completion flag does not make a future close safe.
        if timestamp + interval > now:
            raise TrendInputError(f"candles.{timeframe} contains an unfinished candle")
        normalised.append({
            "timestamp": timestamp, "open": opened, "high": high,
            "low": low, "close": close, "volume": volume,
        })
    last_close = normalised[-1]["timestamp"] + interval
    max_age = interval * float(config["max_candle_age_intervals"])
    if now - last_close > max_age:
        raise TrendInputError(f"candles.{timeframe} is stale")
    return normalised


def _validate_timestamp(timestamp: datetime, now: datetime, name: str,
                        max_age_seconds: float) -> None:
    age = (now - timestamp).total_seconds()
    if age < 0 or age > max_age_seconds:
        raise TrendInputError(f"{name} is stale or in the future")


def _normalise_contract(raw: Any, index: int, now: datetime,
                        market_data_timestamp: datetime,
                        config: Mapping[str, Any]) -> dict[str, Any]:
    row = _mapping(raw, f"option_contracts[{index}]")
    prefix = f"option_contracts[{index}]"
    symbol = str(row.get("symbol") or "").strip()
    if not symbol:
        raise TrendInputError(f"{prefix}.symbol is required")
    option_type = str(row.get("option_type") or "").strip().upper()
    if option_type not in {"CE", "PE"}:
        raise TrendInputError(f"{prefix}.option_type must be CE or PE")
    expiry = _parse_time(row.get("expiry"), f"{prefix}.expiry")
    quote_timestamp = _parse_time(row.get("quote_timestamp"),
                                  f"{prefix}.quote_timestamp")
    _validate_timestamp(quote_timestamp, now, f"{prefix}.quote_timestamp",
                        float(config["max_data_latency_seconds"]))
    if abs((quote_timestamp - market_data_timestamp).total_seconds()) > float(
            config["max_timestamp_alignment_seconds"]):
        raise TrendInputError(f"{prefix}.quote_timestamp is not aligned")
    strike = _finite(row.get("strike"), f"{prefix}.strike", minimum=0.00000001)
    bid = _finite(row.get("bid"), f"{prefix}.bid", minimum=0)
    ask = _finite(row.get("ask"), f"{prefix}.ask", minimum=0)
    if bid > ask:
        raise TrendInputError(f"{prefix}.bid cannot exceed ask")
    volume = _finite(row.get("volume"), f"{prefix}.volume", minimum=0)
    oi = _finite(row.get("open_interest"), f"{prefix}.open_interest", minimum=0)
    iv = _finite(row.get("implied_volatility", row.get("iv")),
                 f"{prefix}.implied_volatility", minimum=0)
    delta = _finite(row.get("delta"), f"{prefix}.delta", minimum=-1, maximum=1)
    if (option_type == "CE" and delta < 0) or (option_type == "PE" and delta > 0):
        raise TrendInputError(f"{prefix}.delta is inconsistent with option type")
    theta = _finite(row.get("theta"), f"{prefix}.theta")
    lot_size = _integer(row.get("lot_size"), f"{prefix}.lot_size", minimum=1)
    # Delta contracts express P&L as premium points * contract_value * lots.
    # This value is mandatory; substituting lot_size would silently mis-size.
    contract_value = _finite(row.get("contract_value", row.get("multiplier")),
                             f"{prefix}.contract_value", minimum=0.0000000001)
    bid_quantity = _finite(row.get("bid_quantity") if row.get("bid_quantity")
                           is not None else 0,
                           f"{prefix}.bid_quantity", minimum=0)
    ask_quantity = _finite(row.get("ask_quantity") if row.get("ask_quantity")
                           is not None else 0,
                           f"{prefix}.ask_quantity", minimum=0)
    vega_value = row.get("vega")
    vega = None if vega_value is None else _finite(
        vega_value, f"{prefix}.vega", minimum=0)
    gamma_value = row.get("gamma")
    gamma = None if gamma_value is None else _finite(
        gamma_value, f"{prefix}.gamma", minimum=0)
    iv_percentile_value = row.get("iv_percentile")
    iv_percentile = None if iv_percentile_value is None else _finite(
        iv_percentile_value, f"{prefix}.iv_percentile", minimum=0, maximum=100)
    max_order_lots = row.get("max_order_lots", config["max_order_lots"])
    max_order_lots = _integer(max_order_lots, f"{prefix}.max_order_lots", minimum=1)
    tick_size = _finite(row.get("tick_size"), f"{prefix}.tick_size",
                        minimum=0.0000000001)

    def optional_number(key: str, *, minimum: float | None = None) -> float | None:
        value = row.get(key)
        if value is None:
            return None
        return _finite(value, f"{prefix}.{key}", minimum=minimum)

    return {
        "symbol": symbol, "option_type": option_type, "expiry": expiry,
        "quote_timestamp": quote_timestamp, "strike": strike, "bid": bid,
        "ask": ask, "volume": volume, "open_interest": oi,
        "implied_volatility": iv, "delta": delta, "theta": theta,
        "lot_size": lot_size, "contract_value": contract_value,
        "bid_quantity": bid_quantity, "ask_quantity": ask_quantity,
        "vega": vega, "gamma": gamma, "iv_percentile": iv_percentile,
        "max_order_lots": max_order_lots, "tick_size": tick_size,
        "expected_exit_price": optional_number("expected_exit_price", minimum=0),
        "stop_option_price": optional_number("stop_option_price", minimum=0),
        "neutral_exit_price": optional_number("neutral_exit_price", minimum=0),
        "evaluated_ask": optional_number("evaluated_ask", minimum=0.00000001),
        "estimated_costs_per_lot": optional_number(
            "estimated_costs_per_lot", minimum=0
        ),
    }


def _validate_optional_market_data(market: Mapping[str, Any]) -> None:
    numeric_nonnegative = {
        "futures_open_interest", "implied_move", "realized_volatility",
        "implied_volatility", "iv_percentile",
    }
    numeric_finite = {
        "benchmark_return", "underlying_return", "sector_return",
        "futures_price_change_pct", "futures_oi_change_pct", "futures_basis_pct",
        "put_call_ratio", "put_oi_change_pct", "call_oi_change_pct",
        "call_put_skew", "opening_gap_pct", "expected_move_direction",
    }
    for key in numeric_nonnegative:
        if key in market and market[key] is not None:
            _finite(market[key], f"market.{key}", minimum=0)
    for key in numeric_finite:
        if key in market and market[key] is not None:
            _finite(market[key], f"market.{key}")
    for section_name in ("breadth", "derivatives", "volatility"):
        if section_name not in market or market[section_name] is None:
            continue
        section = _mapping(market[section_name], f"market.{section_name}")
        if "reliable" in section:
            _boolean(section["reliable"], f"market.{section_name}.reliable")
        for key, value in section.items():
            if key == "reliable" or value is None:
                continue
            if key in {"gap_retained"}:
                _boolean(value, f"market.{section_name}.{key}")
                continue
            _finite(value, f"market.{section_name}.{key}")


def _normalise_snapshot(snapshot: Any, config: Mapping[str, Any]) -> dict[str, Any]:
    root = _mapping(snapshot, "snapshot")
    now = _parse_time(root.get("timestamp"), "timestamp")
    raw_underlying = root.get("underlying")
    if isinstance(raw_underlying, Mapping):
        symbol = str(raw_underlying.get("symbol") or "").strip().upper()
    else:
        symbol = str(raw_underlying or "").strip().upper()
    configured_symbol = str(config.get("underlying") or "").strip().upper()
    if not symbol:
        symbol = configured_symbol
    if not symbol:
        raise TrendInputError("underlying is required")
    if configured_symbol and configured_symbol != symbol:
        raise TrendInputError("snapshot underlying differs from configuration")

    market = _mapping(root.get("market"), "market")
    market_ts = _parse_time(market.get("market_data_timestamp"),
                            "market.market_data_timestamp")
    spot_ts = _parse_time(market.get("spot_timestamp"), "market.spot_timestamp")
    futures_ts = _parse_time(market.get("futures_timestamp"),
                             "market.futures_timestamp")
    option_ts = _parse_time(market.get("option_chain_timestamp"),
                            "market.option_chain_timestamp")
    maximum_age = float(config["max_data_latency_seconds"])
    for name, value in (("market.market_data_timestamp", market_ts),
                        ("market.spot_timestamp", spot_ts),
                        ("market.futures_timestamp", futures_ts),
                        ("market.option_chain_timestamp", option_ts)):
        _validate_timestamp(value, now, name, maximum_age)
    aligned = [spot_ts, futures_ts, option_ts]
    if (max(aligned) - min(aligned)).total_seconds() > float(
            config["max_timestamp_alignment_seconds"]):
        raise TrendInputError("spot, futures and option timestamps are not aligned")
    spot = _finite(market.get("spot"), "market.spot", minimum=0.00000001)
    _validate_optional_market_data(market)

    raw_candles = _mapping(root.get("candles"), "candles")
    candles = {
        timeframe: _normalise_candles(raw_candles.get(timeframe), timeframe,
                                      now, config)
        for timeframe in TIMEFRAMES
    }

    raw_contracts = _list(root.get("option_contracts"), "option_contracts")
    contracts = [
        _normalise_contract(row, index, now, option_ts, config)
        for index, row in enumerate(raw_contracts)
    ]

    account = _mapping(root.get("account"), "account")
    equity = _finite(account.get("equity"), "account.equity", minimum=0.00000001)
    available_funds = _finite(account.get("available_funds"),
                              "account.available_funds", minimum=0)
    daily_pnl = _finite(account.get("daily_pnl"), "account.daily_pnl")
    consecutive_losses = _integer(account.get("consecutive_losses"),
                                  "account.consecutive_losses", minimum=0)
    exposure = _finite(account.get("current_exposure", 0),
                       "account.current_exposure", minimum=0)

    risk = _mapping(root.get("risk"), "risk")
    risk_normalised = {
        "kill_switch_active": _boolean(risk.get("kill_switch_active"),
                                        "risk.kill_switch_active"),
        "broker_connected": _boolean(risk.get("broker_connected"),
                                      "risk.broker_connected"),
        "exchange_operational": _boolean(risk.get("exchange_operational"),
                                          "risk.exchange_operational"),
        "position_state_consistent": _boolean(
            risk.get("position_state_consistent"),
            "risk.position_state_consistent"),
        "orders_state_known": _boolean(risk.get("orders_state_known"),
                                        "risk.orders_state_known"),
        "account_risk_state_known": _boolean(
            risk.get("account_risk_state_known"),
            "risk.account_risk_state_known",
        ),
        "estimated_costs_per_lot": _finite(
            risk.get("estimated_costs_per_lot"),
            "risk.estimated_costs_per_lot", minimum=0),
        "estimated_slippage_per_lot": _finite(
            risk.get("estimated_slippage_per_lot"),
            "risk.estimated_slippage_per_lot", minimum=0),
        "current_exposure": exposure,
    }
    if "abnormal_market" in risk:
        risk_normalised["abnormal_market"] = _boolean(
            risk["abnormal_market"], "risk.abnormal_market")
    else:
        risk_normalised["abnormal_market"] = False
    if "broker_error" in risk:
        risk_normalised["broker_error"] = _boolean(
            risk["broker_error"], "risk.broker_error")
    else:
        risk_normalised["broker_error"] = False

    positions = _list(root.get("positions"), "positions")
    pending_orders = _list(root.get("pending_orders"), "pending_orders")
    raw_events = root.get("events")
    event_available_flag = market.get("event_data_available")
    if event_available_flag is not None:
        event_data_available = _boolean(
            event_available_flag, "market.event_data_available")
    else:
        # A concrete array (including []) is an explicit calendar response.
        # None means the adapter could not establish whether the window is clear.
        event_data_available = isinstance(raw_events, list)
    if raw_events is None:
        events: list[Any] = []
        event_data_available = False
    else:
        events = _list(raw_events, "events")
    for index, item in enumerate(pending_orders):
        row = _mapping(item, f"pending_orders[{index}]")
        if not str(row.get("symbol") or "").strip():
            raise TrendInputError(f"pending_orders[{index}].symbol is required")
        if "state_known" in row and not _boolean(
                row["state_known"], f"pending_orders[{index}].state_known"):
            risk_normalised["orders_state_known"] = False
    event_rows: list[dict[str, Any]] = []
    for index, item in enumerate(events):
        row = _mapping(item, f"events[{index}]")
        event_time = _parse_time(row.get("timestamp", row.get("start")),
                                 f"events[{index}].timestamp")
        prohibited = _boolean(row.get("prohibited", True),
                              f"events[{index}].prohibited")
        event_rows.append({
            "timestamp": event_time,
            "prohibited": prohibited,
            "name": str(row.get("name") or "SCHEDULED_EVENT"),
        })

    forecast_raw = root.get("forecast", {})
    forecast = _mapping(forecast_raw, "forecast")
    forecast_normalised: dict[str, Any] = {}
    for key in ("target_underlying", "invalidation_level", "holding_days",
                "expected_iv_change", "probability_win",
                "cost_adjusted_required_move"):
        if key in forecast and forecast[key] is not None:
            lower = 0 if key in {"target_underlying", "invalidation_level",
                                 "holding_days", "cost_adjusted_required_move"} else None
            forecast_normalised[key] = _finite(
                forecast[key], f"forecast.{key}", minimum=lower)
    if "probability_win" in forecast_normalised and not 0 < forecast_normalised[
            "probability_win"] < 1:
        raise TrendInputError("forecast.probability_win must be between zero and one")
    forecast_normalised["probability_validated"] = _boolean(
        forecast.get("probability_validated", False),
        "forecast.probability_validated")
    if "time_exit" in forecast and forecast["time_exit"] is not None:
        forecast_normalised["time_exit"] = _parse_time(
            forecast["time_exit"], "forecast.time_exit")

    history_raw = root.get("forecast_history_5m")
    forecast_history: dict[str, Any] | None = None
    if history_raw is not None:
        history_block = _mapping(history_raw, "forecast_history_5m")
        source = _mapping(
            history_block.get("source"), "forecast_history_5m.source"
        )
        required_source = {
            "provider": "delta_exchange",
            "transport": "public_rest",
            "endpoint": "/v2/history/candles",
            "symbol": "BTCUSD",
            "resolution": "5m",
            "interval_seconds": 300,
            "completed_only": True,
        }
        for key, expected in required_source.items():
            if source.get(key) != expected:
                raise TrendInputError(
                    f"forecast_history_5m.source.{key} is not approved"
                )
        history_rows = _list(
            history_block.get("candles"), "forecast_history_5m.candles"
        )
        returned_count = _integer(
            history_block.get("returned_count"),
            "forecast_history_5m.returned_count",
        )
        if returned_count != len(history_rows):
            raise TrendInputError(
                "forecast_history_5m.returned_count does not match candles"
            )
        normalised_history_rows: list[dict[str, Any]] = []
        for index, item in enumerate(history_rows):
            row = _mapping(item, f"forecast_history_5m.candles[{index}]")
            if row.get("complete") is not True:
                raise TrendInputError(
                    f"forecast_history_5m.candles[{index}] is not complete"
                )
            normalised_history_rows.append(dict(row))
        if normalised_history_rows:
            if history_block.get("first_timestamp") != normalised_history_rows[0].get(
                    "timestamp"):
                raise TrendInputError(
                    "forecast_history_5m.first_timestamp does not match candles"
                )
            if history_block.get("last_timestamp") != normalised_history_rows[-1].get(
                    "timestamp"):
                raise TrendInputError(
                    "forecast_history_5m.last_timestamp does not match candles"
                )
        requested_limit = _integer(
            history_block.get("requested_limit"),
            "forecast_history_5m.requested_limit",
            minimum=1,
        )
        if requested_limit < returned_count:
            raise TrendInputError(
                "forecast_history_5m.requested_limit is below returned_count"
            )
        forecast_history = {
            "source": dict(source),
            "candles": normalised_history_rows,
            "returned_count": returned_count,
            "requested_limit": requested_limit,
        }

    return {
        "now": now, "symbol": symbol, "market": dict(market), "spot": spot,
        "market_timestamp": market_ts, "candles": candles,
        "contracts": contracts,
        "account": {
            "equity": equity, "available_funds": available_funds,
            "daily_pnl": daily_pnl, "consecutive_losses": consecutive_losses,
            "current_exposure": exposure,
        },
        "risk": risk_normalised, "positions": positions,
        "pending_orders": pending_orders, "events": event_rows,
        "event_data_available": event_data_available,
        "forecast": forecast_normalised,
        "forecast_history_5m": forecast_history,
    }


def _rolling_vwap(candles: Sequence[Mapping[str, Any]], end: int,
                  window: int = 20) -> float:
    subset = candles[max(0, end - window):end]
    total_volume = sum(row["volume"] for row in subset)
    if total_volume <= 0:
        raise TrendInputError("VWAP cannot be calculated from zero volume")
    return sum(((row["high"] + row["low"] + row["close"]) / 3.0)
               * row["volume"] for row in subset) / total_volume


def _price_action(candles: Sequence[Mapping[str, Any]], market: Mapping[str, Any]
                  ) -> tuple[float, dict[str, Any]]:
    atr = _atr(candles)
    last = candles[-1]
    recent = candles[-6:]
    highs = [row["high"] for row in recent]
    lows = [row["low"] for row in recent]
    score = 0.0
    evidence: list[str] = []
    bullish_structure = all(highs[index] > highs[index - 1] and
                            lows[index] > lows[index - 1]
                            for index in range(1, len(highs)))
    bearish_structure = all(highs[index] < highs[index - 1] and
                            lows[index] < lows[index - 1]
                            for index in range(1, len(highs)))
    if bullish_structure:
        score += 6
        evidence.append("higher_highs_higher_lows")
    elif bearish_structure:
        score -= 6
        evidence.append("lower_highs_lower_lows")

    prior = candles[-22:-2]
    resistance = max(row["high"] for row in prior)
    support = min(row["low"] for row in prior)
    material = 0.05 * atr
    bullish_break = last["close"] > resistance + material
    bearish_break = last["close"] < support - material
    if bullish_break:
        score += 6
        evidence.append("closed_breakout")
    elif bearish_break:
        score -= 6
        evidence.append("closed_breakdown")

    # A retest is detected only after the preceding *closed* candle broke the
    # level established before it; the latest candle must close back beyond it.
    retest_reference = candles[-23:-3]
    if retest_reference:
        old_resistance = max(row["high"] for row in retest_reference)
        old_support = min(row["low"] for row in retest_reference)
        previous = candles[-2]
        if (previous["close"] > old_resistance + material
                and last["low"] <= old_resistance + 0.25 * atr
                and last["close"] > old_resistance):
            score += 5
            evidence.append("bullish_retest_held")
        elif (previous["close"] < old_support - material
              and last["high"] >= old_support - 0.25 * atr
              and last["close"] < old_support):
            score -= 5
            evidence.append("bearish_retest_held")

    current_vwap = _rolling_vwap(candles, len(candles))
    prior_vwap = _rolling_vwap(candles, len(candles) - 3)
    if last["close"] > current_vwap and current_vwap > prior_vwap:
        score += 4
        evidence.append("above_rising_vwap")
    elif last["close"] < current_vwap and current_vwap < prior_vwap:
        score -= 4
        evidence.append("below_falling_vwap")

    range_low = min(row["low"] for row in candles[-20:])
    range_high = max(row["high"] for row in candles[-20:])
    location = ((last["close"] - range_low) / (range_high - range_low)
                if range_high > range_low else 0.5)
    if location >= 0.80:
        score += 2
        evidence.append("upper_range_close")
    elif location <= 0.20:
        score -= 2
        evidence.append("lower_range_close")
    elif 0.40 <= location <= 0.60 and not (bullish_break or bearish_break):
        score *= 0.6
        evidence.append("range_center_penalty")

    if market.get("underlying_return") is not None and market.get(
            "benchmark_return") is not None:
        relative = float(market["underlying_return"]) - float(
            market["benchmark_return"])
        if relative > 0:
            score += 2
            evidence.append("positive_relative_strength")
        elif relative < 0:
            score -= 2
            evidence.append("negative_relative_strength")
    return _clamp(score, -25, 25), {
        "atr": atr, "support": support, "resistance": resistance,
        "vwap": current_vwap, "range_location": location,
        "structure": ("BULLISH" if bullish_structure else
                      "BEARISH" if bearish_structure else "UNCLEAR"),
        "evidence": evidence,
        "breakout": bullish_break, "breakdown": bearish_break,
    }


def _candle_dimensions(row: Mapping[str, Any]) -> tuple[float, float, float, float]:
    span = row["high"] - row["low"]
    if span <= 0:
        # A zero-range candle is structurally valid but carries no pattern
        # information, so it must be ignored rather than labelled or rejected.
        return 0.0, 0.0, 0.0, 0.0
    body = abs(row["close"] - row["open"])
    upper = row["high"] - max(row["open"], row["close"])
    lower = min(row["open"], row["close"]) - row["low"]
    return span, body, upper, lower


def _candlestick(candles: Sequence[Mapping[str, Any]], context: Mapping[str, Any],
                 higher_trend: int = 0) -> tuple[float, str, bool]:
    a, b, c, d = candles[-4], candles[-3], candles[-2], candles[-1]
    candidates: list[tuple[float, str, bool, Mapping[str, Any]]] = []
    # Engulfing pattern on c, confirmed at d's actual traded/closing level.
    bull_engulf = (b["close"] < b["open"] and c["close"] > c["open"]
                   and c["open"] <= b["close"] and c["close"] >= b["open"])
    bear_engulf = (b["close"] > b["open"] and c["close"] < c["open"]
                   and c["open"] >= b["close"] and c["close"] <= b["open"])
    if bull_engulf:
        candidates.append((4, "BULLISH_ENGULFING", d["high"] > c["high"], c))
    if bear_engulf:
        candidates.append((-4, "BEARISH_ENGULFING", d["low"] < c["low"], c))
    span, body, upper, lower = _candle_dimensions(c)
    if span > 0 and lower >= 2 * max(body, span * 0.05) and upper <= span * 0.25 \
            and c["close"] >= c["low"] + 0.60 * span:
        candidates.append((3, "HAMMER", d["high"] > c["high"], c))
    if span > 0 and upper >= 2 * max(body, span * 0.05) and lower <= span * 0.25 \
            and c["close"] <= c["low"] + 0.40 * span:
        candidates.append((-3, "SHOOTING_STAR", d["low"] < c["low"], c))
    # Three-candle star ending at c; d provides confirmation.
    _, body_a, _, _ = _candle_dimensions(a)
    _, body_b, _, _ = _candle_dimensions(b)
    if (body_a > 0 and a["close"] < a["open"] and body_b <= body_a * 0.45
            and c["close"] > c["open"]
            and c["close"] >= (a["open"] + a["close"]) / 2):
        candidates.append((5, "MORNING_STAR", d["high"] > c["high"], c))
    if (body_a > 0 and a["close"] > a["open"] and body_b <= body_a * 0.45
            and c["close"] < c["open"]
            and c["close"] <= (a["open"] + a["close"]) / 2):
        candidates.append((-5, "EVENING_STAR", d["low"] < c["low"], c))
    # Inside bar b/c, confirmed by d.  These descriptions are alternatives,
    # never summed with the other label for the same candles.
    if c["high"] < b["high"] and c["low"] > b["low"]:
        if d["close"] > b["high"]:
            candidates.append((3, "BULLISH_INSIDE_BAR_BREAKOUT", True, c))
        elif d["close"] < b["low"]:
            candidates.append((-3, "BEARISH_INSIDE_BAR_BREAKDOWN", True, c))
    span_d, body_d, upper_d, lower_d = _candle_dimensions(d)
    body_ratio = body_d / span_d if span_d > 0 else 0.0
    if body_ratio >= 0.80 and upper_d <= span_d * 0.12 and d["close"] > d["open"] \
            and context["breakout"]:
        candidates.append((4, "BULLISH_MARUBOZU_BREAKOUT", True, d))
    if body_ratio >= 0.80 and lower_d <= span_d * 0.12 and d["close"] < d["open"] \
            and context["breakdown"]:
        candidates.append((-4, "BEARISH_MARUBOZU_BREAKDOWN", True, d))
    if not candidates:
        return 0.0, "NONE", False
    confirmed_candidates = [item for item in candidates if item[2]]
    chosen = max(confirmed_candidates or candidates,
                 key=lambda item: (abs(item[0]), item[1]))
    base, pattern, confirmed, pattern_candle = chosen
    if not confirmed:
        base = math.copysign(min(abs(base), 1), base)
    multiplier = 1.0
    atr = float(context["atr"])
    at_level = (abs(pattern_candle["low"] - context["support"]) <= 0.35 * atr
                if base > 0 else
                abs(pattern_candle["high"] - context["resistance"]) <= 0.35 * atr)
    if at_level:
        multiplier *= 1.5
    if higher_trend and math.copysign(1, base) == higher_trend:
        multiplier *= 1.25
    elif higher_trend:
        multiplier *= 0.5
    average_volume = sum(row["volume"] for row in candles[-21:-1]) / 20
    if average_volume > 0 and pattern_candle["volume"] >= 1.5 * average_volume:
        multiplier *= 1.2
    elif average_volume > 0 and pattern_candle["volume"] <= 0.5 * average_volume:
        multiplier *= 0.5
    if 0.40 <= context["range_location"] <= 0.60:
        multiplier *= 0.5
    return _clamp(base * multiplier, -10, 10), pattern, bool(confirmed)


def _trend(candles: Sequence[Mapping[str, Any]]) -> tuple[float, dict[str, float]]:
    closes = [row["close"] for row in candles]
    ema9 = _ema(closes, 9)
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    price = closes[-1]
    score = 0.0
    if price > ema9[-1] > ema20[-1] > ema50[-1]:
        score += 10
    elif price < ema9[-1] < ema20[-1] < ema50[-1]:
        score -= 10
    else:
        score += 3 if price > ema20[-1] > ema50[-1] else -3 if price < ema20[-1] < ema50[-1] else 0
    slopes = ((ema20[-1] - ema20[-6]), (ema50[-1] - ema50[-6]))
    if slopes[0] > 0 and slopes[1] > 0:
        score += 3
    elif slopes[0] < 0 and slopes[1] < 0:
        score -= 3
    persistence = sum(1 if close > ema20[index] else -1
                      for index, close in enumerate(closes[-10:],
                                                    start=len(closes) - 10))
    if persistence >= 8:
        score += 2
    elif persistence <= -8:
        score -= 2
    atr = _atr(candles)
    extension = abs(price - ema20[-1]) / atr
    if extension > 3:
        score *= max(0.5, 1.0 - 0.12 * (extension - 3))
    return _clamp(score, -15, 15), {
        "ema9": ema9[-1], "ema20": ema20[-1], "ema50": ema50[-1],
        "ema20_slope_5": slopes[0], "ema50_slope_5": slopes[1],
        "atr_extension": extension,
    }


def _momentum(candles: Sequence[Mapping[str, Any]], market: Mapping[str, Any]
              ) -> tuple[float, dict[str, float]]:
    closes = [row["close"] for row in candles]
    score = 0.0
    returns: dict[str, float] = {}
    for period, points in ((5, 1.5), (10, 1.5), (20, 2.0)):
        value = closes[-1] / closes[-period - 1] - 1.0
        returns[f"return_{period}"] = value
        score += points if value > 0 else -points if value < 0 else 0
    rsi_now = _rsi(closes)
    rsi_prior = _rsi(closes[:-3])
    if rsi_now > 55 and rsi_now >= rsi_prior:
        score += 2
    elif rsi_now < 45 and rsi_now <= rsi_prior:
        score -= 2
    fast = _ema(closes, 12)
    slow = _ema(closes, 26)
    macd = fast[-1] - slow[-1]
    macd_prior = fast[-4] - slow[-4]
    if macd > 0 and macd >= macd_prior:
        score += 2
    elif macd < 0 and macd <= macd_prior:
        score -= 2
    if market.get("underlying_return") is not None and market.get(
            "benchmark_return") is not None:
        relative = float(market["underlying_return"]) - float(
            market["benchmark_return"])
        score += 1 if relative > 0 else -1 if relative < 0 else 0
    returns.update({"rsi": rsi_now, "rsi_change": rsi_now - rsi_prior,
                    "macd": macd, "macd_change": macd - macd_prior})
    return _clamp(score, -10, 10), returns


def _volume_vwap(candles: Sequence[Mapping[str, Any]],
                 context: Mapping[str, Any]) -> tuple[float, dict[str, float]]:
    last = candles[-1]
    vwap = float(context["vwap"])
    prior_vwap = _rolling_vwap(candles, len(candles) - 3)
    baseline = sum(row["volume"] for row in candles[-21:-1]) / 20
    relative_volume = last["volume"] / baseline if baseline > 0 else 0
    score = 0.0
    if last["close"] > vwap and vwap > prior_vwap:
        score += 4
    elif last["close"] < vwap and vwap < prior_vwap:
        score -= 4
    if context["breakout"]:
        score += 3 if relative_volume >= 1.2 else -2
    elif context["breakdown"]:
        score += -3 if relative_volume >= 1.2 else 2
    if relative_volume >= 1.5:
        candle_direction = 1 if last["close"] > last["open"] else -1 if last["close"] < last["open"] else 0
        score += 2 * candle_direction
    return _clamp(score, -10, 10), {
        "vwap": vwap, "vwap_slope": vwap - prior_vwap,
        "relative_volume": relative_volume,
    }


def _score_timeframe(candles: Sequence[Mapping[str, Any]],
                     market: Mapping[str, Any], higher_trend: int = 0
                     ) -> dict[str, Any]:
    price_action, pa_features = _price_action(candles, market)
    trend, trend_features = _trend(candles)
    trend_sign = 1 if trend > 3 else -1 if trend < -3 else higher_trend
    candlestick, pattern, confirmed = _candlestick(
        candles, pa_features, higher_trend or trend_sign)
    momentum, momentum_features = _momentum(candles, market)
    volume, volume_features = _volume_vwap(candles, pa_features)
    components = {
        "price_action": price_action, "candlestick": candlestick,
        "trend": trend, "momentum": momentum, "volume_vwap": volume,
    }
    raw = sum(components.values())
    score = _clamp(raw / 70.0 * 100.0, -100, 100)
    return {
        "score": score, "components": components, "pattern": pattern,
        "pattern_confirmed": confirmed, "price_action_features": pa_features,
        "trend_features": trend_features,
        "momentum_features": momentum_features,
        "volume_features": volume_features,
    }


def _reliable_section(market: Mapping[str, Any], name: str) -> Mapping[str, Any] | None:
    value = market.get(name)
    if not isinstance(value, Mapping) or value.get("reliable") is not True:
        return None
    return value


def _breadth_score(market: Mapping[str, Any]) -> tuple[float, dict[str, Any]]:
    data = _reliable_section(market, "breadth")
    if data is None:
        return 0.0, {"available": False}
    score = 0.0
    ratio = data.get("advance_decline_ratio")
    if ratio is not None:
        ratio = float(ratio)
        score += 3 if ratio >= 1.5 else -3 if ratio <= 0.67 else 0
    above = data.get("pct_above_20ema")
    if above is not None:
        above = float(above)
        score += 3 if above >= 60 else -3 if above <= 40 else 0
    highs = data.get("new_highs")
    lows = data.get("new_lows")
    if highs is not None and lows is not None:
        score += 2 if float(highs) > float(lows) else -2 if float(lows) > float(highs) else 0
    equal_weight = data.get("equal_weight_return")
    cap_weight = data.get("cap_weight_return")
    if equal_weight is not None and cap_weight is not None:
        score += 1 if float(equal_weight) >= float(cap_weight) else -1
    sector = data.get("sector_return")
    if sector is not None:
        score += 1 if float(sector) > 0 else -1 if float(sector) < 0 else 0
    return _clamp(score, -10, 10), {"available": True, **dict(data)}


def _derivatives_score(market: Mapping[str, Any], underlying_sign: int
                       ) -> tuple[float, dict[str, Any]]:
    data = _reliable_section(market, "derivatives")
    if data is None or underlying_sign == 0:
        return 0.0, {"available": False}
    score = 0.0
    price_change = data.get("futures_price_change_pct")
    oi_change = data.get("futures_oi_change_pct")
    if price_change is not None and oi_change is not None:
        price_change, oi_change = float(price_change), float(oi_change)
        # Price/OI combinations are confirmation, not stand-alone truth.
        if price_change > 0 and oi_change > 0:
            score += 4
        elif price_change < 0 and oi_change > 0:
            score -= 4
        elif price_change > 0 and oi_change < 0:
            score += 2
        elif price_change < 0 and oi_change < 0:
            score -= 2
    basis = data.get("futures_basis_pct")
    if basis is not None:
        score += 1 if float(basis) > 0 else -1 if float(basis) < 0 else 0
    pcr = data.get("put_call_ratio")
    if pcr is not None:
        score += 1 if float(pcr) >= 1.05 else -1 if float(pcr) <= 0.85 else 0
    put_change = data.get("put_oi_change_pct")
    call_change = data.get("call_oi_change_pct")
    if put_change is not None and call_change is not None:
        score += 3 if float(put_change) > float(call_change) else -3 if float(call_change) > float(put_change) else 0
    skew = data.get("call_put_skew")
    if skew is not None:
        score += 1 if float(skew) > 0 else -1 if float(skew) < 0 else 0
    score = _clamp(score, -10, 10)
    # Option/futures positioning may confirm or reduce an underlying view, but
    # it cannot create the view when underlying evidence is neutral.
    return score, {"available": True, **dict(data)}


def _volatility_score(market: Mapping[str, Any], underlying_sign: int
                      ) -> tuple[float, dict[str, Any]]:
    data = _reliable_section(market, "volatility")
    if data is None or underlying_sign == 0:
        return 0.0, {"available": False}
    score = 0.0
    opening_gap = data.get("opening_gap_pct")
    gap_retained = data.get("gap_retained")
    if opening_gap is not None and gap_retained is not None:
        retained = bool(gap_retained)
        gap_sign = 1 if float(opening_gap) > 0 else -1 if float(opening_gap) < 0 else 0
        score += 3 * gap_sign if retained else -2 * gap_sign
    implied_move = data.get("implied_move")
    forecast_move = data.get("forecast_move")
    if implied_move is not None and forecast_move is not None:
        if abs(float(forecast_move)) > float(implied_move):
            score += 3 * underlying_sign
        else:
            score -= 3 * underlying_sign
    iv_percentile = data.get("iv_percentile")
    if iv_percentile is not None and float(iv_percentile) >= 80:
        score -= 2 * underlying_sign
    realised = data.get("realized_volatility")
    implied = data.get("implied_volatility")
    if realised is not None and implied is not None:
        if float(implied) <= float(realised) * 1.15:
            score += 2 * underlying_sign
        elif float(implied) > float(realised) * 1.5:
            score -= 2 * underlying_sign
    return _clamp(score, -10, 10), {"available": True, **dict(data)}


def _known_event_blackout(context: Mapping[str, Any],
                          config: Mapping[str, Any]) -> bool:
    now = context["now"]
    blackout = timedelta(minutes=float(config["event_blackout_minutes"]))
    return any(event["prohibited"] and
               abs(event["timestamp"] - now) <= blackout
               for event in context["events"])


def _event_pass(context: Mapping[str, Any], config: Mapping[str, Any]) -> bool:
    if not context["event_data_available"]:
        return bool(config["allow_unknown_event_risk"])
    if config["allow_event_trading"]:
        return True
    return not _known_event_blackout(context, config)


def _risk_reasons(context: Mapping[str, Any], config: Mapping[str, Any]
                  ) -> list[str]:
    risk = context["risk"]
    account = context["account"]
    reasons: list[str] = []
    if risk["kill_switch_active"]:
        reasons.append("KILL_SWITCH_ACTIVE")
    if account["daily_pnl"] <= -(account["equity"] *
                                 float(config["max_daily_loss_pct"]) / 100.0):
        reasons.append("DAILY_LOSS_LIMIT")
    if account["consecutive_losses"] >= int(config["max_consecutive_losses"]):
        reasons.append("CONSECUTIVE_LOSS_LIMIT")
    if not risk["broker_connected"]:
        reasons.append("BROKER_CONNECTION_UNRELIABLE")
    if not risk["exchange_operational"] or risk["broker_error"]:
        reasons.append("BROKER_OR_EXCHANGE_ERROR")
    if not risk["position_state_consistent"]:
        reasons.append("POSITION_STATE_MISMATCH")
    if not risk["orders_state_known"]:
        reasons.append("ORDER_STATE_UNKNOWN")
    if not risk["account_risk_state_known"]:
        reasons.append("ACCOUNT_RISK_STATE_UNKNOWN")
    if risk["abnormal_market"]:
        reasons.append("ABNORMAL_SPREAD_OR_VOLATILITY")
    max_exposure = account["equity"] * float(config["max_exposure_pct"]) / 100.0
    if account["current_exposure"] > max_exposure:
        reasons.append("EXPOSURE_LIMIT")
    return reasons


def _direction(context: Mapping[str, Any], config: Mapping[str, Any]
               ) -> dict[str, Any]:
    market = context["market"]
    tf_results: dict[str, Any] = {}
    higher_sign = 0
    for timeframe in TIMEFRAMES:
        result = _score_timeframe(context["candles"][timeframe], market,
                                  higher_trend=higher_sign)
        tf_results[timeframe] = result
        if timeframe == "60m":
            higher_sign = 1 if result["score"] >= 20 else -1 if result["score"] <= -20 else 0
    signs = {
        name: 1 if item["score"] >= 20 else -1 if item["score"] <= -20 else 0
        for name, item in tf_results.items()
    }
    opposing_pairs = sum(
        1 for first, second in (("60m", "15m"), ("60m", "5m"), ("15m", "5m"))
        if signs[first] and signs[second] and signs[first] != signs[second]
    )
    material_conflict = bool(signs["60m"] and signs["15m"] and
                             signs["60m"] != signs["15m"])
    all_conflicted = opposing_pairs >= 2
    conflict_factor = 0.70 if material_conflict else 1.0

    local_components = {
        key: sum(TIMEFRAME_WEIGHTS[timeframe] *
                 tf_results[timeframe]["components"][key]
                 for timeframe in TIMEFRAMES) * conflict_factor
        for key in ("price_action", "candlestick", "trend", "momentum",
                    "volume_vwap")
    }
    underlying_core = sum(local_components.values())
    underlying_sign = (1 if underlying_core >= float(config["min_underlying_core_score"])
                       else -1 if underlying_core <= -float(config["min_underlying_core_score"])
                       else 0)
    breadth, breadth_features = _breadth_score(market)
    derivatives, derivatives_features = _derivatives_score(market, underlying_sign)
    volatility, volatility_features = _volatility_score(market, underlying_sign)
    components = {
        **local_components, "breadth_sector": breadth,
        "derivatives_positioning": derivatives,
        "volatility_catalyst": volatility,
    }
    score = _clamp(sum(components.values()), -100, 100)
    # The entry trigger may not overturn a strongly opposed 60m regime without
    # an explicit, closed-candle reversal confirmation and a higher threshold.
    reversal_required = bool(signs["60m"] and score * signs["60m"] < 0)
    reversal_confirmed = market.get("reversal_confirmation") is True
    reversal_pass = (not reversal_required or
                     (reversal_confirmed and abs(score) >=
                      float(config["min_direction_score"]) +
                      float(config["reversal_score_premium"])))

    abs_score = abs(score)
    confidence = "HIGH" if abs_score >= 70 else "MEDIUM" if abs_score >= 55 else "LOW"
    regime_trend = tf_results["60m"]["components"]["trend"]
    if abs(regime_trend) >= 9:
        regime = "TRENDING_UP" if regime_trend > 0 else "TRENDING_DOWN"
    elif context["market"].get("high_volatility") is True:
        regime = "HIGH_VOLATILITY"
    elif abs(tf_results["60m"]["score"]) < 20:
        regime = "RANGE"
    else:
        regime = "UNCLEAR"
    setup_tf = tf_results["15m"]
    setup_pa = setup_tf["price_action_features"]
    pattern_candidates = [tf_results[name] for name in ("5m", "15m", "60m")
                          if tf_results[name]["pattern"] != "NONE"]
    pattern_result = max(pattern_candidates,
                         key=lambda item: abs(item["components"]["candlestick"])) \
        if pattern_candidates else None
    return {
        "score": score, "components": components,
        "timeframes": {name: tf_results[name]["score"] for name in TIMEFRAMES},
        "details": tf_results, "confidence": confidence, "regime": regime,
        "underlying_core": underlying_core, "underlying_sign": underlying_sign,
        "material_conflict": material_conflict, "all_conflicted": all_conflicted,
        "reversal_pass": reversal_pass,
        "setup": {
            "market_structure": setup_pa["structure"],
            "candlestick_pattern": pattern_result["pattern"] if pattern_result else "NONE",
            "pattern_confirmed": pattern_result["pattern_confirmed"] if pattern_result else False,
            "support": setup_pa["support"], "resistance": setup_pa["resistance"],
        },
        "features": {
            "timeframes": tf_results, "breadth": breadth_features,
            "derivatives": derivatives_features, "volatility": volatility_features,
        },
    }


def _contract_hard_filter(contract: Mapping[str, Any], option_type: str,
                          now: datetime, config: Mapping[str, Any]
                          ) -> tuple[bool, list[str], dict[str, float]]:
    reasons: list[str] = []
    mid = (contract["bid"] + contract["ask"]) / 2.0
    spread_pct = ((contract["ask"] - contract["bid"]) / mid * 100.0
                  if mid > 0 else math.inf)
    seconds_to_expiry = (contract["expiry"] - now).total_seconds()
    dte = seconds_to_expiry / 86400.0
    time_to_expiry_hours = seconds_to_expiry / 3600.0
    if contract["option_type"] != option_type:
        return False, ["WRONG_OPTION_TYPE"], {"mid": mid,
                                              "spread_pct": spread_pct,
                                              "days_to_expiry": dte,
                                              "time_to_expiry_hours":
                                                  time_to_expiry_hours}
    if contract["bid"] <= 0 or contract["ask"] <= 0 or mid <= 0:
        reasons.append("INSUFFICIENT_LIQUIDITY")
    if spread_pct > float(config["max_bid_ask_spread_pct"]):
        reasons.append("SPREAD_TOO_WIDE")
    if contract["volume"] < float(config["min_option_volume"]) or \
            contract["open_interest"] < float(config["min_option_open_interest"]) or \
            contract["bid_quantity"] <= 0 or contract["ask_quantity"] <= 0:
        reasons.append("INSUFFICIENT_LIQUIDITY")
    if time_to_expiry_hours < float(config["min_time_to_expiry_hours"]):
        reasons.append("EXPIRY_RESTRICTION")
    evaluated_ask = contract.get("evaluated_ask")
    if evaluated_ask is not None:
        evaluated = _finite(evaluated_ask, "contract.evaluated_ask",
                            minimum=0.00000001)
        deviation = (contract["ask"] - evaluated) / evaluated * 100.0
        if deviation > float(config["max_entry_slippage_pct"]):
            reasons.append("ENTRY_PRICE_DEVIATION")
    return not reasons, list(dict.fromkeys(reasons)), {
        "mid": mid, "spread_pct": spread_pct, "days_to_expiry": dte,
        "time_to_expiry_hours": time_to_expiry_hours,
    }


def _contract_components(contract: Mapping[str, Any], metrics: Mapping[str, float],
                         universe: Sequence[Mapping[str, Any]],
                         forecast: Mapping[str, Any], spot: float,
                         direction_sign: int, config: Mapping[str, Any]
                         ) -> dict[str, float]:
    max_volume = max(item["volume"] for item in universe) or 1.0
    max_oi = max(item["open_interest"] for item in universe) or 1.0
    liquidity = 10 * contract["volume"] / max_volume + 10 * contract[
        "open_interest"] / max_oi
    spread = 15 * max(0.0, 1.0 - metrics["spread_pct"] /
                      max(float(config["max_bid_ask_spread_pct"]), 1e-12))
    delta = abs(contract["delta"])
    low = float(config["preferred_abs_delta_min"])
    high = float(config["preferred_abs_delta_max"])
    if low <= delta <= high:
        delta_score = 15.0
    elif delta < low:
        delta_score = 15 * max(0.0, (delta - 0.10) / max(low - 0.10, 1e-12))
    else:
        delta_score = 15 * max(0.0, (0.95 - delta) / max(0.95 - high, 1e-12))
    # Once the precise TTE gate passes, expiry contributes equally.  Daily BTC
    # contracts must not be quietly penalised by the removed 14-DTE preference.
    expiry_score = 15.0
    if contract["iv_percentile"] is not None:
        iv_score = 15.0 * (1.0 - 0.65 * contract["iv_percentile"] / 100.0)
    else:
        ivs = [item["implied_volatility"] for item in universe]
        low_iv, high_iv = min(ivs), max(ivs)
        if high_iv > low_iv:
            iv_score = 5.0 + 10.0 * (high_iv - contract["implied_volatility"]) / (high_iv - low_iv)
        else:
            iv_score = 10.0
    entry = contract["ask"]
    expiry_breakeven = (contract["strike"] + entry if direction_sign > 0
                       else contract["strike"] - entry)
    target = forecast.get("target_underlying")
    if target is None:
        breakeven = 0.0
    else:
        target_progress = direction_sign * (float(target) - spot)
        required = direction_sign * (expiry_breakeven - spot)
        if target_progress <= 0:
            breakeven = 0.0
        elif required <= 0:
            breakeven = 10.0
        else:
            breakeven = 10.0 * min(target_progress / required, 1.0)
    theta_burden = abs(contract["theta"]) / metrics["mid"] if metrics["mid"] > 0 else math.inf
    theta_efficiency = 10.0 * max(0.0, 1.0 - theta_burden / 0.05)
    return {
        "liquidity": _clamp(liquidity, 0, 20),
        "spread": _clamp(spread, 0, 15), "delta": _clamp(delta_score, 0, 15),
        "expiry": _clamp(expiry_score, 0, 15), "iv_value": _clamp(iv_score, 0, 15),
        "breakeven": _clamp(breakeven, 0, 10),
        "theta_efficiency": _clamp(theta_efficiency, 0, 10),
    }


def _scenario_and_order(contract: Mapping[str, Any], context: Mapping[str, Any],
                        direction_sign: int, setup: Mapping[str, Any],
                        config: Mapping[str, Any]) -> dict[str, Any]:
    forecast = context["forecast"]
    spot = context["spot"]
    requested_holding_days = float(forecast.get(
        "holding_days", config["forecast_holding_days"]
    ))
    if requested_holding_days <= 0 or requested_holding_days > float(
            config["max_holding_days"]):
        return {"valid": False, "reason": "RISK_CALCULATION_FAILED"}
    settlement_deadline = contract["expiry"] - timedelta(
        minutes=float(config["settlement_exit_buffer_minutes"])
    )
    signal_anchor = (
        context["candles"]["5m"][-1]["timestamp"]
        + timedelta(minutes=TIMEFRAME_MINUTES["5m"])
    )
    requested_time_exit = forecast.get("time_exit")
    if requested_time_exit is not None:
        if requested_time_exit <= context["now"]:
            return {"valid": False, "reason": "RISK_CALCULATION_FAILED"}
        time_exit = min(requested_time_exit, settlement_deadline)
    else:
        time_exit = min(
            signal_anchor + timedelta(days=requested_holding_days),
            settlement_deadline,
        )
    if time_exit <= context["now"]:
        return {"valid": False, "reason": "EXPIRY_RESTRICTION"}
    if time_exit > context["now"] + timedelta(
            days=float(config["max_holding_days"])):
        return {"valid": False, "reason": "RISK_CALCULATION_FAILED"}
    holding_days = (time_exit - context["now"]).total_seconds() / 86400.0
    target = forecast.get("target_underlying")
    if target is None:
        # Target is a transparent ATR projection, not fabricated market input.
        atr60 = _atr(context["candles"]["60m"])
        target = spot + direction_sign * float(config["target_atr_multiple"]) * atr60
    invalidation = forecast.get("invalidation_level")
    if invalidation is None:
        invalidation = setup["support"] if direction_sign > 0 else setup["resistance"]
    if invalidation is None or direction_sign * (spot - invalidation) <= 0:
        return {"valid": False, "reason": "RISK_CALCULATION_FAILED"}
    if direction_sign * (target - spot) <= 0:
        return {"valid": False, "reason": "BREAKEVEN_UNREALISTIC"}
    entry = contract["ask"]
    tick = contract["tick_size"]
    costs_per_lot_raw = contract.get("estimated_costs_per_lot")
    if costs_per_lot_raw is None:
        costs_per_lot = (context["risk"]["estimated_costs_per_lot"] +
                         context["risk"]["estimated_slippage_per_lot"])
    else:
        costs_per_lot = _finite(costs_per_lot_raw,
                                "contract.estimated_costs_per_lot", minimum=0)
    multiplier = contract["contract_value"]
    expected_exit_raw = contract.get("expected_exit_price")
    stop_raw = contract.get("stop_option_price")
    neutral_raw = contract.get("neutral_exit_price")
    replay_edge: dict[str, Any] | None = None
    scenario_evidence: dict[str, Any] | None = None
    history = context.get("forecast_history_5m")
    validated_probability_available = bool(
        forecast.get("probability_validated") is True
        and forecast.get("probability_win") is not None
    )
    use_historical_replay = bool(
        isinstance(history, Mapping) and not validated_probability_available
    )
    if (not use_historical_replay and expected_exit_raw is not None
            and stop_raw is not None):
        expected_exit = _finite(expected_exit_raw, "contract.expected_exit_price",
                                minimum=0)
        stop_option = _finite(stop_raw, "contract.stop_option_price", minimum=0)
        neutral_exit = (_finite(neutral_raw, "contract.neutral_exit_price", minimum=0)
                        if neutral_raw is not None else None)
        risk_expected_exit = expected_exit
        risk_stop_option = stop_option
        pricing_method = "provided_scenarios"
    elif not use_historical_replay and "expected_iv_change" in forecast:
        iv_change = float(forecast["expected_iv_change"])
        if iv_change and contract["vega"] is None:
            return {"valid": False, "reason": "EXPECTED_VALUE_UNAVAILABLE"}
        iv_effect = (contract["vega"] or 0.0) * iv_change
        decay = contract["theta"] * holding_days
        expected_exit = max(tick, (contract["bid"] + contract["ask"]) / 2.0
                            + contract["delta"] * (target - spot)
                            + decay + iv_effect)
        stop_option = max(tick, (contract["bid"] + contract["ask"]) / 2.0
                          + contract["delta"] * (invalidation - spot)
                          + decay + iv_effect)
        neutral_exit = max(tick, (contract["bid"] + contract["ask"]) / 2.0
                           + decay + iv_effect)
        risk_expected_exit = expected_exit
        risk_stop_option = stop_option
        pricing_method = "delta_theta_vega_scenario"
    else:
        if not isinstance(history, Mapping):
            return {"valid": False, "reason": "EXPECTED_VALUE_UNAVAILABLE"}
        replay_edge = conservative_intrinsic_scenario(
            history.get("candles", []),
            spot=spot,
            target_price=float(target),
            invalidation_price=float(invalidation),
            option_type=contract["option_type"],
            strike=contract["strike"],
            entry_price=entry,
            contract_value=multiplier,
            costs_per_lot=costs_per_lot,
            time_exit=time_exit,
            now=context["now"],
            min_complete_days=int(config["scenario_min_complete_days"]),
            lower_quantile=float(config["scenario_lower_quantile"]),
            prepared_history=context.get("prepared_scenario_history"),
        )
        scenario_evidence = dict(replay_edge.get("audit") or {})
        scenario_evidence["source"] = dict(history.get("source") or {})
        if replay_edge.get("valid") is not True:
            return {
                "valid": False,
                "reason": "EXPECTED_VALUE_UNAVAILABLE",
                "pricing_method": "historical_replay_intrinsic_floor",
                "expected_value_method": scenario_evidence.get("method"),
                "scenario_validation_reason": replay_edge.get("reason"),
                "probability_win": None,
                "probability_validated": False,
                "scenario_evidence": scenario_evidence,
            }
        raw_target_option = float(replay_edge["target_option_price"])
        raw_stop_option = float(replay_edge["stop_option_price"])
        raw_neutral_option = float(replay_edge["neutral_option_price"])
        # Tick-sized plan prices remain valid exchange values.  Risk and EV
        # continue to use the lower, uncapped intrinsic values.
        expected_exit = max(tick, raw_target_option)
        stop_option = max(tick, raw_stop_option)
        neutral_exit = max(tick, raw_neutral_option)
        risk_expected_exit = raw_target_option
        risk_stop_option = raw_stop_option
        pricing_method = "historical_replay_intrinsic_floor"
    net_win = (risk_expected_exit - entry) * multiplier - costs_per_lot
    net_loss = (entry - risk_stop_option) * multiplier + costs_per_lot
    loss_basis = "invalidation_intrinsic_and_costs"
    if scenario_evidence is not None:
        worst_replay = scenario_evidence.get("worst_net_pnl_per_lot")
        if worst_replay is not None:
            replay_loss = max(0.0, -float(worst_replay))
            if replay_loss > net_loss:
                net_loss = replay_loss
                loss_basis = "worst_historical_replay_path"
    if net_loss <= 0:
        return {"valid": False, "reason": "RISK_CALCULATION_FAILED"}
    reward_risk = net_win / net_loss
    probability = forecast.get("probability_win")
    calibrated = forecast.get("probability_validated") is True
    if replay_edge is not None:
        net_ev = float(replay_edge["net_expected_value_per_lot"])
        expected_value_pass = replay_edge["expected_value_pass"] is True
        probability = None
        calibrated = False
    elif probability is None or not calibrated:
        net_ev = None
        expected_value_pass = False
    else:
        net_ev = float(probability) * net_win - (1.0 - float(probability)) * net_loss
        expected_value_pass = net_ev > 0
    move = abs(float(target) - spot)
    required_move = forecast.get("cost_adjusted_required_move")
    if required_move is None:
        required_move = max(0.0, (entry - (contract["bid"] + contract["ask"]) / 2.0
                                  + costs_per_lot / multiplier) /
                            max(abs(contract["delta"]), 1e-12))
    forecast_move_pass = move > float(required_move)
    reward_risk_pass = reward_risk >= float(config["min_reward_risk"])
    risk_budget = context["account"]["equity"] * float(
        config["max_risk_per_trade_pct"]) / 100.0
    estimated_loss_per_lot = net_loss
    lots_by_risk = math.floor(risk_budget / estimated_loss_per_lot)
    capital_per_lot = entry * multiplier + costs_per_lot
    lots_by_funds = (math.floor(context["account"]["available_funds"] /
                                capital_per_lot) if capital_per_lot > 0 else 0)
    remaining_exposure = max(
        context["account"]["equity"] * float(config["max_exposure_pct"]) / 100.0
        - context["account"]["current_exposure"], 0.0)
    lots_by_exposure = (math.floor(remaining_exposure / capital_per_lot)
                        if capital_per_lot > 0 else 0)
    lots_by_depth = math.floor(contract["ask_quantity"])
    lots = min(lots_by_risk, lots_by_funds, lots_by_exposure,
               lots_by_depth,
               int(config["max_order_lots"]),
               int(contract["max_order_lots"]))
    increment = contract["lot_size"]
    lots = (lots // increment) * increment
    max_entry = entry * (1.0 + float(config["max_entry_slippage_pct"]) / 100.0)
    replay_quantiles = (
        scenario_evidence.get("net_ev_quantiles_per_lot") or {}
        if scenario_evidence else {}
    )
    lower_percentile = int(round(float(config["scenario_lower_quantile"]) * 100))
    upper_percentile = 100 - lower_percentile
    return {
        "valid": True, "target": float(target), "invalidation": float(invalidation),
        "entry": entry, "maximum_entry": max_entry,
        "expected_exit": expected_exit, "stop_option": stop_option,
        "neutral_exit": neutral_exit, "net_win_per_lot": net_win,
        "net_loss_per_lot": net_loss, "reward_risk": reward_risk,
        "maximum_loss_basis": loss_basis,
        "probability_win": probability if calibrated else None,
        "probability_validated": calibrated,
        "net_expected_value_per_lot": net_ev,
        "expected_value_pass": expected_value_pass,
        "forecast_move_pass": forecast_move_pass,
        "reward_risk_pass": reward_risk_pass, "risk_budget": risk_budget,
        "lots": lots, "lots_by_risk": lots_by_risk,
        "lots_by_funds": lots_by_funds,
        "lots_by_exposure": lots_by_exposure,
        "lots_by_depth": lots_by_depth, "time_exit": time_exit,
        "requested_holding_days": requested_holding_days,
        "signal_anchor": signal_anchor,
        "effective_holding_hours": holding_days * 24.0,
        "settlement_exit_buffer_minutes": float(
            config["settlement_exit_buffer_minutes"]
        ),
        "costs_per_lot": costs_per_lot,
        "total_costs": costs_per_lot * lots,
        "maximum_loss": estimated_loss_per_lot * lots,
        "pricing_method": pricing_method,
        "expected_value_method": (
            scenario_evidence.get("method") if scenario_evidence else
            "calibrated_probability" if calibrated else None
        ),
        "history_hash": (
            scenario_evidence.get("history_sha256") if scenario_evidence else None
        ),
        "history_start": (
            scenario_evidence.get("history_window_start")
            if scenario_evidence else None
        ),
        "history_end": (
            scenario_evidence.get("history_window_end")
            if scenario_evidence else None
        ),
        "complete_day_count": (
            scenario_evidence.get("complete_day_count")
            if scenario_evidence else None
        ),
        "path_count": (
            scenario_evidence.get("total_path_count")
            if scenario_evidence else None
        ),
        "scenario_lower_quantile": (
            float(config["scenario_lower_quantile"])
            if scenario_evidence else None
        ),
        "net_edge_lower_quantile": replay_quantiles.get(
            f"p{lower_percentile:02d}"
        ),
        "net_edge_median": replay_quantiles.get("p50"),
        "net_edge_upper_quantile": replay_quantiles.get(
            f"p{upper_percentile:02d}"
        ),
        "scenario_evidence": scenario_evidence,
    }


def _populate_contract(contract: Mapping[str, Any], metrics: Mapping[str, float],
                       components: Mapping[str, float], score: float) -> dict[str, Any]:
    return {
        "symbol": contract["symbol"], "option_type": contract["option_type"],
        "strike": contract["strike"], "expiry": _iso(contract["expiry"]),
        "days_to_expiry": round(metrics["days_to_expiry"], 4),
        "time_to_expiry_hours": round(metrics["time_to_expiry_hours"], 4),
        "bid": contract["bid"], "ask": contract["ask"], "mid": metrics["mid"],
        "spread_pct": round(metrics["spread_pct"], 4),
        "volume": contract["volume"], "open_interest": contract["open_interest"],
        "implied_volatility": contract["implied_volatility"],
        "delta": contract["delta"], "theta": contract["theta"],
        "contract_score": _round_score(score),
        "contract_components": {key: _round_score(value)
                                for key, value in components.items()},
    }


def _remaining_position_scenario(
    position: Mapping[str, Any],
    context: Mapping[str, Any],
    *,
    option_type: str,
    current_price: float,
    invalidation: float,
    time_exit: datetime,
    config: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Recalculate Phase-1 remaining edge from current executable value."""

    if str(position.get("entry_trigger") or "") != (
            "trend_engine_phase1_confirmed"):
        return None
    history = context.get("forecast_history_5m")
    if not isinstance(history, Mapping):
        return {
            "valid": False,
            "reason": "EXPECTED_VALUE_UNAVAILABLE",
            "scenario_validation_reason": "HISTORY_UNAVAILABLE",
        }
    symbol = str(position.get("symbol") or "")
    matches = [
        contract for contract in context["contracts"]
        if contract["symbol"] == symbol
    ]
    if len(matches) != 1:
        return {
            "valid": False,
            "reason": "EXPECTED_VALUE_UNAVAILABLE",
            "scenario_validation_reason": "CURRENT_CONTRACT_UNAVAILABLE",
        }
    try:
        underlying_target = _finite(
            position.get("underlying_target"),
            "positions[0].underlying_target",
            minimum=0.00000001,
        )
    except TrendInputError as exc:
        return {
            "valid": False,
            "reason": "EXPECTED_VALUE_UNAVAILABLE",
            "scenario_validation_reason": "TARGET_UNAVAILABLE",
            "validation_error": str(exc),
        }
    contract = matches[0]
    costs_per_lot = contract.get("estimated_costs_per_lot")
    if costs_per_lot is None:
        costs_per_lot = (
            context["risk"]["estimated_costs_per_lot"]
            + context["risk"]["estimated_slippage_per_lot"]
        )
    replay = conservative_intrinsic_scenario(
        history.get("candles", []),
        spot=context["spot"],
        target_price=underlying_target,
        invalidation_price=invalidation,
        option_type=option_type,
        strike=contract["strike"],
        entry_price=current_price,
        contract_value=contract["contract_value"],
        # The normalized contract estimate is round-trip, so using it for the
        # remaining exit is deliberately conservative.
        costs_per_lot=costs_per_lot,
        time_exit=time_exit,
        now=context["now"],
        min_complete_days=int(config["scenario_min_complete_days"]),
        lower_quantile=float(config["scenario_lower_quantile"]),
        prepared_history=context.get("prepared_scenario_history"),
    )
    evidence = dict(replay.get("audit") or {})
    evidence["source"] = dict(history.get("source") or {})
    return {
        **replay,
        "reason": (
            replay.get("reason") if replay.get("valid") is True
            else "EXPECTED_VALUE_UNAVAILABLE"
        ),
        "scenario_validation_reason": (
            None if replay.get("valid") is True else replay.get("reason")
        ),
        "scenario_evidence": evidence,
        "edge_semantics": (
            "equal_weighted_historical_scenario_lower_quantile_from_current_bid"
        ),
    }


def _manage_existing(result: dict[str, Any], context: Mapping[str, Any],
                     direction: Mapping[str, Any], risk_reasons: Sequence[str],
                     event_pass: bool, config: Mapping[str, Any]) -> dict[str, Any]:
    positions = context["positions"]
    if len(positions) != 1:
        result["decision"] = "EXIT"
        result["reason_codes"] = ["POSITION_STATE_MISMATCH"]
        result["decision_summary"] = (
            f"Manual review is required: the Trend Engine found {len(positions)} "
            "open positions, but it can safely manage only one position at a "
            "time. No order was submitted."
        )
        result["audit"] = {
            "position_count": len(positions),
            "position_contexts": [{
                "symbol": str(item.get("symbol") or "").strip() or None,
                "slot": str(item.get("slot") or "").strip() or None,
                "source": str(item.get("source") or "").strip() or None,
            } for item in positions if isinstance(item, Mapping)],
        }
        return result
    raw = _mapping(positions[0], "positions[0]")
    position_context = {
        "symbol": str(raw.get("symbol") or "").strip() or None,
        "slot": str(raw.get("slot") or "").strip() or None,
        "source": str(raw.get("source") or "").strip() or None,
    }
    result["audit"] = {"position_context": position_context}
    required = ("symbol", "option_type", "quantity_lots", "entry_price",
                "current_price", "underlying_invalidation", "stop_option_price",
                "target_option_price", "time_exit", "remaining_expected_value")
    try:
        missing_fields = [key for key in required if raw.get(key) is None]
        if missing_fields:
            raise TrendInputError(
                "missing existing-position fields: " + ", ".join(missing_fields)
            )
        symbol = str(raw["symbol"]).strip()
        if not symbol:
            raise TrendInputError("positions[0].symbol is required")
        option_type = str(raw["option_type"]).upper()
        if option_type not in {"CE", "PE"}:
            raise TrendInputError("positions[0].option_type must be CE or PE")
        side = str(raw.get("side") or "long").strip().lower()
        if side not in {"long", "buy", "short", "sell"}:
            raise TrendInputError("positions[0].side is invalid")
        _integer(raw["quantity_lots"], "positions[0].quantity_lots", minimum=1)
        _finite(raw["entry_price"], "positions[0].entry_price",
                minimum=0.00000001)
        current = _finite(raw["current_price"], "positions[0].current_price", minimum=0)
        stop = _finite(raw["stop_option_price"], "positions[0].stop_option_price", minimum=0)
        target = _finite(raw["target_option_price"], "positions[0].target_option_price", minimum=0)
        invalidation = _finite(raw["underlying_invalidation"],
                               "positions[0].underlying_invalidation", minimum=0)
        remaining_ev = _finite(raw["remaining_expected_value"],
                               "positions[0].remaining_expected_value")
        time_exit = _parse_time(raw["time_exit"], "positions[0].time_exit")
    except TrendInputError as exc:
        result["decision"] = "EXIT"
        result["reason_codes"] = ["INVALID_OR_STALE_DATA"]
        result["decision_summary"] = (
            "Manual review is required: the existing position does not contain "
            "enough verified trade-plan data for the engine to justify HOLD. "
            "This is an advisory EXIT decision; no order was submitted."
        )
        result["audit"] = {
            "position_context": position_context,
            "position_state_issue": "INCOMPLETE_OR_INVALID_TRADE_PLAN",
            "position_validation_error": str(exc),
            "missing_position_fields": [
                key for key in required if raw.get(key) is None
            ],
        }
        return result
    if side in {"short", "sell"}:
        result["decision"] = "EXIT"
        result["reason_codes"] = ["NAKED_OPTION_SELLING_PROHIBITED"]
        result["decision_summary"] = (
            f"Exit {raw['symbol']}: short option positions are outside this model."
        )
        return result
    reasons = list(risk_reasons)
    remaining_scenario = _remaining_position_scenario(
        raw,
        context,
        option_type=option_type,
        current_price=current,
        invalidation=invalidation,
        time_exit=time_exit,
        config=config,
    )
    if remaining_scenario is not None:
        result["audit"]["remaining_edge_recalculation"] = remaining_scenario
        if remaining_scenario.get("valid") is True:
            remaining_ev = float(
                remaining_scenario["net_expected_value_per_lot"]
            )
        else:
            reasons.append("EXPECTED_VALUE_UNAVAILABLE")
    spot = context["spot"]
    if (option_type == "CE" and spot <= invalidation) or \
            (option_type == "PE" and spot >= invalidation):
        reasons.append("UNDERLYING_INVALIDATION_REACHED")
    if current <= stop:
        reasons.append("EMERGENCY_OPTION_STOP_REACHED")
    if current >= target:
        reasons.append("TARGET_REACHED")
    if context["now"] >= time_exit:
        reasons.append("TIME_STOP_REACHED")
    if not event_pass:
        reasons.append("EVENT_BLACKOUT" if context["event_data_available"]
                       else "EVENT_DATA_UNAVAILABLE")
    exit_threshold = float(config["exit_direction_score"])
    if (option_type == "CE" and direction["score"] <= -exit_threshold) or \
            (option_type == "PE" and direction["score"] >= exit_threshold):
        reasons.append("DIRECTION_REVERSAL")
    if (remaining_scenario is None or remaining_scenario.get("valid") is True) \
            and remaining_ev <= 0:
        reasons.append("NEGATIVE_EXPECTED_VALUE")
    result["detected_setup"]["invalidation_level"] = invalidation
    if reasons:
        result["decision"] = "EXIT"
        result["reason_codes"] = list(dict.fromkeys(reasons))
        result["decision_summary"] = (
            f"Exit {raw['symbol']}: " + ", ".join(result["reason_codes"][:3]) + ".")
    else:
        result["decision"] = "HOLD"
        result["reason_codes"] = ["EXISTING_POSITION_THESIS_VALID"]
        result["decision_summary"] = f"Hold {raw['symbol']}; thesis and remaining edge remain valid."
    return result


def _invalid_result(snapshot: Any, config: Mapping[str, Any], detail: str) -> dict[str, Any]:
    result = _base_result(snapshot, config)
    result["reason_codes"] = ["INVALID_OR_STALE_DATA"]
    result["decision_summary"] = "Required market, broker, account, or position data is invalid or stale."
    result["audit"] = {"validation_error": detail}
    return result


def evaluate_trend(snapshot: Mapping[str, Any],
                   config_overrides: Mapping[str, Any] | None = None
                   ) -> dict[str, Any]:
    """Evaluate one immutable snapshot and return the required decision schema.

    The same JSON-compatible snapshot and configuration always produce the
    same decision and ``decision_id``.  No exception caused by ordinary bad
    market input escapes this boundary; such data fails closed as NO_TRADE.
    """

    try:
        config = _load_config(config_overrides)
    except TrendInputError as exc:
        return _invalid_result(snapshot, DEFAULT_CONFIG, str(exc))
    result = _base_result(snapshot, config)
    try:
        context = _normalise_snapshot(snapshot, config)
        direction = _direction(context, config)
    except (TrendInputError, TypeError, KeyError, IndexError, ZeroDivisionError) as exc:
        return _invalid_result(snapshot, config, str(exc))

    result["timestamp"] = _iso(context["now"])
    result["market_data_timestamp"] = _iso(context["market_timestamp"])
    result["underlying"] = context["symbol"]
    result["hard_gates"]["data_valid"] = True
    result["direction_score"] = _round_score(direction["score"])
    result["direction_components"] = {
        key: _round_score(value) for key, value in direction["components"].items()
    }
    result["timeframe_scores"] = {
        key: _round_score(value) for key, value in direction["timeframes"].items()
    }
    result["confidence"] = direction["confidence"]
    result["market_regime"] = direction["regime"]
    result["detected_setup"].update(direction["setup"])
    result["risk_state"] = {
        "account_equity": context["account"]["equity"],
        "risk_budget": context["account"]["equity"] * float(
            config["max_risk_per_trade_pct"]) / 100.0,
        "daily_pnl": context["account"]["daily_pnl"],
        "consecutive_losses": context["account"]["consecutive_losses"],
        "kill_switch_active": context["risk"]["kill_switch_active"],
    }
    event_pass = _event_pass(context, config)
    result["hard_gates"]["event_pass"] = event_pass
    known_blackout = bool(
        context["event_data_available"]
        and _known_event_blackout(context, config)
    )
    event_policy = {
        "event_data_available": bool(context["event_data_available"]),
        "unknown_risk_override_applied": bool(
            not context["event_data_available"]
            and config["allow_unknown_event_risk"]
        ),
        "known_blackout_detected": known_blackout,
        "known_blackout_override_applied": bool(
            known_blackout and event_pass and config["allow_event_trading"]
        ),
    }
    risk_reasons = _risk_reasons(context, config)
    portfolio_pass = not risk_reasons and len(context["positions"]) <= int(
        config["max_portfolio_positions"])
    result["hard_gates"]["portfolio_risk_pass"] = portfolio_pass

    history = context.get("forecast_history_5m")
    if isinstance(history, Mapping):
        context["prepared_scenario_history"] = prepare_scenario_history(
            history.get("candles", []),
            now=context["now"],
            min_complete_days=int(config["scenario_min_complete_days"]),
        )

    score = direction["score"]
    threshold = float(config["min_direction_score"])
    if score >= threshold:
        bias, option_type, candidate_decision = "BULLISH", "CE", "BUY_CE"
    elif score <= -threshold:
        bias, option_type, candidate_decision = "BEARISH", "PE", "BUY_PE"
    else:
        bias, option_type, candidate_decision = "NEUTRAL", None, None
    result["directional_bias"] = bias

    if context["positions"]:
        managed = _manage_existing(result, context, direction, risk_reasons,
                                   event_pass, config)
        management_audit = (
            managed.get("audit") if isinstance(managed.get("audit"), dict) else {}
        )
        managed["audit"] = {
            **management_audit,
            "underlying_core_score": _round_score(direction["underlying_core"]),
            "features": direction["features"], "config": dict(config),
            "event_policy": event_policy,
        }
        return managed

    direction_pass = (option_type is not None and
                      abs(direction["underlying_core"]) >= float(
                          config["min_underlying_core_score"]) and
                      not direction["all_conflicted"] and
                      direction["reversal_pass"])
    result["hard_gates"]["direction_pass"] = direction_pass
    price_action = direction["components"]["price_action"]
    price_action_pass = ((option_type == "CE" and price_action >= float(
        config["min_price_action_score"])) or
        (option_type == "PE" and price_action <= -float(
            config["min_price_action_score"])))
    result["hard_gates"]["price_action_pass"] = price_action_pass

    reasons: list[str] = []
    if not event_pass:
        reasons.append("EVENT_BLACKOUT" if context["event_data_available"]
                       else "EVENT_DATA_UNAVAILABLE")
    reasons.extend(risk_reasons)
    if context["pending_orders"]:
        reasons.append("PENDING_ORDER_EXISTS")
        portfolio_pass = False
        result["hard_gates"]["portfolio_risk_pass"] = False
    if direction["all_conflicted"] or not direction["reversal_pass"]:
        reasons.append("TIMEFRAME_CONFLICT")
    if option_type is None or not direction_pass:
        reasons.append("DIRECTION_SCORE_TOO_LOW" if not direction[
            "all_conflicted"] else "TIMEFRAME_CONFLICT")
    if option_type is not None and not price_action_pass:
        reasons.append("PRICE_ACTION_NOT_CONFIRMED")
    if reasons or not direction_pass or not price_action_pass:
        result["reason_codes"] = list(dict.fromkeys(reasons)) or [
            "DIRECTION_SCORE_TOO_LOW"]
        result["decision_summary"] = (
            f"No trade: direction score {result['direction_score']} did not pass all entry gates.")
        result["audit"] = {
            "underlying_core_score": _round_score(direction["underlying_core"]),
            "features": direction["features"], "config": dict(config),
            "event_policy": event_policy,
            "contract_rankings": [],
        }
        return result

    filter_results: list[dict[str, Any]] = []
    eligible: list[tuple[dict[str, Any], dict[str, float]]] = []
    for contract in context["contracts"]:
        passed, contract_reasons, metrics = _contract_hard_filter(
            contract, option_type, context["now"], config)
        filter_results.append({
            "symbol": contract["symbol"], "passed": passed,
            "reasons": contract_reasons, "metrics": metrics,
        })
        if passed:
            eligible.append((contract, metrics))
    if not eligible:
        matching_failures = [reason for row in filter_results
                             if "WRONG_OPTION_TYPE" not in row["reasons"]
                             for reason in row["reasons"]]
        result["reason_codes"] = list(dict.fromkeys(
            matching_failures + ["NO_ELIGIBLE_CONTRACT"]))
        result["decision_summary"] = f"No eligible {option_type} contract passed hard filters."
        result["audit"] = {
            "underlying_core_score": _round_score(direction["underlying_core"]),
            "features": direction["features"], "config": dict(config),
            "event_policy": event_policy,
            "contract_rankings": filter_results,
        }
        return result

    universe = [item[0] for item in eligible]
    ranked: list[dict[str, Any]] = []
    direction_sign = 1 if option_type == "CE" else -1
    resolved_forecast = dict(context["forecast"])
    if resolved_forecast.get("target_underlying") is None:
        resolved_forecast["target_underlying"] = (
            context["spot"]
            + direction_sign * float(config["target_atr_multiple"])
            * _atr(context["candles"]["60m"])
        )
    selection_context = {**context, "forecast": resolved_forecast}
    for contract, metrics in eligible:
        components = _contract_components(
            contract, metrics, universe, resolved_forecast, context["spot"],
            direction_sign, config)
        contract_score = sum(components.values())
        scenario = _scenario_and_order(
            contract, selection_context, direction_sign, direction["setup"], config)
        ranked.append({
            "contract": contract, "metrics": metrics,
            "components": components, "score": contract_score,
            "scenario": scenario,
        })
    ranked.sort(key=lambda item: (-item["score"], item["metrics"]["spread_pct"],
                                  -abs(item["contract"]["delta"]),
                                  item["contract"]["symbol"]))
    for item in ranked:
        candidate_reasons: list[str] = []
        scenario = item["scenario"]
        contract_pass = item["score"] >= float(config["min_contract_score"])
        trade_score = 0.65 * abs(score) + 0.35 * item["score"]
        if not scenario.get("valid"):
            candidate_reasons.append(str(
                scenario.get("reason") or "RISK_CALCULATION_FAILED"
            ))
        else:
            if scenario["net_expected_value_per_lot"] is None:
                candidate_reasons.append("EXPECTED_VALUE_UNAVAILABLE")
            elif not scenario["expected_value_pass"]:
                candidate_reasons.append("NEGATIVE_EXPECTED_VALUE")
            if not scenario["forecast_move_pass"]:
                candidate_reasons.append("BREAKEVEN_UNREALISTIC")
            if not scenario["reward_risk_pass"]:
                candidate_reasons.append("REWARD_RISK_TOO_LOW")
            if scenario["lots"] < 1:
                candidate_reasons.append("MINIMUM_LOT_EXCEEDS_RISK_LIMIT")
        if not contract_pass:
            candidate_reasons.append("CONTRACT_SCORE_TOO_LOW")
        if trade_score < float(config["min_trade_score"]):
            candidate_reasons.append("TRADE_SCORE_TOO_LOW")
        item["trade_score"] = trade_score
        item["entry_reasons"] = list(dict.fromkeys(candidate_reasons))

    # Select the highest contract-quality candidate that passes every gate.
    # If none pass, retain the top candidate only as an auditable evaluation;
    # it remains a NO_TRADE and no order plan is populated.
    selected = next(
        (item for item in ranked if not item["entry_reasons"]), ranked[0]
    )
    contract_pass = selected["score"] >= float(config["min_contract_score"])
    result["hard_gates"]["contract_pass"] = contract_pass
    result["hard_gates"]["spread_pass"] = True
    scenario = selected["scenario"]
    result["hard_gates"]["expiry_pass"] = not (
        scenario.get("valid") is False
        and scenario.get("reason") == "EXPIRY_RESTRICTION"
    )
    result["selected_contract"] = _populate_contract(
        selected["contract"], selected["metrics"], selected["components"],
        selected["score"])
    trade_score = selected["trade_score"]
    result["trade_score"] = _round_score(trade_score)
    reasons = list(selected["entry_reasons"])
    if scenario.get("valid"):
        result["hard_gates"]["expected_value_pass"] = bool(
            scenario["expected_value_pass"])
        result["hard_gates"]["reward_risk_pass"] = bool(
            scenario["reward_risk_pass"])
        result["detected_setup"]["invalidation_level"] = scenario["invalidation"]

    if not reasons and scenario.get("valid"):
        result["decision"] = candidate_decision
        result["reason_codes"] = ["ALL_ENTRY_GATES_PASSED"]
        result["order_plan"] = {
            "order_type": "LIMIT", "entry_price": scenario["entry"],
            "maximum_entry_price": round(scenario["maximum_entry"], 8),
            "quantity_lots": scenario["lots"],
            "lot_size": selected["contract"]["lot_size"],
            "stop_option_price": round(scenario["stop_option"], 8),
            "underlying_invalidation": scenario["invalidation"],
            "target_option_price": round(scenario["expected_exit"], 8),
            "underlying_target": scenario["target"],
            "time_exit": _iso(scenario["time_exit"]),
            "estimated_total_costs": round(scenario["total_costs"], 8),
            "maximum_estimated_loss": round(scenario["maximum_loss"], 8),
            "reward_risk": round(scenario["reward_risk"], 4),
        }
        result["decision_summary"] = (
            f"{candidate_decision} {selected['contract']['symbol']} after all direction, "
            "contract, edge and portfolio gates passed.")
    else:
        result["reason_codes"] = list(dict.fromkeys(reasons)) or ["NO_ELIGIBLE_CONTRACT"]
        result["decision_summary"] = (
            f"No trade: {selected['contract']['symbol']} failed "
            + ", ".join(result["reason_codes"][:3]) + ".")
    result["audit"] = {
        "underlying_core_score": _round_score(direction["underlying_core"]),
        "features": direction["features"], "config": dict(config),
        "event_policy": event_policy,
        "selected_contract_value": selected["contract"]["contract_value"],
        "scenario": scenario,
        "contract_rankings": [{
            "symbol": item["contract"]["symbol"],
            "contract_score": _round_score(item["score"]),
            "trade_score": _round_score(item["trade_score"]),
            "entry_reasons": list(item["entry_reasons"]),
            "components": {key: _round_score(value)
                           for key, value in item["components"].items()},
            "hard_filter": next(row for row in filter_results
                                if row["symbol"] == item["contract"]["symbol"]),
        } for item in ranked],
    }
    # Convert datetimes stored in the audit trail into JSON-compatible text.
    result["audit"] = json.loads(_canonical(result["audit"]))
    if result["decision"] not in DECISIONS:  # defensive invariant
        raise AssertionError("invalid trend-engine decision")
    return result


def evaluate_trend_json(snapshot: Mapping[str, Any],
                        config_overrides: Mapping[str, Any] | None = None) -> str:
    """Return :func:`evaluate_trend` as stable, compact JSON."""

    return _canonical(evaluate_trend(snapshot, config_overrides))


# Descriptive alias for integrations that prefer an explicit noun.
evaluate_trend_decision = evaluate_trend


__all__ = [
    "DEFAULT_CONFIG", "TrendInputError", "evaluate_trend",
    "evaluate_trend_decision", "evaluate_trend_json",
]
