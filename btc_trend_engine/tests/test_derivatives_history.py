"""Tests for the reconstructed derivatives history.

Same standard as the rest of the study: values must match what production
would have computed from a live ticker, and a lookup must never see a bar
that had not yet closed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from btc_trend_engine.market_data.messages import Candle
from btc_trend_engine.research.derivatives_history import DerivativesHistory

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _series(values, symbol="X", start=START):
    return [
        Candle(symbol=symbol, resolution="1h",
               start=start + timedelta(hours=i),
               open=Decimal(str(v)), high=Decimal(str(v)),
               low=Decimal(str(v)), close=Decimal(str(v)),
               volume=Decimal("1"), closed=True)
        for i, v in enumerate(values)
    ]


def test_basis_matches_the_production_formula():
    history = DerivativesHistory(mark=_series([60_600.0]),
                                 spot=_series([60_000.0]))
    features = history(START + timedelta(hours=1))
    assert features["mark_spot_basis_pct"] == pytest.approx(1.0)


def test_negative_basis_is_reported_signed():
    history = DerivativesHistory(mark=_series([59_400.0]),
                                 spot=_series([60_000.0]))
    assert history(START + timedelta(hours=1))["mark_spot_basis_pct"] == (
        pytest.approx(-1.0))


def test_lookup_cannot_see_a_bar_that_has_not_closed():
    """The 01:00 bar covers 01:00-02:00 and is unknown until 02:00."""
    history = DerivativesHistory(mark=_series([100.0, 999.0]),
                                 spot=_series([100.0, 100.0]))
    # At 01:30 only the 00:00 bar has closed.
    mid = history(START + timedelta(hours=1, minutes=30))
    assert mid["mark_spot_basis_pct"] == pytest.approx(0.0)
    # At 02:00 the 01:00 bar is available and the spike appears.
    after = history(START + timedelta(hours=2))
    assert after["mark_spot_basis_pct"] > 100


def test_no_data_before_the_first_close():
    history = DerivativesHistory(mark=_series([100.0]), spot=_series([100.0]))
    assert history(START) == {}
    assert history(START + timedelta(minutes=59)) == {}


def test_funding_percentile_ranks_within_the_trailing_window():
    # 20 rising values; the final one is the highest seen.
    history = DerivativesHistory(funding=_series([float(i) for i in range(20)]))
    features = history(START + timedelta(hours=20))
    assert features["funding_rate"] == pytest.approx(19.0)
    assert features["funding_percentile"] == pytest.approx(19 / 20)


def test_funding_percentile_needs_eight_samples():
    """percentile_rank returns None below 8, so the key must be absent."""
    history = DerivativesHistory(funding=_series([1.0, 2.0, 3.0]))
    features = history(START + timedelta(hours=3))
    assert "funding_rate" in features
    assert "funding_percentile" not in features


def test_as_running_mode_omits_funding_percentile():
    """Production never passes funding_history, so the key is never set."""
    values = [float(i) for i in range(20)]
    designed = DerivativesHistory(funding=_series(values))
    running = DerivativesHistory(funding=_series(values),
                                 include_funding_percentile=False)
    at = START + timedelta(hours=20)
    assert "funding_percentile" in designed(at)
    assert "funding_percentile" not in running(at)
    assert running(at)["funding_rate"] == designed(at)["funding_rate"]


def test_oi_change_uses_notional_not_contract_count():
    """OI notional = contracts x mark, so a price move alone changes it."""
    hours = 8
    contracts = [100.0] * hours                    # contract count flat
    marks = [100.0] * 2 + [200.0] * (hours - 2)    # price doubles early
    history = DerivativesHistory(mark=_series(marks), spot=_series(marks),
                                 open_interest=_series(contracts))
    features = history(START + timedelta(hours=hours))
    # Flat contracts but a doubled mark => notional OI grew.
    assert features["oi_change_6h_pct"] > 0


def test_oi_change_absent_until_six_hours_of_history():
    contracts = [100.0] * 4
    history = DerivativesHistory(mark=_series([100.0] * 4),
                                 spot=_series([100.0] * 4),
                                 open_interest=_series(contracts))
    assert "oi_change_6h_pct" not in history(START + timedelta(hours=4))


def test_growing_open_interest_is_positive_and_shrinking_negative():
    flat_mark = _series([100.0] * 10)
    rising = DerivativesHistory(mark=flat_mark, spot=flat_mark,
                                open_interest=_series(
                                    [100, 100, 100, 100, 110, 120, 130, 140,
                                     150, 160]))
    falling = DerivativesHistory(mark=flat_mark, spot=flat_mark,
                                 open_interest=_series(
                                     [160, 150, 140, 130, 120, 110, 100, 100,
                                      100, 100]))
    at = START + timedelta(hours=10)
    assert rising(at)["oi_change_6h_pct"] > 0
    assert falling(at)["oi_change_6h_pct"] < 0


def test_empty_history_yields_no_features_rather_than_zeros():
    """A missing input must be absent, never a fabricated 0.0."""
    assert DerivativesHistory()(START + timedelta(days=1)) == {}


def test_all_four_features_present_with_full_history():
    hours = 40
    marks = _series([60_000.0 + i for i in range(hours)])
    spots = _series([60_000.0] * hours)
    funding = _series([(i % 11) * 0.001 for i in range(hours)])
    oi = _series([1_000.0 + 10 * i for i in range(hours)])
    features = DerivativesHistory(mark=marks, spot=spots, funding=funding,
                                  open_interest=oi)(START + timedelta(hours=hours))
    assert set(features) == {"mark_spot_basis_pct", "funding_rate",
                             "funding_percentile", "oi_change_6h_pct"}
