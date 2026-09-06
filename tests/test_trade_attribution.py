"""Tests for the Delta -> attribution adapter.

The important behaviours here are the *refusals*: a trade must only be
decomposed when every input is genuinely known, because a guessed underlying
price or contract size produces a confident, wrong answer about where the
money went.
"""

from datetime import datetime, timedelta, timezone

import pytest

from pnl_attribution import CALL, PUT, STRADDLE, bs_price
from trade_attribution import (
    CandleUnderlyingLookup,
    attribute_rows,
    parse_option_symbol,
    parse_utc,
    years_to_expiry,
)

EXPIRY = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
ENTRY_AT = datetime(2026, 7, 6, 9, 30, tzinfo=timezone.utc)
EXIT_AT = datetime(2026, 7, 6, 15, 30, tzinfo=timezone.utc)
SPOT = 60_000.0
STRIKE = 60_000.0
VOL = 0.55
CV = 0.001


# ── Symbol parsing ──────────────────────────────────────────────────────

@pytest.mark.parametrize("symbol,kind", [
    ("C-BTC-63000-080726", CALL),
    ("P-BTC-58000-080726", PUT),
    ("MV-BTC-62800-080726", STRADDLE),
    ("c-btc-63000-080726", CALL),        # case-insensitive
])
def test_parses_dated_option_symbols(symbol, kind):
    contract = parse_option_symbol(symbol)
    assert contract is not None
    assert contract.kind == kind
    assert contract.expiry == datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)


def test_parses_strike_from_symbol():
    assert parse_option_symbol("C-BTC-63000-080726").strike == 63_000.0
    assert parse_option_symbol("MV-BTC-62800-080726").strike == 62_800.0


@pytest.mark.parametrize("symbol", [
    "BTCUSD",                    # perpetual
    "",
    None,
    "C-BTC-63000",               # missing expiry
    "X-BTC-63000-080726",        # unknown instrument prefix
    "C-BTC-abc-080726",          # non-numeric strike
    "C-BTC-63000-991399",        # impossible date
    "C-BTC-0-080726",            # zero strike
])
def test_rejects_non_option_symbols(symbol):
    assert parse_option_symbol(symbol) is None


def test_settlement_hour_is_configurable():
    contract = parse_option_symbol("C-BTC-63000-080726", settlement_hour_utc=8)
    assert contract.expiry.hour == 8


# ── Timestamps ──────────────────────────────────────────────────────────

def test_parse_utc_handles_z_and_offset_and_naive():
    expected = datetime(2026, 7, 6, 9, 30, tzinfo=timezone.utc)
    assert parse_utc("2026-07-06T09:30:00Z") == expected
    assert parse_utc("2026-07-06T09:30:00+00:00") == expected
    assert parse_utc("2026-07-06T09:30:00") == expected
    assert parse_utc("2026-07-06T15:00:00+05:30") == datetime(
        2026, 7, 6, 9, 30, tzinfo=timezone.utc)


def test_parse_utc_rejects_junk():
    assert parse_utc(None) is None
    assert parse_utc("") is None
    assert parse_utc("not a date") is None


def test_years_to_expiry_is_in_years():
    one_day = years_to_expiry(EXPIRY - timedelta(days=1), EXPIRY)
    assert one_day == pytest.approx(1 / 365, rel=1e-9)


# ── Candle lookup ───────────────────────────────────────────────────────

def _candles(start: datetime, minutes: int, price: float = SPOT):
    base = int(start.timestamp()) // 60 * 60
    return [{"time": base + i * 60, "close": price + i} for i in range(minutes)]


def test_candle_lookup_returns_the_containing_minute():
    lookup = CandleUnderlyingLookup(_candles(ENTRY_AT, 10))
    assert lookup(ENTRY_AT) == SPOT
    assert lookup(ENTRY_AT + timedelta(minutes=3)) == SPOT + 3


def test_candle_lookup_uses_a_nearby_minute_within_tolerance():
    candles = _candles(ENTRY_AT, 10)
    del candles[3]                      # a gap in the series
    lookup = CandleUnderlyingLookup(candles)
    assert lookup(ENTRY_AT + timedelta(minutes=3)) is not None


def test_candle_lookup_refuses_beyond_tolerance():
    lookup = CandleUnderlyingLookup(_candles(ENTRY_AT, 5),
                                    tolerance=timedelta(minutes=2))
    assert lookup(ENTRY_AT + timedelta(hours=6)) is None


