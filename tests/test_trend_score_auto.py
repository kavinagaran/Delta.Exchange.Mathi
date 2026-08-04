from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from trend_score_auto import (
    AUTO_TRADE_LOTS,
    CE_2_ITM,
    HOLD,
    PE_2_ITM,
    PE_3_ITM,
    SHORT_MOVE,
    TrendScoreAutoInputError,
    completed_candle_signal_key,
    plan_score_transition,
    score_zone,
    select_directional_option,
    select_move_contract,
    short_move_adx_exit_required,
)


NOW = datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)


def _products(expiry, strikes):
    rows = []
    expiry_code = expiry.strftime("%d%m%y")
    product_id = 10_000
    for strike in strikes:
        for prefix in ("C", "P"):
            product_id += 1
            rows.append({
                "id": product_id,
                "symbol": f"{prefix}-BTC-{strike}-{expiry_code}",
                "state": "live",
                "trading_status": "operational",
                "underlying_asset": {"symbol": "BTC"},
                "strike_price": str(strike),
                "settlement_time": expiry.isoformat(),
                "contract_value": "0.001",
            })
    return rows


def _executable(product, **changes):
    row = {
        "product_id": product["id"],
        "symbol": product["symbol"],
        "option_type": "CE" if product["symbol"].startswith("C-") else "PE",
        "strike": float(product["strike_price"]),
        "expiry": product["settlement_time"],
        "bid": 99.0,
        "ask": 100.0,
        "lot_size": 1,
        "max_order_lots": 10_000,
        "trading_status": "operational",
    }
    row.update(changes)
    return row


def _move_product(expiry, strike, *, product_id=20_001, **changes):
    row = {
        "id": product_id,
        "symbol": f"MV-BTC-{strike}-{expiry.strftime('%d%m%y')}",
        "state": "live",
        "trading_status": "operational",
        "underlying_asset": {"symbol": "BTC"},
        "strike_price": str(strike),
        "settlement_time": expiry.isoformat(),
        "contract_value": "0.001",
        "position_size_limit": "10000",
    }
    row.update(changes)
    return row


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        # Directional only beyond |40|; exact +/-40 and every intermediate
        # score are HOLD. Only +/-30 is a SHORT_MOVE candidate.
        (-100, PE_2_ITM),
        (-40, HOLD),
        (-39.999, HOLD),
        (-30.001, HOLD),
        (-30, SHORT_MOVE),
        (0, SHORT_MOVE),
        (30, SHORT_MOVE),
        (30.001, HOLD),
        (39.999, HOLD),
        (40, HOLD),
        (100, CE_2_ITM),
    ],
)
def test_score_zone_uses_strict_directional_boundaries(score, expected):
    assert score_zone(score) == expected


def test_score_zone_delegates_to_the_engine_so_thresholds_cannot_drift():
    """The bands are defined once, in btc_trend_engine.signals.zones. Two
    copies of a trading threshold is how they end up disagreeing."""
    from btc_trend_engine.signals import zones

    for score in (-100, -40, -35, -30, 0, 30, 35, 40, 100):
        assert score_zone(score) == zones.zone_for_score(float(score))


@pytest.mark.parametrize("score", [None, True, float("nan"), float("inf"), -100.01, 100.01])
def test_score_zone_rejects_missing_nonfinite_and_out_of_range_scores(score):
    with pytest.raises(TrendScoreAutoInputError):
        score_zone(score)


def test_completed_candle_signal_key_is_stable_across_wall_clock_and_quotes():
    snapshot = {
        "underlying": "BTCUSD",
        "timestamp": "2026-07-22T10:04:58Z",
        "market": {"spot": 65_900},
        "candles": {"5m": [
            {"timestamp": "2026-07-22T09:55:00Z", "open": 1, "high": 2,
             "low": 1, "close": 2, "volume": 10, "complete": True},
            {"timestamp": "2026-07-22T10:00:00Z", "open": 2, "high": 3,
             "low": 2, "close": 3, "volume": 11, "complete": True},
            {"timestamp": "2026-07-22T10:05:00Z", "open": 3, "high": 4,
             "low": 3, "close": 4, "volume": 12, "complete": False},
        ]},
    }
    first = completed_candle_signal_key(snapshot)
    snapshot["timestamp"] = "2026-07-22T10:09:59Z"
    snapshot["market"]["spot"] = 66_100
    second = completed_candle_signal_key(snapshot)
    assert first == second
    assert first.endswith("2026-07-22T10:00:00Z")


