import json
from datetime import datetime, timedelta, timezone

import pytest

from trend_engine_live import (
    SnapshotCollectionError,
    _private_list_pages,
    collect_delta_trend_snapshot,
)


class Response:
    def __init__(self, result, *, success=True, meta=None):
        self.payload = {"success": success, "result": result}
        if meta is not None:
            self.payload["meta"] = meta

    def json(self):
        return self.payload


def _market_router(now, calls):
    option_symbol = "C-BTC-66000-310726"

    def get(url, params=None, headers=None, timeout=None):
        calls.append((url, params, headers))
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
            seconds = {"5m": 300, "15m": 900, "1h": 3600}[params["resolution"]]
            bucket = int(now.timestamp()) // seconds * seconds
            rows = [{
                "time": bucket - seconds * (index + 1),
                "open": 100 + index,
                "high": 102 + index,
                "low": 99 + index,
                "close": 101 + index,
                "volume": 1000 + index,
            } for index in range(60)]
            rows.append({
                "time": bucket,
                "open": 1,
                "high": 2,
                "low": 1,
                "close": 2,
                "volume": 1,
            })
            return Response(rows)
        if url.endswith("/v2/products"):
            return Response([{
                "id": 101,
                "symbol": option_symbol,
                "strike_price": "66000",
                "settlement_time": (now + timedelta(days=9)).isoformat(),
                "contract_value": "0.001",
                "tick_size": "0.1",
                "position_size_limit": "70000",
                "trading_status": "operational",
            }])
        if url.endswith("/v2/tickers"):
            return Response([{
                "symbol": option_symbol,
                "product_id": 101,
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
                "greeks": {"delta": "0.55", "theta": "-12"},
            }])
        raise AssertionError(f"unexpected request: {url}")

    return get


def test_dry_run_snapshot_uses_only_closed_candles_and_no_live_account(tmp_path):
    now = datetime(2026, 7, 22, 6, 0, 7, tzinfo=timezone.utc)
    calls = []
    dry_dir = tmp_path / "dry_run"
    dry_dir.mkdir()
    (dry_dir / "trade_history.json").write_text("[]", encoding="utf-8")

    def no_private_sign(*_args, **_kwargs):
        raise AssertionError("DRY RUN must not sign a LIVE account request")

    snapshot = collect_delta_trend_snapshot(
        http_get=_market_router(now, calls),
        api_base="https://example.test",
        sign=no_private_sign,
        user_dir=tmp_path,
        dry_run=True,
        mode_revision="rev-1",
        strategy_config={
            "MOVE_DRY_RUN_CAPITAL_USD": "1000",
            "OPTION_FEE_RATE": "0.0001",
            "TREND_MAX_SLIPPAGE_PCT": "1",
        },
        now=now,
    )

    assert snapshot["underlying"] == "BTCUSD"
    assert snapshot["account"]["equity"] == 1000
    assert snapshot["account"]["execution_mode"] == "dry_run"
    assert snapshot["risk"]["account_risk_state_known"] is True
    assert snapshot["events"] is None
    assert snapshot["market"]["event_data_available"] is False
    assert snapshot["option_contracts"][0]["contract_value"] == 0.001
    assert snapshot["option_contracts"][0]["max_order_lots"] == 70000
    assert snapshot["option_contracts"][0]["tick_size"] == 0.1
    for label, seconds in (("5m", 300), ("15m", 900), ("60m", 3600)):
        assert len(snapshot["candles"][label]) == 60
        latest = datetime.fromisoformat(snapshot["candles"][label][-1]["timestamp"])
        assert latest.timestamp() < int(now.timestamp()) // seconds * seconds
        assert all(row["complete"] is True for row in snapshot["candles"][label])
    assert not any("wallet" in url or "positions" in url or "orders" in url
                   for url, _, _ in calls)


def test_explicit_event_status_distinguishes_clear_from_unknown(tmp_path):
    now = datetime(2026, 7, 22, 6, 0, 7, tzinfo=timezone.utc)
    dry_dir = tmp_path / "dry_run"
    dry_dir.mkdir()
    (dry_dir / "trade_history.json").write_text("[]", encoding="utf-8")

    snapshot = collect_delta_trend_snapshot(
        http_get=_market_router(now, []),
        api_base="https://example.test",
        sign=lambda *_args, **_kwargs: {},
        user_dir=tmp_path,
        dry_run=True,
        mode_revision="rev-1",
        strategy_config={
            "MOVE_DRY_RUN_CAPITAL_USD": "1000",
            "TREND_ENGINE_EVENT_STATUS": "clear",
        },
        now=now,
    )

    assert snapshot["events"] == []
    assert snapshot["market"]["event_data_available"] is True


def test_required_upstream_failure_is_not_silently_coerced(tmp_path):
    def failed_get(*_args, **_kwargs):
        return Response(None, success=False)

    with pytest.raises(SnapshotCollectionError, match="BTCUSD ticker failed"):
        collect_delta_trend_snapshot(
            http_get=failed_get,
            api_base="https://example.test",
            sign=lambda *_args, **_kwargs: {},
            user_dir=tmp_path,
            dry_run=True,
            mode_revision="rev-1",
            strategy_config={"MOVE_DRY_RUN_CAPITAL_USD": "1000"},
        )


