from datetime import datetime, timedelta, timezone
import json
from unittest.mock import Mock, patch

import pytest

import Delta_Straddle_Live as bot
from risk_controls import RiskDecision


def _move_history_paths(tmp_path):
    return patch.object(bot, "DATA_DIR", tmp_path), patch.object(
        bot, "HISTORY_FILE", tmp_path / "trade_history.json")


def test_external_pending_history_preserves_null_realized_fields(tmp_path):
    data_patch, history_patch = _move_history_paths(tmp_path)
    state = {
        "slot": "evening", "status": "CLOSED", "history_pending": True,
        "entry_date": "2026-07-15", "entry_time_utc": "01:00:00",
        "symbol": "MV-BTC-TEST", "lots": 10, "entry_mark": 100,
        "exit_trigger": "settlement_or_external",
    }
    with data_patch, history_patch:
        final = bot.log_trade(state)

    assert final is False
    row = json.loads((tmp_path / "trade_history.json").read_text())[0]
    assert row["exit_mark"] is None
    assert row["gross_pnl_usd"] is None
    assert row["fees_usd"] is None
    assert row["pnl_usd"] is None
    assert row["accounting_status"] == "pending"


def test_pending_external_history_flush_does_not_clear_outbox(tmp_path):
    data_patch, history_patch = _move_history_paths(tmp_path)
    state = {
        "slot": "evening", "status": "CLOSED", "history_pending": True,
        "history_logged": False, "entry_date": "2026-07-15",
        "entry_time_utc": "01:00:00", "symbol": "MV-BTC-TEST",
        "lots": 10, "entry_mark": 100,
        "exit_trigger": "settlement_or_external",
    }
    saved = []
    with data_patch, history_patch, \
         patch.object(bot, "load_morning_state", return_value=None), \
         patch.object(bot, "load_state", return_value=dict(state)), \
         patch.object(bot, "save_state", side_effect=lambda value: saved.append(dict(value))):
        complete = bot._flush_pending_move_history()

    assert complete is False
    assert not saved or saved[-1]["history_pending"] is True


def test_history_retry_never_overwrites_complete_or_stronger_row(tmp_path):
    data_patch, history_patch = _move_history_paths(tmp_path)
    complete_row = {
        "date": "2026-07-15", "entry_date": "2026-07-15",
        "entry_time": "01:00:00", "symbol": "MV-BTC-TEST",
        "exit_mark": 80, "gross_pnl_usd": -2, "fees_usd": .3,
        "entry_fee_usd": .1, "exit_fee_usd": .2, "pnl_usd": -2.3,
        "pnl_includes_fees": True, "exit_trigger": "closed_externally",
        "exit_order_id": 99, "accounting_status": "complete",
    }
    path = tmp_path / "trade_history.json"
    path.write_text(json.dumps([complete_row]))
    incoming = {
        "slot": "evening", "entry_date": "2026-07-15",
        "entry_time_utc": "01:00:00", "symbol": "MV-BTC-TEST",
        "exit_trigger": "settlement_or_external",
    }
    with data_patch, history_patch:
        assert bot.log_trade(incoming) is True
    stored = json.loads(path.read_text())[0]
    assert stored["pnl_usd"] == -2.3
    assert stored["exit_order_id"] == 99

    stronger_pending = dict(
        complete_row, fees_usd=None, entry_fee_usd=None, exit_fee_usd=None,
        pnl_includes_fees=False, accounting_status="pending",
    )
    path.write_text(json.dumps([stronger_pending]))
    with data_patch, history_patch:
        assert bot.log_trade(incoming) is False
    stored = json.loads(path.read_text())[0]
    assert stored["exit_mark"] == 80
    assert stored["gross_pnl_usd"] == -2
    assert stored["exit_order_id"] == 99


def test_exchange_already_flat_close_keeps_history_pending(tmp_path):
    state = {
        "slot": "evening", "status": "OPEN", "side": "long",
        "product_id": 1, "symbol": "MV-X", "lots": 10,
        "entry_mark": 100, "contract_value": .001, "btc_at_entry": 65000,
        "entry_date": "2026-07-15", "entry_time_utc": "01:00:00",
    }
    saved = []
    with patch.object(bot, "DATA_DIR", tmp_path), \
         patch.object(bot, "get_mv_position", return_value=None), \
         patch.object(bot, "get_mv_mark", return_value=110), \
         patch.object(bot, "get_btc_price", return_value=66000), \
         patch.object(bot, "log_trade", return_value=False), \
         patch.object(bot, "audit_event"), patch.object(bot, "send_telegram"):
        bot._close_position_job(state, lambda value: saved.append(dict(value)), "EVENING")

    assert saved[-1]["status"] == "CLOSED"
    assert saved[-1]["history_pending"] is True
    assert saved[-1]["history_logged"] is False