def test_completed_candle_signal_key_rejects_conflicting_duplicate_event():
    snapshot = {"candles": {"5m": [
        {"timestamp": "2026-07-22T10:00:00Z", "open": 1, "high": 2,
         "low": 1, "close": 2, "volume": 10, "complete": True},
        {"timestamp": "2026-07-22T10:00:00Z", "open": 1, "high": 3,
         "low": 1, "close": 2, "volume": 10, "complete": True},
    ]}}
    with pytest.raises(TrendScoreAutoInputError, match="conflicting duplicate"):
        completed_candle_signal_key(snapshot)


def test_ce_counts_two_steps_from_raw_ladder_not_executable_subset():
    expiry = NOW + timedelta(hours=6)
    products = _products(expiry, [64000, 64200, 64400, 64600, 64800, 65000])
    target = next(row for row in products if row["symbol"].startswith("C-BTC-64400-"))
    # The executable universe intentionally omits the intermediate 64600 and
    # 64800 calls. They must still count as raw listed ladder steps.
    selected = select_directional_option(
        products,
        [_executable(target)],
        spot=64850,
        zone=CE_2_ITM,
        now=NOW,
    )
    assert selected["symbol"] == target["symbol"]
    assert selected["atm_strike"] == 64800
    assert selected["strike"] == 64400
    assert selected["lots"] == AUTO_TRADE_LOTS == 1000


def test_pe_selects_three_steps_above_atm():
    expiry = NOW + timedelta(hours=6)
    products = _products(
        expiry, [64200, 64400, 64600, 64800, 65000, 65200, 65400, 65600]
    )
    target = next(row for row in products if row["symbol"].startswith("P-BTC-65400-"))
    selected = select_directional_option(
        products, [_executable(target)], spot=64850, zone=PE_3_ITM, now=NOW
    )
    assert selected["atm_strike"] == 64800
    assert selected["strike"] == 65400
    assert selected["itm_steps"] == 3


def test_atm_tie_is_resolved_to_the_lower_listed_strike():
    expiry = NOW + timedelta(hours=6)
    products = _products(expiry, [64200, 64400, 64600, 64800, 65000, 65200])
    target = next(row for row in products if row["symbol"].startswith("C-BTC-64400-"))
    selected = select_directional_option(
        products, [_executable(target)], spot=64900, zone=CE_2_ITM, now=NOW
    )
    assert selected["atm_strike"] == 64800
    assert selected["strike"] == 64400


def test_exactly_ninety_minutes_is_eligible():
    expiry = NOW + timedelta(minutes=90)
    products = _products(expiry, [64200, 64400, 64600, 64800, 65000, 65200])
    target = next(row for row in products if row["symbol"].startswith("C-BTC-64400-"))
    selected = select_directional_option(
        products, [_executable(target)], spot=64850, zone=CE_2_ITM, now=NOW
    )
    assert selected is not None
    assert selected["time_to_expiry_hours"] == 1.5


def test_sub_ninety_minute_expiry_is_skipped_without_maximum_dte():
    soon = NOW + timedelta(minutes=89, seconds=59)
    distant = NOW + timedelta(days=30)
    products = (
        _products(soon, [64200, 64400, 64600, 64800, 65000, 65200])
        + _products(distant, [64200, 64400, 64600, 64800, 65000, 65200])
    )
    target = next(
        row for row in products
        if row["symbol"].startswith("C-BTC-64400-")
        and row["settlement_time"] == distant.isoformat()
    )
    selected = select_directional_option(
        products, [_executable(target)], spot=64850, zone=CE_2_ITM, now=NOW
    )
    assert selected["expiry"] == distant.isoformat().replace("+00:00", "Z")


def test_missing_exact_target_does_not_substitute_a_strike_or_later_expiry():
    first = NOW + timedelta(hours=6)
    later = NOW + timedelta(days=1)
    products = (
        _products(first, [64200, 64400, 64600, 64800, 65000, 65200])
        + _products(later, [64200, 64400, 64600, 64800, 65000, 65200])
    )
    wrong_first = next(
        row for row in products
        if row["symbol"].startswith("C-BTC-64200-")
        and row["settlement_time"] == first.isoformat()
    )
    valid_later = next(
        row for row in products
        if row["symbol"].startswith("C-BTC-64400-")
        and row["settlement_time"] == later.isoformat()
    )
    assert select_directional_option(
        products,
        [_executable(wrong_first), _executable(valid_later)],
        spot=64850,
        zone=CE_2_ITM,
        now=NOW,
    ) is None


