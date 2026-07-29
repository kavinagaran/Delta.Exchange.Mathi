"""Tests for the signal-research harness.

The load-bearing properties are (a) the statistics are right, and (b) the
replay cannot see the future. A look-ahead leak would make every component
look predictive and would be invisible in the output, so it is tested
directly rather than argued for in a comment.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from btc_trend_engine.market_data.messages import Candle
from btc_trend_engine.research.information_coefficient import (
    SCORE_KEY,
    CalibrationBucket,
    Observation,
    _quantile,
    _ranks,
    collect_observations,
    component_correlation,
    confirmation_flags,
    ic_t_stat,
    information_coefficient,
    information_coefficients,
    pearson,
    score_calibration,
    sideways_gate_profile,
    spearman,
)

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


# ── statistics ──────────────────────────────────────────────────────────

def test_pearson_matches_known_values():
    assert pearson([1, 2, 3, 4], [2, 4, 6, 8]) == pytest.approx(1.0)
    assert pearson([1, 2, 3, 4], [8, 6, 4, 2]) == pytest.approx(-1.0)


def test_pearson_rejects_degenerate_input():
    assert pearson([1, 2], [1, 2]) is None            # too few
    assert pearson([1, 1, 1], [1, 2, 3]) is None      # zero variance
    assert pearson([1, 2, 3], [1, 2]) is None         # mismatched lengths


def test_spearman_is_one_for_any_monotone_relationship():
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert spearman(xs, [1, 4, 9, 16, 25]) == pytest.approx(1.0)
    assert spearman(xs, [math.exp(x) for x in xs]) == pytest.approx(1.0)


def test_spearman_differs_from_pearson_on_an_outlier():
    """Why rank correlation: one fat tail must not decide the answer."""
    xs = [1.0, 2.0, 3.0, 4.0, 100.0]
    ys = [5.0, 4.0, 3.0, 2.0, 100.0]
    assert pearson(xs, ys) > 0.9        # dominated by the outlier
    assert spearman(xs, ys) < 0.9       # ranks keep the disagreement visible


def test_ranks_average_ties():
    assert _ranks([10.0, 20.0, 20.0, 30.0]) == [1.0, 2.5, 2.5, 4.0]
    assert _ranks([5.0, 5.0, 5.0]) == [2.0, 2.0, 2.0]


def test_all_ties_produce_no_correlation():
    assert spearman([1.0, 1.0, 1.0, 1.0], [1.0, 2.0, 3.0, 4.0]) is None


def test_t_stat_grows_with_sample_size():
    small = ic_t_stat(0.1, 30)
    large = ic_t_stat(0.1, 3000)
    assert small is not None and large is not None
    assert abs(large) > abs(small)
    assert ic_t_stat(0.1, 3) is None
    # A perfect IC is clamped, not rejected: it must not read "insignificant".
    perfect = ic_t_stat(1.0, 100)
    assert perfect is not None and perfect > 100


# ── IC on synthetic observations ────────────────────────────────────────

def _observation(at_minute: int, *, good: float, noise: float,
                 outcome: float, horizon: int = 60) -> Observation:
    return Observation(
        at=START + timedelta(minutes=at_minute),
        components={"good": good, "noise": noise},
        score=good * 100.0,
        forward_returns={horizon: outcome},
    )


def test_ic_detects_a_predictive_component_and_rejects_noise():
    observations = []
    for i in range(400):
        signal = (i % 21 - 10) / 10.0            # -1 .. +1, repeating
        observations.append(_observation(
            i * 5, good=signal, noise=((i * 37) % 17 - 8) / 10.0,
            outcome=signal * 0.01))              # outcome follows `good` exactly

    good = information_coefficient(observations, "good", 60)
    noise = information_coefficient(observations, "noise", 60)
    assert good.ic == pytest.approx(1.0, abs=1e-6)
    assert good.significant
    assert abs(noise.ic) < 0.3


def test_independent_subsample_is_smaller_than_the_overlapping_one():
    """Overlap inflates apparent significance; the honest n must be smaller."""
    observations = [_observation(i * 5, good=(i % 7) / 7.0, noise=0.0,
                                 outcome=(i % 7) / 700.0, horizon=120)
                    for i in range(600)]
    result = information_coefficient(observations, "good", 120)
    assert result.n_overlapping == 600
    assert result.n_independent == pytest.approx(600 / (120 / 5), abs=1)
    assert result.n_independent < result.n_overlapping


def test_ic_reports_none_when_a_component_is_absent():
    observations = [_observation(i * 5, good=0.1, noise=0.1, outcome=0.001)
                    for i in range(10)]
    result = information_coefficient(observations, "not_a_component", 60)
    assert result.ic is None and result.n_overlapping == 0


def test_information_coefficients_covers_every_weighted_component():
    observations = [_observation(i * 5, good=(i % 5) / 5.0, noise=0.0,
                                 outcome=(i % 5) / 500.0)
                    for i in range(100)]
    results = information_coefficients(observations, [60])
    names = {r.component for r in results}
    assert SCORE_KEY in names
    assert "higher_timeframe_trend" in names and "order_flow" in names


# ── correlation ─────────────────────────────────────────────────────────

def test_component_correlation_finds_duplicated_opinions():
    observations = []
    for i in range(200):
        value = (i % 13 - 6) / 6.0
        observations.append(Observation(
            at=START + timedelta(minutes=5 * i),
            components={"higher_timeframe_trend": value,
                        "market_structure": value,          # identical
                        "breakout_quality": -value},        # mirrored
            score=value * 100.0, forward_returns={60: 0.0}))
    matrix = component_correlation(observations)
    assert matrix[("higher_timeframe_trend", "market_structure")] == pytest.approx(1.0)
    assert matrix[("higher_timeframe_trend", "breakout_quality")] == pytest.approx(-1.0)


def test_component_correlation_skips_components_never_present():
    observations = [Observation(at=START, components={"market_structure": 0.2},
                                score=20.0, forward_returns={60: 0.0})]
    assert component_correlation(observations) == {}


# ── calibration ─────────────────────────────────────────────────────────

def _scored(score: float, outcome: float) -> Observation:
    return Observation(at=START, components={}, score=score,
                       forward_returns={60: outcome})


def test_calibration_buckets_by_the_live_zone_edges():
    observations = ([_scored(50.0, 0.01)] * 10 + [_scored(0.0, 0.0)] * 5
                    + [_scored(-50.0, -0.01)] * 8)
    buckets = {b.label: b for b in score_calibration(observations, 60)}
    assert buckets["+40..+100"].n == 10
    assert buckets["-30..+30"].n == 5
    assert buckets["-100..-40"].n == 8


def test_hit_rate_is_signed_by_the_band():
    """A bearish band that correctly predicts falls must score high, not low."""
    bullish = score_calibration([_scored(50.0, 0.01)] * 4, 60)
    bearish = score_calibration([_scored(-50.0, -0.01)] * 4, 60)
    assert next(b for b in bullish if b.low == 40).hit_rate == 1.0
    assert next(b for b in bearish if b.high == -40).hit_rate == 1.0


def test_a_useless_signal_calibrates_to_a_coin_toss():
    observations = [_scored(50.0, 0.01 if i % 2 else -0.01) for i in range(100)]
    bucket = next(b for b in score_calibration(observations, 60) if b.low == 40)
    assert bucket.hit_rate == pytest.approx(0.5)
    assert bucket.mean_return == pytest.approx(0.0, abs=1e-9)


def test_empty_bucket_is_reported_not_dropped():
    buckets = score_calibration([_scored(50.0, 0.01)], 60)
    assert len(buckets) == 5
    assert isinstance(buckets[0], CalibrationBucket) and buckets[0].n == 0


# ── replay over candles: look-ahead safety ──────────────────────────────

def _series(count: int, *, drift: float = 0.0004, seed: int = 7) -> list[Candle]:
    """Geometric drift plus deterministic noise -- prices stay positive."""
    candles: list[Candle] = []
    price = 60_000.0
    state = seed
    for i in range(count):
        state = (state * 1103515245 + 12345) % 2147483648
        shock = (state / 2147483648.0 - 0.5) * 0.004
        open_price = price
        price = max(1.0, price * (1.0 + drift + shock))
        high = max(open_price, price) * 1.001
        low = min(open_price, price) * 0.999
        candles.append(Candle(
            symbol="BTCUSD", resolution="5m",
            start=START + timedelta(minutes=5 * i),
            open=Decimal(str(round(open_price, 2))),
            high=Decimal(str(round(high, 2))),
            low=Decimal(str(round(low, 2))),
            close=Decimal(str(round(price, 2))),
            volume=Decimal("10"), closed=True))
    return candles


def test_collect_observations_scores_history():
    candles = _series(3_200)
    observations = collect_observations(
        candles, horizons_minutes=[30, 60], warmup=2_880)
    assert observations
    for obs in observations:
        assert -100.0 <= obs.score <= 100.0
        assert set(obs.forward_returns) == {30, 60}


def test_forward_return_is_measured_from_the_next_bar_open():
    candles = _series(3_000)
    observations = collect_observations(
        candles, horizons_minutes=[30], warmup=2_880)
    assert observations

    opens = [float(c.open) for c in candles]
    starts = [c.start for c in candles]
    obs = observations[0]
    index = starts.index(obs.at)
    expected = opens[index + 1 + 30 // 5] / opens[index + 1] - 1.0
    assert obs.forward_returns[30] == pytest.approx(expected, rel=1e-12)


def test_replay_cannot_see_the_future():
    """Rewriting every candle after the decision must not move any score.

    This is the property that makes the whole study meaningful: if future
    bars leaked into the features, every component would look predictive.
    """
    candles = _series(3_000)
    baseline = collect_observations(candles, horizons_minutes=[30],
                                    warmup=2_880)
    assert baseline

    cutoff = baseline[0].at
    tampered = []
    for candle in candles:
        if candle.start <= cutoff:
            tampered.append(candle)
            continue
        # An absurd future spike: visible to any leak, invisible otherwise.
        tampered.append(Candle(
            symbol=candle.symbol, resolution=candle.resolution,
            start=candle.start, open=candle.open * 5, high=candle.high * 5,
            low=candle.low * 5, close=candle.close * 5,
            volume=candle.volume, closed=True))

    after = collect_observations(tampered, horizons_minutes=[30], warmup=2_880)
    assert after and after[0].at == cutoff
    assert after[0].score == pytest.approx(baseline[0].score)
    for name, value in baseline[0].components.items():
        assert after[0].components[name] == pytest.approx(value)


def test_horizons_must_be_whole_bars():
    candles = _series(3_000)
    for bad in (0, -30, 7):
        with pytest.raises(ValueError):
            collect_observations(candles, horizons_minutes=[bad], warmup=2_880)


def test_too_little_history_yields_no_observations():
    assert collect_observations(_series(50), horizons_minutes=[30],
                                warmup=2_880) == []


def test_longest_horizon_never_runs_past_the_data():
    candles = _series(3_100)
    observations = collect_observations(
        candles, horizons_minutes=[30, 240], warmup=2_880)
    last_start = candles[-1].start
    for obs in observations:
        assert obs.at + timedelta(minutes=240 + 5) <= last_start


# ── path outcome and the sideways gate profile ──────────────────────────

def test_excursion_measures_the_path_not_the_destination():
    """The case the signed return cannot see: price travels a long way and
    comes back, which is exactly what a short straddle is hurt by."""
    candles = _series(3_100)
    observations = collect_observations(
        candles, horizons_minutes=[60], warmup=2_880)
    assert observations
    for obs in observations:
        excursion = obs.forward_max_excursion[60]
        # An excursion is a distance, so it is never negative and can never be
        # smaller than the move actually realised by the endpoint.
        assert excursion >= 0.0
        assert excursion >= abs(obs.forward_returns[60]) - 1e-12


def test_excursion_equals_the_widest_move_inside_the_window():
    """Recomputed straight from the candles: the excursion must span exactly
    the bars traversed between the entry open and the horizon open, so the
    path and the endpoint return describe the same window."""
    candles = _series(3_100)
    observations = collect_observations(
        candles, horizons_minutes=[30], warmup=2_880)
    assert observations
    obs = observations[0]

    index = next(i for i, c in enumerate(candles) if c.start == obs.at)
    entry = float(candles[index + 1].open)
    traversed = candles[index + 1:index + 1 + 30 // 5]
    expected = max(max(float(c.high) for c in traversed) - entry,
                   entry - min(float(c.low) for c in traversed)) / entry
    assert obs.forward_max_excursion[30] == pytest.approx(expected)


def test_observations_carry_the_regime_and_the_widest_component():
    candles = _series(3_100)
    observations = collect_observations(
        candles, horizons_minutes=[30], warmup=2_880)
    assert observations
    for obs in observations:
        assert obs.regime is not None
        assert obs.max_abs_component is not None
        # Display scale, so it must bound every component the study recorded.
        widest = max((abs(v) for v in obs.components.values()), default=0.0)
        assert obs.max_abs_component >= widest * 100.0 - 1e-6


def _obs(minute: int, score: float, *, excursion: float,
         regime: str = "RANGE", widest: float = 5.0,
         adx: float | None = 20.0) -> Observation:
    # adx defaults to a calm reading on purpose: leaving it None would make
    # the live ADX stage reject every bar, and every stage below it would then
    # be asserted against an empty set -- passing vacuously.
    return Observation(
        at=START + timedelta(minutes=minute), components={}, score=score,
        forward_returns={60: 0.0}, forward_max_excursion={60: excursion},
        regime=regime, max_abs_component=widest, adx=adx)


def test_confirmation_needs_consecutive_bars_and_a_gap_resets_it():
    inside = [_obs(5 * i, 0.0, excursion=0.001) for i in range(8)]
    flags = confirmation_flags(inside, confirmation_bars=6)
    # Six in a row means the sixth bar is the first confirmed one.
    assert flags[:5] == [False] * 5
    assert flags[5] is True and flags[7] is True

    # Same bars, but one is missing: the run restarts rather than assuming
    # continuity across the hole.
    gapped = inside[:3] + [_obs(5 * i, 0.0, excursion=0.001)
                           for i in range(4, 9)]
    assert confirmation_flags(gapped, confirmation_bars=6)[:7] == [False] * 7


def test_a_score_leaving_the_band_resets_the_confirmation_run():
    bars = ([_obs(5 * i, 0.0, excursion=0.001) for i in range(5)]
            + [_obs(25, 80.0, excursion=0.001)]
            + [_obs(5 * i, 0.0, excursion=0.001) for i in range(6, 11)])
    assert not any(confirmation_flags(bars, confirmation_bars=6))


def test_gate_profile_stages_are_cumulative_and_report_excursion():
    calm = [_obs(5 * i, 0.0, excursion=0.001) for i in range(10)]
    loud = [_obs(5 * (10 + i), 80.0, excursion=0.05) for i in range(10)]
    profiles = sideways_gate_profile(calm + loud, 60)

    by_label = {p.label: p for p in profiles}
    assert by_label["all scored bars"].n == 20
    band = next(p for p in profiles if p.label.startswith("|score|"))
    assert band.n == 10                       # the loud half scores 80
    assert band.mean_excursion == pytest.approx(0.001)
    # Each stage may only ever remove bars.
    counts = [p.n for p in profiles]
    assert counts == sorted(counts, reverse=True)


def test_gate_profile_applies_the_regime_veto():
    shocked = [_obs(5 * i, 0.0, excursion=0.02, regime="HIGH_VOL_SHOCK")
               for i in range(10)]
    profiles = sideways_gate_profile(shocked, 60)
    assert next(p for p in profiles if p.label == "+ regime safe").n == 0


def test_component_ceilings_tighten_in_order_regardless_of_input_order():
    bars = [_obs(5 * i, 0.0, excursion=0.001, widest=10.0 * (i % 6))
            for i in range(12)]
    profiles = sideways_gate_profile(bars, 60, component_ceilings=(20, 50, 35))
    ceiling_rows = [p for p in profiles if "max|component|" in p.label]
    assert [p.label for p in ceiling_rows] == [
        "+ max|component| <= 50",
        "+ max|component| <= 35",
        "+ max|component| <= 20",
    ]
    assert [p.n for p in ceiling_rows] == sorted(
        [p.n for p in ceiling_rows], reverse=True)


def test_gate_profile_refuses_observations_it_cannot_replay():
    """Silently dropping bars that lack the regime would shrink the sample
    without saying so, which is how a profile lies."""
    bare = [Observation(at=START, components={}, score=0.0,
                        forward_returns={60: 0.0},
                        forward_max_excursion={60: 0.001})]
    with pytest.raises(ValueError, match="regime"):
        sideways_gate_profile(bare, 60)


def test_quantile_interpolates_between_neighbours():
    assert _quantile([0.0, 1.0], 0.5) == pytest.approx(0.5)
    assert _quantile([0.0, 10.0, 20.0], 0.9) == pytest.approx(18.0)
    assert _quantile([], 0.5) == 0.0
    assert _quantile([4.0], 0.9) == 4.0


# ── the live calm test (ADX), which replaced the confirmation window ────

def test_gate_profile_applies_the_live_adx_calm_test():
    """zones.decide refuses SHORT_MOVE unless the 5m ADX is calm, so the
    profile must replay that rather than a retired approximation."""
    calm = [_obs(5 * i, 0.0, excursion=0.001, adx=15.0) for i in range(10)]
    trending = [_obs(5 * (10 + i), 0.0, excursion=0.02, adx=45.0)
                for i in range(10)]

    profiles = sideways_gate_profile(calm + trending, 60)
    adx_stage = next(p for p in profiles if "ADX" in p.label)
    assert adx_stage.n == 10                    # only the calm half survives
    assert adx_stage.mean_excursion == pytest.approx(0.001)


def test_bars_without_an_adx_reading_cannot_pass_the_calm_test():
    """A test that was never computed was never passed."""
    unknown = [_obs(5 * i, 0.0, excursion=0.001, adx=None) for i in range(6)]
    profiles = sideways_gate_profile(unknown, 60)
    assert next(p for p in profiles if "ADX" in p.label).n == 0


def test_calm_adx_threshold_is_configurable_and_can_be_disabled():
    bars = [_obs(5 * i, 0.0, excursion=0.001, adx=25.0) for i in range(6)]

    # Tighter than the readings: nothing survives.
    strict = sideways_gate_profile(bars, 60, calm_adx_max=20.0)
    assert next(p for p in strict if "ADX" in p.label).n == 0

    # Disabled entirely: the stage is absent, not silently passing.
    off = sideways_gate_profile(bars, 60, calm_adx_max=None)
    assert not any("ADX" in p.label for p in off)


def test_retired_confirmation_stage_is_off_by_default():
    """It is opt-in now: defaulting it on would misreport the live gate."""
    bars = [_obs(5 * i, 0.0, excursion=0.001) for i in range(8)]
    assert not any("confirmation" in p.label
                   for p in sideways_gate_profile(bars, 60))
    opted_in = sideways_gate_profile(bars, 60, confirmation_bars=6)
    stage = next(p for p in opted_in if "confirmation" in p.label)
    assert "retired" in stage.label and stage.n > 0