def test_execution_snapshot_rejects_wide_spread():
    ticker = {"result": {"quotes": {"best_bid": "90", "best_ask": "110"},
                         "mark_price": "100", "spot_price": "65000"}}
    book = {"result": {"buy": [{"price": "90", "size": 100}],
                       "sell": [{"price": "110", "size": 100}]}}
    with patch.object(bot, "_retry", side_effect=[ticker, book]), \
         patch.object(bot, "MAX_SPREAD_PCT", 3):
        with pytest.raises(RuntimeError, match="spread gate"):
            bot.get_execution_snapshot("MV-X", "buy")


def test_controlled_entry_chunks_and_records_real_fills(tmp_path):
    snap = {"liquidity_cap": 1000, "limit_price": 101, "tick_size": .1,
            "ask": 100, "bid": 99}
    responses = [
        {"success": True, "result": {"id": 1, "state": "closed", "size": 500, "unfilled_size": 0,
                                      "average_fill_price": "100.5", "paid_commission": ".2"}},
        {"success": True, "result": {"id": 2, "state": "closed", "size": 300, "unfilled_size": 0,
                                      "average_fill_price": "100.8", "paid_commission": ".1"}},
    ]
    with patch.object(bot, "DATA_DIR", tmp_path), patch.object(bot, "DRY_RUN", False), \
         patch.object(bot, "ORDER_CHUNK_LOTS", 500), \
         patch.object(bot, "_post", side_effect=responses), \
         patch.object(bot, "get_execution_snapshot", return_value=snap):
        response, lots = bot.place_controlled_entry(1, "MV-X", "buy", 800, "evening", snap)
    assert lots == 800
    assert response["result"]["order_ids"] == [1, 2]
    assert response["result"]["paid_commission"] == pytest.approx(.3)
    assert (tmp_path / "pending_evening_entry.json").exists()


def test_missing_quote_timestamp_fails_closed():
    ticker = {"result": {"quotes": {"best_bid": "99", "best_ask": "100"},
                         "mark_price": "99.5", "spot_price": "65000"}}
    book = {"result": {"buy": [{"price": "99", "size": 100}],
                       "sell": [{"price": "100", "size": 100}]}}
    with patch.object(bot, "_retry", side_effect=[ticker, book]):
        with pytest.raises(RuntimeError, match="timestamp unavailable"):
            bot.get_execution_snapshot("MV-X", "buy")


def test_terminal_fill_requires_explicit_size_evidence():
    assert bot._verified_terminal_fill(
        {"id": 1, "state": "closed", "average_fill_price": "100"}, 50) is None
    assert bot._verified_terminal_fill(
        {"id": 1, "state": "closed", "unfilled_size": 20,
         "average_fill_price": "100"}, 50) == 30


def test_entry_identity_is_journalled_before_transport_failure(tmp_path):
    snap = {"liquidity_cap": 10, "limit_price": 101, "tick_size": .1,
            "ask": 100, "bid": 99}
    with patch.object(bot, "DATA_DIR", tmp_path), patch.object(bot, "DRY_RUN", False), \
         patch.object(bot, "_post", side_effect=TimeoutError("lost response")), \
         patch.object(bot, "recover_pending_entries") as recover:
        with pytest.raises(TimeoutError):
            bot.place_controlled_entry(7, "MV-X", "buy", 10, "evening", snap)
    journal = json.loads((tmp_path / "pending_evening_entry.json").read_text())
    assert journal["orders"][0]["client_order_id"].startswith("mb-")
    assert journal["orders"][0]["status"] == "submission_unknown"
    recover.assert_called_once_with(("evening",))


def test_affordability_failure_blocks_instead_of_using_configured():
    with patch.object(bot, "DRY_RUN", False), \
         patch.object(bot, "DYNAMIC_LOTS", True), \
         patch.object(bot, "get_available_usd", side_effect=RuntimeError("wallet down")):
        assert bot._effective_lots(1000, 100, .001, "TEST") == 0


