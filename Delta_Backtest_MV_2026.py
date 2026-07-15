"""After-cost, walk-forward backtest for the production BTC MOVE strategy.

The bot has two independent scheduled variants:

* morning: enter today's fixed-strike MOVE contract and normally exit before
  its 12:00 UTC settlement;
* evening: enter tomorrow's contract shortly after its strike is fixed and
  normally exit later that day.

At entry, the production value gate estimates the remaining absolute payoff
from current strike displacement, 15-minute realised volatility and ATR. Long
entries require forecast payoff to exceed ask premium; short entries require
bid premium to exceed forecast payoff. This module mirrors that calculation.

Historical MOVE quotes can be supplied with ``--quotes-csv``. Missing quotes
use a prominently labelled Black-Scholes straddle approximation. Synthetic
pricing is suitable for screening and code validation, not proof of an
executable trading edge.
"""

from __future__ import annotations

import argparse
import math
import os
import statistics
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------------------------
# Legacy-compatible public constants
# ---------------------------------------------------------------------------
BASE_URL = os.getenv("BASE_URL", "https://api.india.delta.exchange")
RESOLUTION = "15m"
RES_SECONDS = 900
SYMBOL = os.getenv("PERPETUAL_SYMBOL", "BTCUSD")
BATCH_SIZE = 500

LOTS = int(os.getenv("STRADDLE_LOTS", "1000"))
CONTRACT_BTC = float(os.getenv("MOVE_CONTRACT_VALUE", "0.001"))
MARKET_IV = float(os.getenv("BACKTEST_MOVE_IV", "0.18"))
STRIKE_STEP = int(os.getenv("MOVE_STRIKE_STEP", "200"))
ENTRY_UTC_H = int(os.getenv("ENTRY_H_UTC", "12"))
EXIT_UTC_H = int(os.getenv("EXIT_H_UTC", "19"))
T_ENTRY_H = 24.0
T_EXIT_H = 16.5
START_DATE = date(2026, 1, 1)
END_DATE = date(2026, 7, 3)
OUT_CSV = Path(os.getenv(
    "BACKTEST_MOVE_OUTPUT_CSV",
    str(Path(__file__).with_name("backtest_mv_2026_daywise.csv")),
))


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _side(value: str) -> str:
    return "sell" if str(value).strip().lower() == "sell" else "buy"


@dataclass(frozen=True)
class SlotConfig:
    name: str
    enabled: bool
    side: str
    entry_hour: int
    entry_minute: int
    exit_hour: int
    exit_minute: int
    settlement_day_offset: int
    configured_lots: int
    risk_budget_usd: float
    stop_loss_usd: float
    scheduled_exit_enabled: bool = True

    def validate(self) -> None:
        if self.name not in {"morning", "evening"}:
            raise ValueError("MOVE slot must be morning or evening")
        if self.side not in {"buy", "sell"}:
            raise ValueError("MOVE side must be buy or sell")
        if not 0 <= self.entry_hour <= 23 or not 0 <= self.exit_hour <= 23:
            raise ValueError("slot hour must be in 0..23")
        if not 0 <= self.entry_minute <= 59 or not 0 <= self.exit_minute <= 59:
            raise ValueError("slot minute must be in 0..59")
        if self.configured_lots < 0 or self.risk_budget_usd < 0 or self.stop_loss_usd < 0:
            raise ValueError("lot and risk settings cannot be negative")


def _default_morning() -> SlotConfig:
    """Stable research defaults; ``from_env`` applies account settings."""
    return SlotConfig(
        name="morning",
        enabled=True,
        side="buy",
        entry_hour=0,
        entry_minute=15,
        exit_hour=11,
        exit_minute=30,
        settlement_day_offset=0,
        configured_lots=2_000,
        risk_budget_usd=200.0,
        stop_loss_usd=0.0,
        scheduled_exit_enabled=True,
    )


def _morning_from_env() -> SlotConfig:
    return SlotConfig(
        name="morning",
        enabled=_env_bool("MORNING_ENABLED", True),
        side=_side(os.getenv("MORNING_SIDE", "buy")),
        entry_hour=int(os.getenv("MORNING_H_UTC", "0")),
        entry_minute=int(os.getenv("MORNING_M_UTC", "15")),
        exit_hour=int(os.getenv("MORNING_EXIT_H_UTC", "11")),
        exit_minute=int(os.getenv("MORNING_EXIT_M_UTC", "30")),
        settlement_day_offset=0,
        configured_lots=int(os.getenv("MORNING_LOTS", "2000")),
        risk_budget_usd=float(os.getenv("RISK_PER_TRADE_USD_MORNING", "200")),
        stop_loss_usd=abs(float(os.getenv("SL_TARGET_PNL_MORNING", "0") or 0)),
        scheduled_exit_enabled=_env_bool("MORNING_EXIT_ENABLED", True),
    )


def _default_evening() -> SlotConfig:
    return SlotConfig(
        name="evening",
        enabled=True,
        side="buy",
        entry_hour=12,
        entry_minute=5,
        exit_hour=19,
        exit_minute=30,
        settlement_day_offset=1,
        configured_lots=1_000,
        risk_budget_usd=200.0,
        stop_loss_usd=0.0,
        scheduled_exit_enabled=True,
    )


def _evening_from_env() -> SlotConfig:
    return SlotConfig(
        name="evening",
        enabled=_env_bool("EVENING_ENABLED", True),
        side=_side(os.getenv("EVENING_SIDE", "buy")),
        entry_hour=int(os.getenv("ENTRY_H_UTC", "12")),
        entry_minute=int(os.getenv("ENTRY_M_UTC", "5")),
        exit_hour=int(os.getenv("EXIT_H_UTC", "19")),
        exit_minute=int(os.getenv("EXIT_M_UTC", "30")),
        settlement_day_offset=1,
        configured_lots=int(os.getenv("STRADDLE_LOTS", "1000")),
        risk_budget_usd=float(os.getenv("RISK_PER_TRADE_USD_EVENING", "200")),
        stop_loss_usd=abs(float(os.getenv("SL_TARGET_PNL", "0") or 0)),
        scheduled_exit_enabled=_env_bool("EVENING_EXIT_ENABLED", True),
    )


