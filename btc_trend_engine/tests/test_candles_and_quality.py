"""Candle aggregation (closed-only emission, rollups, gap detection) and
data-quality staleness/clock-drift verdicts (§6.5, §7)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from btc_trend_engine.clock import FixedClock
from btc_trend_engine.config import DataQualityConfig
from btc_trend_engine.market_data.candle_aggregator import (
    CandleAggregator,
    CandleSeries,
    bucket_start,
)
from btc_trend_engine.market_data.data_quality import (
    DataQualityMonitor,
    Freshness,
)
from btc_trend_engine.market_data.messages import Candle, Trade

T0 = datetime(2026, 7, 25, 10, 0, tzinfo=timezone.utc)


def _trade(offset_seconds: float, price: str, size: str = "1") -> Trade:
    return Trade(symbol="BTCUSD", price=Decimal(price), size=Decimal(size),
                 buyer_is_taker=True,
                 exchange_timestamp=T0 + timedelta(seconds=offset_seconds))


def _dq_config(**overrides) -> DataQualityConfig:
    values = dict(stale_multiplier=3.0, stale_floor_seconds=5.0,
                  median_window=64, max_clock_drift_ms=500.0, fail_closed=True)
    values.update(overrides)
    return DataQualityConfig(**values)


# ── candles ─────────────────────────────────────────────────────────────
def test_bucket_start_aligns_to_resolution():
    ts = datetime(2026, 7, 25, 10, 7, 33, tzinfo=timezone.utc)
    assert bucket_start(ts, "5m") == datetime(2026, 7, 25, 10, 5, tzinfo=timezone.utc)
    assert bucket_start(ts, "30m") == datetime(2026, 7, 25, 10, 0, tzinfo=timezone.utc)
    assert bucket_start(ts, "4h") == datetime(2026, 7, 25, 8, 0, tzinfo=timezone.utc)


def test_only_closed_candles_are_emitted():
    closed: list[Candle] = []
    series = CandleSeries("BTCUSD", "5m", on_close=closed.append)
    series.add_trade(_trade(0, "63900"))
    series.add_trade(_trade(120, "63950", "2"))
    assert closed == []  # bucket still open
    series.add_trade(_trade(301, "64000"))  # next bucket → closes previous
    assert len(closed) == 1
    candle = closed[0]
    assert candle.closed
    assert (candle.open, candle.high, candle.close) == (
        Decimal("63900"), Decimal("63950"), Decimal("63950"))
    assert candle.volume == Decimal("3")


def test_quiet_market_closes_via_clock_roll():
    closed: list[Candle] = []
    series = CandleSeries("BTCUSD", "5m", on_close=closed.append)
    series.add_trade(_trade(10, "63900"))
    series.roll_clock(T0 + timedelta(seconds=299))
    assert closed == []
    series.roll_clock(T0 + timedelta(seconds=300))
    assert len(closed) == 1


def test_late_trade_never_rewrites_a_closed_candle():
    closed: list[Candle] = []
    series = CandleSeries("BTCUSD", "5m", on_close=closed.append)
    series.add_trade(_trade(0, "63900"))
    series.add_trade(_trade(301, "64000"))
    series.add_trade(_trade(200, "1"))  # late for the closed bucket
    assert len(closed) == 1
    assert closed[0].low == Decimal("63900")


def test_seed_closed_is_idempotent_and_detects_gaps():
    series = CandleSeries("BTCUSD", "5m")
    def candle(minute: int) -> Candle:
        return Candle(symbol="BTCUSD", resolution="5m",
                      start=T0 + timedelta(minutes=minute),
                      open=Decimal(1), high=Decimal(1), low=Decimal(1),
                      close=Decimal(1), volume=Decimal(0), closed=True)
    series.seed_closed([candle(0), candle(5)])
    series.seed_closed([candle(0), candle(5)])  # duplicate bootstrap
    assert len(series.closed_candles()) == 2
    assert not series.gap_detected
    series.seed_closed([candle(20)])  # 10 and 15 missing
    assert series.gap_detected


def test_aggregator_rolls_all_resolutions_from_one_stream():
    aggregator = CandleAggregator("BTCUSD", ["5m", "15m"])
    for minute in range(16):
        aggregator.add_trade(_trade(minute * 60, "63900"))
    five = aggregator.series["5m"].closed_candles()
    fifteen = aggregator.series["15m"].closed_candles()
    assert len(five) == 3   # 10:00, 10:05, 10:10 closed
    assert len(fifteen) == 1
    assert fifteen[0].start == T0


# ── data quality ────────────────────────────────────────────────────────
def test_feed_never_seen_is_not_fresh():
    clock = FixedClock(T0)
    monitor = DataQualityMonitor(_dq_config(), clock, feeds=["trades"])
    assert monitor.verdicts()["trades"].freshness is Freshness.NEVER
    assert monitor.data_quality(book_valid=True) == "STALE_L1"


def test_staleness_uses_dynamic_threshold_from_cadence():
    clock = FixedClock(T0)
    monitor = DataQualityMonitor(_dq_config(), clock, feeds=["trades"])
    for _ in range(20):  # 1 Hz cadence
        monitor.record("trades")
        clock.advance(1.0)
    verdict = monitor.verdicts()["trades"]
    assert verdict.freshness is Freshness.FRESH
    # hard threshold = max(3×1s, 5s floor) = 5s
    clock.advance(3.5)
    assert monitor.verdicts()["trades"].freshness is Freshness.WARNING
    clock.advance(2.0)
    assert monitor.verdicts()["trades"].freshness is Freshness.STALE
    assert monitor.data_quality(book_valid=True) == "STALE_L1"


def test_slow_feed_gets_proportionally_slower_threshold():
    clock = FixedClock(T0)
    monitor = DataQualityMonitor(_dq_config(), clock, feeds=["funding"])
    for _ in range(10):  # one message every 30 s
        monitor.record("funding")
        clock.advance(30.0)
    clock.advance(50.0)  # age 80 s < 3×30 s warning threshold
    assert monitor.verdicts()["funding"].freshness is Freshness.FRESH


def test_stale_l2_and_invalid_book_labels():
    clock = FixedClock(T0)
    monitor = DataQualityMonitor(_dq_config(), clock,
                                 feeds=["trades", "l2"])
    for _ in range(10):
        monitor.record("trades")
        monitor.record("l2")
        clock.advance(1.0)
    assert monitor.data_quality(book_valid=True) == "OK"
    assert monitor.data_quality(book_valid=False) == "BOOK_INVALID"
    # stop only l2
    for _ in range(6):
        monitor.record("trades")
        clock.advance(1.0)
    assert monitor.data_quality(book_valid=True) == "STALE_L2"


def test_clock_drift_beyond_bound_degrades_everything():
    clock = FixedClock(T0)
    monitor = DataQualityMonitor(_dq_config(), clock, feeds=["trades"])
    for _ in range(10):
        # exchange timestamps 2 s in the past → 2000 ms median offset
        monitor.record("trades", exchange_ts_epoch=clock.now().timestamp() - 2.0)
        clock.advance(0.5)
    assert monitor.data_quality(book_valid=True) == "CLOCK_DRIFT"


def test_single_delayed_packet_does_not_fake_drift():
    clock = FixedClock(T0)
    monitor = DataQualityMonitor(_dq_config(), clock, feeds=["trades"])
    for i in range(20):
        late = 5.0 if i == 7 else 0.05
        monitor.record("trades", exchange_ts_epoch=clock.now().timestamp() - late)
        clock.advance(0.5)
    assert monitor.data_quality(book_valid=True) == "OK"


def test_fail_closed_cannot_be_configured_off():
    with pytest.raises(ValueError):
        _dq_config(fail_closed=False)
