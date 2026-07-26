"""Read-only complete Delta trade history used by the Performance page."""

import dashboard


def _result(response):
    if isinstance(response, tuple):
        payload, status = response
    else:
        payload, status = response, response.status_code
    return payload.get_json(), int(status)


def test_complete_delta_trade_history_follows_all_cursors_and_hides_fill_details(
        monkeypatch):
    pages = iter([
        {
            "success": True,
            "result": [{
                "id": "fill-older", "order_id": "order-1", "product_id": 10,
                "product_symbol": "MV-BTC-65000-260726", "side": "buy",
                "size": 4, "price": "12.5", "commission": "0.001",
                "settling_asset_symbol": "USDT", "role": "taker",
                "fill_type": "normal", "created_at": "1785000000000000",
                "meta_data": {"order_type": "limit_order"},
            }],
            "meta": {"after": "page-two"},
        },
        {
            "success": True,
            "result": [{
                "id": "fill-newer", "order_id": "order-2", "product_id": 10,
                "product_symbol": "MV-BTC-65000-260726", "side": "sell",
                "size": "4", "price": "3.125", "commission": "0.002",
                "settling_asset_symbol": "USDT", "role": "maker",
                "fill_type": "settlement", "created_at": "1785000001000000",
            }],
            "meta": {},
        },
    ])
    calls = []

    class Response:
        def json(self):
            return next(pages)

    def fake_get(url, *, params, headers, timeout):
        calls.append((url, dict(params), dict(headers), timeout))
        return Response()

    monkeypatch.setattr(dashboard, "_active_creds", lambda: ("key", "secret"))
    monkeypatch.setattr(dashboard.req, "get", fake_get)
    monkeypatch.setattr(dashboard, "_delta_contract_value", lambda product_id: 100.0)

    with dashboard.app.test_request_context("/api/performance/delta-trades"):
        payload, status = _result(dashboard.api_performance_delta_trades())

    assert status == 200
    assert payload["ok"] is True
    assert payload["count"] == 1
    assert [call[1] for call in calls] == [
        {"page_size": 50}, {"page_size": 50, "after": "page-two"},
    ]
    trade = payload["records"][0]
    assert trade["entry_time_ist"].endswith("IST")
    assert trade["side"] == "LONG"
    assert trade["lots"] == 4
    assert trade["entry_price"] == 12.5
    assert trade["exit_price"] == 3.125
    assert trade["status"] == "CLOSED"
    assert trade["fees"] == [{"asset": "USDT", "amount": 0.003}]
    assert trade["gross_pnl_usd"] == -3750.0
    assert trade["net_pnl_usd"] is None  # USDT fees are not labelled USD.
    assert not {"order_id", "fill_id", "exchange_details", "role", "fill_type"} & set(trade)


def test_trade_reconstruction_keeps_partial_closes_and_reversals_as_trades():
    fills = [
        {"product_symbol": "C-BTC-65000-260726", "side": "buy", "size": 10,
         "price": "5", "commission": "0.01", "settling_asset_symbol": "USDT",
         "created_at": "1785000000000000"},
        {"product_symbol": "C-BTC-65000-260726", "side": "sell", "size": 15,
         "price": "6", "commission": "0.015", "settling_asset_symbol": "USDT",
         "created_at": "1785000001000000"},
    ]

    closed, open_trade = dashboard._reconstruct_delta_trades(
        fills, contract_value_lookup=lambda product_id: 100.0)

    assert closed["status"] == "CLOSED"
    assert closed["side"] == "LONG"
    assert closed["lots"] == 10
    assert closed["entry_price"] == 5
    assert closed["exit_price"] == 6
    assert closed["fees"] == [{"asset": "USDT", "amount": 0.02}]
    assert closed["gross_pnl_usd"] == 1000.0
    assert closed["net_pnl_usd"] is None
    assert open_trade["status"] == "OPEN"
    assert open_trade["side"] == "SHORT"
    assert open_trade["lots"] == 5
    assert open_trade["open_lots"] == 5
    assert open_trade["fees"] == [{"asset": "USDT", "amount": 0.005}]


def test_delta_fill_history_never_claims_a_repeated_cursor_is_complete(monkeypatch):
    pages = iter([
        {"success": True, "result": [{"id": "one"}], "meta": {"after": "same"}},
        {"success": True, "result": [{"id": "two"}], "meta": {"after": "same"}},
    ])

    class Response:
        def json(self):
            return next(pages)

    monkeypatch.setattr(dashboard, "_active_creds", lambda: ("key", "secret"))
    monkeypatch.setattr(dashboard.req, "get", lambda *args, **kwargs: Response())

    with dashboard.app.test_request_context("/api/performance/delta-trades"):
        payload, status = _result(dashboard.api_performance_delta_trades())

    assert status == 502
    assert payload["ok"] is False
    assert "repeated a cursor" in payload["error"]
