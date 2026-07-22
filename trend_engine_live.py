"""Strict, read-only Delta snapshot adapter for the rules-based Trend Engine.

This module deliberately knows nothing about Flask and cannot submit orders.
It translates public Delta market data plus the active account's isolated
state into the normalized input accepted by :mod:`trend_engine`.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlencode


TIMEFRAMES = {
    "5m": ("5m", 300),
    "15m": ("15m", 900),
    "60m": ("1h", 3600),
}
INDICATOR_CANDLE_LIMIT = 320
FORECAST_HISTORY_5M_LIMIT = 4_000
DELTA_CANDLE_RESPONSE_LIMIT = 2_000
OPTION_QUOTE_ALIGNMENT_SECONDS = 30
STATE_FILES = ("morning_state.json", "straddle_state.json", "trend_state.json")


class SnapshotCollectionError(RuntimeError):
    """Raised when a required upstream snapshot cannot be proven reliable."""


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _integer(value: Any) -> int | None:
    number = _finite(value)
    if number is None or not number.is_integer():
        return None
    return int(number)


def _truthy(value: Any, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _iso_utc(value: Any) -> str | None:
    """Normalize Delta epoch values or timezone-aware ISO text to UTC ISO."""
    if isinstance(value, str) and value.strip():
        raw = value.strip()
        try:
            parsed_number = float(raw)
        except (TypeError, ValueError, OverflowError):
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                return None
            return parsed.astimezone(timezone.utc).isoformat()
        value = parsed_number
    number = _finite(value)
    if number is None or number <= 0:
        return None
    while number > 100_000_000_000:
        number /= 1000.0
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def _strict_json_response(response: Any, label: str, expected: type) -> Any:
    try:
        payload = response.json()
    except Exception as exc:
        raise SnapshotCollectionError(f"{label} returned invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("success") is not True:
        error = payload.get("error") if isinstance(payload, dict) else None
        raise SnapshotCollectionError(f"{label} failed: {error or 'unknown response'}")
    result = payload.get("result")
    if not isinstance(result, expected):
        raise SnapshotCollectionError(f"{label} returned malformed data")
    return result


def _strict_list_page(response: Any, label: str) -> tuple[list[Any], str | None]:
    try:
        payload = response.json()
    except Exception as exc:
        raise SnapshotCollectionError(f"{label} returned invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("success") is not True:
        error = payload.get("error") if isinstance(payload, dict) else None
        raise SnapshotCollectionError(f"{label} failed: {error or 'unknown response'}")
    rows = payload.get("result")
    if not isinstance(rows, list):
        raise SnapshotCollectionError(f"{label} returned malformed data")
    meta = payload.get("meta")
    if meta is not None and not isinstance(meta, dict):
        raise SnapshotCollectionError(f"{label} returned malformed pagination")
    after = (meta or {}).get("after")
    if after in (None, ""):
        return rows, None
    if not isinstance(after, str):
        raise SnapshotCollectionError(f"{label} returned an invalid cursor")
    return rows, after


def _private_list_pages(
    http_get: Callable[..., Any],
    api_base: str,
    sign: Callable[..., Mapping[str, str]],
    path: str,
    base_params: Mapping[str, Any],
    label: str,
) -> list[Any]:
    rows: list[Any] = []
    cursor: str | None = None
    seen: set[str] = set()
    for _ in range(100):
        params = dict(base_params)
        if cursor is not None:
            params["after"] = cursor
        query = "?" + urlencode(params)
        page, after = _strict_list_page(
            http_get(
                f"{api_base}{path}", params=params,
                headers=sign("GET", path, query), timeout=8,
            ),
            label,
        )
        rows.extend(page)
        if after is None:
            return rows
        if after in seen:
            raise SnapshotCollectionError(f"{label} repeated its pagination cursor")
        seen.add(after)
        cursor = after
    raise SnapshotCollectionError(f"{label} exceeded the pagination limit")


def _public_list_pages(
    http_get: Callable[..., Any],
    api_base: str,
    path: str,
    base_params: Mapping[str, Any],
    label: str,
    *,
    timeout: int = 12,
) -> list[Any]:
    """Read every page from a public cursor-paginated Delta endpoint."""
    rows: list[Any] = []
    cursor: str | None = None
    seen: set[str] = set()
    for _ in range(100):
        params = dict(base_params)
        if cursor is not None:
            params["after"] = cursor
        page, after = _strict_list_page(
            http_get(f"{api_base}{path}", params=params, timeout=timeout),
            label,
        )
        rows.extend(page)
        if after is None:
            return rows
        if after in seen:
            raise SnapshotCollectionError(
                f"{label} repeated its pagination cursor"
            )
        seen.add(after)
        cursor = after
    raise SnapshotCollectionError(f"{label} exceeded the pagination limit")


def _strict_json_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise SnapshotCollectionError(f"{path.name} is unreadable") from exc
    if not isinstance(value, type(default)):
        raise SnapshotCollectionError(f"{path.name} has an invalid structure")
    return value


def _closed_candles(
    http_get: Callable[..., Any],
    api_base: str,
    now: datetime,
    label: str,
    resolution: str,
    seconds: int,
    *,
    limit: int = INDICATOR_CANDLE_LIMIT,
) -> list[dict[str, Any]]:
    if limit <= 0:
        raise ValueError("candle limit must be positive")
    end = int(now.timestamp())
    current_bucket = end - end % seconds
    history_start = current_bucket - seconds * limit
    normalized_by_epoch: dict[int, dict[str, Any]] = {}

    # Delta documents a maximum of 2,000 candles per response.  Adjacent,
    # end-exclusive windows keep this bounded and avoid relying on any
    # undocumented ordering or truncation behavior.
    window_start = history_start
    while window_start < current_bucket:
        window_end = min(
            window_start + seconds * DELTA_CANDLE_RESPONSE_LIMIT,
            current_bucket,
        )
        response = http_get(
            f"{api_base}/v2/history/candles",
            params={
                "resolution": resolution,
                "symbol": "BTCUSD",
                "start": window_start,
                # Ending one second before the next bucket makes adjacent
                # request ranges unambiguously disjoint.
                "end": window_end - 1,
            },
            timeout=10,
        )
        rows = _strict_json_response(response, f"{label} candles", list)
        for raw in rows:
            if not isinstance(raw, dict):
                raise SnapshotCollectionError(
                    f"{label} candles contain a malformed row"
                )
            epoch = _integer(raw.get("time"))
            # The decision prompt forbids unfinished candles.  Also discard
            # any upstream row outside the explicitly requested history range.
            if (
                epoch is None
                or epoch < window_start
                or epoch >= window_end
            ):
                continue
            normalized = {
                "timestamp": datetime.fromtimestamp(
                    epoch, tz=timezone.utc
                ).isoformat(),
                "open": raw.get("open"),
                "high": raw.get("high"),
                "low": raw.get("low"),
                "close": raw.get("close"),
                "volume": raw.get("volume"),
                "complete": True,
            }
            previous = normalized_by_epoch.get(epoch)
            if previous is not None and previous != normalized:
                raise SnapshotCollectionError(
                    f"{label} candles contain conflicting duplicate rows"
                )
            normalized_by_epoch[epoch] = normalized
        window_start = window_end

    epochs = sorted(normalized_by_epoch)
    return [normalized_by_epoch[epoch] for epoch in epochs[-limit:]]


def _option_contracts(
    http_get: Callable[..., Any],
    api_base: str,
    *,
    spot: float,
    fee_rate: float,
    fee_cap_pct: float,
    slippage_pct: float,
) -> tuple[list[dict[str, Any]], str | None, dict[str, dict[str, Any]]]:
    filters = {
        "contract_types": "call_options,put_options",
        "underlying_asset_symbols": "BTC",
    }
    products = _public_list_pages(
        http_get,
        api_base,
        "/v2/products",
        {**filters, "states": "live", "page_size": 1000},
        "option products",
    )
    tickers = _strict_json_response(
        http_get(f"{api_base}/v2/tickers", params=filters, timeout=12),
        "option tickers",
        list,
    )
    ticker_by_symbol = {
        str(row.get("symbol") or ""): row
        for row in tickers
        if isinstance(row, dict) and row.get("symbol")
    }
    contracts = []
    quote_times = []
    for product in products:
        if not isinstance(product, dict):
            raise SnapshotCollectionError("option products contain a malformed row")
        symbol = str(product.get("symbol") or "")
        if not (symbol.startswith("C-BTC-") or symbol.startswith("P-BTC-")):
            continue
        ticker = ticker_by_symbol.get(symbol)
        if not isinstance(ticker, dict):
            # A product without a quote remains absent from the eligible universe;
            # the core distinguishes an empty valid universe from invalid data.
            continue
        quotes = ticker.get("quotes")
        greeks = ticker.get("greeks")
        if not isinstance(quotes, dict):
            quotes = {}
        if not isinstance(greeks, dict):
            greeks = {}
        quote_timestamp = _iso_utc(ticker.get("timestamp"))
        expiry = _iso_utc(product.get("settlement_time"))
        strike = _finite(product.get("strike_price") or ticker.get("strike_price"))
        bid = _finite(quotes.get("best_bid"))
        ask = _finite(quotes.get("best_ask"))
        volume = _finite(ticker.get("volume"))
        open_interest = _finite(ticker.get("oi_contracts") or ticker.get("oi"))
        implied_volatility = _finite(quotes.get("mark_iv"))
        if implied_volatility is None:
            bid_iv = _finite(quotes.get("bid_iv"))
            ask_iv = _finite(quotes.get("ask_iv"))
            if bid_iv is not None and ask_iv is not None:
                implied_volatility = (bid_iv + ask_iv) / 2.0
            else:
                implied_volatility = _finite(ticker.get("mark_vol"))
        delta = _finite(greeks.get("delta"))
        theta = _finite(greeks.get("theta"))
        contract_value = _finite(product.get("contract_value") or ticker.get("contract_value"))
        tick_size = _finite(ticker.get("tick_size") or product.get("tick_size"))
        max_order_lots = _integer(product.get("position_size_limit"))
        trading_status = str(
            product.get("trading_status") or ticker.get("product_trading_status") or ""
        ).strip().lower()
        option_type = "CE" if symbol.startswith("C-") else "PE"
        # Missing or structurally invalid quotes are not eligible contracts.
        # Excluding them is distinct from fabricating a zero-valued Greek.
        if (not quote_timestamp or not expiry or strike is None or strike <= 0
                or bid is None or ask is None or bid < 0 or ask <= 0 or bid > ask
                or volume is None or volume < 0
                or open_interest is None or open_interest < 0
                or implied_volatility is None or implied_volatility < 0
                or delta is None or not -1 <= delta <= 1
                or (option_type == "CE" and delta < 0)
                or (option_type == "PE" and delta > 0)
                or theta is None or contract_value is None or contract_value <= 0
                or tick_size is None or tick_size <= 0
                or max_order_lots is None or max_order_lots <= 0
                or trading_status != "operational"):
            continue
        quote_times.append(quote_timestamp)
        round_trip_fee = (
            2.0 * min(fee_rate * spot, fee_cap_pct * ask) * contract_value
        )
        round_trip_slippage = 2.0 * ask * contract_value * slippage_pct / 100.0
        contracts.append({
            "product_id": product.get("id") or ticker.get("product_id"),
            "symbol": symbol,
            "option_type": option_type,
            "strike": strike,
            "expiry": expiry,
            "quote_timestamp": quote_timestamp,
            "bid": bid,
            "ask": ask,
            "bid_size": quotes.get("bid_size"),
            "ask_size": quotes.get("ask_size"),
            "bid_quantity": quotes.get("bid_size"),
            "ask_quantity": quotes.get("ask_size"),
            "mark": ticker.get("mark_price"),
            "volume": volume,
            "open_interest": open_interest,
            "iv": implied_volatility,
            "delta": delta,
            "theta": theta,
            "vega": greeks.get("vega"),
            "gamma": greeks.get("gamma"),
            "lot_size": 1,
            "contract_value": contract_value,
            "position_size_limit": max_order_lots,
            "max_order_lots": max_order_lots,
            "trading_status": trading_status,
            "tick_size": tick_size,
            "estimated_costs_per_lot": round(
                round_trip_fee + round_trip_slippage, 12
            ),
        })
    option_timestamp = max(quote_times) if quote_times else None
    if option_timestamp is not None:
        latest = datetime.fromisoformat(option_timestamp.replace("Z", "+00:00"))
        contracts = [
            contract for contract in contracts
            if abs((latest - datetime.fromisoformat(
                str(contract["quote_timestamp"]).replace("Z", "+00:00")
            )).total_seconds()) <= OPTION_QUOTE_ALIGNMENT_SECONDS
        ]
    return contracts, option_timestamp, ticker_by_symbol


def _persisted_remaining_expected_value(
    state: Mapping[str, Any],
    now: datetime,
) -> tuple[float, str]:
    """Reuse a persisted EV only inside its explicit server-issued lifetime.

    An entry EV is not a live valuation.  Once its short validity window has
    elapsed (or legacy/manual state lacks provenance), returning zero makes
    the core recommend advisory EXIT instead of pretending the old edge is
    current.  This adapter never fabricates a refreshed expected value.
    """
    value = _finite(state.get("remaining_expected_value"))
    source = str(state.get("remaining_expected_value_source") or "").strip()
    as_of_raw = str(
        state.get("remaining_expected_value_as_of_utc") or ""
    ).strip()
    valid_until_raw = str(
        state.get("remaining_expected_value_valid_until_utc") or ""
    ).strip()
    if value is None or not source or not as_of_raw or not valid_until_raw:
        return 0.0, "unverified_persisted_value"
    try:
        as_of = datetime.fromisoformat(as_of_raw.replace("Z", "+00:00"))
        valid_until = datetime.fromisoformat(
            valid_until_raw.replace("Z", "+00:00")
        )
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=timezone.utc)
        if valid_until.tzinfo is None:
            valid_until = valid_until.replace(tzinfo=timezone.utc)
        as_of = as_of.astimezone(timezone.utc)
        valid_until = valid_until.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return 0.0, "invalid_persisted_value_lifetime"
    if as_of > now + timedelta(seconds=5) or valid_until <= as_of:
        return 0.0, "invalid_persisted_value_lifetime"
    if now >= valid_until:
        return 0.0, "stale_persisted_value"
    return value, "valid_persisted_value"


def _state_positions(
    data_dir: Path,
    now: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    positions = []
    pending = []
    states = []
    for name in STATE_FILES:
        state = _strict_json_file(data_dir / name, {})
        if not state:
            continue
        states.append(state)
        status = str(state.get("status") or "").upper()
        if status == "OPEN":
            symbol = str(state.get("symbol") or "")
            remaining_ev, remaining_ev_status = (
                _persisted_remaining_expected_value(state, now)
            )
            positions.append({
                "source": "dry_run",
                "slot": state.get("slot") or ("evening" if name == "straddle_state.json" else name.removesuffix("_state.json")),
                "symbol": symbol,
                "product_id": state.get("product_id"),
                "option_type": state.get("option_type") or (
                    "CE" if symbol.startswith("C-") else "PE" if symbol.startswith("P-") else None
                ),
                "side": str(state.get("side") or "long").lower(),
                "lots": state.get("lots"),
                "quantity_lots": state.get("lots"),
                "entry_price": state.get("entry_mark"),
                "contract_value": state.get("contract_value"),
                "entry_time": (
                    f"{state.get('entry_date')}T{state.get('entry_time_utc')}+00:00"
                    if state.get("entry_date") and state.get("entry_time_utc") else None
                ),
                "model_version": state.get("model_version"),
                "entry_decision_id": state.get("entry_decision_id"),
                "underlying_invalidation": state.get("underlying_invalidation"),
                "time_exit": state.get("time_exit"),
                "stop_option_price": state.get("stop_option_price"),
                "target_option_price": state.get("target_option_price"),
                "underlying_target": state.get("underlying_target"),
                "entry_trigger": state.get("entry_trigger"),
                "ownership": state.get("ownership"),
                "remaining_expected_value": remaining_ev,
                "remaining_expected_value_status": remaining_ev_status,
                "remaining_expected_value_as_of_utc": state.get(
                    "remaining_expected_value_as_of_utc"
                ),
                "remaining_expected_value_valid_until_utc": state.get(
                    "remaining_expected_value_valid_until_utc"
                ),
                "remaining_expected_value_source": state.get(
                    "remaining_expected_value_source"
                ),
            })
        if status not in {"", "IDLE", "OPEN", "CLOSED"} or any(
            state.get(key) not in (None, "")
            for key in (
                "pending_entry_client_order_id", "pending_entry_order_id",
                "pending_close_client_order_id", "pending_close_order_id",
            )
        ):
            pending.append({
                "source": "local_state",
                "symbol": state.get("symbol"),
                "status": status or "UNKNOWN",
                "client_order_id": state.get("pending_entry_client_order_id")
                or state.get("pending_close_client_order_id"),
                "order_id": state.get("pending_entry_order_id")
                or state.get("pending_close_order_id"),
            })
    journal_paths = [
        *sorted(data_dir.glob("pending_*_entry.json")),
        *sorted(data_dir.glob("pending_trend_order_*.json")),
    ]
    for journal in journal_paths:
        payload = _strict_json_file(journal, {})
        orders = payload.get("orders") if isinstance(payload.get("orders"), list) else []
        first_order = orders[0] if orders and isinstance(orders[0], dict) else {}
        pending.append({
            "source": "local_journal",
            "symbol": payload.get("symbol") or first_order.get("symbol"),
            "status": str(payload.get("status") or "PENDING").upper(),
            "client_order_id": payload.get("client_order_id")
            or first_order.get("client_order_id"),
            "order_id": payload.get("order_id") or first_order.get("order_id"),
            "journal": journal.name,
        })
    return positions, pending, states


def _row_timestamp(row: Mapping[str, Any]) -> datetime | None:
    for key in ("exit_at_utc", "closed_at_utc"):
        raw = str(row.get(key) or "")
        if raw:
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            except ValueError:
                pass
    date = str(row.get("exit_date") or row.get("entry_date") or row.get("date") or "")
    clock = str(row.get("exit_time_utc") or row.get("entry_time_utc") or row.get("entry_time") or "00:00:00")
    try:
        return datetime.fromisoformat(f"{date}T{clock}+00:00")
    except ValueError:
        return None


def _risk_history(data_dir: Path, now: datetime, offset_minutes: int) -> tuple[float, int, int]:
    history = _strict_json_file(data_dir / "trade_history.json", [])
    rows = []
    for index, row in enumerate(history):
        if not isinstance(row, dict):
            raise SnapshotCollectionError(
                f"trade_history.json row {index} is malformed"
            )
        pnl = _finite(row.get("pnl_usd"))
        stamp = _row_timestamp(row)
        if pnl is None or stamp is None or stamp > now:
            raise SnapshotCollectionError(
                f"trade_history.json row {index} has unknown P&L or time"
            )
        rows.append((stamp, pnl))
    local_tz = timezone(timedelta(minutes=offset_minutes))
    today = now.astimezone(local_tz).date()
    daily_pnl = 0.0
    trades_today = 0
    closed = []
    for stamp, pnl in rows:
        closed.append((stamp, pnl))
        if stamp.astimezone(local_tz).date() == today:
            daily_pnl += pnl
            trades_today += 1
    consecutive = 0
    for _, pnl in sorted(closed, key=lambda item: item[0], reverse=True):
        if pnl < 0:
            consecutive += 1
        else:
            break
    return round(daily_pnl, 8), consecutive, trades_today


def _position_quote(position: dict[str, Any], ticker_by_symbol: Mapping[str, dict[str, Any]]) -> None:
    ticker = ticker_by_symbol.get(str(position.get("symbol") or ""))
    if not isinstance(ticker, dict):
        return
    quotes = ticker.get("quotes") if isinstance(ticker.get("quotes"), dict) else {}
    side = str(position.get("side") or "long").lower()
    current = _finite(quotes.get("best_ask") if side == "short" else quotes.get("best_bid"))
    if current is None:
        current = _finite(ticker.get("mark_price"))
    entry = _finite(position.get("entry_price"))
    lots = _finite(position.get("lots"))
    multiplier = _finite(position.get("contract_value"))
    if None in (current, entry, lots, multiplier):
        return
    sign = -1 if side == "short" else 1
    position["current_price"] = current
    position["unrealized_pnl"] = round((current - entry) * multiplier * lots * sign, 8)


def collect_delta_trend_snapshot(
    *,
    http_get: Callable[..., Any],
    api_base: str,
    sign: Callable[..., Mapping[str, str]],
    user_dir: Path,
    dry_run: bool,
    mode_revision: str,
    strategy_config: Mapping[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Collect one strict, account-scoped, side-effect-free engine snapshot."""
    fixed_now = now is not None
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)

    underlying = _strict_json_response(
        http_get(f"{api_base}/v2/tickers/BTCUSD", timeout=8),
        "BTCUSD ticker",
        dict,
    )
    underlying_timestamp = _iso_utc(underlying.get("timestamp"))
    spot = underlying.get("spot_price")
    futures_mark = underlying.get("mark_price")
    if underlying_timestamp is None or _finite(spot) is None or _finite(futures_mark) is None:
        raise SnapshotCollectionError("BTCUSD ticker is missing price or timestamp")

    forecast_history_error: str | None = None
    try:
        forecast_history_5m = _closed_candles(
            http_get,
            api_base,
            now,
            "forecast history 5m",
            "5m",
            TIMEFRAMES["5m"][1],
            limit=FORECAST_HISTORY_5M_LIMIT,
        )
    except Exception as exc:
        # Extended history is optional edge evidence.  Current indicators and
        # open-position safety management must remain available when this
        # larger public request fails.
        forecast_history_5m = []
        forecast_history_error = f"{type(exc).__name__}: {exc}"[:500]
    candles = {
        "5m": (
            forecast_history_5m[-INDICATOR_CANDLE_LIMIT:]
            if forecast_history_5m else _closed_candles(
                http_get,
                api_base,
                now,
                "5m",
                TIMEFRAMES["5m"][0],
                TIMEFRAMES["5m"][1],
                limit=INDICATOR_CANDLE_LIMIT,
            )
        ),
        **{
            label: _closed_candles(
                http_get,
                api_base,
                now,
                label,
                resolution,
                seconds,
                limit=INDICATOR_CANDLE_LIMIT,
            )
            for label, (resolution, seconds) in TIMEFRAMES.items()
            if label != "5m"
        },
    }
    fee_rate = _finite(strategy_config.get("OPTION_FEE_RATE"))
    if fee_rate is None:
        fee_rate = 0.00010
    fee_cap_pct = _finite(strategy_config.get("OPTION_FEE_CAP_PCT"))
    if fee_cap_pct is None:
        fee_cap_pct = 0.035
    slippage_pct = _finite(strategy_config.get("TREND_ENGINE_SLIPPAGE_PCT"))
    if slippage_pct is None:
        slippage_pct = _finite(strategy_config.get("TREND_MAX_SLIPPAGE_PCT"))
    if slippage_pct is None:
        slippage_pct = 1.0
    contracts, option_timestamp, ticker_by_symbol = _option_contracts(
        http_get, api_base, spot=float(spot), fee_rate=fee_rate,
        fee_cap_pct=fee_cap_pct, slippage_pct=slippage_pct,
    )

    data_dir = user_dir / "dry_run" if dry_run else user_dir
    local_positions, local_pending, states = _state_positions(data_dir, now)
    positions = list(local_positions)
    position_state_consistent = not bool(local_pending)
    if dry_run:
        equity = _finite(strategy_config.get("TREND_ENGINE_DRY_RUN_EQUITY_USD"))
        if equity is None:
            equity = _finite(strategy_config.get("MOVE_DRY_RUN_CAPITAL_USD"))
        available = equity
        open_orders = local_pending
        broker_connected = True
        orders_known = True
    else:
        wallet = _strict_json_response(
            http_get(
                f"{api_base}/v2/wallet/balances",
                headers=sign("GET", "/v2/wallet/balances"),
                timeout=8,
            ),
            "wallet",
            list,
        )
        usd = next((row for row in wallet if isinstance(row, dict)
                    and row.get("asset_symbol") == "USD"), None)
        if not isinstance(usd, dict):
            raise SnapshotCollectionError("USD wallet balance is unavailable")
        equity = _finite(usd.get("balance"))
        available = _finite(usd.get("available_balance"))

        exchange_positions = _strict_json_response(
            http_get(
                f"{api_base}/v2/positions/margined",
                headers=sign("GET", "/v2/positions/margined"),
                timeout=8,
            ),
            "exchange positions",
            list,
        )
        positions = []
        for row in exchange_positions:
            if not isinstance(row, dict):
                raise SnapshotCollectionError("exchange positions contain a malformed row")
            size = _finite(row.get("size"))
            if size in (None, 0):
                continue
            symbol = str(row.get("product_symbol") or row.get("symbol") or "")
            ticker = ticker_by_symbol.get(symbol, {})
            positions.append({
                "source": "exchange",
                "symbol": symbol,
                "product_id": row.get("product_id"),
                "option_type": "CE" if symbol.startswith("C-") else "PE" if symbol.startswith("P-") else None,
                "side": "long" if size > 0 else "short",
                "lots": abs(size),
                "quantity_lots": abs(size),
                "entry_price": row.get("entry_price"),
                "current_price": row.get("mark_price") or ticker.get("mark_price"),
                "unrealized_pnl": row.get("unrealized_pnl"),
                "contract_value": ticker.get("contract_value"),
                "model_version": None,
                "entry_decision_id": None,
            })

        unmatched_local = list(local_positions)
        for position in positions:
            matches = []
            for local in unmatched_local:
                same_product = (
                    local.get("product_id") not in (None, "")
                    and position.get("product_id") not in (None, "")
                    and str(local.get("product_id")) == str(position.get("product_id"))
                )
                same_symbol = (
                    str(local.get("symbol") or "")
                    and str(local.get("symbol") or "")
                    == str(position.get("symbol") or "")
                )
                if same_product or same_symbol:
                    matches.append(local)
            if len(matches) != 1:
                position_state_consistent = False
                continue
            local = matches[0]
            unmatched_local.remove(local)
            local_lots = _finite(local.get("quantity_lots"))
            exchange_lots = _finite(position.get("quantity_lots"))
            if (local_lots is None or exchange_lots is None
                    or abs(local_lots - exchange_lots) > 1e-9
                    or str(local.get("side") or "").lower()
                    != str(position.get("side") or "").lower()):
                position_state_consistent = False
                continue
            for key in (
                "model_version", "entry_decision_id", "underlying_invalidation",
                "time_exit", "stop_option_price", "target_option_price",
                "remaining_expected_value", "remaining_expected_value_status",
                "remaining_expected_value_as_of_utc",
                "remaining_expected_value_valid_until_utc",
                "remaining_expected_value_source",
                "underlying_target", "entry_trigger", "ownership",
            ):
                position[key] = local.get(key)
        if unmatched_local:
            position_state_consistent = False
        open_orders = _private_list_pages(
            http_get, api_base, sign, "/v2/orders",
            {"states": "open", "page_size": 50}, "open orders",
        )
        if local_pending:
            open_orders = [*open_orders, *local_pending]
        broker_connected = True
        orders_known = True

    if equity is None or available is None or equity <= 0 or available < 0:
        raise SnapshotCollectionError("account equity or available funds are unavailable")

    for position in positions:
        _position_quote(position, ticker_by_symbol)

    # In production, stamp the immutable snapshot only after every network
    # read completes.  Otherwise a ticker updated during collection can appear
    # to come from the future relative to a start-of-collection clock.
    if not fixed_now:
        now = datetime.now(timezone.utc)

    offset = int(_finite(strategy_config.get("RISK_DAY_TZ_OFFSET_MIN")) or 330)
    daily_pnl, consecutive, trades_today = _risk_history(data_dir, now, offset)
    unrealized = sum(
        _finite(position.get("unrealized_pnl")) or 0.0 for position in positions
    )
    daily_pnl = round(daily_pnl + unrealized, 8)
    open_risk = sum(
        _finite(state.get("risk_at_entry_usd"))
        or _finite(state.get("total_cost_usd"))
        or 0.0
        for state in states
        if str(state.get("status") or "").upper() == "OPEN"
    )

    event_status = str(
        strategy_config.get("TREND_ENGINE_EVENT_STATUS") or "unknown"
    ).strip().lower()
    if event_status == "clear":
        events: list[dict[str, Any]] | None = []
        event_data_available = True
    elif event_status == "blackout":
        events = [{
            "timestamp": now.isoformat(),
            "prohibited": True,
            "name": "ACCOUNT_CONFIGURED_EVENT_BLACKOUT",
        }]
        event_data_available = True
    else:
        events = None
        event_data_available = False

    return {
        "timestamp": now.isoformat(),
        "underlying": "BTCUSD",
        "market": {
            "market_data_timestamp": underlying_timestamp,
            "spot": spot,
            "futures_mark": futures_mark,
            "spot_timestamp": underlying_timestamp,
            "futures_timestamp": underlying_timestamp,
            "option_chain_timestamp": option_timestamp,
            "event_data_available": event_data_available,
            "futures_open": underlying.get("open"),
            "futures_volume": underlying.get("volume"),
            "futures_open_interest": underlying.get("oi_contracts") or underlying.get("oi"),
            "futures_oi_change_usd_6h": underlying.get("oi_change_usd_6h"),
            "futures_basis_pct": (
                (_finite(futures_mark) - _finite(spot)) / _finite(spot) * 100
                if _finite(futures_mark) is not None and _finite(spot) not in (None, 0)
                else None
            ),
            "breadth_available": False,
            "breadth_provenance": "not_applicable_to_btc",
            "forecast_history_available": forecast_history_error is None,
            "forecast_history_error": forecast_history_error,
        },
        "candles": candles,
        "forecast_history_5m": ({
            "source": {
                "provider": "delta_exchange",
                "transport": "public_rest",
                "endpoint": "/v2/history/candles",
                "symbol": "BTCUSD",
                "resolution": "5m",
                "interval_seconds": TIMEFRAMES["5m"][1],
                "completed_only": True,
            },
            "requested_limit": FORECAST_HISTORY_5M_LIMIT,
            "returned_count": len(forecast_history_5m),
            "first_timestamp": (
                forecast_history_5m[0]["timestamp"]
                if forecast_history_5m else None
            ),
            "last_timestamp": (
                forecast_history_5m[-1]["timestamp"]
                if forecast_history_5m else None
            ),
            "candles": forecast_history_5m,
        } if forecast_history_error is None else None),
        "option_contracts": contracts,
        "account": {
            "equity": equity,
            "available_funds": available,
            "daily_pnl": daily_pnl,
            "consecutive_losses": consecutive,
            "execution_mode": "dry_run" if dry_run else "live",
            "mode_revision": mode_revision,
            "trades_today": trades_today,
            "open_risk": round(open_risk, 8),
            "current_exposure": round(open_risk, 8),
        },
        "risk": {
            "broker_connected": broker_connected,
            "exchange_operational": True,
            "position_state_consistent": position_state_consistent,
            "orders_state_known": orders_known,
            "account_risk_state_known": bool(dry_run),
            "kill_switch_active": _truthy(
                strategy_config.get("TREND_ENGINE_KILL_SWITCH"), False
            ),
            # Every normalized option carries its exact fee/slippage estimate;
            # these zeros are a required fallback and are never used for one
            # of the collected contracts.
            "estimated_costs_per_lot": 0.0,
            "estimated_slippage_per_lot": 0.0,
            "max_open_positions": 1,
        },
        "positions": positions,
        "pending_orders": open_orders,
        "events": events,
    }
