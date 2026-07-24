"""Pure policy helpers shared by DRY RUN and LIVE score automation.

This module deliberately performs no network, filesystem, Flask, or exchange
work.  It turns a validated Trend Engine score into one of the three approved
policy zones, derives a stable completed-candle identity, selects the exact
listed directional option requested by the policy, and plans an idempotent
single-position transition.

Strike *steps* are counted from the raw operational product ladder.  The
quote-filtered contract universe is consulted only after the exact target has
been identified.  Consequently, a missing quote cannot silently turn a
two-step option into a different strike.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Collection, Mapping, Sequence


PE_3_ITM = "PE_3_ITM"
SHORT_MOVE = "SHORT_MOVE"
CE_2_ITM = "CE_2_ITM"
SCORE_ZONES = frozenset({PE_3_ITM, SHORT_MOVE, CE_2_ITM})

AUTO_TRADE_LOTS = 1_000
MIN_TIME_TO_EXPIRY_SECONDS = 90 * 60


class TrendScoreAutoInputError(ValueError):
    """The controller input is incomplete, ambiguous, or unsafe to use."""


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise TrendScoreAutoInputError(f"{name} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise TrendScoreAutoInputError(f"{name} must be numeric") from exc
    if not math.isfinite(number):
        raise TrendScoreAutoInputError(f"{name} must be finite")
    return number


def _positive_integer(value: Any, name: str) -> int:
    number = _finite(value, name)
    integer = int(number)
    if number != integer or integer <= 0:
        raise TrendScoreAutoInputError(f"{name} must be a positive integer")
    return integer


def _utc_time(value: Any, name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        if not raw:
            raise TrendScoreAutoInputError(f"{name} is required")
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise TrendScoreAutoInputError(f"{name} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise TrendScoreAutoInputError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def score_zone(score: Any) -> str:
    """Return the exact approved action zone for a validated engine score.

    ``-25`` belongs to the bearish PE zone and ``+25`` belongs to the bullish
    CE zone.  A missing/invalid score is never treated as neutral because that
    would turn a feed failure into permission to short MOVE.
    """

    value = _finite(score, "direction_score")
    if value < -100 or value > 100:
        raise TrendScoreAutoInputError(
            "direction_score must be between -100 and 100"
        )
    if value <= -25:
        return PE_3_ITM
    if value >= 25:
        return CE_2_ITM
    return SHORT_MOVE


def completed_candle_signal_key(
    snapshot: Mapping[str, Any],
    *,
    timeframe: str = "5m",
) -> str:
    """Identify one completed-candle event independently of wall-clock time.

    The key intentionally uses the terminal completed candle timestamp rather
    than quotes, score, or contract selection.  Re-polling the same closed
    five-minute candle therefore cannot create another transition merely
    because a live ticker changed.
    """

    if not isinstance(snapshot, Mapping):
        raise TrendScoreAutoInputError("snapshot must be an object")
    candles = snapshot.get("candles")
    if not isinstance(candles, Mapping):
        raise TrendScoreAutoInputError("snapshot.candles must be an object")
    rows = candles.get(timeframe)
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise TrendScoreAutoInputError(
            f"snapshot.candles.{timeframe} must be a list"
        )

    complete: dict[datetime, tuple[Any, ...]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping) or row.get("complete") is not True:
            continue
        timestamp = _utc_time(
            row.get("timestamp"),
            f"snapshot.candles.{timeframe}[{index}].timestamp",
        )
        evidence = tuple(
            row.get(key) for key in ("open", "high", "low", "close", "volume")
        )
        previous = complete.get(timestamp)
        if previous is not None and previous != evidence:
            raise TrendScoreAutoInputError(
                f"snapshot.candles.{timeframe} has a conflicting duplicate candle"
            )
        complete[timestamp] = evidence
    if not complete:
        raise TrendScoreAutoInputError(
            f"snapshot.candles.{timeframe} has no completed candle"
        )

    underlying = str(snapshot.get("underlying") or "BTCUSD").strip().upper()
    if not underlying:
        raise TrendScoreAutoInputError("snapshot.underlying is required")
    terminal = max(complete)
    return f"trend-score-auto|{underlying}|{timeframe}|{_iso_utc(terminal)}"


def _product_option_type(symbol: str) -> str | None:
    if symbol.startswith("C-BTC-"):
        return "CE"
    if symbol.startswith("P-BTC-"):
        return "PE"
    return None


def _listed_vanilla_products(
    raw_products: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
) -> dict[datetime, dict[str, Any]]:
    """Normalize the authoritative raw strike ladder by exact settlement."""

    if not isinstance(raw_products, Sequence) or isinstance(
        raw_products, (str, bytes)
    ):
        raise TrendScoreAutoInputError("raw_products must be a list")

    by_expiry: dict[datetime, dict[str, Any]] = {}
    for index, product in enumerate(raw_products):
        if not isinstance(product, Mapping):
            raise TrendScoreAutoInputError(
                f"raw_products[{index}] must be an object"
            )
        symbol = str(product.get("symbol") or "").strip()
        option_type = _product_option_type(symbol)
        if option_type is None:
            continue
        # Only a currently listed, operational BTC vanilla option contributes
        # a ladder step.  Non-operational strikes are not executable listings.
        if str(product.get("state") or "").strip().lower() != "live":
            continue
        if str(product.get("trading_status") or "").strip().lower() != "operational":
            continue
        underlying = product.get("underlying_asset")
        if isinstance(underlying, Mapping):
            underlying = underlying.get("symbol")
        if underlying not in (None, "", "BTC"):
            continue
        try:
            expiry = _utc_time(
                product.get("settlement_time"),
                f"raw_products[{index}].settlement_time",
            )
            strike = _finite(
                product.get("strike_price"),
                f"raw_products[{index}].strike_price",
            )
            product_id = _positive_integer(
                product.get("id"), f"raw_products[{index}].id"
            )
        except TrendScoreAutoInputError:
            # A malformed row that claims to be a live operational BTC option
            # makes the ladder ambiguous.  Propagate the error and fail closed.
            raise
        if strike <= 0:
            raise TrendScoreAutoInputError(
                f"raw_products[{index}].strike_price must be positive"
            )
        if (expiry - now).total_seconds() < MIN_TIME_TO_EXPIRY_SECONDS:
            continue

        expiry_group = by_expiry.setdefault(
            expiry, {"strikes": set(), "products": {}}
        )
        expiry_group["strikes"].add(strike)
        key = (option_type, strike)
        normalized = {
            "symbol": symbol,
            "product_id": product_id,
            "option_type": option_type,
            "strike": strike,
            "expiry": expiry,
            "raw": dict(product),
        }
        previous = expiry_group["products"].get(key)
        if previous is not None and (
            previous["symbol"] != symbol or previous["product_id"] != product_id
        ):
            raise TrendScoreAutoInputError(
                "raw option ladder contains two products for the same type, "
                "settlement, and strike"
            )
        expiry_group["products"][key] = normalized
    return by_expiry


def select_directional_option(
    raw_products: Sequence[Mapping[str, Any]],
    executable_contracts: Sequence[Mapping[str, Any]],
    *,
    spot: Any,
    zone: str,
    now: datetime,
) -> dict[str, Any] | None:
    """Select the exact policy strike or return ``None`` without substitution.

    The earliest listed operational expiry with at least 90 minutes remaining
    is authoritative.  CE selects ``ATM index - 2`` and PE selects
    ``ATM index + 3``.  If that exact product is absent or not executable for
    all 1,000 lots, the function returns ``None``; it never shifts strike or
    tries a later expiry.
    """

    if zone == CE_2_ITM:
        option_type, steps, direction = "CE", 2, -1
    elif zone == PE_3_ITM:
        option_type, steps, direction = "PE", 3, 1
    else:
        raise TrendScoreAutoInputError(
            "zone must be CE_2_ITM or PE_3_ITM for option selection"
        )
    current = _utc_time(now, "now")
    current_spot = _finite(spot, "spot")
    if current_spot <= 0:
        raise TrendScoreAutoInputError("spot must be positive")
    if not isinstance(executable_contracts, Sequence) or isinstance(
        executable_contracts, (str, bytes)
    ):
        raise TrendScoreAutoInputError("executable_contracts must be a list")

    by_expiry = _listed_vanilla_products(raw_products, now=current)
    if not by_expiry:
        return None
    expiry = min(by_expiry)
    expiry_group = by_expiry[expiry]
    strikes = sorted(expiry_group["strikes"])
    if not strikes:
        return None
    atm_index = min(
        range(len(strikes)),
        key=lambda index: (abs(strikes[index] - current_spot), strikes[index]),
    )
    target_index = atm_index + direction * steps
    if target_index < 0 or target_index >= len(strikes):
        return None
    target_strike = strikes[target_index]
    raw_target = expiry_group["products"].get((option_type, target_strike))
    if raw_target is None:
        return None

    symbol = raw_target["symbol"]
    matches = [
        contract
        for contract in executable_contracts
        if isinstance(contract, Mapping)
        and str(contract.get("symbol") or "").strip() == symbol
    ]
    if len(matches) != 1:
        return None
    contract = matches[0]
    try:
        product_id = _positive_integer(
            contract.get("product_id"), "executable_contract.product_id"
        )
        ask = _finite(contract.get("ask"), "executable_contract.ask")
        contract_strike = _finite(
            contract.get("strike"), "executable_contract.strike"
        )
        contract_expiry = _utc_time(
            contract.get("expiry"), "executable_contract.expiry"
        )
        order_limit_value = contract.get("max_order_lots")
        if order_limit_value in (None, ""):
            order_limit_value = contract.get("position_size_limit")
        order_limit = _positive_integer(
            order_limit_value, "executable_contract.max_order_lots"
        )
    except TrendScoreAutoInputError:
        return None
    if (
        product_id != raw_target["product_id"]
        or ask <= 0
        or contract_strike != target_strike
        or contract_expiry != expiry
        or str(contract.get("option_type") or "").strip().upper() != option_type
        or order_limit < AUTO_TRADE_LOTS
    ):
        return None
    status = str(contract.get("trading_status") or "operational").strip().lower()
    if status != "operational":
        return None
    try:
        lot_size = _positive_integer(
            contract.get("lot_size", 1), "executable_contract.lot_size"
        )
    except TrendScoreAutoInputError:
        return None
    if AUTO_TRADE_LOTS % lot_size:
        return None

    return {
        "zone": zone,
        "instrument": "BTC_OPTION",
        "side": "buy",
        "option_type": option_type,
        "itm_steps": steps,
        "lots": AUTO_TRADE_LOTS,
        "symbol": symbol,
        "product_id": product_id,
        "spot": current_spot,
        "atm_strike": strikes[atm_index],
        "strike": target_strike,
        "expiry": _iso_utc(expiry),
        "time_to_expiry_hours": round(
            (expiry - current).total_seconds() / 3600.0, 8
        ),
        "entry_price": ask,
        "max_order_lots": order_limit,
        "raw_product": dict(raw_target["raw"]),
        "executable_contract": dict(contract),
    }


def select_move_contract(
    raw_products: Sequence[Mapping[str, Any]],
    *,
    spot: Any,
    now: datetime,
) -> dict[str, Any] | None:
    """Select the nearest-expiry ATM BTC MOVE contract for a 1,000-lot short.

    Eligibility depends only on the authoritative listing, exact settlement
    timestamp, and product limits.  There is deliberately no morning/evening
    session argument.  The current expiry remains eligible at exactly 90
    minutes and is skipped only below that floor; no maximum DTE is imposed.
    """

    current = _utc_time(now, "now")
    current_spot = _finite(spot, "spot")
    if current_spot <= 0:
        raise TrendScoreAutoInputError("spot must be positive")
    if not isinstance(raw_products, Sequence) or isinstance(
        raw_products, (str, bytes)
    ):
        raise TrendScoreAutoInputError("raw_products must be a list")

    by_expiry: dict[datetime, list[dict[str, Any]]] = {}
    for index, product in enumerate(raw_products):
        if not isinstance(product, Mapping):
            raise TrendScoreAutoInputError(
                f"raw_products[{index}] must be an object"
            )
        symbol = str(product.get("symbol") or "").strip()
        if not symbol.startswith("MV-BTC-"):
            continue
        if str(product.get("state") or "").strip().lower() != "live":
            continue
        if str(product.get("trading_status") or "").strip().lower() != "operational":
            continue
        underlying = product.get("underlying_asset")
        if isinstance(underlying, Mapping):
            underlying = underlying.get("symbol")
        if underlying not in (None, "", "BTC"):
            continue
        expiry = _utc_time(
            product.get("settlement_time"),
            f"raw_products[{index}].settlement_time",
        )
        strike = _finite(
            product.get("strike_price"),
            f"raw_products[{index}].strike_price",
        )
        if strike <= 0:
            raise TrendScoreAutoInputError(
                f"raw_products[{index}].strike_price must be positive"
            )
        if (expiry - current).total_seconds() < MIN_TIME_TO_EXPIRY_SECONDS:
            continue
        by_expiry.setdefault(expiry, []).append({
            "symbol": symbol,
            "strike": strike,
            "raw": dict(product),
        })
    if not by_expiry:
        return None

    expiry = min(by_expiry)
    candidates = sorted(
        by_expiry[expiry],
        key=lambda row: (
            abs(row["strike"] - current_spot),
            row["strike"],
            row["symbol"],
        ),
    )
    target = candidates[0]
    equally_exact = [
        row for row in candidates
        if row["strike"] == target["strike"]
    ]
    if len(equally_exact) != 1:
        # Two product identities at the chosen strike are ambiguous.  Do not
        # pick one merely because the API happened to return it first.
        return None
    product = target["raw"]
    try:
        product_id = _positive_integer(
            product.get("id"), "selected_move_product.id"
        )
        contract_value = _finite(
            product.get("contract_value"),
            "selected_move_product.contract_value",
        )
        position_limit = _positive_integer(
            product.get("position_size_limit"),
            "selected_move_product.position_size_limit",
        )
    except TrendScoreAutoInputError:
        return None
    if contract_value <= 0 or position_limit < AUTO_TRADE_LOTS:
        return None

    return {
        "zone": SHORT_MOVE,
        "instrument": "BTC_MOVE",
        "side": "sell",
        "lots": AUTO_TRADE_LOTS,
        "symbol": target["symbol"],
        "product_id": product_id,
        "spot": current_spot,
        "atm_strike": target["strike"],
        "strike": target["strike"],
        "expiry": _iso_utc(expiry),
        "time_to_expiry_hours": round(
            (expiry - current).total_seconds() / 3600.0, 8
        ),
        "contract_value": contract_value,
        "max_order_lots": position_limit,
        "raw_product": dict(product),
    }


def position_score_zone(position: Mapping[str, Any]) -> str:
    """Classify one controller-owned position without guessing its side."""

    if not isinstance(position, Mapping):
        raise TrendScoreAutoInputError("owned position must be an object")
    persisted = str(position.get("trend_score_zone") or "").strip().upper()
    if persisted:
        if persisted not in SCORE_ZONES:
            raise TrendScoreAutoInputError(
                "owned position has an invalid trend_score_zone"
            )
        return persisted
    symbol = str(position.get("symbol") or "").strip()
    side = str(position.get("side") or "").strip().lower()
    if symbol.startswith("C-BTC-") and side in {"long", "buy"}:
        return CE_2_ITM
    if symbol.startswith("P-BTC-") and side in {"long", "buy"}:
        return PE_3_ITM
    if symbol.startswith("MV-BTC-") and side in {"short", "sell"}:
        return SHORT_MOVE
    raise TrendScoreAutoInputError(
        "owned position cannot be mapped to an approved score zone"
    )


def plan_score_transition(
    *,
    score: Any,
    signal_key: str,
    owned_positions: Sequence[Mapping[str, Any]],
    consumed_signal_keys: Collection[str] | Mapping[str, Any] = (),
) -> dict[str, Any]:
    """Plan one idempotent transition for zero or one owned position.

    This function only describes the mutation.  Its caller must execute
    ``CLOSE_THEN_OPEN`` under the account/state locks and persist the signal
    ledger atomically with the resulting state.
    """

    target = score_zone(score)
    key = str(signal_key or "").strip()
    if not key:
        raise TrendScoreAutoInputError("signal_key is required")
    if not isinstance(owned_positions, Sequence) or isinstance(
        owned_positions, (str, bytes)
    ):
        raise TrendScoreAutoInputError("owned_positions must be a list")
    if len(owned_positions) > 1:
        raise TrendScoreAutoInputError(
            "score automation can manage at most one owned position"
        )
    try:
        already_consumed = key in consumed_signal_keys
    except TypeError as exc:
        raise TrendScoreAutoInputError(
            "consumed_signal_keys must support membership checks"
        ) from exc
    if already_consumed:
        return {
            "action": "NOOP",
            "reason": "SIGNAL_ALREADY_CONSUMED",
            "signal_key": key,
            "target_zone": target,
            "current_zone": (
                position_score_zone(owned_positions[0])
                if owned_positions else None
            ),
            "close_position": None,
            "open_zone": None,
            "consume_signal": False,
        }

    if not owned_positions:
        return {
            "action": "OPEN",
            "reason": "NO_OWNED_POSITION",
            "signal_key": key,
            "target_zone": target,
            "current_zone": None,
            "close_position": None,
            "open_zone": target,
            "consume_signal": True,
        }

    position = owned_positions[0]
    current = position_score_zone(position)
    if current == target:
        return {
            "action": "HOLD",
            "reason": "POSITION_ALREADY_MATCHES_SCORE_ZONE",
            "signal_key": key,
            "target_zone": target,
            "current_zone": current,
            "close_position": None,
            "open_zone": None,
            "consume_signal": True,
        }
    return {
        "action": "CLOSE_THEN_OPEN",
        "reason": "SCORE_ZONE_CHANGED",
        "signal_key": key,
        "target_zone": target,
        "current_zone": current,
        "close_position": dict(position),
        "open_zone": target,
        "consume_signal": True,
    }


__all__ = [
    "AUTO_TRADE_LOTS",
    "CE_2_ITM",
    "MIN_TIME_TO_EXPIRY_SECONDS",
    "PE_3_ITM",
    "SCORE_ZONES",
    "SHORT_MOVE",
    "TrendScoreAutoInputError",
    "completed_candle_signal_key",
    "plan_score_transition",
    "position_score_zone",
    "score_zone",
    "select_directional_option",
    "select_move_contract",
]