@pytest.mark.parametrize(
    "change",
    [
        {"ask": 0},
        {"product_id": 999999},
        {"max_order_lots": 999},
        {"option_type": "PE"},
        {"trading_status": "disrupted_post_only"},
    ],
)
def test_exact_target_must_be_executable_for_all_1000_lots(change):
    expiry = NOW + timedelta(hours=6)
    products = _products(expiry, [64200, 64400, 64600, 64800, 65000, 65200])
    target = next(row for row in products if row["symbol"].startswith("C-BTC-64400-"))
    assert select_directional_option(
        products,
        [_executable(target, **change)],
        spot=64850,
        zone=CE_2_ITM,
        now=NOW,
    ) is None


def test_selection_is_independent_of_api_row_order():
    expiry = NOW + timedelta(hours=6)
    products = _products(expiry, [64200, 64400, 64600, 64800, 65000, 65200])
    target = next(row for row in products if row["symbol"].startswith("C-BTC-64400-"))
    expected = select_directional_option(
        products, [_executable(target)], spot=64850, zone=CE_2_ITM, now=NOW
    )
    shuffled = list(products)
    random.Random(12345).shuffle(shuffled)
    actual = select_directional_option(
        shuffled, [_executable(target)], spot=64850, zone=CE_2_ITM, now=NOW
    )
    assert actual["symbol"] == expected["symbol"]
    assert actual["atm_strike"] == expected["atm_strike"]


def test_move_uses_earliest_eligible_expiry_and_lower_atm_tie():
    expiry = NOW + timedelta(hours=6)
    products = [
        _move_product(expiry, 64800, product_id=20_001),
        _move_product(expiry, 65000, product_id=20_002),
        _move_product(NOW + timedelta(days=2), 64900, product_id=20_003),
    ]
    selected = select_move_contract(products, spot=64900, now=NOW)
    assert selected["symbol"] == products[0]["symbol"]
    assert selected["strike"] == 64800
    assert selected["side"] == "sell"
    assert selected["zone"] == SHORT_MOVE
    assert selected["lots"] == 1000


def test_move_requires_more_than_ninety_minutes_without_a_session_window():
    expiry = NOW + timedelta(minutes=90)
    product = _move_product(expiry, 64800)
    # Exactly 90 minutes is intentionally excluded; there is no legacy
    # morning/evening window involved in this decision.
    assert select_move_contract([product], spot=64820, now=NOW) is None


def test_move_skips_sub_ninety_minutes_and_has_no_maximum_dte():
    soon = _move_product(
        NOW + timedelta(minutes=89, seconds=59), 64800, product_id=20_001
    )
    distant = _move_product(
        NOW + timedelta(days=30), 65000, product_id=20_002
    )
    selected = select_move_contract([soon, distant], spot=64900, now=NOW)
    assert selected["symbol"] == distant["symbol"]
    assert selected["time_to_expiry_hours"] == 30 * 24


@pytest.mark.parametrize(
    "change",
    [
        {"id": None},
        {"contract_value": "0"},
        {"contract_value": "nan"},
        {"position_size_limit": "999"},
        {"position_size_limit": "1000.5"},
    ],
)
def test_move_exact_atm_product_must_support_all_1000_lots(change):
    expiry = NOW + timedelta(hours=6)
    target = _move_product(expiry, 64800, **change)
    substitute = _move_product(expiry, 65000, product_id=20_002)
    assert select_move_contract(
        [target, substitute], spot=64810, now=NOW
    ) is None


def test_move_ignores_nonoperational_and_non_btc_products():
    expiry = NOW + timedelta(hours=6)
    disrupted = _move_product(
        expiry, 64800, product_id=20_001,
        trading_status="disrupted_post_only",
    )
    wrong_underlying = _move_product(
        expiry, 64900, product_id=20_002,
        underlying_asset={"symbol": "ETH"},
    )
    operational = _move_product(expiry, 65000, product_id=20_003)
    selected = select_move_contract(
        [disrupted, wrong_underlying, operational], spot=64810, now=NOW
    )
    assert selected["symbol"] == operational["symbol"]