def test_dry_run_sizing_uses_virtual_cap_without_wallet_access():
    with patch.object(bot, "DRY_RUN", True), \
         patch.object(bot, "MAX_ORDER_LOTS", 600), \
         patch.object(bot, "get_available_usd") as wallet, \
         patch.object(bot, "get_btc_price") as spot:
        assert bot._effective_lots(1000, 100, .001, "TEST") == 600

    wallet.assert_not_called()
    spot.assert_not_called()


def test_move_value_gate_uses_only_completed_candles():
    now = datetime.now(timezone.utc)
    base = int(now.timestamp()) - 100 * 900
    candles = []
    price = 64000.0
    for i in range(100):
        price += 25 if i % 2 else -10
        candles.append({"time": base + i * 900, "open": price - 5,
                        "high": price + 40, "low": price - 40, "close": price})
    contract = {"symbol": "MV-X", "settlement_time": (now + timedelta(hours=12)).isoformat()}
    snapshot = {"ask": 100, "bid": 95}
    with patch.object(bot, "_retry", return_value={"result": candles}), \
         patch.object(bot, "MOVE_VALUE_FILTER_ENABLED", False):
        result = bot.move_value_signal(contract, snapshot, "buy")
    assert result["tte_minutes"] > 600
    assert "forecast_abs_move" in result


def test_short_move_requires_explicit_enable():
    with patch.object(bot, "ALLOW_SHORT_MOVE", False):
        with pytest.raises(RuntimeError, match="short MOVE entries are disabled"):
            bot.build_move_entry_plan({"symbol": "MV-X"}, 10, "sell", "evening")


def test_scheduled_dry_short_uses_short_cap_as_paper_risk_assumption(tmp_path):
    snapshot = {
        "ask": 145, "bid": 144, "spot": 64000,
        "liquidity_cap": 1000,
    }
    decision = RiskDecision(
        True, "risk checks passed", "2026-07-18", 0, 0, 0, 0)
    with patch.object(bot, "DATA_DIR", tmp_path), \
         patch.object(bot, "DRY_RUN", True), \
         patch.object(bot, "ALLOW_SHORT_MOVE", True), \
         patch.object(bot, "SHORT_MAX_RISK_USD", 250), \
         patch.object(bot, "MAX_ORDER_LOTS", 1000), \
         patch.object(bot, "_assert_entry_configuration"), \
         patch.object(bot, "_protection_snapshot",
                      return_value={"tp_target_pnl": 200, "sl_target_pnl": 0}), \
         patch.object(bot, "load_states", return_value={}), \
         patch.object(bot, "get_execution_snapshot", return_value=snapshot), \
         patch.object(bot, "move_value_signal", return_value={"eligible": True}), \
         patch.object(bot, "_effective_lots", return_value=1000), \
         patch.object(bot, "_slot_risk", return_value=(500, 0)), \
         patch.object(bot, "evaluate_entry", return_value=decision), \
         patch.object(bot, "audit_event"):
        plan = bot.build_move_entry_plan(
            {"id": 7, "symbol": "MV-X", "contract_value": ".001"},
            1000, "sell", "evening")

    assert plan["lots"] == 1000
    assert plan["risk_budget_usd"] == 250
    assert plan["configured_stop_loss_usd"] == 0
    assert plan["stop_loss_usd"] == 250
    assert plan["paper_short_risk_assumption_usd"] == 250
    assert plan["risk_at_entry_usd"] == 250


def test_scheduled_live_short_still_requires_positive_sl(tmp_path):
    snapshot = {
        "ask": 145, "bid": 144, "spot": 64000,
        "liquidity_cap": 100,
    }
    with patch.object(bot, "DATA_DIR", tmp_path), \
         patch.object(bot, "DRY_RUN", False), \
         patch.object(bot, "ALLOW_SHORT_MOVE", True), \
         patch.object(bot, "SHORT_MAX_RISK_USD", 250), \
         patch.object(bot, "_assert_entry_configuration"), \
         patch.object(bot, "_protection_snapshot",
                      return_value={"tp_target_pnl": 200, "sl_target_pnl": 0}), \
         patch.object(bot, "load_states", return_value={}), \
         patch.object(bot, "get_mv_position", return_value=None), \
         patch.object(bot, "get_execution_snapshot", return_value=snapshot), \
         patch.object(bot, "move_value_signal", return_value={"eligible": True}), \
         patch.object(bot, "_effective_lots", return_value=100), \
         patch.object(bot, "_slot_risk", return_value=(500, 0)):
        with pytest.raises(RuntimeError, match="positive SL"):
            bot.build_move_entry_plan(
                {"id": 7, "symbol": "MV-X", "contract_value": ".001"},
                100, "sell", "evening")


