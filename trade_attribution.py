"""Map real Delta trade rows onto the P&L attribution model.

``pnl_attribution`` is pure maths and knows nothing about Delta. This module
is the adapter: it turns the position-cycle rows the Performance page already
builds (``_reconstruct_delta_trades`` in dashboard.py) into the two market
observations the attribution needs, and reports honestly on everything it
could not decompose.

I/O is injected rather than performed here -- callers pass an
``underlying_at`` lookup and a ``contract_value_for`` lookup -- so the whole
mapping is testable offline and holds no credentials of its own. The CLI that
supplies the real network calls is ``scripts/attribute_trades.py``.

A trade is only decomposed when every input is genuinely known. Anything
missing (an unparseable symbol, no underlying price at that minute, a
still-open position) is returned in ``skipped`` with a reason, never guessed
at, because a fabricated input would produce a confident and wrong answer
about where the money went.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from pnl_attribution import (
    CALL,
    PUT,
    STRADDLE,
    Attribution,
    Observation,
    attribute_trade,
    summarise,
)

# Delta India options settle at 12:00 UTC on their expiry date. The symbol
# carries only DDMMYY, so the time of day comes from here; callers that have
# the product's authoritative ``settlement_time`` should pass it instead.
SETTLEMENT_HOUR_UTC = 12

_KIND_BY_PREFIX = {"C": CALL, "P": PUT, "MV": STRADDLE}

_YEAR_SECONDS = 365.0 * 24.0 * 3600.0


@dataclass(frozen=True, slots=True)
class OptionContract:
    kind: str
    strike: float
    expiry: datetime          # UTC


def parse_option_symbol(symbol: str,
                        settlement_hour_utc: int = SETTLEMENT_HOUR_UTC
                        ) -> OptionContract | None:
    """``C-BTC-63000-080726`` -> call / 63000 / 2026-07-08 12:00 UTC.

    Returns None for anything that is not a dated option -- perpetuals and
    futures included -- so callers can skip them explicitly rather than
    mis-price them as options.
    """
    if not symbol:
        return None
    parts = str(symbol).strip().upper().split("-")
    if len(parts) != 4:
        return None
    prefix, _asset, strike_text, expiry_text = parts
    kind = _KIND_BY_PREFIX.get(prefix)
    if kind is None:
        return None
    try:
        strike = float(strike_text)
    except (TypeError, ValueError):
        return None
    if strike <= 0:
        return None
    try:
        expiry = datetime.strptime(expiry_text, "%d%m%y").replace(
            hour=settlement_hour_utc, tzinfo=timezone.utc)
    except ValueError:
        return None
    return OptionContract(kind=kind, strike=strike, expiry=expiry)


def parse_utc(value: str | None) -> datetime | None:
    """Parse the ISO-8601 UTC stamps the trade rows carry."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def years_to_expiry(moment: datetime, expiry: datetime) -> float:
    return (expiry - moment).total_seconds() / _YEAR_SECONDS


@dataclass(frozen=True, slots=True)
class AttributedTrade:
    symbol: str
    kind: str
    side: str
    lots: float
    strike: float
    entry_at: datetime
    exit_at: datetime
    entry_underlying: float
    exit_underlying: float
    held_hours: float
    attribution: Attribution


@dataclass(frozen=True, slots=True)
class SkippedTrade:
    symbol: str
    reason: str
    pnl_usd: float | None = None      # known even when the split is not