def test_transition_plans_open_hold_and_same_signal_idempotency():
    opened = plan_score_transition(
        score=50, signal_key="signal-1", owned_positions=[]
    )
    assert opened["action"] == "OPEN"
    assert opened["open_zone"] == CE_2_ITM
    assert opened["consume_signal"] is True

    position = {
        "symbol": "C-BTC-64400-230726",
        "side": "long",
        "trend_score_zone": CE_2_ITM,
    }
    held = plan_score_transition(
        score=70, signal_key="signal-2", owned_positions=[position]
    )
    assert held["action"] == "HOLD"
    assert held["open_zone"] is None

    repeated = plan_score_transition(
        score=-70,
        signal_key="signal-2",
        owned_positions=[position],
        consumed_signal_keys={"signal-2": {"at": "earlier"}},
    )
    assert repeated["action"] == "NOOP"
    assert repeated["reason"] == "SIGNAL_ALREADY_CONSUMED"
    assert repeated["consume_signal"] is False


def test_transition_closes_then_opens_new_zone_on_same_signal():
    move = {
        "symbol": "MV-BTC-64800-230726",
        "side": "short",
        "trend_score_zone": SHORT_MOVE,
    }
    plan = plan_score_transition(
        score=-40.1, signal_key="signal-3", owned_positions=[move]
    )
    assert plan["action"] == "CLOSE_THEN_OPEN"
    assert plan["current_zone"] == SHORT_MOVE
    assert plan["open_zone"] == PE_2_ITM
    assert plan["close_position"] == move


@pytest.mark.parametrize(
    ("adx", "expected"),
    ((24.9, False), (25.0, True), (40.0, True), (None, False), ("bad", False)),
)
def test_short_move_adx_exit_uses_the_complementary_calm_boundary(adx, expected):
    assert short_move_adx_exit_required(adx) is expected


def test_non_calm_committed_adx_closes_only_an_open_short_move():
    move = {
        "symbol": "MV-BTC-64800-230726",
        "side": "short",
        "trend_score_zone": SHORT_MOVE,
    }
    plan = plan_score_transition(
        score=0,
        signal_key="signal-adx-exit",
        owned_positions=[move],
        short_move_adx=25.0,
    )
    assert plan["action"] == "CLOSE"
    assert plan["reason"] == "SHORT_MOVE_ADX_NO_LONGER_CALM"
    assert plan["close_position"] == move
    assert plan["open_zone"] is None


@pytest.mark.parametrize("score", [30.1, 40.0, -30.1, -40.0])
def test_score_leaving_neutral_range_closes_short_move_even_in_hold_band(score):
    move = {
        "symbol": "MV-BTC-64800-230726",
        "side": "short",
        "trend_score_zone": SHORT_MOVE,
    }
    plan = plan_score_transition(
        score=score,
        signal_key=f"signal-score-exit-{score}",
        owned_positions=[move],
        short_move_adx=24.9,
    )
    assert plan["action"] == "CLOSE"
    assert plan["target_zone"] == HOLD
    assert plan["close_position"] == move
    assert plan["open_zone"] is None


@pytest.mark.parametrize("score", [40.1, 80.0, -40.1, -80.0])
@pytest.mark.parametrize("adx", [None, 10.0, 24.9, 25.0, 60.0])
def test_directional_transition_is_independent_of_adx(score, adx):
    plan = plan_score_transition(
        score=score,
        signal_key=f"signal-directional-{score}-{adx}",
        owned_positions=[],
        short_move_adx=adx,
    )
    assert plan["action"] == "OPEN"
    assert plan["open_zone"] == (CE_2_ITM if score > 0 else PE_2_ITM)


def test_non_calm_short_move_signal_cannot_close_or_open_other_positions():
    call = {
        "symbol": "C-BTC-64400-230726",
        "side": "long",
        "trend_score_zone": CE_2_ITM,
    }
    held = plan_score_transition(
        score=0,
        signal_key="signal-adx-ce",
        owned_positions=[call],
        short_move_adx=45.0,
    )
    flat = plan_score_transition(
        score=0,
        signal_key="signal-adx-flat",
        owned_positions=[],
        short_move_adx=45.0,
    )
    assert held["action"] == "NOOP"
    assert held["close_position"] is None
    assert held["open_zone"] is None
    assert flat["action"] == "NOOP"
    assert flat["open_zone"] is None


def test_transition_fails_closed_for_multiple_or_unrecognized_positions():
    call = {"symbol": "C-BTC-X", "side": "long"}
    put = {"symbol": "P-BTC-X", "side": "long"}
    with pytest.raises(TrendScoreAutoInputError, match="at most one"):
        plan_score_transition(
            score=50,
            signal_key="signal-4",
            owned_positions=[call, put],
        )
    with pytest.raises(TrendScoreAutoInputError, match="cannot be mapped"):
        plan_score_transition(
            score=0,
            signal_key="signal-4",
            owned_positions=[{"symbol": "UNKNOWN", "side": "long"}],
        )