def test_external_position_blocks_new_automated_risk():
    response = {"success": True, "result": [
        {"product_id": 99, "product_symbol": "BTCUSD", "size": 1,
         "unrealized_pnl": "5"},
    ]}
    with patch.object(bot, "_retry", return_value=response), \
         patch.object(bot, "load_states", return_value={}), \
         patch.object(bot, "ALLOW_EXTERNAL_POSITIONS_WITH_BOT", False):
        with pytest.raises(RuntimeError, match="external/manual position"):
            bot._account_unrealized_pnl()


def test_account_unrealized_includes_every_owned_position():
    response = {"success": True, "result": [
        {"product_id": 1, "product_symbol": "MV-X", "size": 10,
         "unrealized_pnl": "-12.5"},
        {"product_id": 2, "product_symbol": "C-X", "size": 10,
         "unrealized_pnl": "3.5"},
    ]}
    states = {"evening": {"status": "OPEN", "product_id": 1},
              "trend": {"status": "OPEN", "product_id": 2}}
    with patch.object(bot, "_retry", return_value=response), \
         patch.object(bot, "load_states", return_value=states):
        assert bot._account_unrealized_pnl() == -9.0


def test_unowned_stop_blocks_entry_without_cancelling():
    open_orders = {"success": True, "result": [
        {"id": 9, "product_id": 1, "stop_order_type": "stop_loss_order",
         "client_order_id": "manual-stop"},
    ]}
    with patch.object(bot, "DRY_RUN", False), \
         patch.object(bot, "load_state", return_value={}), \
         patch.object(bot, "load_morning_state", return_value={}), \
         patch.object(bot, "_retry", return_value=open_orders), \
         patch.object(bot.requests, "delete") as delete:
        with pytest.raises(RuntimeError, match="unowned resting protection"):
            bot.cancel_product_stops(1)
    delete.assert_not_called()


def test_scheduled_close_stays_open_when_exchange_size_remains():
    state = {"slot": "evening", "status": "OPEN", "side": "long",
             "product_id": 1, "symbol": "MV-X", "lots": 10,
             "entry_mark": 100, "contract_value": .001, "btc_at_entry": 65000}
    position = {"product_id": 1, "size": 10}
    saved = []
    order = {"id": 77, "state": "closed", "unfilled_size": 5,
             "average_fill_price": "110", "paid_commission": ".01"}
    with patch.object(bot, "get_mv_position", side_effect=[position] * 5), \
         patch.object(bot, "get_mv_mark", return_value=110), \
         patch.object(bot, "get_btc_price", return_value=66000), \
         patch.object(bot, "place_market_order", return_value={"success": True, "result": order}) as place, \
         patch.object(bot, "_wait_for_terminal_fill", return_value=(order, 5)), \
         patch.object(bot, "audit_event"), patch.object(bot, "send_telegram"), \
         patch.object(bot.time, "sleep"):
        with pytest.raises(RuntimeError, match="left 10 lots open"):
            bot._close_position_job(state, lambda value: saved.append(dict(value)), "EVENING")
    assert saved[-1]["status"] == "OPEN"
    assert place.call_args.kwargs["reduce_only"] is True


def test_scheduled_close_only_marks_closed_after_zero_size():
    state = {"slot": "evening", "status": "OPEN", "side": "long",
             "product_id": 1, "symbol": "MV-X", "lots": 10,
             "entry_mark": 100, "contract_value": .001, "btc_at_entry": 65000,
             "entry_commission_usd": .02, "entry_date": "2026-07-15",
             "entry_time_utc": "01:00:00"}
    position = {"product_id": 1, "size": 10}
    saved = []
    order = {"id": 78, "state": "closed", "unfilled_size": 0,
             "average_fill_price": "110", "paid_commission": ".01"}
    with patch.object(bot, "get_mv_position", side_effect=[position, None]), \
         patch.object(bot, "get_mv_mark", return_value=109), \
         patch.object(bot, "get_btc_price", return_value=66000), \
         patch.object(bot, "place_market_order", return_value={"success": True, "result": order}) as place, \
         patch.object(bot, "_wait_for_terminal_fill", return_value=(order, 10)), \
         patch.object(bot, "log_trade"), patch.object(bot, "audit_event"), \
         patch.object(bot, "send_telegram"):
        bot._close_position_job(state, lambda value: saved.append(dict(value)), "EVENING")
    assert saved[-1]["status"] == "CLOSED"
    assert saved[-1]["exit_order_id"] == 78
    assert place.call_args.kwargs["reduce_only"] is True


