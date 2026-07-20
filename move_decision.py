"""Pure MOVE payoff forecasting and automatic LONG/SHORT decision logic.

The module deliberately has no exchange credentials, filesystem access, or
order-placement capability.  Production, dashboard, and backtest callers feed
it one normalized snapshot per decision cycle and receive an auditable result.

All MOVE prices and forecast values are USD quote units per 1 BTC.  Dollar
exposure per exchange contract is calculated only after applying
``contract.contract_multiplier`` (currently 0.001 for BTC MOVE on Delta).
"""

from __future__ import annotations

import hashlib
import math
from datetime import datetime, timezone
from typing import Any, Iterable

import numpy as np


NO_TRADE = "NO_TRADE"
MANAGE_EXISTING_POSITION = "MANAGE_EXISTING_POSITION"
LONG_MOVE = "LONG_MOVE"
SHORT_MOVE = "SHORT_MOVE"


class MoveInputError(ValueError):
    """Raised when a forecast or decision snapshot is unsafe to evaluate."""


def _finite(value: Any, name: str, *, minimum: float | None = None) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MoveInputError(f"{name} must be numeric") from exc
    if not math.isfinite(number):
        raise MoveInputError(f"{name} must be finite")
    if minimum is not None and number < minimum:
        raise MoveInputError(f"{name} must be at least {minimum}")
    return number


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    number = _finite(value, name)
    if not number.is_integer():
        raise MoveInputError(f"{name} must be an integer")
    result = int(number)
    if result < minimum:
        raise MoveInputError(f"{name} must be at least {minimum}")
    return result


