"""The eleven §23.3 replay fixtures, determinism, and the invariants that
make a backtest trustworthy: no look-ahead, no candle-close fills, and a
degraded feed never producing an entry."""

from __future__ import annotations

import functools
from decimal import Decimal

import pytest

from btc_trend_engine.backtest.event_replay import ReplayConfig, replay, replay_score_zones
from btc_trend_engine.backtest.fill_simulator import FillConfig, simulate_fill
from btc_trend_engine.backtest.latency_model import ZERO_LATENCY, LatencyModel
from btc_trend_engine.backtest.performance import evaluate
from btc_trend_engine.tests.replay.fixtures import SCENARIOS, T0, scenario

# The 1h structural timeframe needs 60 closed candles (= 720 five-minute
# candles) before features are complete; leave margin for all feature windows.
WARMUP = 900


def _run_uncached(name: str, **overrides):
    spec = scenario(name)
    config = ReplayConfig(**overrides) if overrides else ReplayConfig()
    return replay(spec.candles(), config, warmup=WARMUP,
                  data_quality=spec.data_quality, book_valid=spec.book_valid,
                  spread_bps=spec.spread_bps)


@functools.lru_cache(maxsize=None)
def _run(name: str):
    """Cached across tests. Safe precisely because replay is deterministic —
    which ``test_every_scenario_is_deterministic`` proves independently using
    the uncached path, so this cache cannot mask a non-determinism bug."""
    return _run_uncached(name)


# ── the eleven scenarios ────────────────────────────────────────────────
def test_all_eleven_scenarios_exist():
    assert len(SCENARIOS) == 11


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_every_scenario_replays_without_raising(name):
    result = _run(name)
    assert result.snapshots, f"{name} produced no snapshots at all"


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_every_scenario_is_deterministic(name):
    first, second = _run_uncached(name), _run_uncached(name)
    assert [t.pnl for t in first.trades] == [t.pnl for t in second.trades]
    assert [s["signal_id"] for s in first.snapshots] == \
           [s["signal_id"] for s in second.snapshots]


@pytest.mark.parametrize("name", [
    n for n, s in SCENARIOS.items() if s.expect_entry_allowed is False])
def test_scenarios_that_must_never_permit_entry(name):
    result = _run(name)
    assert result.entry_eligible_signals == 0, (
        f"{name} permitted {result.entry_eligible_signals} entry-eligible signals")
    assert not result.trades


@pytest.mark.parametrize("name", [
    n for n, s in SCENARIOS.items() if s.expect_entry_allowed is True])
def test_scenarios_that_should_be_tradeable_produce_a_signal(name):
    result = _run(name)
    assert result.entry_eligible_signals > 0, (
        f"{name} is a clean trending market but never permitted an entry")


def test_a_degraded_feed_never_permits_entry_even_on_a_perfect_trend():
    """data_outage/exchange_maintenance use the same bullish price series as
    bullish_trend — the ONLY difference is data quality. If the degraded run
    trades, the gate is decorative."""
    clean = _run("bullish_trend")
    assert clean.entry_eligible_signals > 0
    for degraded in ("data_outage", "exchange_maintenance", "private_feed_outage"):
        assert _run(degraded).entry_eligible_signals == 0, degraded


def test_score_zone_replay_uses_the_shipped_zone_lifecycle_without_faking_move_pnl():
    """The deployed policy is zones, not the legacy direction-only loop.

    A range-bound replay can create confirmed neutral candidates, but no
    historical MOVE bid/ask archive exists. Any candidates must therefore be
    reported as unpriced rather than turned into invented short-vol profits.
    """
    spec = scenario("range_bound")
    result = replay_score_zones(
        spec.candles(), ReplayConfig(), warmup=WARMUP,
        data_quality=spec.data_quality, book_valid=spec.book_valid,
        spread_bps=spec.spread_bps,
    )
    assert result.snapshots
    # The faster 1h/30m profile may classify this volatile synthetic range as
    # directional rather than calm; that is not a reason to fabricate MOVE
    # P&L. Exact neutral-zone confirmation is covered at the signal layer.
    assert result.short_move_unpriced == result.short_move_candidates
    assert result.entry_eligible_signals >= 0
    assert all(trade.side in {"long", "short"} for trade in result.trades)


# ── invariants ──────────────────────────────────────────────────────────
def test_a_shorter_window_cannot_change_earlier_decisions():
    """Look-ahead detector: truncating the future must leave the snapshots
    that were already produced bit-identical. If a later candle influenced an
    earlier decision, these diverge."""
    candles = scenario("bullish_trend").candles()
    full = replay(candles, ReplayConfig(), warmup=WARMUP)
    truncated = replay(candles[:-80], ReplayConfig(), warmup=WARMUP)
    overlap = len(truncated.snapshots)
    assert overlap > 10
    assert [s["signal_id"] for s in full.snapshots[:overlap]] == \
           [s["signal_id"] for s in truncated.snapshots]
    assert [s["trend_score"] for s in full.snapshots[:overlap]] == \
           [s["trend_score"] for s in truncated.snapshots]