def test_candle_lookup_survives_malformed_rows():
    lookup = CandleUnderlyingLookup([
        {"time": int(ENTRY_AT.timestamp()), "close": SPOT},
        {"time": "bad", "close": 1.0},
        {"close": 2.0},
        {"time": 123, "close": None},
    ])
    assert len(lookup) == 1
    assert lookup(ENTRY_AT) == SPOT


def test_empty_candle_lookup_returns_none():
    assert CandleUnderlyingLookup([])(ENTRY_AT) is None


# ── Row mapping ─────────────────────────────────────────────────────────

def _row(**overrides):
    entry_t = years_to_expiry(ENTRY_AT, EXPIRY)
    exit_t = years_to_expiry(EXIT_AT, EXPIRY)
    row = {
        "status": "CLOSED",
        "symbol": "C-BTC-60000-080726",
        "side": "LONG",
        "lots": 1000.0,
        "entry_price": bs_price(CALL, SPOT, STRIKE, entry_t, VOL),
        "exit_price": bs_price(CALL, SPOT * 1.02, STRIKE, exit_t, VOL),
        "entry_at_utc": ENTRY_AT.isoformat().replace("+00:00", "Z"),
        "exit_at_utc": EXIT_AT.isoformat().replace("+00:00", "Z"),
        "fees": [],
    }
    row.update(overrides)
    return row


def _prices(entry=SPOT, exit_=SPOT * 1.02):
    def lookup(when):
        return entry if when <= ENTRY_AT else exit_
    return lookup


def _run(rows, *, prices=None, contract_value=CV):
    return attribute_rows(rows, underlying_at=prices or _prices(),
                          contract_value_for=lambda _s: contract_value)


def test_attributes_a_clean_closed_option_trade():
    report = _run([_row()])
    assert len(report.attributed) == 1
    assert report.skipped == []

    trade = report.attributed[0]
    assert trade.kind == CALL
    assert trade.side == "LONG"
    assert trade.held_hours == pytest.approx(6.0)
    assert trade.attribution.reliable is True
    # priced consistently, so the decomposition is exact
    assert trade.attribution.residual == pytest.approx(0.0, abs=1e-3)
    assert trade.attribution.delta > 0        # spot rose, long call


def test_total_matches_the_rows_own_price_move():
    row = _row()
    report = _run([row])
    expected = (row["exit_price"] - row["entry_price"]) * CV * row["lots"]
    assert report.attributed[0].attribution.total == pytest.approx(
        expected, abs=1e-9)


def test_usd_fees_are_subtracted_and_land_in_residual():
    row = _row(fees=[{"asset": "USD", "amount": 12.5}])
    report = _run([row])
    assert report.attributed[0].attribution.residual == pytest.approx(
        -12.5, abs=1e-3)


def test_non_usd_fees_are_not_converted_at_an_assumed_rate():
    """Better to leave them out than invent an FX rate."""
    row = _row(fees=[{"asset": "INR", "amount": 900.0}])
    report = _run([row])
    assert report.attributed[0].attribution.residual == pytest.approx(
        0.0, abs=1e-3)


def test_short_move_straddle_is_attributed_as_a_straddle():
    entry_t = years_to_expiry(ENTRY_AT, EXPIRY)
    exit_t = years_to_expiry(EXIT_AT, EXPIRY)
    row = _row(symbol="MV-BTC-60000-080726", side="SHORT",
               entry_price=bs_price(STRADDLE, SPOT, STRIKE, entry_t, VOL),
               exit_price=bs_price(STRADDLE, SPOT, STRIKE, exit_t, VOL))
    report = _run([row], prices=lambda _w: SPOT)
    trade = report.attributed[0]
    assert trade.kind == STRADDLE
    assert trade.attribution.theta > 0        # short premium collects decay
    assert trade.attribution.total > 0


@pytest.mark.parametrize("overrides,reason", [
    ({"status": "OPEN"}, "position still open"),
    ({"symbol": "BTCUSD"}, "not a dated option symbol"),
    ({"entry_at_utc": None}, "entry/exit timestamp missing"),
    ({"exit_at_utc": ""}, "entry/exit timestamp missing"),
    ({"exit_price": None}, "entry/exit price missing"),
    ({"entry_price": 0.0}, "non-positive price or size"),
    ({"lots": 0.0}, "non-positive price or size"),
    ({"side": "FLAT"}, "direction not reported"),
])
def test_incomplete_rows_are_skipped_with_a_reason(overrides, reason):
    report = _run([_row(**overrides)])
    assert report.attributed == []
    assert [s.reason for s in report.skipped] == [reason]