def test_close_response_loss_recovers_exact_persisted_identity(tmp_path):
    state = {"slot": "evening", "status": "OPEN", "side": "long",
             "product_id": 1, "symbol": "MV-X", "lots": 10,
             "owned_entry_lots": 10, "entry_mark": 100,
             "contract_value": .001, "btc_at_entry": 65000,
             "entry_commission_usd": .02, "entry_date": "2026-07-15",
             "entry_time_utc": "01:00:00"}
    position = {"product_id": 1, "size": 10}
    saved = []
    submitted = {}

    def save(value):
        saved.append(dict(value))

    def lose_response(product_id, symbol, side, size, **kwargs):
        submitted["client_id"] = kwargs["client_order_id"]
        # The durable identity must exist before the network call starts.
        assert saved[-1]["pending_close_client_order_id"] == submitted["client_id"]
        assert saved[-1]["pending_close_submission_state"] == "prepared"
        raise TimeoutError("response lost")

    def recover(order_id, client_order_id, product_id):
        assert order_id is None
        assert client_order_id == submitted["client_id"]
        return {"id": 91, "client_order_id": client_order_id,
                "product_id": product_id, "side": "sell", "reduce_only": True,
                "state": "closed", "unfilled_size": 0,
                "average_fill_price": "110", "paid_commission": ".01"}

    with patch.object(bot, "DATA_DIR", tmp_path), \
         patch.object(bot, "get_mv_position", side_effect=[position, None]), \
         patch.object(bot, "place_market_order", side_effect=lose_response) as place, \
         patch.object(bot, "_lookup_owned_order", side_effect=recover) as lookup, \
         patch.object(bot, "get_btc_price", return_value=66000), \
         patch.object(bot, "log_trade"), patch.object(bot, "audit_event"), \
         patch.object(bot, "send_telegram"):
        bot._close_position_job(state, save, "EVENING")

    assert place.call_count == 1
    assert lookup.call_count == 1
    assert saved[-1]["status"] == "CLOSED"
    assert saved[-1]["exit_order_id"] == 91
    assert saved[-1]["exit_client_order_id"] == submitted["client_id"]
    assert saved[-1]["pending_close_client_order_id"] is None


def test_reduce_only_market_submission_is_single_attempt():
    with patch.object(bot, "DRY_RUN", False), \
         patch.object(bot, "_post", side_effect=TimeoutError("response lost")) as post, \
         patch.object(bot, "_retry") as retry:
        with pytest.raises(TimeoutError, match="response lost"):
            bot.place_market_order(
                1, "MV-X", "sell", 10, force_real=True,
                client_order_id="mc-exact-close", reduce_only=True,
            )

    post.assert_called_once()
    retry.assert_not_called()


def test_unresolved_close_response_blocks_duplicate_submission(tmp_path):
    state = {"slot": "evening", "status": "OPEN", "side": "long",
             "product_id": 1, "symbol": "MV-X", "lots": 10,
             "entry_mark": 100, "contract_value": .001, "btc_at_entry": 65000}
    position = {"product_id": 1, "size": 10}
    saved = []

    with patch.object(bot, "DATA_DIR", tmp_path), \
         patch.object(bot, "get_mv_position", return_value=position), \
         patch.object(bot, "place_market_order", side_effect=TimeoutError("lost")) as first, \
         patch.object(bot, "_lookup_owned_order", return_value=None):
        with pytest.raises(RuntimeError, match="exact recovery pending"):
            bot._close_position_job(state, lambda value: saved.append(dict(value)), "EVENING")

    assert first.call_count == 1
    pending = dict(saved[-1])
    durable_id = pending["pending_close_client_order_id"]
    assert durable_id
    assert pending["pending_close_submission_state"] == "submission_unknown"

    with patch.object(bot, "DATA_DIR", tmp_path), \
         patch.object(bot, "get_mv_position", return_value=position), \
         patch.object(bot, "place_market_order") as duplicate, \
         patch.object(bot, "_lookup_owned_order", return_value=None):
        with pytest.raises(RuntimeError, match="duplicate close blocked"):
            bot._close_position_job(
                pending, lambda value: saved.append(dict(value)), "EVENING")

    duplicate.assert_not_called()
    assert saved[-1]["pending_close_client_order_id"] == durable_id


