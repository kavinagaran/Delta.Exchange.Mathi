from datetime import datetime, timedelta, timezone

import pytest

import trend_engine_live
from trend_engine_live import (
    DELTA_CANDLE_RESPONSE_LIMIT,
    FORECAST_HISTORY_5M_LIMIT,
    INDICATOR_CANDLE_LIMIT,
    SnapshotCollectionError,
    _option_contracts,
    _public_list_pages,
    collect_delta_trend_snapshot,
)


class Response:
    def __init__(self, result, *, success=True, meta=None):
        self.payload = {"success": success, "result": result}
        if meta is not None:
            self.payload["meta"] = meta

    def json(self):
        return self.payload


def _product(product_id, symbol, now):
    return {
        "id": product_id,
        "symbol": symbol,
        "strike_price": "66000",
        "settlement_time": (now + timedelta(days=1)).isoformat(),
        "contract_value": "0.001",
        "tick_size": "0.1",
        "position_size_limit": "70000",
        "trading_status": "operational",
    }


def _ticker(product_id, symbol, now, delta):
    return {
        "symbol": symbol,
        "product_id": product_id,
        "mark_price": "850",
        "contract_value": "0.001",
        "timestamp": int(now.timestamp() * 1_000_000),
        "volume": "5000",
        "oi_contracts": "25000",
        "product_trading_status": "operational",
        "quotes": {
            "best_bid": "845",
            "best_ask": "850",
            "bid_size": "3000",
            "ask_size": "3000",
            "mark_iv": "0.45",
        },
        "greeks": {"delta": str(delta), "theta": "-12"},
    }


def test_snapshot_chunks_forecast_history_and_paginates_all_option_products(
    tmp_path,
):
    now = datetime(2026, 7, 22, 6, 0, 7, tzinfo=timezone.utc)
    calls = []
    first_symbol = "C-BTC-66000-230726"
    second_symbol = "P-BTC-66000-230726"

    def get(url, params=None, headers=None, timeout=None):
        params = dict(params or {})
        calls.append((url, params, headers, timeout))
        if url.endswith("/v2/tickers/BTCUSD"):
            return Response({
                "symbol": "BTCUSD",
                "spot_price": "66500",
                "mark_price": "66510",
                "timestamp": int(now.timestamp() * 1_000_000),
                "open": "66000",
                "volume": "1000",
                "oi_contracts": "2000",
            })
        if url.endswith("/v2/history/candles"):
            resolution_seconds = {
                "5m": 300,
                "15m": 900,
                "1h": 3600,
            }[params["resolution"]]
            rows = []
            epoch = params["start"]
            while epoch <= params["end"]:
                rows.append({
                    "time": epoch,
                    "open": "100",
                    "high": "102",
                    "low": "99",
                    "close": "101",
                    "volume": "1000",
                })
                epoch += resolution_seconds
            # Identical duplicates must not leak into the normalized history.
            if rows:
                rows.append(dict(rows[-1]))
            return Response(rows)
        if url.endswith("/v2/products"):
            assert params["contract_types"] == "call_options,put_options"
            assert params["underlying_asset_symbols"] == "BTC"
            assert params["states"] == "live"
            assert params["page_size"] == 1000
            if params.get("after") == "next-products-page":
                return Response(
                    [_product(102, second_symbol, now)],
                    meta={"after": None},
                )
            assert "after" not in params
            return Response(
                [_product(101, first_symbol, now)],
                meta={"after": "next-products-page"},
            )
        if url.endswith("/v2/tickers"):
            return Response([
                _ticker(101, first_symbol, now, 0.55),
                _ticker(102, second_symbol, now, -0.55),
            ])
        raise AssertionError(f"unexpected request: {url}")

    dry_dir = tmp_path / "dry_run"
    dry_dir.mkdir()
    (dry_dir / "trade_history.json").write_text("[]", encoding="utf-8")

    snapshot = collect_delta_trend_snapshot(
        http_get=get,
        api_base="https://example.test",
        sign=lambda *_args, **_kwargs: pytest.fail(
            "DRY RUN must not sign a private request"
        ),
        user_dir=tmp_path,
        dry_run=True,
        mode_revision="rev-history",
        strategy_config={"MOVE_DRY_RUN_CAPITAL_USD": "1000"},
        now=now,
    )

    history = snapshot["forecast_history_5m"]
    assert history["source"] == {
        "provider": "delta_exchange",
        "transport": "public_rest",
        "endpoint": "/v2/history/candles",
        "symbol": "BTCUSD",
        "resolution": "5m",
        "interval_seconds": 300,
        "completed_only": True,
    }
    assert history["requested_limit"] == FORECAST_HISTORY_5M_LIMIT
    assert history["returned_count"] == FORECAST_HISTORY_5M_LIMIT
    assert len(history["candles"]) == FORECAST_HISTORY_5M_LIMIT
    assert len({row["timestamp"] for row in history["candles"]}) == 4000
    assert history["first_timestamp"] == history["candles"][0]["timestamp"]
    assert history["last_timestamp"] == history["candles"][-1]["timestamp"]
    assert all(row["complete"] is True for row in history["candles"])
    assert history["candles"] == sorted(
        history["candles"], key=lambda row: row["timestamp"]
    )

    # The directional indicators keep their existing bounded working set.
    assert len(snapshot["candles"]["5m"]) == 320
    assert snapshot["candles"]["5m"] == history["candles"][-320:]
    assert len(snapshot["candles"]["15m"]) == 320
    assert len(snapshot["candles"]["60m"]) == 320

    history_calls = [
        params for url, params, _, _ in calls
        if url.endswith("/v2/history/candles") and params["resolution"] == "5m"
    ]
    assert len(history_calls) == 2
    assert history_calls[0]["end"] + 1 == history_calls[1]["start"]
    for params in history_calls:
        requested_bars = (params["end"] + 1 - params["start"]) // 300
        assert requested_bars == DELTA_CANDLE_RESPONSE_LIMIT

    product_calls = [
        params for url, params, _, _ in calls if url.endswith("/v2/products")
    ]
    assert len(product_calls) == 2
    assert "after" not in product_calls[0]
    assert product_calls[1]["after"] == "next-products-page"
    assert {row["symbol"] for row in snapshot["option_contracts"]} == {
        first_symbol,
        second_symbol,
    }