@dataclass(frozen=True)
class MoveBacktestConfig:
    morning: SlotConfig = field(default_factory=_default_morning)
    evening: SlotConfig = field(default_factory=_default_evening)

    contract_value: float = CONTRACT_BTC
    strike_step: float = float(STRIKE_STEP)
    market_iv: float = MARKET_IV
    vol_lookback: int = 96
    value_filter_enabled: bool = True
    min_edge_pct: float = 5.0
    long_min_edge_pct: float | None = None
    short_min_edge_pct: float | None = None
    min_tte_minutes: int = 90
    max_tte_hours: float = 30.0

    starting_balance_usd: float = 1_000.0
    balance_buffer_pct: float = 2.0
    max_order_lots: int = 5_000
    allow_short_move: bool = False
    short_max_risk_usd: float = 0.0

    max_spread_pct: float = 3.0
    slippage_pct: float = 1.0
    min_book_depth_multiple: float = 1.0
    fallback_spread_pct: float = 2.0
    fallback_liquidity_lots: int = 5_000
    max_quote_age_seconds: int = 900
    option_fee_rate: float = 0.00010
    option_fee_cap_pct: float = 0.035

    walk_forward_folds: int = 3
    walk_forward_train_fraction: float = 0.50
    validation_min_trades: int = 100
    validation_min_profit_factor: float = 1.20
    validation_max_drawdown_usd: float = 500.0

    @classmethod
    def from_env(cls) -> "MoveBacktestConfig":
        cfg = cls(
            morning=_morning_from_env(),
            evening=_evening_from_env(),
            contract_value=float(os.getenv("MOVE_CONTRACT_VALUE", "0.001")),
            strike_step=float(os.getenv("MOVE_STRIKE_STEP", "200")),
            market_iv=float(os.getenv("BACKTEST_MOVE_IV", "0.18")),
            vol_lookback=max(int(os.getenv("MOVE_VOL_LOOKBACK", "96")), 30),
            value_filter_enabled=_env_bool("MOVE_VALUE_FILTER_ENABLED", True),
            min_edge_pct=float(os.getenv("MOVE_MIN_EDGE_PCT", "5.0")),
            long_min_edge_pct=float(
                os.getenv("MOVE_MIN_EDGE_PCT_LONG", os.getenv("MOVE_MIN_EDGE_PCT", "5.0"))
            ),
            short_min_edge_pct=float(
                os.getenv("MOVE_MIN_EDGE_PCT_SHORT", os.getenv("MOVE_MIN_EDGE_PCT", "5.0"))
            ),
            min_tte_minutes=int(os.getenv("MOVE_MIN_TTE_MINUTES", "90")),
            max_tte_hours=float(os.getenv("MOVE_MAX_TTE_HOURS", "30")),
            starting_balance_usd=float(os.getenv("BACKTEST_STARTING_BALANCE_USD", "1000")),
            balance_buffer_pct=float(os.getenv("BACKTEST_BALANCE_BUFFER_PCT", "2")),
            max_order_lots=int(os.getenv("MAX_ORDER_LOTS", "5000")),
            allow_short_move=_env_bool("ALLOW_SHORT_MOVE", False),
            short_max_risk_usd=float(os.getenv("SHORT_MAX_RISK_USD", "0")),
            max_spread_pct=float(os.getenv("MAX_SPREAD_PCT", "3.0")),
            slippage_pct=float(os.getenv("MAX_SLIPPAGE_PCT", "1.0")),
            min_book_depth_multiple=float(os.getenv("MIN_BOOK_DEPTH_MULTIPLE", "1.0")),
            fallback_spread_pct=float(os.getenv("BACKTEST_MOVE_FALLBACK_SPREAD_PCT", "2.0")),
            fallback_liquidity_lots=int(os.getenv("BACKTEST_MOVE_FALLBACK_DEPTH_LOTS", "5000")),
            max_quote_age_seconds=int(os.getenv("BACKTEST_MOVE_QUOTE_MAX_AGE", "900")),
            option_fee_rate=float(os.getenv("OPTION_FEE_RATE", "0.00010")),
            option_fee_cap_pct=float(os.getenv("OPTION_FEE_CAP_PCT", "0.035")),
            walk_forward_folds=int(os.getenv("BACKTEST_WF_FOLDS", "3")),
            walk_forward_train_fraction=float(os.getenv("BACKTEST_WF_TRAIN_FRACTION", "0.50")),
            validation_min_trades=int(os.getenv("BACKTEST_MIN_TRADES", "100")),
            validation_min_profit_factor=float(os.getenv("BACKTEST_MIN_PROFIT_FACTOR", "1.20")),
            validation_max_drawdown_usd=float(os.getenv("BACKTEST_MAX_DRAWDOWN_USD", "500")),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        self.morning.validate()
        self.evening.validate()
        if self.contract_value <= 0 or self.strike_step <= 0 or self.market_iv <= 0:
            raise ValueError("contract value, strike step and fallback IV must be positive")
        if self.vol_lookback < 30:
            raise ValueError("MOVE vol lookback must be at least 30 bars")
        if self.max_order_lots <= 0 or self.starting_balance_usd <= 0:
            raise ValueError("order cap and starting balance must be positive")
        if not 0 <= self.balance_buffer_pct < 100:
            raise ValueError("balance buffer must be in [0, 100)")
        if not 0 <= self.max_spread_pct < 100 or not 0 <= self.slippage_pct < 100:
            raise ValueError("spread and slippage percentages must be in [0, 100)")
        if not 0 < self.walk_forward_train_fraction < 1:
            raise ValueError("walk-forward train fraction must be between 0 and 1")

    @property
    def slots(self) -> tuple[SlotConfig, SlotConfig]:
        return self.morning, self.evening


# ---------------------------------------------------------------------------
# Pricing helpers
# ---------------------------------------------------------------------------
def _ncdf(x: float) -> float:
    z = abs(x)
    t = 1.0 / (1.0 + 0.2316419 * z)
    poly = t * (
        0.319381530
        + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429)))
    )
    pdf = math.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)
    n = 1.0 - pdf * poly
    return n if x >= 0 else 1.0 - n


def bs_straddle(spot: float, strike: float, time_hours: float, sigma: float) -> float:
    """Call-plus-put value in USD per BTC, used only as a labelled fallback."""
    if time_hours <= 0:
        return abs(spot - strike)
    years = time_hours / 8760.0
    volatility = max(sigma, 1e-6)
    sigma_root_t = volatility * math.sqrt(years)
    if spot <= 0 or strike <= 0 or sigma_root_t <= 0:
        return 0.0
    d1 = (math.log(spot / strike) + 0.5 * volatility**2 * years) / sigma_root_t
    d2 = d1 - sigma_root_t
    call = spot * _ncdf(d1) - strike * _ncdf(d2)
    put = strike * _ncdf(-d2) - spot * _ncdf(-d1)
    return call + put


def atm_strike(btc_price: float, step: int | float) -> float:
    return round(btc_price / step) * step