def test_unknown_contract_value_is_skipped_not_assumed():
    report = _run([_row()], contract_value=None)
    assert report.attributed == []
    assert report.skipped[0].reason == "contract value unknown"


def test_missing_underlying_price_is_skipped_not_interpolated():
    report = _run([_row()], prices=lambda _w: None)
    assert report.attributed == []
    assert report.skipped[0].reason == (
        "underlying price unavailable for that minute")


def test_trade_entered_after_expiry_is_skipped():
    late = EXPIRY + timedelta(hours=1)
    report = _run([_row(entry_at_utc=late.isoformat(),
                        exit_at_utc=(late + timedelta(hours=1)).isoformat())])
    assert report.attributed == []
    assert report.skipped[0].reason == "entered at or after expiry"


# ── Reporting ───────────────────────────────────────────────────────────

def test_report_groups_by_kind_so_the_two_books_read_apart():
    entry_t = years_to_expiry(ENTRY_AT, EXPIRY)
    exit_t = years_to_expiry(EXIT_AT, EXPIRY)
    rows = [
        _row(),
        _row(symbol="P-BTC-60000-080726",
             entry_price=bs_price(PUT, SPOT, STRIKE, entry_t, VOL),
             exit_price=bs_price(PUT, SPOT * 1.02, STRIKE, exit_t, VOL)),
        _row(symbol="MV-BTC-60000-080726", side="SHORT",
             entry_price=bs_price(STRADDLE, SPOT, STRIKE, entry_t, VOL),
             exit_price=bs_price(STRADDLE, SPOT * 1.02, STRIKE, exit_t, VOL)),
    ]
    report = _run(rows)
    by_kind = report.by_kind()
    assert set(by_kind) == {CALL, PUT, STRADDLE}
    assert by_kind[CALL]["trades"] == 1
    assert report.summary["trades"] == 3
    assert report.summary["total"] == pytest.approx(
        sum(k["total"] for k in by_kind.values()))


def test_skip_reasons_are_counted():
    report = _run([_row(status="OPEN"), _row(status="OPEN"),
                   _row(symbol="BTCUSD")])
    assert report.skip_reasons() == {
        "position still open": 2,
        "not a dated option symbol": 1,
    }


def test_empty_input_produces_an_empty_report():
    report = _run([])
    assert report.attributed == [] and report.skipped == []
    assert report.summary["trades"] == 0
    assert report.by_kind() == {}


# ── Coverage: undecomposed P&L must never pose as a measurement ─────────

def _uninvertible_row():
    """A near-expiry exit printed through intrinsic: no BS vol reproduces it."""
    late_exit = EXPIRY - timedelta(minutes=2)
    return _row(symbol="C-BTC-60000-080726",
                entry_price=bs_price(CALL, SPOT, STRIKE,
                                     years_to_expiry(ENTRY_AT, EXPIRY), VOL),
                exit_price=1.0,                       # far below intrinsic
                exit_at_utc=late_exit.isoformat())


def test_uninvertible_trade_is_skipped_not_attributed():
    # exit prints at 1.0 while intrinsic is ~1,200: no vol reproduces it
    report = _run([_uninvertible_row()])
    assert report.attributed == []
    assert len(report.skipped) == 1
    assert "not invertible" in report.skipped[0].reason


def test_undecomposed_pnl_is_reported_and_excluded_from_totals():
    """Folding it into `residual` would let unexplained money masquerade as
    a measured execution cost -- the failure this guards against."""
    report = _run([_row(), _uninvertible_row()])
    assert report.summary["trades"] == 1               # only the clean one
    assert report.undecomposed_pnl != 0.0
    assert report.summary["residual"] == pytest.approx(0.0, abs=1e-3)


def test_coverage_reports_the_decomposed_share():
    assert _run([_row()]).coverage == pytest.approx(1.0)
    assert _run([_uninvertible_row()]).coverage == pytest.approx(0.0)


def test_coverage_is_zero_with_no_trades_at_all():
    assert _run([]).coverage == 0.0
