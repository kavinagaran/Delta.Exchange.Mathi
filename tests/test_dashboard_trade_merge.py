import json
from pathlib import Path

import pytest

import dashboard


@pytest.fixture
def isolated_trade_history(tmp_path, monkeypatch):
    users = tmp_path / "users"
    account = users / "alice"
    account.mkdir(parents=True)
    monkeypatch.setattr(dashboard, "USERS_DIR", users)
    monkeypatch.setattr(dashboard, "DASH_USER", "alice")
    monkeypatch.setattr(dashboard, "BOT_USER", "alice")
    return account / "trade_history.json"


def _write_history(path: Path, rows: list[dict]) -> None:
    path.write_text(json.dumps(rows), encoding="utf-8")


def _stored_trade(**updates) -> dict:
    trade = {
        "slot": "evening",
        "symbol": "P-BTC-63200-150726",
        "product_id": 142150,
        "entry_date": "2026-07-14",
        "entry_time_utc": "12:49:26",
        "exit_time_utc": "03:59:49",
        "side": "long",
        "lots": 114,
        "entry_mark": 259.63157894736844,
        "exit_mark": 3.4,
        "pnl_usd": -29.21,
        "exit_order_id": 1417416018,
        "entry_trigger": "exchange_sync",
    }
    trade.update(updates)
    return trade


def _reconstructed_trade(**updates) -> dict:
    trade = {
        "symbol": "P-BTC-63200-150726",
        "product_id": 142150,
        "date": "2026-07-14",
        "entry_time": "12:49:25",
        "exit_time": "03:59:49",
        "side": "LONG",
        "lots": 114.0,
        "entry_mark": 259.6316,
        "exit_mark": 3.4,
        "pnl_usd": -29.21,
        "exit_order_ids": [1417416018],
    }
    trade.update(updates)
    return trade


def test_merge_keeps_authoritative_stored_trade_when_exit_order_id_matches(
        isolated_trade_history, monkeypatch):
    stored = _stored_trade()
    _write_history(isolated_trade_history, [stored])
    monkeypatch.setattr(
        dashboard, "_fetch_reconstructed_trades",
        lambda: [_reconstructed_trade()])

    merged = dashboard._all_trades_merged()

    assert len(merged) == 1
    assert merged[0]["entry_trigger"] == "exchange_sync"
    assert dashboard._pnl_stats(merged)["total_pnl"] == -29.21


def test_merge_handles_legacy_one_second_skew_only_on_full_economic_match(
        isolated_trade_history, monkeypatch):
    stored = _stored_trade(exit_order_id=None)
    reconstructed = _reconstructed_trade(exit_order_ids=[])
    _write_history(isolated_trade_history, [stored])
    monkeypatch.setattr(
        dashboard, "_fetch_reconstructed_trades",
        lambda: [reconstructed])

    assert len(dashboard._all_trades_merged()) == 1

    reconstructed["exit_mark"] = 3.5
    assert len(dashboard._all_trades_merged()) == 2


def test_merge_does_not_collapse_distinct_order_ids_even_if_values_match(
        isolated_trade_history, monkeypatch):
    stored = _stored_trade(exit_order_id=111)
    reconstructed = _reconstructed_trade(exit_order_ids=[222])
    _write_history(isolated_trade_history, [stored])
    monkeypatch.setattr(
        dashboard, "_fetch_reconstructed_trades",
        lambda: [reconstructed])

    assert len(dashboard._all_trades_merged()) == 2


def test_merge_never_cross_matches_exit_id_to_reconstructed_entry_id(
        isolated_trade_history, monkeypatch):
    stored = _stored_trade(exit_order_id=111)
    reconstructed = _reconstructed_trade(
        exit_order_ids=[], entry_order_ids=[111])
    _write_history(isolated_trade_history, [stored])
    monkeypatch.setattr(
        dashboard, "_fetch_reconstructed_trades",
        lambda: [reconstructed])

    assert len(dashboard._all_trades_merged()) == 2


def test_merge_does_not_collapse_legacy_trade_outside_clock_tolerance(
        isolated_trade_history, monkeypatch):
    stored = _stored_trade(exit_order_id=None)
    reconstructed = _reconstructed_trade(
        exit_order_ids=[], entry_time="12:49:40")
    _write_history(isolated_trade_history, [stored])
    monkeypatch.setattr(
        dashboard, "_fetch_reconstructed_trades",
        lambda: [reconstructed])

    assert len(dashboard._all_trades_merged()) == 2


def test_reconstruction_carries_all_entry_and_exit_order_identities(monkeypatch):
    monkeypatch.setattr(
        dashboard, "_product_info",
        lambda product_id: {"contract_value": 0.001, "symbol": "P-BTC-TEST"})
    orders = [
        {
            "id": 101, "client_order_id": "entry-a", "state": "closed",
            "product_symbol": "P-BTC-63200-150726", "product_id": 142150,
            "side": "buy", "size": 100, "average_fill_price": "274",
            "created_at": "2026-07-14T12:49:25.906658Z",
        },
        {
            "id": 102, "client_order_id": "entry-b", "state": "closed",
            "product_symbol": "P-BTC-63200-150726", "product_id": 142150,
            "side": "buy", "size": 14, "average_fill_price": "157",
            "created_at": "2026-07-14T13:25:39.347860Z",
        },
        {
            "id": 103, "client_order_id": "exit-a", "state": "closed",
            "product_symbol": "P-BTC-63200-150726", "product_id": 142150,
            "side": "sell", "size": 114, "average_fill_price": "3.4",
            "created_at": "2026-07-15T03:59:49.232365Z",
        },
    ]

    trades = dashboard._reconstruct_trades_from_orders(orders)

    assert len(trades) == 1
    assert trades[0]["entry_order_ids"] == [101, 102]
    assert trades[0]["entry_client_order_ids"] == ["entry-a", "entry-b"]
    assert trades[0]["exit_order_ids"] == [103]
    assert trades[0]["exit_client_order_ids"] == ["exit-a"]
    assert trades[0]["order_id"] == 101
    assert trades[0]["exit_order_id"] == 103