def test_public_product_pagination_rejects_a_repeated_cursor():
    calls = []

    def get(url, params=None, timeout=None):
        calls.append(dict(params or {}))
        return Response([], meta={"after": "same-cursor"})

    with pytest.raises(
        SnapshotCollectionError,
        match="option products repeated its pagination cursor",
    ):
        _public_list_pages(
            get,
            "https://example.test",
            "/v2/products",
            {"states": "live", "page_size": 1000},
            "option products",
        )

    assert len(calls) == 2
    assert "after" not in calls[0]
    assert calls[1]["after"] == "same-cursor"


def test_stale_contract_quote_is_excluded_without_invalidating_fresh_universe():
    now = datetime(2026, 7, 22, 6, 0, tzinfo=timezone.utc)
    fresh_symbol = "C-BTC-66000-230726"
    stale_symbol = "P-BTC-66000-230726"

    def get(url, params=None, timeout=None):
        if url.endswith("/v2/products"):
            return Response([
                _product(101, fresh_symbol, now),
                _product(102, stale_symbol, now),
            ])
        if url.endswith("/v2/tickers"):
            fresh = _ticker(101, fresh_symbol, now, 0.55)
            stale = _ticker(102, stale_symbol, now, -0.55)
            stale["timestamp"] = int(
                (now - timedelta(minutes=2)).timestamp() * 1_000_000
            )
            return Response([fresh, stale])
        raise AssertionError(url)

    contracts, timestamp, tickers = _option_contracts(
        get,
        "https://example.test",
        spot=66_500,
        fee_rate=0.0001,
        fee_cap_pct=0.035,
        slippage_pct=1,
    )

    assert timestamp == now.isoformat()
    assert [row["symbol"] for row in contracts] == [fresh_symbol]
    assert set(tickers) == {fresh_symbol, stale_symbol}


def test_extended_history_failure_keeps_current_snapshot_available(
    tmp_path, monkeypatch,
):
    now = datetime(2026, 7, 22, 6, 0, 7, tzinfo=timezone.utc)
    symbol = "C-BTC-66000-230726"

    def closed(_get, _base, evaluated_now, label, _resolution, seconds, *, limit):
        if limit == FORECAST_HISTORY_5M_LIMIT:
            raise SnapshotCollectionError("extended history unavailable")
        bucket = int(evaluated_now.timestamp())
        bucket -= bucket % seconds
        return [{
            "timestamp": datetime.fromtimestamp(
                bucket - seconds * (limit - index), tz=timezone.utc
            ).isoformat(),
            "open": 100,
            "high": 102,
            "low": 99,
            "close": 101,
            "volume": 1_000,
            "complete": True,
        } for index in range(limit)]

    monkeypatch.setattr(trend_engine_live, "_closed_candles", closed)

    def get(url, params=None, headers=None, timeout=None):
        if url.endswith("/v2/tickers/BTCUSD"):
            return Response({
                "symbol": "BTCUSD",
                "spot_price": "66500",
                "mark_price": "66510",
                "timestamp": int(now.timestamp() * 1_000_000),
                "open": "66000",
                "volume": "1000",
                "oi_contracts": "2000",
            })
        if url.endswith("/v2/products"):
            return Response([_product(101, symbol, now)])
        if url.endswith("/v2/tickers"):
            return Response([_ticker(101, symbol, now, 0.55)])
        raise AssertionError(url)

    dry_dir = tmp_path / "dry_run"
    dry_dir.mkdir()
    (dry_dir / "trade_history.json").write_text("[]", encoding="utf-8")
    snapshot = collect_delta_trend_snapshot(
        http_get=get,
        api_base="https://example.test",
        sign=lambda *_args, **_kwargs: {},
        user_dir=tmp_path,
        dry_run=True,
        mode_revision="history-optional",
        strategy_config={"TREND_ENGINE_DRY_RUN_EQUITY_USD": "1000"},
        now=now,
    )

    assert snapshot["forecast_history_5m"] is None
    assert snapshot["market"]["forecast_history_available"] is False
    assert "extended history unavailable" in snapshot["market"][
        "forecast_history_error"
    ]
    assert len(snapshot["candles"]["5m"]) == INDICATOR_CANDLE_LIMIT
    assert snapshot["option_contracts"][0]["symbol"] == symbol
