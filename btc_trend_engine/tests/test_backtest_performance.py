"""Performance metrics and walk-forward mechanics.

The point of these is that the numbers must be *honestly* wrong-free: a
bootstrap CI that silently appears from three trades, or a walk-forward that
lets the training window leak into the test window, would make every
downstream cutover decision unsound.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from btc_trend_engine.backtest.event_replay import BacktestTrade, ReplayConfig
from btc_trend_engine.backtest.performance import (
    bootstrap_mean_ci,
    by_regime,
    evaluate,
    max_drawdown,
)
from btc_trend_engine.backtest.walk_forward import (
    Candidate,
    default_candidates,
    run_walk_forward,
)
from btc_trend_engine.tests.replay.fixtures import scenario

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _trade(pnl: float, *, regime: str = "TREND_UP", fees: float = 0.5,
           index: int = 0) -> BacktestTrade:
    return BacktestTrade(
        entered_at=T0 + timedelta(minutes=5 * index),
        exited_at=T0 + timedelta(minutes=5 * index + 5),
        side="long", lots=1, entry_price=Decimal("100"), exit_price=Decimal("101"),
        fees=Decimal(str(fees)), pnl=Decimal(str(pnl)),
        entry_signal_id=f"sig-{index}", exit_reason="signal flat",
        regime_at_entry=regime)


# ── metrics ─────────────────────────────────────────────────────────────
def test_empty_trades_produce_zeroed_metrics_not_a_crash():
    performance = evaluate([])
    assert performance.trades == 0
    assert performance.expectancy == 0.0
    assert performance.expectancy_ci is None
    assert not performance.is_significant


def test_win_rate_and_totals():
    trades = [_trade(10, index=0), _trade(-4, index=1), _trade(6, index=2)]
    performance = evaluate(trades)
    assert performance.trades == 3
    assert performance.wins == 2 and performance.losses == 1
    assert round(performance.win_rate, 4) == round(2 / 3, 4)
    assert performance.total_pnl == 12.0
    assert performance.gross_profit == 16.0 and performance.gross_loss == 4.0
    assert performance.profit_factor == 4.0


def test_max_drawdown_measures_peak_to_trough_not_worst_trade():
    # +10, -4, +2, -8 -> cumulative 10, 6, 8, 0; peak 10, trough 0 => -10.
    assert max_drawdown([10, -4, 2, -8]) == -10.0


def test_max_drawdown_of_a_monotonic_winner_is_zero():
    assert max_drawdown([1, 2, 3]) == 0.0


def test_a_losing_only_run_reports_no_profit_factor_rather_than_infinity():
    performance = evaluate([_trade(-1, index=i) for i in range(3)])
    assert performance.gross_profit == 0.0
    assert performance.profit_factor == 0.0


def test_fees_are_reported_separately_from_pnl():
    performance = evaluate([_trade(10, fees=2.5, index=0)])
    assert performance.total_fees == 2.5
    assert performance.total_pnl == 10.0  # pnl is already net; fees are shown too


# ── bootstrap ───────────────────────────────────────────────────────────
def test_no_confidence_interval_below_five_observations():
    assert bootstrap_mean_ci([1.0, 2.0, 3.0, 4.0]) is None


def test_a_confidence_interval_appears_at_five_observations():
    assert bootstrap_mean_ci([1.0, 2.0, 3.0, 4.0, 5.0]) is not None


def test_the_bootstrap_is_reproducible():
    values = [3.0, -1.0, 4.0, -2.0, 5.0, 0.5, -3.0]
    assert bootstrap_mean_ci(values) == bootstrap_mean_ci(values)


def test_a_noisy_break_even_series_is_not_significant():
    values = [10.0, -10.0, 9.0, -9.0, 11.0, -11.0, 1.0, -1.0]
    ci = bootstrap_mean_ci(values)
    assert ci is not None and not ci.excludes_zero


def test_a_consistent_winner_is_significant():
    values = [5.0, 6.0, 4.5, 5.5, 6.5, 5.0, 4.0, 5.2]
    ci = bootstrap_mean_ci(values)
    assert ci is not None and ci.excludes_zero
    assert evaluate([_trade(v, index=i) for i, v in enumerate(values)]).is_significant


def test_regime_breakdown_groups_trades():
    trades = [_trade(5, regime="TREND_UP", index=0),
              _trade(-3, regime="RANGE", index=1),
              _trade(7, regime="TREND_UP", index=2)]
    grouped = by_regime(trades)
    assert set(grouped) == {"TREND_UP", "RANGE"}
    assert grouped["TREND_UP"].trades == 2
    assert grouped["RANGE"].total_pnl == -3.0


# ── walk-forward ────────────────────────────────────────────────────────
def test_default_candidates_cover_the_threshold_grid():
    labels = {c.label() for c in default_candidates()}
    assert len(labels) == 9
    # The shipped default under the 2026-07-26 zone spec.
    assert "entry=35,conf=0.62" in labels


def test_a_candidate_overrides_only_its_own_thresholds():
    base = ReplayConfig().signal
    applied = Candidate(70.0, 0.8).applied_to(base)
    assert applied.entry_score == 70.0 and applied.minimum_confidence == 0.8
    assert applied.ttl_seconds == base.ttl_seconds
    assert applied.hold_score == base.hold_score


def test_too_little_data_is_reported_as_a_warning_not_a_silent_empty_report():
    candles = scenario("bullish_trend").candles()[:400]
    report = run_walk_forward(candles, folds=4, warmup=320,
                              candidates=[Candidate(65.0, 0.62)])
    assert report.folds == []
    assert report.warnings, "an unusable dataset must say so"


def test_a_fold_is_skipped_rather_than_leaking_training_data_into_its_test():
    """If the test window is shorter than the warmup, borrowing the tail of
    the training window would be the convenient thing to do and would
    invalidate the whole exercise."""
    candles = scenario("bullish_trend").candles()
    report = run_walk_forward(candles, folds=8, warmup=3000,
                              candidates=[Candidate(65.0, 0.62)])
    assert any("skipped" in w for w in report.warnings)