@dataclass(frozen=True, slots=True)
class AttributionReport:
    attributed: list[AttributedTrade]
    skipped: list[SkippedTrade]

    @property
    def undecomposed_pnl(self) -> float:
        """P&L of trades we could not split.

        Reported separately and never folded into the attributed summary:
        rolling it into ``residual`` would let undecomposed money pose as a
        measured execution cost, which is how an attribution table starts
        lying. Read it as the coverage gap.
        """
        return sum(s.pnl_usd for s in self.skipped if s.pnl_usd is not None)

    @property
    def coverage(self) -> float:
        """Share of gross P&L that was actually decomposed."""
        attributed = sum(abs(t.attribution.total) for t in self.attributed)
        missed = sum(abs(s.pnl_usd) for s in self.skipped
                     if s.pnl_usd is not None)
        denominator = attributed + missed
        return attributed / denominator if denominator else 0.0

    @property
    def summary(self) -> dict[str, object]:
        return summarise([t.attribution for t in self.attributed])

    def by_kind(self) -> dict[str, dict[str, object]]:
        """Per-leg totals -- the directional book and the MOVE book are
        different strategies and have to be read apart."""
        groups: dict[str, list[Attribution]] = {}
        for trade in self.attributed:
            groups.setdefault(trade.kind, []).append(trade.attribution)
        return {kind: summarise(items) for kind, items in sorted(groups.items())}

    def skip_reasons(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.skipped:
            counts[item.reason] = counts.get(item.reason, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def _usd_fees(row: dict) -> float:
    """Round-trip USD fees. Non-USD fee assets are ignored rather than
    converted at an assumed rate; they surface as residual instead."""
    total = 0.0
    for entry in row.get("fees") or []:
        if str(entry.get("asset", "")).upper() == "USD":
            try:
                total += float(entry.get("amount") or 0.0)
            except (TypeError, ValueError):
                continue
    return total


def attribute_rows(rows: list[dict], *, underlying_at, contract_value_for,
                   settlement_hour_utc: int = SETTLEMENT_HOUR_UTC,
                   rate: float = 0.0) -> AttributionReport:
    """Decompose every closed option round trip in ``rows``.

    ``underlying_at(when)`` returns the BTC price at that instant (or None),
    and ``contract_value_for(symbol)`` the contract multiplier (or None).
    Both may perform I/O; this function performs none.
    """
    attributed: list[AttributedTrade] = []
    skipped: list[SkippedTrade] = []

    for row in rows:
        symbol = str(row.get("symbol") or "")

        if str(row.get("status") or "").upper() != "CLOSED":
            skipped.append(SkippedTrade(symbol, "position still open"))
            continue

        contract = parse_option_symbol(symbol, settlement_hour_utc)
        if contract is None:
            skipped.append(SkippedTrade(symbol, "not a dated option symbol"))
            continue

        entry_at = parse_utc(row.get("entry_at_utc"))
        exit_at = parse_utc(row.get("exit_at_utc"))
        if entry_at is None or exit_at is None:
            skipped.append(SkippedTrade(symbol, "entry/exit timestamp missing"))
            continue

        try:
            entry_price = float(row.get("entry_price"))
            exit_price = float(row.get("exit_price"))
            lots = float(row.get("lots") or 0.0)
        except (TypeError, ValueError):
            skipped.append(SkippedTrade(symbol, "entry/exit price missing"))
            continue
        if entry_price <= 0 or exit_price <= 0 or lots <= 0:
            skipped.append(SkippedTrade(symbol, "non-positive price or size"))
            continue

        side = str(row.get("side") or "").upper()
        if side not in ("LONG", "SHORT"):
            skipped.append(SkippedTrade(symbol, "direction not reported"))
            continue

        contract_value = contract_value_for(symbol)
        if not contract_value or contract_value <= 0:
            skipped.append(SkippedTrade(symbol, "contract value unknown"))
            continue

        entry_underlying = underlying_at(entry_at)
        exit_underlying = underlying_at(exit_at)
        if not entry_underlying or not exit_underlying:
            skipped.append(SkippedTrade(
                symbol, "underlying price unavailable for that minute"))
            continue

        entry_t = years_to_expiry(entry_at, contract.expiry)
        exit_t = years_to_expiry(exit_at, contract.expiry)
        if entry_t <= 0:
            skipped.append(SkippedTrade(symbol, "entered at or after expiry"))
            continue

        attribution = attribute_trade(
            kind=contract.kind,
            strike=contract.strike,
            entry=Observation(underlying=float(entry_underlying),
                              option_price=entry_price, t_years=entry_t),
            exit=Observation(underlying=float(exit_underlying),
                             option_price=exit_price, t_years=exit_t),
            lots=lots,
            contract_value=float(contract_value),
            direction=side,
            rate=rate,
            fees_usd=_usd_fees(row),
        )
        if attribution.entry_iv is None or attribution.exit_iv is None:
            # No greeks at all -- typically an exit at or through intrinsic
            # near expiry, where no Black-Scholes vol reproduces the print.
            # Keep the P&L visible but out of the attributed totals.
            skipped.append(SkippedTrade(
                symbol,
                "implied vol not invertible (near-expiry or off-market mark)",
                pnl_usd=attribution.total))
            continue

        attributed.append(AttributedTrade(
            symbol=symbol, kind=contract.kind, side=side, lots=lots,
            strike=contract.strike, entry_at=entry_at, exit_at=exit_at,
            entry_underlying=float(entry_underlying),
            exit_underlying=float(exit_underlying),
            held_hours=(exit_at - entry_at).total_seconds() / 3600.0,
            attribution=attribution,
        ))

    return AttributionReport(attributed=attributed, skipped=skipped)


class CandleUnderlyingLookup:
    """Underlying price at an instant, from 1-minute BTCUSD candles.

    Built from ``/v2/history/candles`` rows (``time`` in epoch seconds). The
    price used is the close of the minute containing the timestamp; if that
    exact minute is missing the nearest candle within ``tolerance`` is used,
    and beyond that the lookup returns None rather than reaching for a price
    that was never observed.
    """

    def __init__(self, candles, tolerance: timedelta = timedelta(minutes=5)):
        self._by_minute: dict[int, float] = {}
        for candle in candles or []:
            try:
                minute = int(candle["time"]) // 60
                self._by_minute[minute] = float(candle["close"])
            except (KeyError, TypeError, ValueError):
                continue
        self._tolerance_minutes = max(0, int(tolerance.total_seconds() // 60))

    def __len__(self) -> int:
        return len(self._by_minute)

    def __call__(self, when: datetime) -> float | None:
        if not self._by_minute:
            return None
        target = int(when.timestamp()) // 60
        exact = self._by_minute.get(target)
        if exact is not None:
            return exact
        for offset in range(1, self._tolerance_minutes + 1):
            for minute in (target - offset, target + offset):
                nearby = self._by_minute.get(minute)
                if nearby is not None:
                    return nearby
        return None
