from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from trend_scenario import conservative_intrinsic_scenario, prepare_scenario_history


BAR = timedelta(minutes=5)


def _history(
    days=7,
    *,
    start=datetime(2026, 7, 1, tzinfo=timezone.utc),
    opened=200.0,
    high=206.0,
    low=198.0,
    close=200.0,
    partial_bars=0,
):
    rows = []
    for index in range(days * 288 + partial_bars):
        rows.append({
            "timestamp": (start + BAR * index).isoformat(),
            "open": opened,
            "high": high,
            "low": low,
            "close": close,
        })
    return rows


def _call(history, **overrides):
    latest = overrides.get("now")
    if latest is None:
        latest = max(
            datetime.fromisoformat(row["timestamp"]) for row in history
        ) + BAR
    values = {
        "spot": 100,
        "target_price": 102,
        "invalidation_price": 98,
        "option_type": "CE",
        "strike": 100,
        "entry_price": 1,
        "contract_value": 1,
        "costs_per_lot": 0,
        "now": latest,
        "time_exit": latest + timedelta(hours=1),
    }
    values.update(overrides)
    return conservative_intrinsic_scenario(history, **values)


def test_bullish_replays_scale_to_spot_and_use_intrinsic_only():
    # Historical bars are around 200, but scale to 100.  Their high becomes
    # 103, so every CE replay reaches the current 102 target.
    result = _call(_history())

    assert result["valid"] is True
    assert result["reason"] == "OK"
    assert result["expected_value_pass"] is True
    assert result["net_expected_value_per_lot"] == 1
    assert result["target_option_price"] == 2
    assert result["stop_option_price"] == 0
    assert result["probability_win"] is None
    assert result["probability_validated"] is False
    audit = result["audit"]
    assert audit["valuation_method"] == "intrinsic_only_zero_residual_time_value"
    assert audit["complete_day_count"] == 7
    assert audit["total_path_count"] == 7 * 24
    assert audit["target_exit_path_count"] == audit["total_path_count"]
    assert audit["net_ev_quantiles_per_lot"] == {"p20": 1, "p50": 1, "p80": 1}
    assert len(audit["history_sha256"]) == 64


def test_costs_and_contract_value_are_applied_per_lot():
    result = _call(
        _history(), entry_price=1, contract_value=0.5, costs_per_lot=0.25
    )

    # (target intrinsic 2 - entry 1) * 0.5 - 0.25
    assert result["net_expected_value_per_lot"] == 0.25
    assert result["expected_value_pass"] is True


def test_non_positive_conservative_ev_is_valid_but_fails_the_edge_gate():
    result = _call(_history(high=202, low=198, close=200))

    assert result["valid"] is True
    assert result["reason"] == "NEGATIVE_EXPECTED_VALUE"
    assert result["expected_value_pass"] is False
    assert result["net_expected_value_per_lot"] == -1
    assert result["audit"]["time_exit_path_count"] == result["audit"][
        "total_path_count"
    ]


def test_same_bar_target_and_invalidation_assumes_stop_first():
    result = _call(_history(high=206, low=194, close=200))

    assert result["net_expected_value_per_lot"] == -1
    assert result["audit"]["ambiguous_bar_stop_first_count"] == result[
        "audit"
    ]["total_path_count"]
    assert result["audit"]["invalidation_exit_path_count"] == result[
        "audit"
    ]["total_path_count"]


def test_put_replay_is_directionally_symmetric():
    # Scale low 194 / 200 to 97, crossing the PE target at 98 without crossing
    # its invalidation at 102.
    history = _history(high=202, low=194, close=200)
    result = _call(
        history,
        option_type="PE",
        target_price=98,
        invalidation_price=102,
    )

    assert result["valid"] is True
    assert result["expected_value_pass"] is True
    assert result["target_option_price"] == 2
    assert result["net_expected_value_per_lot"] == 1


def test_complete_days_are_used_and_edge_partial_days_are_ignored():
    # Begin with a partial UTC day, followed by seven whole UTC days, then a
    # recent partial day so the history is fresh relative to now.
    start = datetime(2026, 7, 1, 12, tzinfo=timezone.utc)
    history = _history(days=8, start=start, partial_bars=12)
    result = _call(history)

    assert result["valid"] is True
    assert result["audit"]["complete_day_count"] == 7
    assert result["audit"]["used_utc_days"] == [
        f"2026-07-{day:02d}" for day in range(2, 9)
    ]
    assert result["audit"]["input_bar_count"] > result["audit"][
        "history_bar_count"
    ]


def test_replay_is_reproducible_and_input_order_independent():
    history = _history()
    original = deepcopy(history)

    first = _call(history)
    second = _call(list(reversed(history)))

    assert first == second
    assert history == original


def test_prepared_history_reuses_identical_validated_evidence():
    history = _history()
    now = datetime(2026, 7, 8, tzinfo=timezone.utc)
    prepared = prepare_scenario_history(history, now=now, min_complete_days=7)

    direct = _call(history, now=now)
    reused = conservative_intrinsic_scenario(
        history,
        spot=100,
        target_price=102,
        invalidation_price=98,
        option_type="CE",
        strike=100,
        entry_price=1,
        contract_value=1,
        costs_per_lot=0,
        now=now,
        time_exit=now + timedelta(hours=1),
        prepared_history=prepared,
    )

    assert prepared["valid"] is True
    assert reused == direct


