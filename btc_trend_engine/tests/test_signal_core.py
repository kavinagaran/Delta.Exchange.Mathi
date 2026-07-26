"""Signal core: indicators vs hand-computed fixtures, feature missingness,
regime transitions + hysteresis, score properties, snapshot invariants
(§23.1, §23.4, docs/trend-snapshot-contract.md)."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from btc_trend_engine.features import indicators as ind
from btc_trend_engine.features.pipeline import (
    TimeframeFeatures,
    compute_timeframe_features,
    derivatives_features,
)
from btc_trend_engine.market_data.messages import Candle
from btc_trend_engine.signals import zones
from btc_trend_engine.signals.forecast import forecast_from_closes
from btc_trend_engine.signals.regime import (
    NON_TRADEABLE,
    Regime,
    RegimeClassifier,
)
from btc_trend_engine.signals.score import V1_WEIGHTS, compute_score
from btc_trend_engine.signals.snapshot import (
    SignalConfig,
    SignalHysteresis,
    build_snapshot,
    signal_id_for,
)

T0 = datetime(2026, 7, 25, 0, 0, tzinfo=timezone.utc)


def _candles(closes, *, spread=1.0, volume=10.0, resolution="5m", start=T0):
    out = []
    step = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400}[resolution]
    previous = closes[0]
    for i, close in enumerate(closes):
        high = max(previous, close) + spread
        low = min(previous, close) - spread
        out.append(Candle(
            symbol="BTCUSD", resolution=resolution,
            start=start + timedelta(seconds=i * step),
            open=Decimal(str(previous)), high=Decimal(str(high)),
            low=Decimal(str(low)), close=Decimal(str(close)),
            volume=Decimal(str(volume)), closed=True, trade_count=5))
        previous = close
    return out


def _trending(n=120, start_price=60000.0, step=30.0):
    return [start_price + i * step for i in range(n)]


def _ranging(n=120, base=60000.0, amplitude=40.0):
    return [base + amplitude * math.sin(i / 3.0) for i in range(n)]


def _zigzag_up(n=120, start_price=60000.0):
    """Realistic uptrend: five bars up, two bars down, net higher each cycle.
    Unlike a straight line this produces confirmable swing highs/lows —
    a perfectly monotonic series has none, by construction."""
    closes = [start_price]
    for i in range(1, n):
        step = 30.0 if i % 7 < 5 else -40.0
        closes.append(closes[-1] + step)
    return closes


# ── indicators vs hand-computed values ──────────────────────────────────
def test_ema_matches_hand_computation():
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    series = ind.ema(values, 3)
    # seed = mean(1,2,3)=2; k=0.5 → 2, 3, 4
    assert series == [2.0, 3.0, 4.0]


def test_atr_on_constant_range_is_that_range():
    candles = _candles([100.0] * 20, spread=2.0)
    assert ind.atr(candles, 14) == pytest.approx(4.0)


def test_rsi_extremes():
    assert ind.rsi(_trending(30), 14) == 100.0
    down = list(reversed(_trending(30)))
    assert ind.rsi(down, 14) == pytest.approx(0.0)


def test_directional_efficiency_signed():
    assert ind.directional_efficiency(_trending(30), 20) == pytest.approx(1.0)
    assert ind.directional_efficiency(
        list(reversed(_trending(30))), 20) == pytest.approx(-1.0)
    churn = [100.0, 101.0] * 15
    assert abs(ind.directional_efficiency(churn, 20)) < 0.2


def test_insufficient_windows_return_none_not_zero():
    short = _candles([100.0] * 5)
    assert ind.atr(short, 14) is None
    assert ind.ema([1.0, 2.0], 5) is None
    assert ind.rsi([1.0] * 5, 14) is None
    assert ind.realized_volatility([1.0] * 3, 10) is None


def test_tied_extremes_emit_one_swing_not_several():
    """A flat top is one swing high. Emitting one per tied bar makes two
    equal 'swings', and higher-high detection then reads a clean uptrend as
    no structure."""
    candles = _candles([100, 102, 104, 104, 104, 102, 100, 98], spread=0.0)
    highs, _ = ind.swing_points(candles, strength=2)
    assert len(highs) == 1


def test_swing_points_never_use_future_bars():
    closes = _trending(30) + [61000.0, 60000.0]  # spike at the very end
    candles = _candles(closes)
    highs, lows = ind.swing_points(candles, strength=2)
    # the last `strength` bars cannot be confirmed swings
    assert all(i < len(candles) - 2 for i in highs + lows)


# ── feature pipeline ────────────────────────────────────────────────────
def test_trending_series_produces_positive_trend_features():
    features = compute_timeframe_features(
        "1h", _candles(_zigzag_up(), resolution="1h"))
    assert features.get("ema_distance_atr") > 0
    assert features.get("structure_bias") == 1.0
    assert features.get("donchian_position") > 0.7
    assert features.get("last_swing_low") < features.get("last_swing_high")
    # A 5-up(+30) / 2-down(-40) cycle nets 70 over 230 of path: efficiency
    # tops out near 0.30 and reads lower at a partial-cycle boundary. What
    # matters is that a choppy uptrend sits well above churn but well below
    # the straight line — measured against both, not an arbitrary constant.
    choppy = features.get("directional_efficiency")
    straight = compute_timeframe_features(
        "1h", _candles(_trending(), resolution="1h")
    ).get("directional_efficiency")
    churn = compute_timeframe_features(
        "1h", _candles(_ranging(), resolution="1h")
    ).get("directional_efficiency")
    assert abs(churn) < choppy < straight
    assert straight == pytest.approx(1.0)


def test_perfectly_monotonic_series_has_no_structure_reading():
    """No zigzag → no confirmable swings → structure honestly missing,
    never fabricated (§3.2)."""
    features = compute_timeframe_features(
        "1h", _candles(_trending(), resolution="1h"))
    assert "structure_bias" in features.missing
    assert features.get("directional_efficiency") > 0.8


def test_short_series_reports_missing_features_not_zeros():
    features = compute_timeframe_features("5m", _candles([100.0] * 10))
    assert "ema_distance_atr" in features.missing
    assert "ema_distance_atr" not in features.features


def test_derivatives_features_from_live_ticker_shape():
    ticker = {"mark_price": "63941.76", "spot_price": "63969.8",
              "funding_rate": 0.01, "oi_value_usd": "75899519.76",
              "oi_change_usd_6h": "3713941.12"}
    features = derivatives_features(ticker, funding_history=[0.005] * 10)
    assert features["mark_spot_basis_pct"] < 0
    assert features["funding_percentile"] == 1.0
    assert features["oi_change_6h_pct"] == pytest.approx(4.893, abs=0.01)


# ── score ───────────────────────────────────────────────────────────────
def _tf(features: dict) -> TimeframeFeatures:
    return TimeframeFeatures(timeframe="x", features=features, missing=[])


def _full_bull_inputs():
    up = {"ema_distance_atr": 2.0, "ema_slope_atr": 1.0,
          "directional_efficiency": 0.8, "structure_bias": 1.0,
          "donchian_position": 0.95, "rsi": 70.0, "vwap_distance_atr": 1.0,
          "breakout_direction": 1.0, "breakout_body_atr": 1.5,
          "close_location": 0.9, "volume_ratio": 2.0, "atr": 50.0,
          "vol_ratio": 1.0, "jump_score": 0.5,
          "last_swing_low": 63000.0, "last_swing_high": 64000.0}
    return dict(structural=_tf(up), primary=_tf(up), setup=_tf(up),
                trigger=_tf(up), derivatives={"oi_change_6h_pct": 4.0,
                                              "funding_percentile": 0.5})


def test_weights_must_sum_to_one():
    with pytest.raises(ValueError):
        compute_score(**_full_bull_inputs(), weights={"higher_timeframe_trend": 0.5})


def test_strong_bull_scores_high_and_bounded():
    result = compute_score(**_full_bull_inputs())
    assert result.trend_score is not None
    assert 65.0 <= result.trend_score < 100.0


def test_score_is_antisymmetric_for_mirrored_inputs():
    bull = compute_score(**_full_bull_inputs()).trend_score
    down = {"ema_distance_atr": -2.0, "ema_slope_atr": -1.0,
            "directional_efficiency": -0.8, "structure_bias": -1.0,
            "donchian_position": 0.05, "rsi": 30.0, "vwap_distance_atr": -1.0,
            "breakout_direction": -1.0, "breakout_body_atr": 1.5,
            "close_location": 0.1, "volume_ratio": 2.0, "atr": 50.0}
    # The mirror of "longs building" (price up, OI up) is "shorts building"
    # (price down, OI up) — §9.5 table — so the mirrored fixture keeps
    # oi_change POSITIVE. OI shrinking would be long liquidation, a different
    # regime, and correctly scores differently.
    bear = compute_score(
        structural=_tf(down), primary=_tf(down), setup=_tf(down),
        trigger=_tf(down), derivatives={"oi_change_6h_pct": 4.0,
                                        "funding_percentile": 0.5}).trend_score
    assert bear == pytest.approx(-bull, abs=1.0)


def test_order_flow_weight_is_zero_and_unavailable_in_v1():
    result = compute_score(**_full_bull_inputs())
    order_flow = next(c for c in result.components if c.name == "order_flow")
    assert order_flow.weight == 0.0
    assert not order_flow.available
    assert sum(c.weight for c in result.components) == pytest.approx(1.0)
    assert V1_WEIGHTS["higher_timeframe_trend"] == 0.40


def test_insufficient_inputs_yield_no_score_not_neutral():
    empty = TimeframeFeatures("x", {}, ["all"])
    result = compute_score(structural=empty, primary=empty, setup=empty,
                           trigger=empty, derivatives={})
    assert result.trend_score is None


# ── regime ──────────────────────────────────────────────────────────────
def test_regime_starts_degraded_and_needs_good_data():
    classifier = RegimeClassifier()
    assert classifier.current is Regime.DEGRADED
    decision = classifier.classify(
        data_quality_ok=False, trend_score=90.0,
        setup=_tf({"vol_ratio": 1.0}))
    assert decision.regime is Regime.DEGRADED


def test_vol_shock_beats_trend():
    classifier = RegimeClassifier()
    decision = classifier.classify(
        data_quality_ok=True, trend_score=90.0,
        setup=_tf({"vol_ratio": 3.0}))
    assert decision.regime is Regime.HIGH_VOL_SHOCK


def test_regime_hysteresis_enter_hold_exit():
    classifier = RegimeClassifier()
    quiet = _tf({"vol_ratio": 1.0, "volume_ratio": 1.0,
                 "breakout_direction": 0.0, "breakout_body_atr": 0.0})
    assert classifier.classify(data_quality_ok=True, trend_score=65.0,
                               setup=quiet).regime is Regime.TREND_UP
    # decays below enter but above exit → held
    assert classifier.classify(data_quality_ok=True, trend_score=40.0,
                               setup=quiet).regime is Regime.TREND_UP
    # below exit → released to RANGE
    decision = classifier.classify(data_quality_ok=True, trend_score=20.0,
                                   setup=quiet)
    assert decision.regime is Regime.RANGE
    assert decision.changed


def test_breakout_requires_body():
    classifier = RegimeClassifier()
    weak = _tf({"vol_ratio": 1.0, "volume_ratio": 1.0,
                "breakout_direction": 1.0, "breakout_body_atr": 0.3})
    assert classifier.classify(data_quality_ok=True, trend_score=10.0,
                               setup=weak).regime is Regime.RANGE
    strong = _tf({"vol_ratio": 1.0, "volume_ratio": 1.0,
                  "breakout_direction": 1.0, "breakout_body_atr": 1.5})
    assert classifier.classify(data_quality_ok=True, trend_score=10.0,
                               setup=strong).regime is Regime.BREAKOUT_UP


# ── signal hysteresis ───────────────────────────────────────────────────
def test_signal_hysteresis_matrix():
    """Thresholds are the 2026-07-26 operator spec: enter at |35|, hold to
    |25|. The 25-35 gap is the hold band (see signals/zones.py)."""
    h = SignalHysteresis(SignalConfig())
    assert h.update(30.0) == 0        # inside the hold band, never entered
    assert h.update(40.0) == 1        # enter long at >= 35
    assert h.update(30.0) == 1        # hold band keeps an OPEN position
    assert h.update(20.0) == 0        # exit at <= 25
    assert h.update(-40.0) == -1      # enter short at <= -35
    assert h.update(-26.0) == -1      # hold
    assert h.update(-10.0) == 0       # exit
    assert h.update(None) == 0        # no score → flat, always


def test_the_hold_band_is_asymmetric_between_entering_and_holding():
    """A score of 30 must NOT open a position but must not close one either.
    This is the whole point of the gap in the spec."""
    entering = SignalHysteresis(SignalConfig())
    assert entering.update(30.0) == 0

    holding = SignalHysteresis(SignalConfig())
    holding.update(40.0)
    assert holding.update(30.0) == 1


# ── snapshot invariants (contract §invariants, §23.4) ───────────────────
def _snapshot(regime=Regime.TREND_UP, data_quality="OK", direction=1,
              gates_ok=True, score_value=80.0, gates=None):
    inputs = _full_bull_inputs()
    score = compute_score(**inputs)
    if score_value is not None:
        object.__setattr__(score, "trend_score", score_value)
    gates = gates or [
        {"name": "data_fresh", "passed": gates_ok, "detail": None},
        {"name": "risk_lock_clear", "passed": True, "detail": None},
    ]
    return build_snapshot(
        symbol="BTCUSD", now=T0 + timedelta(minutes=10),
        candle_close=T0 + timedelta(minutes=10),
        regime=regime, regime_since=T0, score=score, direction=direction,
        structural=inputs["structural"], primary=inputs["primary"],
        setup=inputs["setup"], trigger=inputs["trigger"],
        timeframe_closes={tf: T0 for tf in ("4h", "1h", "15m", "5m")},
        gates=gates, data_quality=data_quality, config=SignalConfig(),
        forecast={"expected_return_bps": None,
                  "expected_absolute_move_bps": 43.0,
                  "forecast_volatility_bps": 39.0, "jump_probability": 0.02})


def test_snapshot_matches_contract_shape():
    snapshot = _snapshot()
    for key in ("schema_version", "signal_id", "regime", "direction",
                "trend_score", "confidence", "entry_allowed",
                "signal_ttl_seconds", "components", "timeframes", "gates",
                "reason_codes", "data_quality", "invalidation_price",
                "zone", "zone_action_allowed", "zone_reason"):
        assert key in snapshot, key
    # 1.1.0 added the zone fields additively; the client compares major only.
    assert snapshot["schema_version"] == "1.1.0"
    assert snapshot["entry_allowed"] is True
    assert snapshot["invalidation_price"] == "63000.0"
    assert snapshot["suggested_stop_bps"] == pytest.approx(58.5)
    assert isinstance(snapshot["invalidation_price"], str)  # Decimal-safe


def test_invariant_bad_data_quality_never_allows_entry():
    for quality in ("STALE_L1", "STALE_L2", "BOOK_INVALID", "CLOCK_DRIFT"):
        assert _snapshot(data_quality=quality)["entry_allowed"] is False


def test_invariant_non_tradeable_regimes_never_allow_entry():
    for regime in NON_TRADEABLE:
        assert _snapshot(regime=regime)["entry_allowed"] is False


def test_invariant_failed_gate_blocks_entry_and_emits_code():
    snapshot = _snapshot(gates_ok=False)
    assert snapshot["entry_allowed"] is False
    assert "GATE_DATA_FRESH_FAILED" in snapshot["reason_codes"]


def test_sideways_zone_ignores_only_directional_entry_gates():
    gates = [
        {"name": "data_fresh", "passed": True, "detail": None},
        {"name": "book_valid", "passed": True, "detail": None},
        {"name": "features_complete", "passed": True, "detail": None},
        {"name": "spread_acceptable", "passed": True, "detail": None},
        {"name": "regime_tradeable", "passed": False, "detail": "regime is RANGE"},
        {
            "name": "score_beyond_entry_threshold",
            "passed": False,
            "detail": "|score| 0.0 < 35.0",
        },
        {"name": "risk_lock_clear", "passed": True, "detail": None},
    ]
    snapshot = _snapshot(
        regime=Regime.RANGE,
        direction=0,
        score_value=0.0,
        gates=gates,
    )
    assert snapshot["zone"] == zones.SHORT_MOVE
    assert snapshot["zone_action_allowed"] is True


def test_sideways_zone_still_obeys_shared_safety_gates():
    gates = [
        {"name": "data_fresh", "passed": True, "detail": None},
        {"name": "book_valid", "passed": False, "detail": "book invalid"},
        {"name": "regime_tradeable", "passed": False, "detail": "regime is RANGE"},
        {
            "name": "score_beyond_entry_threshold",
            "passed": False,
            "detail": "|score| 0.0 < 35.0",
        },
    ]
    snapshot = _snapshot(
        regime=Regime.RANGE,
        direction=0,
        score_value=0.0,
        gates=gates,
    )
    assert snapshot["zone"] == zones.SHORT_MOVE
    assert snapshot["zone_action_allowed"] is False


def test_invariant_score_bounds_and_weights():
    snapshot = _snapshot()
    assert abs(snapshot["trend_score"]) <= 100
    assert snapshot["signal_ttl_seconds"] > 0
    assert sum(c["weight"] for c in snapshot["components"]) == pytest.approx(1.0)


def test_signal_id_is_deterministic_and_input_sensitive():
    a = signal_id_for("BTCUSD", "2026-07-25T10:15:00Z")
    b = signal_id_for("BTCUSD", "2026-07-25T10:15:00Z")
    c = signal_id_for("BTCUSD", "2026-07-25T10:20:00Z")
    assert a == b != c


def test_reason_codes_snapshot_contract():
    codes = _snapshot()["reason_codes"]
    assert "4H_TREND_UP" in codes
    assert "1H_TREND_UP" in codes
    assert "15M_BREAKOUT_CONFIRMED" in codes


# ── forecast ────────────────────────────────────────────────────────────
def test_forecast_is_transparent_and_declines_drift():
    closes = _ranging(200)
    forecast = forecast_from_closes(closes)
    assert forecast["expected_return_bps"] is None      # no fake edge in v1
    assert forecast["forecast_volatility_bps"] > 0
    assert forecast["expected_absolute_move_bps"] < forecast["forecast_volatility_bps"]
    assert 0.0 <= forecast["jump_probability"] <= 1.0


def test_forecast_fails_closed_without_history():
    forecast = forecast_from_closes([100.0] * 5)
    assert all(v is None for v in forecast.values())