def _boolean(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    raw = str(value if value is not None else "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise MoveInputError(f"{name} must be boolean")


def _timestamp_ms(value: Any, name: str) -> int:
    raw = _finite(value, name, minimum=1)
    # Delta has emitted seconds, milliseconds, microseconds and nanoseconds.
    while raw < 100_000_000_000:
        raw *= 1000
    while raw > 100_000_000_000_000:
        raw /= 1000
    return int(raw)


def _seed_from(parts: Iterable[Any]) -> int:
    material = "|".join(str(part) for part in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big")


def _normalise_candles(
    candles: Iterable[dict[str, Any]],
    *,
    now_ms: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, int]:
    rows: dict[int, tuple[float, float, float]] = {}
    for row in candles or ():
        if not isinstance(row, dict):
            continue
        try:
            timestamp = _timestamp_ms(row.get("time"), "candle.time")
            close = _finite(row.get("close"), "candle.close", minimum=0.00000001)
            high = _finite(row.get("high", close), "candle.high", minimum=close)
            low = _finite(row.get("low", close), "candle.low", minimum=0.00000001)
        except MoveInputError:
            continue
        if low > close or high < close:
            continue
        rows[timestamp] = (close, high, low)
    if len(rows) < 3:
        raise MoveInputError("not enough valid index candles")
    ordered = sorted(rows.items())
    times = np.asarray([item[0] for item in ordered], dtype=np.int64)
    intervals = np.diff(times)
    positive = intervals[intervals > 0]
    if positive.size == 0:
        raise MoveInputError("index candle interval is unavailable")
    interval_ms = int(np.median(positive))
    # This model is deliberately limited to the 1m/5m source data specified
    # for the strategy.  Larger bars would understate settlement-window risk.
    if interval_ms < 45_000 or interval_ms > 330_000:
        raise MoveInputError(
            f"forecast requires completed 1m/5m candles, got {interval_ms}ms")
    completed = times + interval_ms <= now_ms
    times = times[completed]
    ordered_values = [value for (_, value), keep in zip(ordered, completed) if keep]
    if len(ordered_values) < 3:
        raise MoveInputError("not enough completed index candles")
    closes = np.asarray([value[0] for value in ordered_values], dtype=float)
    highs = np.asarray([value[1] for value in ordered_values], dtype=float)
    lows = np.asarray([value[2] for value in ordered_values], dtype=float)
    return times, closes, highs, lows, interval_ms


def _annualized_volatility(returns: np.ndarray, bars_per_day: int) -> float:
    if returns.size < 20:
        raise MoveInputError("not enough returns for realized volatility")
    return float(np.std(returns, ddof=1) * math.sqrt(bars_per_day * 365))


def _daily_sigmas(returns: np.ndarray, bars_per_day: int) -> np.ndarray:
    days = returns.size // bars_per_day
    if days < 7:
        raise MoveInputError("at least seven complete volatility days are required")
    trimmed = returns[-days * bars_per_day:]
    blocks = trimmed.reshape(days, bars_per_day)
    values = np.std(blocks, axis=1, ddof=1)
    return values[np.isfinite(values) & (values > 0)]


def forecast_move_distribution(
    candles: Iterable[dict[str, Any]],
    *,
    current_index_price: float,
    strike: float,
    now_ms: int,
    settlement_end_ts_ms: int,
    current_30m_twap: float = 0.0,
    fraction_of_final_twap_fixed: float = 0.0,
    scheduled_event_score: float | None = None,
    outer_scenarios: int = 32,
    paths_per_scenario: int = 128,
    minimum_history_days: int = 7,
    seed: int | None = None,
) -> dict[str, Any]:
    """Forecast the final ``abs(30m TWAP - strike)`` payoff distribution.

    Low/mid/high are uncertainty bounds around *fair value*: each outer
    volatility scenario produces an expected payoff from its inner paths, and
    the 20th/50th/80th percentiles of those expected values form the band.
    ``payoff_p99`` instead comes from the pooled individual payoff paths.
    """

    now_ms = _timestamp_ms(now_ms, "now_ms")
    settlement_end_ts_ms = _timestamp_ms(
        settlement_end_ts_ms, "settlement_end_ts_ms")
    if settlement_end_ts_ms <= now_ms:
        raise MoveInputError("MOVE settlement has already passed")
    current_index_price = _finite(
        current_index_price, "current_index_price", minimum=0.00000001)
    strike = _finite(strike, "strike", minimum=0.00000001)
    fixed_fraction = _finite(
        fraction_of_final_twap_fixed,
        "fraction_of_final_twap_fixed",
        minimum=0,
    )
    if fixed_fraction > 1:
        raise MoveInputError("fraction_of_final_twap_fixed cannot exceed 1")
    fixed_twap = _finite(current_30m_twap, "current_30m_twap", minimum=0)
    if fixed_fraction > 0 and fixed_twap <= 0:
        raise MoveInputError("a positive fixed TWAP requires current_30m_twap")

    times, closes, highs, lows, interval_ms = _normalise_candles(
        candles, now_ms=now_ms)
    bars_per_day = max(int(round(86_400_000 / interval_ms)), 1)
    required_bars = max(int(minimum_history_days), 7) * bars_per_day
    if closes.size < required_bars:
        raise MoveInputError(
            f"forecast requires {required_bars} completed bars; got {closes.size}")
    closes = closes[-max(required_bars, 30 * bars_per_day):]
    highs = highs[-closes.size:]
    lows = lows[-closes.size:]
    times = times[-closes.size:]
    returns = np.diff(np.log(closes))
    returns = returns[np.isfinite(returns)]
    if returns.size < required_bars - 2:
        raise MoveInputError("not enough valid completed index returns")

    one_day = returns[-min(returns.size, bars_per_day):]
    seven_days = returns[-min(returns.size, 7 * bars_per_day):]
    thirty_days = returns[-min(returns.size, 30 * bars_per_day):]
    vol_1d = _annualized_volatility(one_day, bars_per_day)
    vol_7d = _annualized_volatility(seven_days, bars_per_day)
    vol_30d = _annualized_volatility(thirty_days, bars_per_day)
    daily_sigmas = _daily_sigmas(thirty_days, bars_per_day)
    if daily_sigmas.size < 7:
        raise MoveInputError("not enough complete volatility regimes")

    recent_closes = closes[-min(closes.size, bars_per_day):]
    recent_highs = highs[-recent_closes.size:]
    recent_lows = lows[-recent_closes.size:]
    intraday_range = float(
        (np.max(recent_highs) - np.min(recent_lows)) / current_index_price)
    recent_jump_measure = float(np.max(np.abs(one_day)))
    daily_mean = float(np.mean(daily_sigmas))
    vol_of_vol = (
        float(np.std(daily_sigmas, ddof=1) / daily_mean)
        if daily_mean > 0 and daily_sigmas.size > 1 else 0.0
    )
    robust_sigma = max(
        float(np.median(np.abs(thirty_days - np.median(thirty_days))) * 1.4826),
        1e-9,
    )
    jump_ratio = recent_jump_measure / robust_sigma
    market_jump_score = min(
        1.0,
        max(
            0.0,
            0.55 * min(max((jump_ratio - 3.0) / 7.0, 0.0), 1.0)
            + 0.25 * min(vol_of_vol / 0.75, 1.0)
            + 0.20 * min(intraday_range / 0.08, 1.0),
        ),
    )
    event_available = scheduled_event_score is not None
    if event_available:
        event_score = _finite(
            scheduled_event_score, "scheduled_event_score", minimum=0)
        if event_score > 1:
            raise MoveInputError("scheduled_event_score cannot exceed 1")
    else:
        # Explicitly fail short-volatility decisions closed when no trusted
        # economic-event feed is configured.
        event_score = 1.0
    jump_event_score = max(market_jump_score, event_score)

    outer_scenarios = _integer(
        outer_scenarios, "outer_scenarios", minimum=8)
    paths_per_scenario = _integer(
        paths_per_scenario, "paths_per_scenario", minimum=32)
    if outer_scenarios > 128 or paths_per_scenario > 1024:
        raise MoveInputError("forecast simulation size exceeds the safety limit")

    horizon_steps = max(
        int(math.ceil((settlement_end_ts_ms - now_ms) / interval_ms)), 1)
    twap_steps = max(int(round(1_800_000 / interval_ms)), 1)
    model_timestamp_ms = int(times[-1] + interval_ms)
    model_clock = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
    rng = np.random.default_rng(
        seed if seed is not None else _seed_from((
            int(times[-1]), round(strike, 8), settlement_end_ts_ms,
            outer_scenarios, paths_per_scenario,
        ))
    )

    centred = thirty_days - float(np.median(thirty_days))
    pool_sigma = float(np.std(centred, ddof=1))
    if not math.isfinite(pool_sigma) or pool_sigma <= 0:
        raise MoveInputError("return distribution has zero variance")
    standardized = np.clip(centred / pool_sigma, -12.0, 12.0)
    # Blend the three horizons but retain an outer distribution of recent
    # daily regimes so the fair-value band reflects parameter uncertainty.
    blended_step_sigma = (
        0.50 * vol_1d + 0.30 * vol_7d + 0.20 * vol_30d
    ) / math.sqrt(bars_per_day * 365)
    scenario_sigmas = rng.choice(
        daily_sigmas, size=outer_scenarios, replace=True)
    scenario_sigmas = np.sqrt(
        0.65 * np.square(scenario_sigmas)
        + 0.35 * blended_step_sigma * blended_step_sigma)
    drift = float(np.clip(np.median(thirty_days), -0.25 * blended_step_sigma,
                          0.25 * blended_step_sigma))

    fair_values: list[float] = []
    payoff_batches: list[np.ndarray] = []
    for scenario_sigma in scenario_sigmas:
        sampled = rng.choice(
            standardized,
            size=(paths_per_scenario, horizon_steps),
            replace=True,
        )
        path_returns = drift + sampled * float(scenario_sigma)
        path_prices = current_index_price * np.exp(
            np.cumsum(path_returns, axis=1))
        if fixed_fraction > 0:
            remaining_twap = np.mean(
                path_prices[:, -min(twap_steps, horizon_steps):], axis=1)
            settlement_twap = (
                fixed_fraction * fixed_twap
                + (1.0 - fixed_fraction) * remaining_twap
            )
        else:
            settlement_twap = np.mean(
                path_prices[:, -min(twap_steps, horizon_steps):], axis=1)
        payoffs = np.abs(settlement_twap - strike)
        fair_values.append(float(np.mean(payoffs)))
        payoff_batches.append(payoffs)

    fair = np.asarray(fair_values, dtype=float)
    pooled = np.concatenate(payoff_batches)
    low, mid, high = np.percentile(fair, [20, 50, 80])
    payoff_p99 = float(np.percentile(pooled, 99))
    if not (0 <= low <= mid <= high <= payoff_p99 + 1e-9):
        raise MoveInputError("forecast distribution is internally inconsistent")

    return {
        "expected_payoff_low": round(float(low), 8),
        "expected_payoff_mid": round(float(mid), 8),
        "expected_payoff_high": round(float(high), 8),
        "payoff_p99": round(payoff_p99, 8),
        "jump_event_score": round(float(jump_event_score), 8),
        "market_jump_score": round(float(market_jump_score), 8),
        "scheduled_event_score": (
            round(float(event_score), 8) if event_available else None),
        "event_score_available": event_available,
        "event_risk_source": (
            "provided" if event_available else "unknown_high_risk"),
        "model_timestamp_ms": model_timestamp_ms,
        "model_features": {
            "realized_volatility_1d": round(vol_1d, 8),
            "realized_volatility_7d": round(vol_7d, 8),
            "realized_volatility_30d": round(vol_30d, 8),
            "intraday_high_low_range": round(intraday_range, 8),
            "recent_jump_measure": round(recent_jump_measure, 8),
            "volatility_of_volatility": round(vol_of_vol, 8),
            "current_btc_index_price": round(current_index_price, 8),
            "current_30m_twap": round(fixed_twap, 8),
            "move_strike": round(strike, 8),
            "seconds_until_settlement_window": max(
                int((settlement_end_ts_ms - 1_800_000 - now_ms) / 1000), 0),
            "seconds_until_final_settlement": max(
                int((settlement_end_ts_ms - now_ms) / 1000), 0),
            "fraction_of_final_twap_fixed": round(fixed_fraction, 8),
            "hour_of_day_utc": model_clock.hour,
            "day_of_week_utc": model_clock.weekday(),
            "source_interval_seconds": int(interval_ms / 1000),
            "completed_bars": int(closes.size),
        },
        "simulation": {
            "outer_scenarios": outer_scenarios,
            "paths_per_scenario": paths_per_scenario,
            "total_paths": outer_scenarios * paths_per_scenario,
            "horizon_steps": horizon_steps,
            "twap_steps": twap_steps,
            "deterministic": True,
        },
    }


def _section(snapshot: dict[str, Any], name: str) -> dict[str, Any]:
    value = snapshot.get(name)
    if not isinstance(value, dict):
        raise MoveInputError(f"{name} must be an object")
    return value


def evaluate_move_decision(
    snapshot: dict[str, Any],
    strategy_config: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate the attached LONG/SHORT specification with hard gates."""

    timestamp = _section(snapshot, "timestamp")
    contract = _section(snapshot, "contract")
    market = _section(snapshot, "market")
    underlying = _section(snapshot, "underlying")
    forecast = _section(snapshot, "forecast")
    costs = _section(snapshot, "costs")
    account = _section(snapshot, "account")
    exchange = _section(snapshot, "exchange")

    now_ms = _timestamp_ms(timestamp.get("now_ms"), "timestamp.now_ms")
    expiry_ms = _timestamp_ms(
        contract.get("expiry_ts_ms"), "contract.expiry_ts_ms")
    settlement_start_ms = _timestamp_ms(
        contract.get("settlement_window_start_ts_ms"),
        "contract.settlement_window_start_ts_ms",
    )
    settlement_end_ms = _timestamp_ms(
        contract.get("settlement_window_end_ts_ms"),
        "contract.settlement_window_end_ts_ms",
    )
    if not expiry_ms == settlement_end_ms or settlement_start_ms >= settlement_end_ms:
        raise MoveInputError("contract settlement timestamps are inconsistent")
    symbol = str(contract.get("symbol") or "").strip()
    if not symbol:
        raise MoveInputError("contract.symbol is required")
    _finite(contract.get("strike"), "contract.strike", minimum=0.00000001)
    multiplier = _finite(
        contract.get("contract_multiplier"),
        "contract.contract_multiplier",
        minimum=0.00000001,
    )
    _finite(contract.get("tick_size"), "contract.tick_size",
            minimum=0.00000001)
    _finite(contract.get("lot_size"), "contract.lot_size",
            minimum=0.00000001)
    _finite(contract.get("min_order_size"), "contract.min_order_size",
            minimum=0.00000001)
    max_position_size = _finite(
        contract.get("max_position_size"),
        "contract.max_position_size",
        minimum=1,
    )

    bid = _finite(market.get("bid"), "market.bid", minimum=0)
    ask = _finite(market.get("ask"), "market.ask", minimum=0)
    bid_size = _finite(market.get("bid_size"), "market.bid_size", minimum=0)
    ask_size = _finite(market.get("ask_size"), "market.ask_size", minimum=0)
    quote_ms = _timestamp_ms(
        market.get("quote_timestamp_ms"), "market.quote_timestamp_ms")
    if market.get("mark_price") not in (None, ""):
        _finite(market.get("mark_price"), "market.mark_price", minimum=0)
    index_price = _finite(
        underlying.get("btc_index_price"),
        "underlying.btc_index_price",
        minimum=0.00000001,
    )
    fixed_fraction = _finite(
        underlying.get("fraction_of_final_twap_fixed"),
        "underlying.fraction_of_final_twap_fixed",
        minimum=0,
    )
    if fixed_fraction > 1:
        raise MoveInputError(
            "underlying.fraction_of_final_twap_fixed cannot exceed 1")
    current_twap = _finite(
        underlying.get("current_30m_twap"),
        "underlying.current_30m_twap",
        minimum=0,
    )
    if fixed_fraction > 0 and current_twap <= 0:
        raise MoveInputError(
            "underlying.current_30m_twap must be positive once TWAP is fixed")

    payoff_low = _finite(
        forecast.get("expected_payoff_low"),
        "forecast.expected_payoff_low",
        minimum=0,
    )
    payoff_mid = _finite(
        forecast.get("expected_payoff_mid"),
        "forecast.expected_payoff_mid",
        minimum=0,
    )
    payoff_high = _finite(
        forecast.get("expected_payoff_high"),
        "forecast.expected_payoff_high",
        minimum=0,
    )
    payoff_p99 = _finite(
        forecast.get("payoff_p99"), "forecast.payoff_p99", minimum=0)
    if not payoff_low <= payoff_mid <= payoff_high <= payoff_p99:
        raise MoveInputError(
            "forecast must satisfy low <= mid <= high <= payoff_p99")
    jump_score = _finite(
        forecast.get("jump_event_score"),
        "forecast.jump_event_score",
        minimum=0,
    )
    if jump_score > 1:
        raise MoveInputError("forecast.jump_event_score cannot exceed 1")
    model_ms = _timestamp_ms(
        forecast.get("model_timestamp_ms"), "forecast.model_timestamp_ms")

    long_cost = (
        _finite(
            costs.get("long_round_trip_cost_per_contract"),
            "costs.long_round_trip_cost_per_contract",
            minimum=0,
        )
        + _finite(
            costs.get("long_slippage_per_contract"),
            "costs.long_slippage_per_contract",
            minimum=0,
        )
    )
    short_cost = (
        _finite(
            costs.get("short_round_trip_cost_per_contract"),
            "costs.short_round_trip_cost_per_contract",
            minimum=0,
        )
        + _finite(
            costs.get("short_slippage_per_contract"),
            "costs.short_slippage_per_contract",
            minimum=0,
        )
    )
    position_qty = _finite(
        account.get("current_position_qty"),
        "account.current_position_qty",
    )
    _finite(
        account.get("average_entry_price"),
        "account.average_entry_price",
        minimum=0,
    )
    available_margin = _finite(
        account.get("available_margin"), "account.available_margin", minimum=0)
    liquidation_buffer = _finite(
        account.get("liquidation_buffer"),
        "account.liquidation_buffer",
        minimum=0,
    )
    open_orders = _integer(
        account.get("open_orders_count"),
        "account.open_orders_count",
        minimum=0,
    )

    allow_long = _boolean(
        strategy_config.get("allow_long"), "strategy_config.allow_long")
    allow_short = _boolean(
        strategy_config.get("allow_short"), "strategy_config.allow_short")
    min_long_abs = _finite(
        strategy_config.get("min_long_edge_absolute"),
        "strategy_config.min_long_edge_absolute",
        minimum=0,
    )
    min_short_abs = _finite(
        strategy_config.get("min_short_edge_absolute"),
        "strategy_config.min_short_edge_absolute",
        minimum=0,
    )
    min_long_pct = _finite(
        strategy_config.get("min_long_edge_pct"),
        "strategy_config.min_long_edge_pct",
        minimum=0,
    )
    min_short_pct = _finite(
        strategy_config.get("min_short_edge_pct"),
        "strategy_config.min_short_edge_pct",
        minimum=0,
    )
    max_spread_pct = _finite(
        strategy_config.get("max_spread_pct"),
        "strategy_config.max_spread_pct",
        minimum=0,
    )
    max_quote_age_ms = _integer(
        strategy_config.get("max_quote_age_ms"),
        "strategy_config.max_quote_age_ms",
        minimum=0,
    )
    max_model_age_ms = _integer(
        strategy_config.get("max_model_age_ms"),
        "strategy_config.max_model_age_ms",
        minimum=0,
    )
    min_bid_size = _finite(
        strategy_config.get("min_bid_size"),
        "strategy_config.min_bid_size",
        minimum=0,
    )
    min_ask_size = _finite(
        strategy_config.get("min_ask_size"),
        "strategy_config.min_ask_size",
        minimum=0,
    )
    max_jump_short = _finite(
        strategy_config.get("max_jump_event_score_for_short"),
        "strategy_config.max_jump_event_score_for_short",
        minimum=0,
    )
    if max_jump_short > 1:
        raise MoveInputError(
            "strategy_config.max_jump_event_score_for_short cannot exceed 1")
    max_long_risk = _finite(
        strategy_config.get("max_long_premium_risk"),
        "strategy_config.max_long_premium_risk",
        minimum=0,
    )
    max_short_p99 = _finite(
        strategy_config.get("max_short_p99_loss"),
        "strategy_config.max_short_p99_loss",
        minimum=0,
    )
    max_contracts = _finite(
        strategy_config.get("max_contracts"),
        "strategy_config.max_contracts",
        minimum=1,
    )
    max_total_position = _finite(
        strategy_config.get("max_total_position"),
        "strategy_config.max_total_position",
        minimum=1,
    )
    no_entry_seconds = _integer(
        strategy_config.get("no_new_entry_seconds_before_settlement"),
        "strategy_config.no_new_entry_seconds_before_settlement",
        minimum=0,
    )
    require_no_orders = _boolean(
        strategy_config.get("require_no_existing_orders"),
        "strategy_config.require_no_existing_orders",
    )
    require_flat = _boolean(
        strategy_config.get("require_flat_before_entry"),
        "strategy_config.require_flat_before_entry",
    )
    required_liquidation_buffer = _finite(
        strategy_config.get("required_liquidation_buffer"),
        "strategy_config.required_liquidation_buffer",
        minimum=0,
    )
    max_open_loss = _finite(
        strategy_config.get("max_open_loss"),
        "strategy_config.max_open_loss",
        minimum=0,
    )

    mid = (bid + ask) / 2 if bid > 0 and ask > 0 else 0.0
    spread_pct = ((ask - bid) / mid if mid > 0 and ask >= bid
                  else float("inf"))
    quote_age_ms = now_ms - quote_ms
    model_age_ms = now_ms - model_ms
    seconds_to_settlement = int((settlement_end_ms - now_ms) / 1000)
    long_edge = (payoff_low - ask) * multiplier - long_cost
    short_edge = (bid - payoff_high) * multiplier - short_cost
    short_p99_loss = (
        max(0.0, payoff_p99 - bid) * multiplier + short_cost)
    long_premium_risk = ask * multiplier + long_cost
    long_hurdle = max(min_long_abs, min_long_pct * ask * multiplier)
    short_hurdle = max(min_short_abs, min_short_pct * bid * multiplier)

    common = {
        "quote_not_future": quote_age_ms >= 0,
        "model_not_future": model_age_ms >= 0,
        "quote_fresh": 0 <= quote_age_ms <= max_quote_age_ms,
        "model_fresh": 0 <= model_age_ms <= max_model_age_ms,
        "two_sided_market": bid > 0 and ask > 0 and ask >= bid,
        "spread_within_limit": spread_pct <= max_spread_pct,
        "system_operational": _boolean(
            exchange.get("system_operational"),
            "exchange.system_operational",
        ),
        "product_operational": _boolean(
            exchange.get("product_operational"),
            "exchange.product_operational",
        ),
        "trading_enabled": _boolean(
            exchange.get("trading_enabled"),
            "exchange.trading_enabled",
        ),
        "settlement_buffer": seconds_to_settlement > no_entry_seconds,
        "no_existing_orders": (not require_no_orders or open_orders == 0),
        "position_within_exchange_limit": (
            abs(position_qty) < min(max_position_size, max_total_position)
            and abs(position_qty) < max_contracts
        ),
        "index_price_valid": index_price > 0,
    }
    long_gates = {
        "allowed": allow_long,
        "edge": long_edge >= long_hurdle,
        "ask_liquidity": ask_size >= min_ask_size and ask > 0,
        "premium_risk_per_contract": long_premium_risk <= max_long_risk,
    }
    short_gates = {
        "allowed": allow_short,
        "edge": short_edge >= short_hurdle,
        "bid_liquidity": bid_size >= min_bid_size and bid > 0,
        "jump_event_risk": jump_score <= max_jump_short,
        "p99_risk_per_contract": short_p99_loss <= max_short_p99,
        "available_margin": available_margin > 0,
        "liquidation_buffer": (
            liquidation_buffer >= required_liquidation_buffer),
    }
    common_passed = all(common.values())
    long_signal = common_passed and all(long_gates.values())
    short_signal = common_passed and all(short_gates.values())
    conflict = long_signal and short_signal

    if not common_passed:
        action = NO_TRADE
    elif require_flat and position_qty != 0:
        action = MANAGE_EXISTING_POSITION
    elif position_qty != 0:
        action = MANAGE_EXISTING_POSITION
    elif long_signal and not short_signal:
        action = LONG_MOVE
    elif short_signal and not long_signal:
        action = SHORT_MOVE
    else:
        action = NO_TRADE

    failed_common = [name for name, passed in common.items() if not passed]
    failed_long = [name for name, passed in long_gates.items() if not passed]
    failed_short = [name for name, passed in short_gates.items() if not passed]
    return {
        "action": action,
        "side": "buy" if action == LONG_MOVE else "sell" if action == SHORT_MOVE else None,
        "common_passed": common_passed,
        "long_signal": long_signal,
        "short_signal": short_signal,
        "conflict": conflict,
        "decision_timestamp_ms": now_ms,
        "metrics": {
            "mid": mid,
            "spread_pct": spread_pct,
            "quote_age_ms": quote_age_ms,
            "model_age_ms": model_age_ms,
            "seconds_until_final_settlement": seconds_to_settlement,
            "long_all_in_cost_per_contract": long_cost,
            "short_all_in_cost_per_contract": short_cost,
            "long_edge_per_contract": long_edge,
            "short_edge_per_contract": short_edge,
            "long_hurdle": long_hurdle,
            "short_hurdle": short_hurdle,
            "long_premium_risk_per_contract": long_premium_risk,
            "short_p99_loss_per_contract": short_p99_loss,
            "current_position_qty": position_qty,
            "available_margin": available_margin,
            "liquidation_buffer": liquidation_buffer,
            "fraction_of_final_twap_fixed": fixed_fraction,
            "current_30m_twap": current_twap,
            "max_open_loss": max_open_loss,
        },
        "gates": {
            "common": common,
            "long": long_gates,
            "short": short_gates,
        },
        "failed_gates": {
            "common": failed_common,
            "long": failed_long,
            "short": failed_short,
        },
    }


def aggregate_risk_lot_caps(
    decision: dict[str, Any],
    strategy_config: dict[str, Any],
    *,
    available_margin: float,
    short_initial_margin_per_contract: float,
    current_position_qty: float = 0.0,
) -> dict[str, int]:
    """Convert aggregate funding and position limits into lot caps.

    The SHORT p99 estimate remains a decision diagnostic, but it is not a
    sizing input. SHORT exposure is sized by configured quantity and position
    ceilings; live affordability remains authoritative at exchange submission,
    where an insufficient-margin response safely downsizes the order. Its
    configured SL is the risk amount used by the portfolio ledger.
    """

    metrics = decision.get("metrics") or {}
    action = decision.get("action")
    max_contracts = int(_finite(
        strategy_config.get("max_contracts"), "max_contracts", minimum=1))
    max_total = int(_finite(
        strategy_config.get("max_total_position"),
        "max_total_position",
        minimum=1,
    ))
    remaining_position = max(
        min(max_contracts, max_total) - int(abs(current_position_qty)), 0)
    caps: dict[str, int] = {"position": remaining_position}
    if action == LONG_MOVE:
        per_contract = _finite(
            metrics.get("long_premium_risk_per_contract"),
            "long_premium_risk_per_contract",
            minimum=0.0000000001,
        )
        total = _finite(
            strategy_config.get("max_long_premium_risk"),
            "max_long_premium_risk",
            minimum=0,
        )
        caps["premium_risk"] = max(int(total // per_contract), 0)
    caps["effective"] = min(caps.values()) if caps else 0
    return caps