def test_history_accepts_utc_datetime_and_epoch_time_rows():
    datetime_rows = _history()
    for row in datetime_rows:
        row["timestamp"] = datetime.fromisoformat(row["timestamp"])
    datetime_result = _call(
        datetime_rows,
        now=datetime(2026, 7, 8, tzinfo=timezone.utc),
    )

    epoch_rows = _history()
    for row in epoch_rows:
        stamp = datetime.fromisoformat(row.pop("timestamp"))
        row["time"] = int(stamp.timestamp() * 1_000_000)
    epoch_result = _call(
        epoch_rows,
        now=datetime(2026, 7, 8, tzinfo=timezone.utc),
    )

    assert datetime_result["valid"] is True
    assert epoch_result["valid"] is True
    assert datetime_result["audit"]["history_sha256"] == epoch_result["audit"][
        "history_sha256"
    ]


def test_requested_horizon_is_floored_and_never_exceeds_time_exit():
    result = _call(_history(), time_exit=datetime(2026, 7, 8, 1, 7, tzinfo=timezone.utc))

    assert result["valid"] is True
    assert result["audit"]["requested_horizon_seconds"] == 67 * 60
    assert result["audit"]["evaluated_horizon_seconds"] == 65 * 60
    assert result["audit"]["horizon_bars"] == 13


def test_lower_quantile_is_taken_across_equal_weighted_daily_means():
    history = _history(high=202, low=198, close=200)
    # Make two of seven complete UTC days profitable; five remain -1.  The
    # P20 daily-regime value must therefore remain negative.
    for row in history:
        day = datetime.fromisoformat(row["timestamp"]).day
        if day in {6, 7}:
            row["high"] = 206
    result = _call(history)

    assert result["valid"] is True
    assert result["net_expected_value_per_lot"] == -1
    assert result["audit"]["net_ev_quantiles_per_lot"]["p50"] == -1
    assert result["audit"]["net_ev_quantiles_per_lot"]["p80"] == pytest.approx(0.6)


@pytest.mark.parametrize(
    ("history_factory", "overrides", "reason"),
    [
        (lambda: _history(days=6), {}, "INSUFFICIENT_COMPLETE_DAYS"),
        (lambda: _history(), {"now": datetime(2026, 7, 8, 0, 11, tzinfo=timezone.utc)},
         "STALE_HISTORY"),
        (lambda: _history(), {"time_exit": datetime(2026, 7, 8, tzinfo=timezone.utc)},
         "INVALID_HORIZON"),
        (lambda: _history(), {"time_exit": datetime(2026, 7, 8, 0, 4, tzinfo=timezone.utc)},
         "HORIZON_TOO_SHORT"),
        (lambda: _history(), {"time_exit": datetime(2026, 7, 9, 0, 5, tzinfo=timezone.utc)},
         "HORIZON_TOO_LONG"),
        (lambda: _history(), {"target_price": 99}, "INVALID_BARRIERS"),
        (lambda: _history(), {"option_type": "CALL"}, "INVALID_INPUT"),
        (lambda: _history(), {"lower_quantile": 0.5}, "INVALID_INPUT"),
        (lambda: _history(), {"min_complete_days": 7.5}, "INVALID_INPUT"),
    ],
)
def test_invalid_inputs_fail_closed_with_stable_probability_fields(
    history_factory, overrides, reason
):
    history = history_factory()
    result = _call(history, **overrides)

    assert result["valid"] is False
    assert result["reason"] == reason
    assert result["expected_value_pass"] is False
    assert result["net_expected_value_per_lot"] is None
    assert result["probability_win"] is None
    assert result["probability_validated"] is False
    assert result["audit"]["validation_error"]


def test_gap_duplicate_unfinished_and_bad_ohlc_history_are_rejected():
    base = _history()

    gap = base[:100] + base[101:]
    duplicate = base[:100] + [deepcopy(base[99])] + base[100:]
    unfinished = deepcopy(base)
    latest = datetime.fromisoformat(unfinished[-1]["timestamp"])
    bad_ohlc = deepcopy(base)
    bad_ohlc[10]["high"] = bad_ohlc[10]["low"] - 1

    assert _call(gap)["reason"] == "IRREGULAR_HISTORY"
    assert _call(duplicate)["reason"] == "DUPLICATE_HISTORY"
    assert _call(unfinished, now=latest + timedelta(minutes=4))["reason"] == (
        "UNFINISHED_HISTORY"
    )
    assert _call(bad_ohlc)["reason"] == "INVALID_HISTORY"


def test_naive_history_timestamp_is_rejected_instead_of_assuming_utc():
    history = _history()
    history[0]["timestamp"] = "2026-07-01T00:00:00"

    result = _call(history, now=datetime(2026, 7, 8, tzinfo=timezone.utc))

    assert result["valid"] is False
    assert result["reason"] == "INVALID_INPUT"
    assert "UTC offset" in result["audit"]["validation_error"]
