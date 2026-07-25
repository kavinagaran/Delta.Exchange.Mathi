"""Event-driven replay over closed candles (§28, plan Phase 5).

The loop is deliberately boring, because every interesting decision is
delegated to the production objects:

    for each closed 5m trigger candle T:
        producer.produce(...)        <- production SnapshotProducer
        risk_manager.evaluate_entry  <- production risk manager (optional)
        fill at candle T+1 open      <- never at T's close

**Look-ahead is prevented structurally, not by discipline.** The producer is
only ever handed candles whose close is <= the decision time, and a fill can
only reference a candle strictly after the decision candle. There is no code
path here that can see a future bar, which is why the invariant test can
assert it rather than trusting a reviewer to spot it.

Position model: the engine emits a hysteresis direction in {-1, 0, +1}. A
position opens when direction becomes non-zero *and* the snapshot permits
entry, and closes when direction returns to zero or flips. A flip closes and
reopens in the same bar, paying both sides' costs.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..market_data.messages import Candle
from ..market_data.normalizer import resolution_seconds
from ..signals.producer import PRIMARY, SETUP, STRUCTURAL, TRIGGER, SnapshotProducer
from ..signals.snapshot import SignalConfig
from .fill_simulator import FillConfig, simulate_fill
from .latency_model import LatencyModel

ROLES = (STRUCTURAL, PRIMARY, SETUP, TRIGGER)


@dataclass(frozen=True, slots=True)
class BacktestTrade:
    entered_at: datetime
    exited_at: datetime
    side: str
    lots: int
    entry_price: Decimal
    exit_price: Decimal
    fees: Decimal
    pnl: Decimal
    entry_signal_id: str
    exit_reason: str
    regime_at_entry: str

    @property
    def won(self) -> bool:
        return self.pnl > 0


@dataclass(frozen=True, slots=True)
class ReplayConfig:
    symbol: str = "BTCUSD"
    lots: int = 1
    signal: SignalConfig = field(default_factory=SignalConfig)
    fill: FillConfig = field(default_factory=FillConfig)
    latency: LatencyModel = field(default_factory=LatencyModel)
    """Retained for order-flow-level replay. NOT binding at candle
    granularity — see the fill-time comment in ``replay``."""

    limit_slippage_bps: Decimal = Decimal("20.0")
    """How far beyond the reference the marketable limit is placed. Wider
    than the assumed spread, or nothing would ever fill."""


@dataclass
class ReplayResult:
    trades: list[BacktestTrade] = field(default_factory=list)
    snapshots: list[dict[str, Any]] = field(default_factory=list)
    entry_eligible_signals: int = 0
    rejected_fills: int = 0
    skipped_no_entry: int = 0

    @property
    def entries(self) -> int:
        return len(self.trades)


@dataclass
class _OpenPosition:
    side: str
    lots: int
    entry_price: Decimal
    entered_at: datetime
    entry_fee: Decimal
    signal_id: str
    regime: str


def _resample(base: Sequence[Candle], resolution: str, symbol: str) -> list[Candle]:
    """Roll a 5m base series up to a higher timeframe. Only fully-elapsed
    buckets are emitted — a partial bucket is not a closed candle."""
    step = resolution_seconds(resolution)
    base_step = resolution_seconds(TRIGGER)
    if step == base_step:
        return list(base)
    per_bucket = step // base_step
    buckets: dict[int, list[Candle]] = {}
    for candle in base:
        epoch = int(candle.start.timestamp())
        buckets.setdefault(epoch - epoch % step, []).append(candle)

    out: list[Candle] = []
    for start_epoch in sorted(buckets):
        members = buckets[start_epoch]
        if len(members) < per_bucket:
            continue  # incomplete bucket; not closed
        out.append(Candle(
            symbol=symbol, resolution=resolution,
            start=datetime.fromtimestamp(start_epoch, tz=members[0].start.tzinfo),
            open=members[0].open,
            high=max(c.high for c in members),
            low=min(c.low for c in members),
            close=members[-1].close,
            volume=sum((c.volume for c in members), Decimal(0)),
            closed=True,
            trade_count=sum(c.trade_count for c in members),
        ))
    return out


def replay(
    base_candles: Sequence[Candle],
    config: ReplayConfig | None = None,
    *,
    warmup: int = 320,
    risk_check: Callable[[Mapping[str, Any]], bool] | None = None,
    ticker: Mapping[str, Any] | None = None,
    data_quality: str = "OK",
    book_valid: bool = True,
    spread_bps: float | None = None,
    feature_window: int = 400,
) -> ReplayResult:
    """Replay a 5m base series through the production signal path.

    ``risk_check`` is an optional hook receiving each entry-eligible snapshot
    and returning whether the risk layer permits the entry. It is injected
    rather than constructed here because the production risk manager reads an
    account's ``data_dir``, which a historical replay has no equivalent of —
    see ``walk_forward`` for how the caller supplies one.

    ``data_quality``, ``book_valid`` and ``spread_bps`` are held constant for
    the whole run. That is enough for the §23.3 feed-condition fixtures, which
    each describe one sustained condition; time-varying degradation needs
    recorded data and arrives with order-flow-level replay.

    ``feature_window`` bounds how many closed candles per timeframe are handed
    to the producer. Production is also bounded (``CandleSeries.max_closed``),
    just at a larger number; the slowest indicator lookback is 50 bars, so at
    400 the residual EMA seeding weight is ~1e-7. This is not taken on faith —
    ``test_backtest_replay`` asserts a wider window produces identical scores.
    """
    config = config or ReplayConfig()
    result = ReplayResult()
    if len(base_candles) <= warmup + 1:
        return result

    ordered = sorted(base_candles, key=lambda c: c.start)
    producer = SnapshotProducer(config.symbol, config=config.signal)
    position: _OpenPosition | None = None

    # Resample once for the whole run, then slice per decision by close time.
    # Resampling the visible prefix on every bar instead would be O(n^2) and,
    # more importantly, would make the look-ahead boundary implicit; here it
    # is one explicit comparison against the decision moment.
    full_series = {role: _resample(ordered, role, config.symbol) for role in ROLES}
    series_starts = {role: [c.start for c in candles]
                     for role, candles in full_series.items()}
    base_step = resolution_seconds(TRIGGER)

    for index in range(warmup, len(ordered) - 1):
        decision_candle = ordered[index]
        next_candle = ordered[index + 1]
        decision_time = decision_candle.start
        # A higher-timeframe candle is visible only once it has fully closed
        # at or before the trigger candle's own close.
        decision_close = decision_time + timedelta(seconds=base_step)

        candles = {}
        for role in ROLES:
            step = resolution_seconds(role)
            cutoff = decision_close - timedelta(seconds=step)
            end = bisect_right(series_starts[role], cutoff)
            start = max(0, end - feature_window)
            candles[role] = full_series[role][start:end]
        snapshot = producer.produce(
            now=decision_time,
            candles=candles,
            ticker=dict(ticker or {}),
            data_quality=data_quality,
            book_valid=book_valid,
            spread_bps=(float(config.fill.spread_bps) if spread_bps is None
                        else spread_bps),
        )
        if snapshot is None:
            continue
        result.snapshots.append(snapshot)

        direction = int(snapshot["direction"])
        entry_allowed = bool(snapshot["entry_allowed"])
        if entry_allowed:
            result.entry_eligible_signals += 1

        # The fill reference is the NEXT candle's open, never this close.
        #
        # The recorded fill time is that next bar, not decision+latency. At 5m
        # granularity the latency model is not binding: 250ms of modelled
        # latency is invisible when the finest observable price is 5 minutes
        # away, and next-bar-open is already the far more conservative
        # assumption. Stamping decision+250ms while filling at a price from
        # five minutes later would be a timestamp the price cannot support.
        # LatencyModel stays built and tested for order-flow-level replay,
        # where it will actually bind.
        reference = next_candle.open
        fill_time = next_candle.start

        # ── exit ─────────────────────────────────────────────────────────
        if position is not None:
            flipped = direction != 0 and direction != (1 if position.side == "long" else -1)
            flattened = direction == 0
            if flipped or flattened:
                exit_side = "short" if position.side == "long" else "long"
                exit_fill = simulate_fill(
                    side=exit_side, lots=position.lots, reference_price=reference,
                    limit_price=_limit_for(exit_side, reference, config),
                    config=config.fill)
                if exit_fill.filled:
                    result.trades.append(_close(
                        position, exit_fill.price, exit_fill.fee, fill_time,
                        "flip" if flipped else "signal flat"))
                    position = None
                else:
                    result.rejected_fills += 1

        # ── entry ────────────────────────────────────────────────────────
        if position is None and direction != 0 and entry_allowed:
            if risk_check is not None and not risk_check(snapshot):
                result.skipped_no_entry += 1
                continue
            side = "long" if direction > 0 else "short"
            entry_fill = simulate_fill(
                side=side, lots=config.lots, reference_price=reference,
                limit_price=_limit_for(side, reference, config), config=config.fill)
            if entry_fill.filled:
                position = _OpenPosition(
                    side=side, lots=entry_fill.filled_lots,
                    entry_price=entry_fill.price, entered_at=fill_time,
                    entry_fee=entry_fill.fee, signal_id=snapshot["signal_id"],
                    regime=str(snapshot["regime"]))
            else:
                result.rejected_fills += 1
        elif position is None and direction != 0 and not entry_allowed:
            result.skipped_no_entry += 1

    # An open position at the end of the window is closed at the last known
    # price. Leaving it out would silently drop a losing tail.
    if position is not None:
        last = ordered[-1]
        exit_side = "short" if position.side == "long" else "long"
        exit_fill = simulate_fill(
            side=exit_side, lots=position.lots, reference_price=last.close,
            limit_price=_limit_for(exit_side, last.close, config), config=config.fill)
        price = exit_fill.price if exit_fill.filled else last.close
        fee = exit_fill.fee if exit_fill.filled else Decimal(0)
        result.trades.append(_close(position, price, fee, last.start,
                                    "end of window"))

    return result


def _limit_for(side: str, reference: Decimal, config: ReplayConfig) -> Decimal:
    offset = reference * config.limit_slippage_bps / Decimal(10_000)
    return reference + offset if side == "long" else reference - offset


def _close(position: _OpenPosition, exit_price: Decimal, exit_fee: Decimal,
           exited_at: datetime, reason: str) -> BacktestTrade:
    direction = Decimal(1) if position.side == "long" else Decimal(-1)
    gross = (exit_price - position.entry_price) * direction * Decimal(position.lots)
    fees = position.entry_fee + exit_fee
    return BacktestTrade(
        entered_at=position.entered_at, exited_at=exited_at, side=position.side,
        lots=position.lots, entry_price=position.entry_price, exit_price=exit_price,
        fees=fees, pnl=gross - fees, entry_signal_id=position.signal_id,
        exit_reason=reason, regime_at_entry=position.regime)


def candles_from_rows(rows: Iterable[Mapping[str, Any]], symbol: str,
                      resolution: str = TRIGGER) -> list[Candle]:
    """Build a Candle series from cached JSON rows (scripts/backtest.py)."""
    from datetime import timezone

    out = []
    for row in rows:
        out.append(Candle(
            symbol=symbol, resolution=resolution,
            start=datetime.fromtimestamp(int(row["time"]), tz=timezone.utc),
            open=Decimal(str(row["open"])), high=Decimal(str(row["high"])),
            low=Decimal(str(row["low"])), close=Decimal(str(row["close"])),
            volume=Decimal(str(row.get("volume", 0))), closed=True,
            trade_count=int(row.get("trade_count", 0))))
    out.sort(key=lambda c: c.start)
    return out