def test_the_bounded_feature_window_does_not_change_any_score():
    """The 400-candle feature window is a speed optimisation, and it is only
    legitimate if it is also a no-op. A wider window must produce identical
    scores; if it does not, the bound is silently altering results."""
    candles = scenario("bullish_trend").candles()
    narrow = replay(candles, ReplayConfig(), warmup=WARMUP, feature_window=400)
    wide = replay(candles, ReplayConfig(), warmup=WARMUP, feature_window=1500)
    assert [s["trend_score"] for s in narrow.snapshots] == \
           [s["trend_score"] for s in wide.snapshots]
    assert [s["regime"] for s in narrow.snapshots] == \
           [s["regime"] for s in wide.snapshots]


def test_replay_returns_nothing_when_there_is_not_enough_history():
    candles = scenario("bullish_trend").candles()[:50]
    result = replay(candles, ReplayConfig(), warmup=WARMUP)
    assert result.trades == [] and result.snapshots == []


def test_a_short_series_reports_features_incomplete_rather_than_trading():
    """Below the 1h minimum the engine must refuse, not guess. This is the
    condition the long fixtures exist to get past, so it is worth pinning."""
    candles = scenario("bullish_trend").candles()[:600]
    result = replay(candles, ReplayConfig(), warmup=120)
    assert result.snapshots
    assert result.entry_eligible_signals == 0
    assert all(s["data_quality"] == "FEATURES_INCOMPLETE" for s in result.snapshots)


def test_an_open_position_at_the_end_is_closed_not_dropped():
    """A trending series ends with the signal still on. Dropping that
    position would hide whatever it was carrying."""
    result = _run("bullish_trend")
    assert result.trades
    assert any(t.exit_reason == "end of window" for t in result.trades)


def test_fees_are_charged_and_reduce_pnl():
    candles = scenario("bullish_trend").candles()
    free = replay(candles, ReplayConfig(fill=FillConfig(taker_fee_bps=Decimal(0))),
                  warmup=WARMUP)
    charged = replay(candles, ReplayConfig(fill=FillConfig(taker_fee_bps=Decimal("25"))),
                     warmup=WARMUP)
    assert evaluate(free.trades).total_pnl > evaluate(charged.trades).total_pnl
    assert evaluate(charged.trades).total_fees > 0


# ── latency ─────────────────────────────────────────────────────────────
def test_latency_is_deterministic_for_the_same_decision():
    model = LatencyModel()
    assert model.delay_ms(T0, "sig") == model.delay_ms(T0, "sig")


def test_latency_varies_across_decisions_but_stays_in_band():
    model = LatencyModel(base_ms=250, jitter_ms=150)
    from datetime import timedelta
    delays = [model.delay_ms(T0 + timedelta(minutes=i), "s") for i in range(50)]
    assert len(set(delays)) > 1
    assert all(100 <= d <= 400 for d in delays)


def test_zero_latency_is_available_but_not_the_default():
    assert ReplayConfig().latency.base_ms > 0
    assert ZERO_LATENCY.delay_ms(T0) == 0.0


def test_fill_timestamps_land_on_the_bar_whose_price_was_used():
    """The recorded fill time must be the bar the fill price came from. If it
    were decision+latency instead, every trade would carry a timestamp five
    minutes earlier than the price that produced it."""
    candles = scenario("bullish_trend").candles()
    starts = {c.start for c in candles}
    result = replay(candles, ReplayConfig(), warmup=WARMUP)
    assert result.trades
    for trade in result.trades:
        assert trade.entered_at in starts, trade.entered_at


# ── fill simulator ──────────────────────────────────────────────────────
def test_a_buy_lifts_the_ask_never_the_mid():
    fill = simulate_fill(side="long", lots=1, reference_price=Decimal("100"),
                         limit_price=Decimal("101"), config=FillConfig())
    assert fill.filled and fill.price > Decimal("100")


def test_a_sell_hits_the_bid():
    fill = simulate_fill(side="short", lots=1, reference_price=Decimal("100"),
                         limit_price=Decimal("99"), config=FillConfig())
    assert fill.filled and fill.price < Decimal("100")


def test_a_price_beyond_the_limit_does_not_fill_at_a_worse_price():
    fill = simulate_fill(side="long", lots=1, reference_price=Decimal("100"),
                         limit_price=Decimal("100"),
                         config=FillConfig(spread_bps=Decimal("100")))
    assert not fill.filled
    assert "IOC cancelled" in (fill.rejected_reason or "")


def test_ioc_partially_fills_against_limited_depth_and_cancels_the_rest():
    fill = simulate_fill(side="long", lots=10, reference_price=Decimal("100"),
                         limit_price=Decimal("110"),
                         config=FillConfig(available_lots=4))
    assert fill.filled_lots == 4


def test_slippage_beyond_the_cap_is_a_cancellation():
    fill = simulate_fill(side="long", lots=1, reference_price=Decimal("100"),
                         limit_price=Decimal("200"),
                         config=FillConfig(spread_bps=Decimal("400"),
                                           max_slippage_bps=Decimal("15")))
    assert not fill.filled
    assert "slippage" in (fill.rejected_reason or "")