def test_close_reloads_pending_identity_after_acquiring_lock(tmp_path):
    stale = {"slot": "evening", "status": "OPEN", "side": "long",
             "product_id": 1, "symbol": "MV-X", "lots": 10,
             "entry_mark": 100, "contract_value": .001,
             "btc_at_entry": 65000}
    durable_id = "mc-durable-before-lock"
    latest = dict(stale, pending_close_order_id=None,
                  pending_close_client_order_id=durable_id,
                  pending_close_requested_lots=10,
                  pending_close_start_size=10,
                  pending_close_side="sell",
                  pending_close_submission_state="submission_unknown")
    saved = []

    with patch.object(bot, "DATA_DIR", tmp_path), \
         patch.object(bot, "get_mv_position",
                      return_value={"product_id": 1, "size": 10}), \
         patch.object(bot, "_lookup_owned_order", return_value=None) as lookup, \
         patch.object(bot, "place_market_order") as duplicate:
        with pytest.raises(RuntimeError, match="duplicate close blocked"):
            bot._close_position_job(
                stale, lambda value: saved.append(dict(value)), "EVENING",
                load_fn=lambda: dict(latest),
            )

    lookup.assert_called_once_with(None, durable_id, 1)
    duplicate.assert_not_called()
    assert saved[-1]["pending_close_client_order_id"] == durable_id


def test_recovered_partial_close_accounting_is_carried_to_final_exit(tmp_path):
    state = {"slot": "evening", "status": "OPEN", "side": "long",
             "product_id": 1, "symbol": "MV-X", "lots": 10,
             "owned_entry_lots": 10, "entry_mark": 100,
             "contract_value": .001, "btc_at_entry": 65000,
             "entry_commission_usd": .02, "entry_date": "2026-07-15",
             "entry_time_utc": "01:00:00"}
    position_10 = {"product_id": 1, "size": 10}
    position_5 = {"product_id": 1, "size": 5}
    saved = []
    first_client = {}

    def lose_then_recover(product_id, symbol, side, size, **kwargs):
        first_client["id"] = kwargs["client_order_id"]
        raise TimeoutError("response lost after partial fill")

    def recovered_partial(order_id, client_order_id, product_id):
        return {"id": 101, "client_order_id": client_order_id,
                "product_id": product_id, "side": "sell", "reduce_only": True,
                "state": "closed", "unfilled_size": 5,
                "average_fill_price": "110", "paid_commission": ".01"}

    with patch.object(bot, "DATA_DIR", tmp_path), \
         patch.object(bot, "get_mv_position",
                      side_effect=[position_10, position_5, position_5,
                                   position_5, position_5]), \
         patch.object(bot, "place_market_order", side_effect=lose_then_recover), \
         patch.object(bot, "_lookup_owned_order", side_effect=recovered_partial), \
         patch.object(bot, "get_btc_price", return_value=66000), \
         patch.object(bot, "audit_event"), patch.object(bot, "send_telegram"), \
         patch.object(bot.time, "sleep"):
        with pytest.raises(RuntimeError, match="left 5 lots open"):
            bot._close_position_job(
                state, lambda value: saved.append(dict(value)), "EVENING")

    partial = dict(saved[-1])
    assert partial["status"] == "OPEN"
    assert partial["lots"] == 5
    assert partial["partial_exit_gross_pnl_usd"] == pytest.approx(.05)
    assert partial["partial_exit_fees_usd"] == pytest.approx(.01)
    assert partial["pending_close_client_order_id"] is None
    assert partial["last_close_client_order_id"] == first_client["id"]

    final_order = {"id": 102, "state": "closed", "unfilled_size": 0,
                   "average_fill_price": "120", "paid_commission": ".02"}
    with patch.object(bot, "DATA_DIR", tmp_path), \
         patch.object(bot, "get_mv_position", side_effect=[position_5, None]), \
         patch.object(bot, "place_market_order",
                      return_value={"success": True, "result": final_order}) as second, \
         patch.object(bot, "get_btc_price", return_value=66000), \
         patch.object(bot, "log_trade"), patch.object(bot, "audit_event"), \
         patch.object(bot, "send_telegram"):
        bot._close_position_job(
            partial, lambda value: saved.append(dict(value)), "EVENING")

    final = saved[-1]
    assert second.call_count == 1
    assert second.call_args.kwargs["client_order_id"] != first_client["id"]
    assert final["status"] == "CLOSED"
    assert final["gross_pnl_usd"] == pytest.approx(.15)
    assert final["exit_commission_usd"] == pytest.approx(.03)
    assert final["fees_usd"] == pytest.approx(.05)
    assert final["pnl_usd"] == pytest.approx(.10)