def test_private_open_order_collection_follows_every_cursor():
    requests = []
    signatures = []

    def get(url, params=None, headers=None, timeout=None):
        requests.append(dict(params or {}))
        if params.get("after") == "next-page":
            return Response([{"symbol": "P-BTC-2"}], meta={"after": None})
        return Response([{"symbol": "C-BTC-1"}], meta={"after": "next-page"})

    def sign(method, path, query):
        signatures.append((method, path, query))
        return {}

    rows = _private_list_pages(
        get, "https://example.test", sign, "/v2/orders",
        {"states": "open", "page_size": 50}, "open orders",
    )

    assert [row["symbol"] for row in rows] == ["C-BTC-1", "P-BTC-2"]
    assert requests[1]["after"] == "next-page"
    assert signatures[1][2].endswith("&after=next-page")


def test_malformed_trade_history_blocks_risk_approval(tmp_path):
    now = datetime(2026, 7, 22, 6, 0, 7, tzinfo=timezone.utc)
    dry_dir = tmp_path / "dry_run"
    dry_dir.mkdir()
    (dry_dir / "trade_history.json").write_text(
        '[{"exit_date":"2026-07-22","exit_time_utc":"05:00:00"}]',
        encoding="utf-8",
    )

    with pytest.raises(SnapshotCollectionError, match="unknown P&L or time"):
        collect_delta_trend_snapshot(
            http_get=_market_router(now, []),
            api_base="https://example.test",
            sign=lambda *_args, **_kwargs: {},
            user_dir=tmp_path,
            dry_run=True,
            mode_revision="rev-1",
            strategy_config={"MOVE_DRY_RUN_CAPITAL_USD": "1000"},
            now=now,
        )


def test_live_position_state_mismatch_is_explicit_and_account_risk_is_unknown(
    tmp_path,
):
    now = datetime(2026, 7, 22, 6, 0, 7, tzinfo=timezone.utc)
    calls = []
    (tmp_path / "trade_history.json").write_text("[]", encoding="utf-8")
    (tmp_path / "trend_state.json").write_text(
        '{"status":"OPEN","symbol":"C-BTC-66000-310726",'
        '"product_id":101,"side":"long","lots":10,"entry_mark":800}',
        encoding="utf-8",
    )
    public_get = _market_router(now, calls)

    def get(url, params=None, headers=None, timeout=None):
        if url.endswith("/v2/wallet/balances"):
            return Response([{
                "asset_symbol": "USD", "balance": "1000",
                "available_balance": "900",
            }])
        if url.endswith("/v2/positions/margined"):
            return Response([])
        if url.endswith("/v2/orders"):
            return Response([])
        return public_get(url, params=params, headers=headers, timeout=timeout)

    snapshot = collect_delta_trend_snapshot(
        http_get=get,
        api_base="https://example.test",
        sign=lambda *_args, **_kwargs: {},
        user_dir=tmp_path,
        dry_run=False,
        mode_revision="rev-live",
        strategy_config={},
        now=now,
    )

    assert snapshot["positions"] == []
    assert snapshot["risk"]["position_state_consistent"] is False
    assert snapshot["risk"]["account_risk_state_known"] is False


def test_matching_live_position_carries_only_persisted_engine_thesis(tmp_path):
    now = datetime(2026, 7, 22, 6, 0, 7, tzinfo=timezone.utc)
    symbol = "C-BTC-66000-310726"
    (tmp_path / "trade_history.json").write_text("[]", encoding="utf-8")
    (tmp_path / "trend_state.json").write_text(json.dumps({
        "status": "OPEN",
        "symbol": symbol,
        "product_id": 101,
        "side": "long",
        "lots": 10,
        "entry_mark": 800,
        "model_version": "trend-engine-1.0.0",
        "entry_decision_id": "trend-entry-1",
        "underlying_invalidation": 65000,
        "stop_option_price": 600,
        "target_option_price": 1200,
        "time_exit": (now + timedelta(hours=6)).isoformat(),
        "remaining_expected_value": 12.5,
        "remaining_expected_value_as_of_utc": (
            now - timedelta(seconds=5)
        ).isoformat(),
        "remaining_expected_value_valid_until_utc": (
            now + timedelta(minutes=5)
        ).isoformat(),
        "remaining_expected_value_source": (
            "entry_decision.audit.scenario.net_expected_value_per_lot"
        ),
    }), encoding="utf-8")
    public_get = _market_router(now, [])

    def get(url, params=None, headers=None, timeout=None):
        if url.endswith("/v2/wallet/balances"):
            return Response([{
                "asset_symbol": "USD", "balance": "1000",
                "available_balance": "900",
            }])
        if url.endswith("/v2/positions/margined"):
            return Response([{
                "product_symbol": symbol,
                "product_id": 101,
                "size": "10",
                "entry_price": "800",
                "mark_price": "850",
                "unrealized_pnl": "0.5",
            }])
        if url.endswith("/v2/orders"):
            return Response([])
        return public_get(url, params=params, headers=headers, timeout=timeout)

    snapshot = collect_delta_trend_snapshot(
        http_get=get,
        api_base="https://example.test",
        sign=lambda *_args, **_kwargs: {},
        user_dir=tmp_path,
        dry_run=False,
        mode_revision="rev-live",
        strategy_config={},
        now=now,
    )

    assert snapshot["risk"]["position_state_consistent"] is True
    assert snapshot["positions"][0]["entry_decision_id"] == "trend-entry-1"
    assert snapshot["positions"][0]["remaining_expected_value"] == 12.5
    assert snapshot["positions"][0]["remaining_expected_value_status"] == (
        "valid_persisted_value"
    )
