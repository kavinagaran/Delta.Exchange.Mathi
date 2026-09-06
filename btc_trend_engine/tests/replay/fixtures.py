"""The eleven §23.3 scenarios as deterministic 5m candle series.

Every generator is a pure function of its arguments — no clock, no RNG that
is not explicitly seeded — so a fixture produced today is byte-identical to
one produced next year. ``test_backtest_replay.py`` asserts that.

Scenarios that describe a *feed* condition rather than a *price* condition
(data outage, private-feed outage, exchange maintenance, order-book
corruption, spread widening) carry a healthy price series plus the
``data_quality`` / ``book_valid`` / ``spread_bps`` the replay should feed the
engine. Encoding them as prices alone would be dishonest — a data outage is
not a flat market, it is an absence of knowledge, and the engine must treat
the two differently. Several of them reuse the *bullish_trend* price series
verbatim, so the only difference from a tradeable scenario is the feed
condition itself.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from btc_trend_engine.market_data.messages import Candle

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
SYMBOL = "BTCUSD"
BASE_PRICE = 60_000.0

# The 1h timeframe needs SnapshotProducer.MIN_CANDLES (60) closed candles
# before features are considered complete, and one 1h candle is 12 five-minute
# candles. Anything shorter than 60*12 = 720 makes every fixture report
# FEATURES_INCOMPLETE, which would silently turn each scenario into a test
# that the engine refuses to trade on insufficient history — true, but not
# what these fixtures are for. 1200 leaves 100 closed 1h candles.
N = 1200

# Consequence of that warmup: several thousand bars are consumed before the
# first decision is made. A scenario whose defining event happens early is
# therefore a scenario that tests nothing — the engine only ever sees its
# aftermath. Event-shaped fixtures deliberately place their event in the
# final EVENT_WINDOW bars so it lands inside the decision window.
EVENT_WINDOW = 250


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    description: str
    closes: list[float]
    spread_bps: float = 2.0
    data_quality: str = "OK"
    book_valid: bool = True
    expect_entry_allowed: bool | None = None
    """None means "not asserted" — several scenarios are about the engine not
    crashing or not over-trading, and pinning a direction would make the
    fixture a tautology."""

    def candles(self, *, resolution: str = "5m", volatility: float = 0.0008
                ) -> list[Candle]:
        return _to_candles(self.closes, resolution=resolution, volatility=volatility)


def _to_candles(closes: list[float], *, resolution: str = "5m",
                volatility: float = 0.0008, start: datetime = T0) -> list[Candle]:
    """Deterministic OHLC around a close series. High/low are derived from the
    move itself plus a fixed fraction, never from a random draw."""
    step = {"5m": 300, "15m": 900, "30m": 1800, "1h": 3600}[resolution]
    out: list[Candle] = []
    previous = closes[0]
    for index, close in enumerate(closes):
        wick = abs(close) * volatility
        high = max(previous, close) + wick
        low = min(previous, close) - wick
        out.append(Candle(
            symbol=SYMBOL, resolution=resolution,
            start=start + timedelta(seconds=index * step),
            open=Decimal(f"{previous:.2f}"), high=Decimal(f"{high:.2f}"),
            low=Decimal(f"{low:.2f}"), close=Decimal(f"{close:.2f}"),
            volume=Decimal("10"), closed=True, trade_count=25))
        previous = close
    return out


# ── price-shape generators ──────────────────────────────────────────────
# Every generator adds seeded per-bar variation. A perfectly smooth series is
# not a "clean" fixture, it is a degenerate one: with identical bar-to-bar
# moves the return standard deviation collapses toward zero, and the
# standardised last-bar move (jump_score) explodes — a linear ramp scored 142
# against a shock threshold of 4.0, classifying a textbook trend as
# HIGH_VOL_SHOCK. The noise is what makes these series merely synthetic
# rather than pathological.
#
# All drift is geometric (compounded log-returns), never additive. An
# additive ramp is not merely unrealistic over 3,400 bars: at -22/bar from
# 60,000 it crosses zero and goes negative around bar 2,700, at which point
# the fill simulator correctly rejects every order as an invalid price and
# the scenario silently stops testing anything.
NOISE_SEED = 20260725


def _rng(tag: str) -> random.Random:
    return random.Random(f"{NOISE_SEED}:{tag}")


def _walk(n: int, drift_per_bar: float, noise_bps: float, base: float,
          tag: str) -> list[float]:
    rng = _rng(tag)
    price = base
    out = []
    for _ in range(n):
        price *= math.exp(drift_per_bar + rng.gauss(0.0, noise_bps / 10_000.0))
        out.append(price)
    return out


def _trend(n: int, total_return: float, base: float = BASE_PRICE,
           noise_bps: float = 5.0, tag: str = "trend") -> list[float]:
    """``total_return`` is the compounded move across the whole series, so
    +0.6 is "up 60% over the window" regardless of how long the window is."""
    return _walk(n, math.log1p(total_return) / n, noise_bps, base,
                 f"{tag}:{total_return}")


def _range(n: int, amplitude_bps: float = 90.0, base: float = BASE_PRICE,
           tag: str = "range") -> list[float]:
    rng = _rng(tag)
    return [base * math.exp(amplitude_bps / 10_000.0 * math.sin(i / 4.0)
                            + rng.gauss(0.0, 0.0004))
            for i in range(n)]


def _false_breakout(n: int, base: float = BASE_PRICE) -> list[float]:
    """Range, a convincing thrust up, then a full retrace back into it.

    The thrust/retrace pair sits in the last EVENT_WINDOW bars so the engine
    actually makes decisions across it rather than warming up through it.
    """
    span = EVENT_WINDOW * 2 // 5
    body = _range(n - EVENT_WINDOW, base=base, tag="fb-body")
    start = body[-1] if body else base
    thrust = _walk(span, 0.00075, 6.0, start, "fb-thrust")
    peak = thrust[-1] if thrust else start
    retrace = _walk(span, -0.00075, 6.0, peak, "fb-retrace")
    settled = retrace[-1] if retrace else peak
    tail = _range(n - len(body) - len(thrust) - len(retrace), base=settled,
                  tag="fb-tail")
    return body + thrust + retrace + tail


def _liquidation_cascade(n: int, base: float = BASE_PRICE) -> list[float]:
    """Calm, then a violent one-way collapse, then a partial bounce.

    The collapse sits in the last EVENT_WINDOW bars, for the same reason as
    the false breakout.
    """
    calm = _range(n - EVENT_WINDOW, amplitude_bps=45.0, base=base,
                  tag="cascade-calm")
    start = calm[-1] if calm else base
    crash_len = EVENT_WINDOW // 2
    # ~-35% over the crash leg, with volatility several times the calm regime.
    crash = _walk(crash_len, math.log(0.65) / crash_len, 40.0, start, "cascade-crash")
    bottom = crash[-1] if crash else start
    bounce_len = n - len(calm) - len(crash)
    bounce = _walk(bounce_len, math.log(1.15) / max(bounce_len, 1), 22.0,
                   bottom, "cascade-bounce")
    return calm + crash + bounce


def _high_latency(n: int, base: float = BASE_PRICE) -> list[float]:
    """A mild uptrend punctuated by gaps, standing in for bars that arrive
    late and therefore move further than a continuous series would."""
    rng = _rng("high_latency")
    out = []
    price = base
    for i in range(n):
        gap = 0.0012 if i % 12 == 0 else 0.0
        price *= math.exp(0.00006 + gap + rng.gauss(0.0, 0.0006))
        out.append(price)
    return out


SCENARIOS: dict[str, Scenario] = {
    "bullish_trend": Scenario(
        "bullish_trend", "Sustained upward trend; the engine should be able to go long.",
        _trend(N, 0.60), expect_entry_allowed=True),
    "bearish_trend": Scenario(
        "bearish_trend", "Sustained downward trend; the engine should be able to go short.",
        _trend(N, -0.38), expect_entry_allowed=True),
    # expect_entry_allowed was False under the old |60| entry threshold. Under
    # the 2026-07-26 spec's |35| it is no longer true and the fixture must not
    # pretend otherwise: this deliberately sideways series reaches -59..+44 and
    # crosses |35| on ~30% of decisions, classifying as TREND_UP/TREND_DOWN for
    # 92 of 249 bars. That is a real property of the lower threshold -- a range
    # now reads as a trend nearly a third of the time -- and is the mechanism
    # behind the backtest's "more trades at a worse win rate" result.
    "range_bound": Scenario(
        "range_bound", "Oscillating market; at |35| it still reads directional ~30% of the time.",
        _range(N), expect_entry_allowed=None),
    "false_breakout": Scenario(
        "false_breakout", "A thrust out of a range that fully retraces.",
        _false_breakout(N)),
    "liquidation_cascade": Scenario(
        "liquidation_cascade", "Violent one-way collapse then a bounce.",
        _liquidation_cascade(N)),
    "spread_widening": Scenario(
        "spread_widening", "Healthy prices but a spread far beyond the execution cap.",
        _trend(N, 0.60), spread_bps=250.0, expect_entry_allowed=False),
    "orderbook_corruption": Scenario(
        "orderbook_corruption", "Book failed checksum/sequence validation.",
        _trend(N, 0.60), book_valid=False, expect_entry_allowed=False),
    "data_outage": Scenario(
        "data_outage", "Feed is stale; absence of data is not a flat market.",
        _trend(N, 0.60), data_quality="STALE_L1", expect_entry_allowed=False),
    "private_feed_outage": Scenario(
        "private_feed_outage",
        "Position/order feed is unavailable; positions cannot be reconciled.",
        _trend(N, 0.60), data_quality="PRIVATE_FEED_DOWN", expect_entry_allowed=False),
    "exchange_maintenance": Scenario(
        "exchange_maintenance", "Venue is down; no fresh data of any kind.",
        _trend(N, 0.60), data_quality="ENGINE_UNREACHABLE", book_valid=False,
        expect_entry_allowed=False),
    "high_latency": Scenario(
        "high_latency", "Bars arrive late and gap; fills must not assume continuity.",
        _high_latency(N)),
}


def scenario(name: str) -> Scenario:
    try:
        return SCENARIOS[name]
    except KeyError:
        raise KeyError(
            f"unknown scenario {name!r}; have {sorted(SCENARIOS)}") from None