def calculate_option_fee(
    spot: float, premium: float, lots: int, config: MoveBacktestConfig
) -> float:
    per_btc = min(
        config.option_fee_rate * abs(spot),
        config.option_fee_cap_pct * abs(premium),
    )
    return per_btc * config.contract_value * lots


@dataclass
class MoveQuote:
    timestamp: int
    symbol: str
    strike: float
    expiry_timestamp: int
    bid: float
    ask: float
    mark: float
    bid_depth_lots: float
    ask_depth_lots: float
    source: str
    mark_iv: float | None = None

    @property
    def spread_pct(self) -> float:
        midpoint = (self.bid + self.ask) / 2.0
        return (self.ask - self.bid) / midpoint * 100.0 if midpoint > 0 else float("inf")


def _timestamps_to_seconds(values: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    output = numeric.copy()
    parse_mask = numeric.isna() & values.notna()
    if parse_mask.any():
        parsed = pd.to_datetime(values[parse_mask], utc=True, errors="coerce")
        output.loc[parse_mask] = parsed.map(
            lambda value: value.timestamp() if pd.notna(value) else np.nan
        )
    for _ in range(3):
        output = output.where(output.abs() < 10_000_000_000, output / 1_000)
    return output


def _column(data: pd.DataFrame, aliases: tuple[str, ...], default: Any = np.nan) -> pd.Series:
    for alias in aliases:
        if alias in data:
            return data[alias]
    return pd.Series(default, index=data.index)


def normalise_move_quotes(quotes: pd.DataFrame) -> pd.DataFrame:
    source = quotes.copy()
    expiry = _column(source, ("expiry_timestamp", "expiry", "settlement_time"))
    result = pd.DataFrame(
        {
            "timestamp": _timestamps_to_seconds(_column(source, ("timestamp", "time"))),
            "slot": _column(source, ("slot",), "").fillna("").astype(str).str.lower(),
            "symbol": _column(source, ("symbol", "product_symbol"), "").fillna("").astype(str),
            "strike": pd.to_numeric(_column(source, ("strike", "strike_price")), errors="coerce"),
            "expiry_timestamp": _timestamps_to_seconds(expiry),
            "bid": pd.to_numeric(_column(source, ("bid", "best_bid", "bid_price")), errors="coerce"),
            "ask": pd.to_numeric(_column(source, ("ask", "best_ask", "ask_price")), errors="coerce"),
            "mark": pd.to_numeric(_column(source, ("mark", "mark_price", "close", "price")), errors="coerce"),
            "bid_depth_lots": pd.to_numeric(
                _column(source, ("bid_depth_lots", "buy_depth_lots", "bid_size", "best_bid_size", "depth_lots")),
                errors="coerce",
            ),
            "ask_depth_lots": pd.to_numeric(
                _column(source, ("ask_depth_lots", "sell_depth_lots", "ask_size", "best_ask_size", "depth_lots")),
                errors="coerce",
            ),
            "mark_iv": pd.to_numeric(
                _column(source, ("mark_iv", "iv", "implied_volatility")), errors="coerce"
            ),
        }
    )
    midpoint = (result["bid"] + result["ask"]) / 2.0
    result["mark"] = result["mark"].fillna(midpoint)
    result.dropna(
        subset=["timestamp", "strike", "expiry_timestamp", "bid", "ask", "mark"],
        inplace=True,
    )
    result = result.loc[
        (result["strike"] > 0)
        & (result["expiry_timestamp"] > result["timestamp"])
        & (result["bid"] > 0)
        & (result["ask"] >= result["bid"])
        & (result["mark"] > 0)
    ].copy()
    for name in ("timestamp", "expiry_timestamp"):
        result[name] = result[name].astype("int64")
    result.sort_values(["timestamp", "symbol"], inplace=True)
    result.reset_index(drop=True, inplace=True)
    return result


class MoveQuoteBook:
    """Point-in-time MOVE quote lookup with entry-gate diagnostics."""

    def __init__(self, quotes: pd.DataFrame, config: MoveBacktestConfig):
        self.data = normalise_move_quotes(quotes)
        self.config = config
        self.last_had_candidate = False
        self.last_rejection_reason = ""

    def _target_candidates(
        self, timestamp: int, expiry_timestamp: int, slot: str
    ) -> pd.DataFrame:
        candidates = self.data.loc[
            (self.data["timestamp"] <= timestamp)
            & (self.data["timestamp"] >= timestamp - self.config.max_quote_age_seconds)
            & (self.data["expiry_timestamp"] == expiry_timestamp)
        ].copy()
        if (candidates["slot"] != "").any():
            candidates = candidates.loc[(candidates["slot"] == "") | (candidates["slot"] == slot)]
        if candidates.empty:
            return candidates
        keys = ["symbol"] if (candidates["symbol"] != "").any() else ["strike", "expiry_timestamp"]
        candidates.sort_values("timestamp", inplace=True)
        return candidates.groupby(keys, dropna=False).tail(1)

    def entry_quote(
        self, timestamp: int, expiry_timestamp: int, slot: str, side: str
    ) -> MoveQuote | None:
        self.last_had_candidate = False
        self.last_rejection_reason = ""
        candidates = self._target_candidates(timestamp, expiry_timestamp, slot)
        if candidates.empty:
            return None
        self.last_had_candidate = True
        midpoint = (candidates["bid"] + candidates["ask"]) / 2.0
        candidates["spread_pct"] = (candidates["ask"] - candidates["bid"]) / midpoint * 100.0
        candidates = candidates.loc[candidates["spread_pct"] <= self.config.max_spread_pct]
        if candidates.empty:
            self.last_rejection_reason = "spread gate rejected available MOVE quote"
            return None
        depth_column = "ask_depth_lots" if side == "buy" else "bid_depth_lots"
        candidates = candidates.loc[candidates[depth_column].fillna(0) > 0]
        if candidates.empty:
            self.last_rejection_reason = "executable depth is unavailable for MOVE quote"
            return None
        # A daily MOVE expiry should normally have one product. If duplicate
        # data exists, use the freshest and tightest executable quote.
        candidates.sort_values(["timestamp", "spread_pct"], ascending=[False, True], inplace=True)
        return self._to_quote(candidates.iloc[0])

    def contract_quote(self, timestamp: int, contract: MoveQuote) -> MoveQuote | None:
        recent = self.data.loc[
            (self.data["timestamp"] <= timestamp)
            & (self.data["timestamp"] >= timestamp - self.config.max_quote_age_seconds)
        ]
        if contract.symbol:
            recent = recent.loc[recent["symbol"] == contract.symbol]
        else:
            recent = recent.loc[
                (recent["expiry_timestamp"] == contract.expiry_timestamp)
                & np.isclose(recent["strike"], contract.strike)
            ]
        if recent.empty:
            return None
        return self._to_quote(recent.iloc[-1])

    @staticmethod
    def _to_quote(row: pd.Series) -> MoveQuote:
        return MoveQuote(
            timestamp=int(row["timestamp"]),
            symbol=str(row["symbol"]),
            strike=float(row["strike"]),
            expiry_timestamp=int(row["expiry_timestamp"]),
            bid=float(row["bid"]),
            ask=float(row["ask"]),
            mark=float(row["mark"]),
            bid_depth_lots=float(row["bid_depth_lots"]) if pd.notna(row["bid_depth_lots"]) else 0.0,
            ask_depth_lots=float(row["ask_depth_lots"]) if pd.notna(row["ask_depth_lots"]) else 0.0,
            source="move_option_quotes",
            mark_iv=float(row["mark_iv"]) if pd.notna(row["mark_iv"]) else None,
        )


def _synthetic_quote(
    timestamp: int,
    spot: float,
    strike: float,
    expiry_timestamp: int,
    sigma: float,
    config: MoveBacktestConfig,
) -> MoveQuote:
    hours = max(expiry_timestamp - timestamp, 0) / 3600.0
    mark = max(bs_straddle(spot, strike, hours, sigma), 0.0001)
    half_spread = config.fallback_spread_pct / 200.0
    return MoveQuote(
        timestamp=timestamp,
        symbol=f"MV-BTC-{int(strike)}-{datetime.fromtimestamp(expiry_timestamp, timezone.utc):%d%m%y}-SYNTH",
        strike=strike,
        expiry_timestamp=expiry_timestamp,
        bid=max(mark * (1.0 - half_spread), 0.0001),
        ask=mark * (1.0 + half_spread),
        mark=mark,
        bid_depth_lots=float(config.fallback_liquidity_lots),
        ask_depth_lots=float(config.fallback_liquidity_lots),
        source="black_scholes_fallback",
        mark_iv=sigma,
    )


# ---------------------------------------------------------------------------
# Candle data and point-in-time value forecast
# ---------------------------------------------------------------------------
def fetch_candles(
    symbol: str, resolution: str, start_ts: int, end_ts: int
) -> list[dict[str, Any]]:
    """Fetch all requested candles in bounded pages."""
    seconds = 900 if resolution == "15m" else 3600 if resolution == "1h" else 300
    rows: list[dict[str, Any]] = []
    cursor = start_ts
    while cursor < end_ts:
        batch_end = min(cursor + seconds * BATCH_SIZE, end_ts)
        for attempt in range(4):
            try:
                response = requests.get(
                    f"{BASE_URL}/v2/history/candles",
                    params={
                        "symbol": symbol,
                        "resolution": resolution,
                        "start": cursor,
                        "end": batch_end,
                    },
                    timeout=(5, 30),
                )
                response.raise_for_status()
                rows.extend(response.json().get("result") or [])
                break
            except Exception as exc:
                if attempt == 3:
                    raise
                wait_seconds = 2**attempt
                print(f"  Retry {attempt + 1}/3 after {wait_seconds}s - {exc}")
                time.sleep(wait_seconds)
        cursor = batch_end + seconds
        time.sleep(0.2)
    return rows


def normalise_candles(candles: pd.DataFrame | list[dict[str, Any]]) -> pd.DataFrame:
    data = pd.DataFrame(candles).copy()
    if "timestamp" not in data and "time" in data:
        data.rename(columns={"time": "timestamp"}, inplace=True)
    if not {"timestamp", "close"}.issubset(data.columns):
        raise ValueError("candle data requires timestamp/time and close")
    data["timestamp"] = _timestamps_to_seconds(data["timestamp"])
    for name in ("open", "high", "low", "close", "volume"):
        if name not in data:
            data[name] = data["close"] if name != "volume" else 0.0
        data[name] = pd.to_numeric(data[name], errors="coerce")
    data.dropna(subset=["timestamp", "close"], inplace=True)
    data["timestamp"] = data["timestamp"].astype("int64")
    data.sort_values("timestamp", inplace=True)
    data.drop_duplicates("timestamp", keep="last", inplace=True)
    data.reset_index(drop=True, inplace=True)
    if len(data) > 1:
        median_step = float(data["timestamp"].diff().dropna().median())
        if median_step > RES_SECONDS:
            raise ValueError("MOVE backtesting requires 15-minute or finer candles")
        if median_step < RES_SECONDS:
            index = pd.to_datetime(data["timestamp"], unit="s", utc=True)
            indexed = data.set_axis(index)
            data = indexed.resample("15min", label="left", closed="left").agg(
                open=("open", "first"),
                high=("high", "max"),
                low=("low", "min"),
                close=("close", "last"),
                volume=("volume", "sum"),
                child_count=("close", "count"),
            )
            data = data.loc[data["child_count"] > 0].copy()
            data["timestamp"] = data.index.as_unit("ns").asi8 // 1_000_000_000
            data.reset_index(drop=True, inplace=True)
    return data


def build_hourly_index(candles: list[dict]) -> dict[int, float]:
    """Legacy helper retained for existing notebooks."""
    index: dict[int, float] = {}
    for candle in candles:
        timestamp = int(candle.get("time") or candle.get("timestamp") or 0)
        opening = float(candle.get("open") or 0)
        if timestamp > 0 and opening > 0:
            index[timestamp] = opening
    return index


def get_hour_price(index: dict[int, float], when: datetime) -> float | None:
    timestamp = int(when.replace(minute=0, second=0, microsecond=0).timestamp())
    return index.get(timestamp)


def _spot_at(data: pd.DataFrame, timestamp: int) -> float | None:
    bucket = (timestamp // RES_SECONDS) * RES_SECONDS
    row = data.loc[data["timestamp"] == bucket]
    if row.empty:
        return None
    value = float(row.iloc[-1]["open"])
    return value if value > 0 else None


def expected_absolute_normal(displacement: float, sigma_usd: float) -> float:
    """E|N(displacement, sigma_usd)|."""
    if sigma_usd <= 0:
        return abs(displacement)
    z = displacement / sigma_usd
    return (
        sigma_usd * math.sqrt(2.0 / math.pi) * math.exp(-0.5 * z * z)
        + displacement * math.erf(z / math.sqrt(2.0))
    )


def move_value_signal(
    completed_candles: pd.DataFrame,
    current_spot: float,
    strike: float,
    tte_seconds: float,
    executable_premium: float,
    side: str,
    config: MoveBacktestConfig,
) -> dict[str, float | bool]:
    """Pure implementation of the production realised-vol/ATR value gate."""
    if tte_seconds < config.min_tte_minutes * 60:
        raise ValueError("MOVE expiry is below the configured minimum TTE")
    if tte_seconds > config.max_tte_hours * 3600:
        raise ValueError("MOVE expiry exceeds the configured maximum TTE")
    candles = completed_candles.tail(config.vol_lookback)
    if len(candles) < 30:
        raise ValueError("not enough completed candles for MOVE value filter")
    closes = pd.to_numeric(candles["close"], errors="coerce").dropna().tolist()
    highs = pd.to_numeric(candles["high"], errors="coerce").tolist()
    lows = pd.to_numeric(candles["low"], errors="coerce").tolist()
    returns = [math.log(end / start) for start, end in zip(closes, closes[1:]) if start > 0 and end > 0]
    if len(returns) < 20:
        raise ValueError("not enough valid returns for MOVE value filter")
    sigma_15m = statistics.stdev(returns)
    remaining_bars = max(tte_seconds / RES_SECONDS, 1.0)
    displacement = current_spot - strike
    future_sigma_usd = closes[-1] * sigma_15m * math.sqrt(remaining_bars)
    expected_rv = expected_absolute_normal(displacement, future_sigma_usd)

    true_ranges: list[float] = []
    for i in range(max(1, len(closes) - 14), len(closes)):
        previous = closes[i - 1]
        true_ranges.append(
            max(highs[i] - lows[i], abs(highs[i] - previous), abs(lows[i] - previous))
        )
    atr14 = statistics.mean(true_ranges)
    expected_atr = math.sqrt(
        displacement * displacement + (atr14 * math.sqrt(remaining_bars)) ** 2
    )
    forecast = (expected_rv + expected_atr) / 2.0
    raw_edge = forecast - executable_premium if side == "buy" else executable_premium - forecast
    edge_pct = raw_edge / executable_premium * 100.0 if executable_premium > 0 else -100.0
    threshold = (
        config.long_min_edge_pct
        if side == "buy" and config.long_min_edge_pct is not None
        else config.short_min_edge_pct
        if side == "sell" and config.short_min_edge_pct is not None
        else config.min_edge_pct
    )
    return {
        "forecast_abs_move": forecast,
        "forecast_rv_move": expected_rv,
        "forecast_atr_move": expected_atr,
        "atr14": atr14,
        "sigma_15m": sigma_15m,
        "current_displacement": displacement,
        "premium": executable_premium,
        "edge_usd": raw_edge,
        "edge_pct": edge_pct,
        "tte_minutes": tte_seconds / 60.0,
        "required_edge_pct": float(threshold),
        "passed": edge_pct >= threshold,
    }


# ---------------------------------------------------------------------------
# Sizing and replay
# ---------------------------------------------------------------------------
def risk_sized_lots(
    configured: int,
    affordable: int,
    liquidity_cap: int,
    max_order_lots: int,
    risk_budget_usd: float,
    stop_loss_usd: float,
    premium_per_lot: float,
    round_trip_fee_per_lot: float,
    slippage_per_lot: float,
    short: bool = False,
) -> int:
    """Mirror production ``risk_based_lots`` without runtime dependencies."""
    caps = [configured, affordable, liquidity_cap, max_order_lots]
    if any(int(cap) <= 0 for cap in caps) or risk_budget_usd <= 0:
        return 0
    if short and stop_loss_usd <= 0:
        return 0
    per_lot = (
        max(premium_per_lot, 0.0)
        + max(round_trip_fee_per_lot, 0.0)
        + max(slippage_per_lot, 0.0)
    )
    if per_lot <= 0 or stop_loss_usd > risk_budget_usd:
        return 0
    capital_cap = math.floor(risk_budget_usd / per_lot)
    return max(min(*(int(cap) for cap in caps), capital_cap), 0)


def _event_timestamp(day: date, hour: int, minute: int) -> int:
    return int(datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc).timestamp())


def _settlement_timestamp(day: date, offset: int) -> int:
    settlement_day = day + timedelta(days=offset)
    return _event_timestamp(settlement_day, 12, 0)


def _schedule(data: pd.DataFrame, config: MoveBacktestConfig) -> list[tuple[int, date, SlotConfig]]:
    if data.empty:
        return []
    first_day = datetime.fromtimestamp(int(data["timestamp"].min()), timezone.utc).date()
    last_day = datetime.fromtimestamp(int(data["timestamp"].max()), timezone.utc).date()
    events: list[tuple[int, date, SlotConfig]] = []
    current = first_day
    while current <= last_day:
        for slot in config.slots:
            if slot.enabled:
                entry = _event_timestamp(current, slot.entry_hour, slot.entry_minute)
                events.append((entry, current, slot))
        current += timedelta(days=1)
    return sorted(events, key=lambda event: event[0])


def run_backtest(
    candles: pd.DataFrame | list[dict[str, Any]] | None = None,
    config: MoveBacktestConfig | None = None,
    move_quotes: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Replay both scheduled MOVE slots using only point-in-time information."""
    cfg = config or MoveBacktestConfig.from_env()
    cfg.validate()
    if candles is None:
        start = int(datetime.combine(START_DATE - timedelta(days=2), datetime.min.time(), timezone.utc).timestamp())
        end = int(datetime.combine(END_DATE + timedelta(days=2), datetime.min.time(), timezone.utc).timestamp())
        candles = fetch_candles(SYMBOL, RESOLUTION, start, end)
    data = normalise_candles(candles)
    quote_book = (
        MoveQuoteBook(move_quotes, cfg)
        if move_quotes is not None and not move_quotes.empty
        else None
    )
    balance = cfg.starting_balance_usd
    records: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    def skip(day: date, slot: SlotConfig, reason: str) -> None:
        skipped.append({"date": day.isoformat(), "slot": slot.name, "reason": reason})

    for entry_timestamp, trading_day, slot in _schedule(data, cfg):
        settlement_timestamp = _settlement_timestamp(trading_day, slot.settlement_day_offset)
        tte_seconds = settlement_timestamp - entry_timestamp
        if tte_seconds < cfg.min_tte_minutes * 60 or tte_seconds > cfg.max_tte_hours * 3600:
            skip(trading_day, slot, "TTE_GATE")
            continue
        if slot.side == "sell" and not cfg.allow_short_move:
            skip(trading_day, slot, "SHORT_DISABLED")
            continue
        if slot.side == "sell" and cfg.short_max_risk_usd > 0 and slot.risk_budget_usd > cfg.short_max_risk_usd:
            skip(trading_day, slot, "SHORT_RISK_CAP")
            continue
        entry_spot = _spot_at(data, entry_timestamp)
        if entry_spot is None:
            skip(trading_day, slot, "ENTRY_SPOT_MISSING")
            continue

        entry_quote = (
            quote_book.entry_quote(entry_timestamp, settlement_timestamp, slot.name, slot.side)
            if quote_book
            else None
        )
        strike_source = "move_option_quote"
        if entry_quote is None and quote_book and quote_book.last_had_candidate:
            skip(trading_day, slot, quote_book.last_rejection_reason or "QUOTE_GATE")
            continue
        if entry_quote is None:
            inception_timestamp = settlement_timestamp - 24 * 3600
            inception_spot = _spot_at(data, inception_timestamp)
            if inception_spot is None:
                skip(trading_day, slot, "STRIKE_INCEPTION_SPOT_MISSING")
                continue
            strike = atm_strike(inception_spot, cfg.strike_step)
            strike_source = "rounded_inception_spot_fallback"
            entry_quote = _synthetic_quote(
                entry_timestamp,
                entry_spot,
                strike,
                settlement_timestamp,
                cfg.market_iv,
                cfg,
            )

        if entry_quote.spread_pct > cfg.max_spread_pct:
            skip(trading_day, slot, "SPREAD_GATE")
            continue

        entry_premium = entry_quote.ask if slot.side == "buy" else entry_quote.bid
        completed = data.loc[data["timestamp"] + RES_SECONDS <= entry_timestamp]
        try:
            value = move_value_signal(
                completed,
                entry_spot,
                entry_quote.strike,
                tte_seconds,
                entry_premium,
                slot.side,
                cfg,
            )
        except ValueError as exc:
            skip(trading_day, slot, f"VALUE_DATA: {exc}")
            continue
        if cfg.value_filter_enabled and not value["passed"]:
            skip(trading_day, slot, "VALUE_EDGE_GATE")
            continue

        one_lot_fee = calculate_option_fee(entry_spot, entry_premium, 1, cfg)
        one_lot_cost = entry_premium * cfg.contract_value + one_lot_fee
        spendable = balance * (1.0 - cfg.balance_buffer_pct / 100.0)
        affordable = math.floor(spendable / one_lot_cost) if one_lot_cost > 0 else 0
        side_depth = entry_quote.ask_depth_lots if slot.side == "buy" else entry_quote.bid_depth_lots
        liquidity_cap = math.floor(side_depth / max(cfg.min_book_depth_multiple, 0.01))
        slippage_per_lot = entry_premium * cfg.contract_value * cfg.slippage_pct / 100.0
        lots = risk_sized_lots(
            configured=slot.configured_lots,
            affordable=affordable,
            liquidity_cap=liquidity_cap,
            max_order_lots=cfg.max_order_lots,
            risk_budget_usd=slot.risk_budget_usd,
            stop_loss_usd=slot.stop_loss_usd,
            premium_per_lot=entry_premium * cfg.contract_value,
            round_trip_fee_per_lot=one_lot_fee * 2.0,
            slippage_per_lot=slippage_per_lot,
            short=slot.side == "sell",
        )
        if lots < 1:
            skip(trading_day, slot, "RISK_SIZING_ZERO")
            continue

        exit_timestamp = _event_timestamp(
            trading_day, slot.exit_hour, slot.exit_minute
        )
        if exit_timestamp <= entry_timestamp:
            exit_timestamp += 86400
        exit_reason = "SCHEDULED"
        if not slot.scheduled_exit_enabled or exit_timestamp > settlement_timestamp:
            exit_timestamp = settlement_timestamp
            exit_reason = "SETTLEMENT"
        exit_spot = _spot_at(data, exit_timestamp)
        if exit_spot is None:
            skip(trading_day, slot, "EXIT_SPOT_MISSING")
            continue

        exit_quote = None
        used_gap_fallback = False
        if exit_reason == "SETTLEMENT":
            intrinsic = abs(exit_spot - entry_quote.strike)
            exit_quote = MoveQuote(
                timestamp=exit_timestamp,
                symbol=entry_quote.symbol,
                strike=entry_quote.strike,
                expiry_timestamp=settlement_timestamp,
                bid=intrinsic,
                ask=intrinsic,
                mark=intrinsic,
                bid_depth_lots=float("inf"),
                ask_depth_lots=float("inf"),
                source="settlement_payoff",
                mark_iv=None,
            )
        elif quote_book and entry_quote.source == "move_option_quotes":
            exit_quote = quote_book.contract_quote(exit_timestamp, entry_quote)
        if exit_quote is None:
            fallback_iv = entry_quote.mark_iv or cfg.market_iv
            exit_quote = _synthetic_quote(
                exit_timestamp,
                exit_spot,
                entry_quote.strike,
                settlement_timestamp,
                fallback_iv,
                cfg,
            )
            used_gap_fallback = entry_quote.source == "move_option_quotes"

        slippage = cfg.slippage_pct / 100.0
        exit_slippage = 0.0 if exit_reason == "SETTLEMENT" else slippage
        if slot.side == "buy":
            entry_fill = entry_quote.ask * (1.0 + slippage)
            exit_fill = max(exit_quote.bid * (1.0 - exit_slippage), 0.0)
            gross_pnl = (exit_fill - entry_fill) * cfg.contract_value * lots
        else:
            entry_fill = entry_quote.bid * (1.0 - slippage)
            exit_fill = exit_quote.ask * (1.0 + exit_slippage)
            gross_pnl = (entry_fill - exit_fill) * cfg.contract_value * lots
        entry_fee = calculate_option_fee(entry_spot, entry_fill, lots, cfg)
        exit_fee = calculate_option_fee(exit_spot, exit_fill, lots, cfg)
        net_pnl = gross_pnl - entry_fee - exit_fee
        balance_before = balance
        balance += net_pnl
        pricing_source = entry_quote.source
        if used_gap_fallback:
            pricing_source = "option_quote_with_bs_gap_fallback"
        elif exit_reason == "SETTLEMENT":
            pricing_source = f"{entry_quote.source}+settlement_payoff"

        records.append(
            {
                "date": trading_day.isoformat(),
                "slot": slot.name,
                "side": slot.side,
                "entry_timestamp": entry_timestamp,
                "exit_timestamp": exit_timestamp,
                "entry_time": datetime.fromtimestamp(entry_timestamp, timezone.utc).isoformat(),
                "exit_time": datetime.fromtimestamp(exit_timestamp, timezone.utc).isoformat(),
                "exit_reason": exit_reason,
                "symbol": entry_quote.symbol,
                "strike": round(entry_quote.strike, 2),
                "strike_source": strike_source,
                "settlement_timestamp": settlement_timestamp,
                "tte_minutes": round(tte_seconds / 60.0, 1),
                "btc_entry": round(entry_spot, 2),
                "btc_exit": round(exit_spot, 2),
                "current_displacement": round(float(value["current_displacement"]), 2),
                "btc_move_pct": round((exit_spot - entry_spot) / entry_spot * 100.0, 4),
                "entry_bid": round(entry_quote.bid, 6),
                "entry_ask": round(entry_quote.ask, 6),
                "entry_spread_pct": round(entry_quote.spread_pct, 4),
                "entry_premium": round(entry_fill, 6),
                "exit_premium": round(exit_fill, 6),
                "entry_mark": round(entry_quote.mark, 6),
                "exit_mark": round(exit_quote.mark, 6),
                "configured_lots": slot.configured_lots,
                "affordable_lots": affordable,
                "liquidity_cap_lots": liquidity_cap,
                "lots": lots,
                "risk_budget_usd": slot.risk_budget_usd,
                "stop_loss_usd": slot.stop_loss_usd,
                "premium_at_risk_usd": round(entry_fill * cfg.contract_value * lots, 2),
                "forecast_abs_move": round(float(value["forecast_abs_move"]), 2),
                "forecast_rv_move": round(float(value["forecast_rv_move"]), 2),
                "forecast_atr_move": round(float(value["forecast_atr_move"]), 2),
                "atr14": round(float(value["atr14"]), 2),
                "sigma_15m": round(float(value["sigma_15m"]), 8),
                "edge_usd": round(float(value["edge_usd"]), 2),
                "edge_pct": round(float(value["edge_pct"]), 2),
                "required_edge_pct": round(float(value["required_edge_pct"]), 2),
                "gross_pnl_usd": round(gross_pnl, 2),
                "entry_fee_usd": round(entry_fee, 4),
                "exit_fee_usd": round(exit_fee, 4),
                "fees_usd": round(entry_fee + exit_fee, 4),
                "pnl_usd": round(net_pnl, 2),
                "balance_before_usd": round(balance_before, 2),
                "balance_after_usd": round(balance, 2),
                "pricing_source": pricing_source,
                "exit_pricing_source": exit_quote.source,
            }
        )

    trades = pd.DataFrame(records)
    trades.attrs["config"] = cfg
    trades.attrs["skipped"] = pd.DataFrame(skipped)
    trades.attrs["start_timestamp"] = int(data["timestamp"].min()) if not data.empty else None
    trades.attrs["end_timestamp"] = int(data["timestamp"].max() + RES_SECONDS) if not data.empty else None
    trades.attrs["ending_balance_usd"] = balance
    trades.attrs["pricing_warning"] = (
        "Black-Scholes MOVE fallback is theoretical and not an executable quote."
    )
    return trades


# ---------------------------------------------------------------------------
# OOS reports and release gates
# ---------------------------------------------------------------------------
def performance_metrics(trades: pd.DataFrame) -> dict[str, float | int]:
    if trades.empty or "pnl_usd" not in trades:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate_pct": 0.0,
            "net_pnl_usd": 0.0,
            "expectancy_usd": 0.0,
            "profit_factor": 0.0,
            "max_drawdown_usd": 0.0,
            "fees_usd": 0.0,
        }
    pnl = pd.to_numeric(trades["pnl_usd"], errors="coerce").fillna(0.0)
    gross_profit = float(pnl.loc[pnl > 0].sum())
    gross_loss = abs(float(pnl.loc[pnl < 0].sum()))
    profit_factor = (
        gross_profit / gross_loss
        if gross_loss > 0
        else float("inf") if gross_profit > 0 else 0.0
    )
    equity = pd.Series(np.r_[0.0, pnl.cumsum().to_numpy()])
    drawdown = equity.cummax() - equity
    return {
        "trades": int(len(pnl)),
        "wins": int((pnl > 0).sum()),
        "losses": int((pnl < 0).sum()),
        "win_rate_pct": float((pnl > 0).mean() * 100.0),
        "net_pnl_usd": float(pnl.sum()),
        "expectancy_usd": float(pnl.mean()),
        "profit_factor": float(profit_factor),
        "max_drawdown_usd": float(drawdown.max()),
        "fees_usd": float(pd.to_numeric(trades.get("fees_usd", 0), errors="coerce").fillna(0).sum()),
    }


def per_slot_metrics(trades: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for slot in ("morning", "evening"):
        subset = trades.loc[trades["slot"] == slot] if "slot" in trades else trades.iloc[:0]
        rows.append({"slot": slot, **performance_metrics(subset)})
    return pd.DataFrame(rows).set_index("slot")


def walk_forward_analysis(
    trades: pd.DataFrame,
    start_timestamp: int,
    end_timestamp: int,
    config: MoveBacktestConfig,
) -> pd.DataFrame:
    if config.walk_forward_folds <= 0 or end_timestamp <= start_timestamp:
        return pd.DataFrame()
    first_oos = start_timestamp + int(
        (end_timestamp - start_timestamp) * config.walk_forward_train_fraction
    )
    boundaries = np.linspace(
        first_oos, end_timestamp, config.walk_forward_folds + 1, dtype=np.int64
    )
    entry = pd.to_numeric(trades.get("entry_timestamp", pd.Series(dtype=float)), errors="coerce")
    rows = []
    for fold in range(config.walk_forward_folds):
        test_start, test_end = int(boundaries[fold]), int(boundaries[fold + 1])
        train = trades.loc[entry < test_start]
        test_mask = (entry >= test_start) & (
            entry <= test_end if fold == config.walk_forward_folds - 1 else entry < test_end
        )
        test = trades.loc[test_mask]
        train_metrics = performance_metrics(train)
        test_metrics = performance_metrics(test)
        rows.append(
            {
                "fold": fold + 1,
                "train_end": datetime.fromtimestamp(test_start, timezone.utc).isoformat(),
                "test_end": datetime.fromtimestamp(test_end, timezone.utc).isoformat(),
                **{f"train_{key}": value for key, value in train_metrics.items()},
                **{f"oos_{key}": value for key, value in test_metrics.items()},
            }
        )
    return pd.DataFrame(rows).set_index("fold")


def evaluate_validation_gates(
    metrics: dict[str, float | int], config: MoveBacktestConfig
) -> pd.DataFrame:
    checks = [
        ("minimum_trades", float(metrics["trades"]), float(config.validation_min_trades), ">="),
        (
            "profit_factor",
            float(metrics["profit_factor"]),
            float(config.validation_min_profit_factor),
            ">=",
        ),
        ("positive_expectancy", float(metrics["expectancy_usd"]), 0.0, ">"),
        (
            "maximum_drawdown",
            float(metrics["max_drawdown_usd"]),
            float(config.validation_max_drawdown_usd),
            "<=",
        ),
    ]
    rows = []
    for name, actual, threshold, operator in checks:
        passed = actual >= threshold if operator == ">=" else actual > threshold if operator == ">" else actual <= threshold
        rows.append(
            {
                "gate": name,
                "actual": actual,
                "operator": operator,
                "threshold": threshold,
                "passed": bool(passed),
            }
        )
    return pd.DataFrame(rows).set_index("gate")


def analyze_backtest(
    trades: pd.DataFrame, config: MoveBacktestConfig | None = None
) -> dict[str, Any]:
    cfg = config or trades.attrs.get("config") or MoveBacktestConfig.from_env()
    start = trades.attrs.get("start_timestamp")
    end = trades.attrs.get("end_timestamp")
    if start is None or end is None:
        timestamps = pd.to_numeric(trades.get("entry_timestamp", pd.Series(dtype=float)), errors="coerce")
        start = int(timestamps.min()) if not timestamps.empty else 0
        end = int(timestamps.max()) + 1 if not timestamps.empty else 1
    walk_forward = walk_forward_analysis(trades, int(start), int(end), cfg)
    oos_start = int(start + (end - start) * cfg.walk_forward_train_fraction)
    oos = (
        trades.loc[pd.to_numeric(trades["entry_timestamp"], errors="coerce") >= oos_start]
        if "entry_timestamp" in trades and cfg.walk_forward_folds > 0
        else trades
    )
    evaluation = performance_metrics(oos)
    return {
        "overall": performance_metrics(trades),
        "per_slot": per_slot_metrics(trades),
        "walk_forward": walk_forward,
        "evaluation_scope": "out_of_sample" if cfg.walk_forward_folds > 0 else "full_period",
        "evaluation": evaluation,
        "gates": evaluate_validation_gates(evaluation, cfg),
    }


def print_report(
    trades: pd.DataFrame,
    config: MoveBacktestConfig | None = None,
    output_csv: str | Path | None = OUT_CSV,
) -> None:
    cfg = config or trades.attrs.get("config") or MoveBacktestConfig.from_env()
    analysis = analyze_backtest(trades, cfg)
    metrics = analysis["overall"]
    skipped = trades.attrs.get("skipped", pd.DataFrame())

    print("\n" + "=" * 78)
    print("  BTC MOVE BACKTEST | MORNING + EVENING | AFTER-COST")
    print("=" * 78)
    print(
        f"  Value gate      : realised-vol/ATR expected |move|, long/short edge >= "
        f"{(cfg.long_min_edge_pct if cfg.long_min_edge_pct is not None else cfg.min_edge_pct):g}%/"
        f"{(cfg.short_min_edge_pct if cfg.short_min_edge_pct is not None else cfg.min_edge_pct):g}% "
        f"({'enabled' if cfg.value_filter_enabled else 'disabled'})"
    )
    print(
        f"  Execution       : max spread {cfg.max_spread_pct:g}%, slippage {cfg.slippage_pct:g}%, "
        f"fee rate/cap {cfg.option_fee_rate:.4%}/{cfg.option_fee_cap_pct:.2%}"
    )
    print("  Strike model    : fixed at the contract's 12:00 UTC inception")
    if not trades.empty:
        sources = ", ".join(
            f"{name}={count}" for name, count in trades["pricing_source"].value_counts().items()
        )
        print(f"  Pricing sources : {sources}")
    print("  WARNING         : Black-Scholes fallback is not executable market evidence.")
    print()
    print(f"  Trades          : {metrics['trades']} (skipped {len(skipped)})")
    print(f"  Win rate        : {metrics['win_rate_pct']:.1f}%")
    print(f"  Net P&L         : ${metrics['net_pnl_usd']:,.2f}")
    print(f"  Fees            : ${metrics['fees_usd']:,.2f}")
    print(f"  Expectancy      : ${metrics['expectancy_usd']:,.2f} / trade")
    print(f"  Profit factor   : {metrics['profit_factor']:.2f}")
    print(f"  Max drawdown    : ${metrics['max_drawdown_usd']:,.2f}")
    print(f"  Ending balance  : ${trades.attrs.get('ending_balance_usd', cfg.starting_balance_usd):,.2f}")
    print("\n  Per slot:")
    print(
        analysis["per_slot"][[
            "trades", "win_rate_pct", "net_pnl_usd", "expectancy_usd",
            "profit_factor", "max_drawdown_usd",
        ]].round(2).to_string()
    )
    if not analysis["walk_forward"].empty:
        print("\n  Walk-forward folds:")
        print(
            analysis["walk_forward"][[
                "train_trades", "oos_trades", "oos_net_pnl_usd",
                "oos_expectancy_usd", "oos_profit_factor", "oos_max_drawdown_usd",
            ]].round(2).to_string()
        )
    print(f"\n  Validation scope: {analysis['evaluation_scope']}")
    print(analysis["gates"].to_string())
    passed = bool(analysis["gates"]["passed"].all())
    print(f"\n  VALIDATION RESULT: {'PASS' if passed else 'FAIL - DO NOT SCALE/AUTO-ENABLE'}")
    print("=" * 78)

    if output_csv:
        output = Path(output_csv)
        output.parent.mkdir(parents=True, exist_ok=True)
        trades.to_csv(output, index=False)
        print(f"\n  Trade log saved -> {output}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candles-csv", help="Offline BTC 15-minute OHLCV CSV")
    parser.add_argument("--quotes-csv", help="Optional historical MOVE bid/ask CSV")
    parser.add_argument("--output", default=str(OUT_CSV), help="Trade-log output CSV")
    parser.add_argument("--slots", choices=("both", "morning", "evening"), default="both")
    parser.add_argument("--no-network", action="store_true", help="Require --candles-csv")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    cfg = MoveBacktestConfig.from_env()
    if args.slots == "morning":
        cfg = replace(cfg, evening=replace(cfg.evening, enabled=False))
    elif args.slots == "evening":
        cfg = replace(cfg, morning=replace(cfg.morning, enabled=False))
    if args.candles_csv:
        candle_data: pd.DataFrame | None = pd.read_csv(args.candles_csv)
    elif args.no_network:
        raise SystemExit("--no-network requires --candles-csv")
    else:
        candle_data = None
    quote_data = pd.read_csv(args.quotes_csv) if args.quotes_csv else None
    trades = run_backtest(candle_data, cfg, quote_data)
    print_report(trades, cfg, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