def test_pe_2_itm_selects_two_steps_above_atm():
    """2026-07-26 spec: puts are 2-step ITM, symmetric with calls. The legacy
    PE_3_ITM path stays alive only so an old position remains closable."""
    expiry = NOW + timedelta(hours=6)
    products = _products(
        expiry, [64200, 64400, 64600, 64800, 65000, 65200, 65400, 65600]
    )
    target = next(row for row in products if row["symbol"].startswith("P-BTC-65200-"))
    selected = select_directional_option(
        products, [_executable(target)], spot=64850, zone=PE_2_ITM, now=NOW
    )
    assert selected["atm_strike"] == 64800
    assert selected["strike"] == 65200
    assert selected["itm_steps"] == 2


def test_call_and_put_step_depth_are_now_symmetric():
    expiry = NOW + timedelta(hours=6)
    strikes = [64200, 64400, 64600, 64800, 65000, 65200, 65400]
    products = _products(expiry, strikes)
    call = next(r for r in products if r["symbol"].startswith("C-BTC-64400-"))
    put = next(r for r in products if r["symbol"].startswith("P-BTC-65200-"))
    ce = select_directional_option(products, [_executable(call)], spot=64850,
                                   zone=CE_2_ITM, now=NOW)
    pe = select_directional_option(products, [_executable(put)], spot=64850,
                                   zone=PE_2_ITM, now=NOW)
    assert ce["itm_steps"] == pe["itm_steps"] == 2
    # Equidistant from ATM, in opposite directions.
    assert ce["atm_strike"] - ce["strike"] == pe["strike"] - pe["atm_strike"]


def test_hold_band_keeps_an_open_position_instead_of_closing_it():
    """The hold band means 'no new action, keep what is open'.

    Without an explicit guard an open position falls through to
    CLOSE_THEN_OPEN (current != HOLD), and that path CLOSES FIRST -- so a
    score drifting into 30..40 would flatten the position and only then fail
    to open a 'HOLD' contract. Net effect: the hysteresis band would cause
    the exact churn it exists to prevent, plus a real exit.
    """
    position = {"trend_score_zone": CE_2_ITM, "symbol": "C-BTC-64000-260726",
                "side": "long"}
    plan = plan_score_transition(
        score=35, signal_key="sig-hold", owned_positions=[position])
    assert plan["action"] == "NOOP"
    assert plan["reason"] == "SCORE_IN_HOLD_BAND"
    assert plan["close_position"] is None
    assert plan["open_zone"] is None
    # The signal is NOT consumed: the same candle must stay actionable if the
    # score leaves the band on a later evaluation.
    assert plan["consume_signal"] is False


def test_hold_band_opens_nothing_when_flat():
    plan = plan_score_transition(
        score=-35, signal_key="sig-hold-flat", owned_positions=[])
    assert plan["action"] == "NOOP"
    assert plan["open_zone"] is None


def test_opposite_hold_band_closes_directional_position_without_reversal():
    position = {"trend_score_zone": CE_2_ITM, "symbol": "C-BTC-64000-260726",
                "side": "long"}
    plan = plan_score_transition(
        score=-35, signal_key="sig-directional-invalidation",
        owned_positions=[position])
    assert plan["action"] == "CLOSE"
    assert plan["current_zone"] == CE_2_ITM
    assert plan["target_zone"] == HOLD
    assert plan["open_zone"] is None
    assert plan["consume_signal"] is True


def test_a_persisted_pe_2_itm_zone_is_not_misread_as_the_legacy_pe_3():
    """Both PE zones share the P-BTC- symbol prefix, so symbol inference
    alone cannot tell them apart. If a PE_2_ITM position were read back as
    PE_3_ITM the planner would see a zone change on the very next candle and
    close/reopen the same put every 5 minutes. The persisted
    trend_score_zone (written by trend_score_live_execution) prevents that.
    """
    from trend_score_auto import position_score_zone

    live = {"trend_score_zone": PE_2_ITM, "symbol": "P-BTC-65200-260726",
            "side": "long"}
    assert position_score_zone(live) == PE_2_ITM

    plan = plan_score_transition(score=-60, signal_key="k", owned_positions=[live])
    assert plan["action"] == "HOLD"          # already matches, no churn
    assert plan["close_position"] is None

    # A position predating the zone field falls back to the symbol, and
    # PE_3_ITM is the right guess there -- it was opened under that policy.
    legacy = {"symbol": "P-BTC-65400-260726", "side": "long"}
    assert position_score_zone(legacy) == PE_3_ITM
