import json
import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import tp_monitor


class _StopLoop(RuntimeError):
    pass


class TpMonitorSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.state_file = root / "state.json"
        self.history_file = root / "history.json"
        self.health_file = root / "health.json"
        self.path_patches = [
            patch.object(tp_monitor, "STATE_FILE", self.state_file),
            patch.object(tp_monitor, "HISTORY_FILE", self.history_file),
            patch.object(tp_monitor, "HEALTH_FILE", self.health_file),
        ]
        for item in self.path_patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.path_patches):
            item.stop()
        self.temp_dir.cleanup()

    def write_state(self, **overrides):
        state = {
            "status": "OPEN",
            "product_id": 101,
            "symbol": "C-BTC-TEST",
            "side": "long",
            "lots": 10,
            "entry_mark": 1.0,
            "contract_value": 1.0,
            "entry_date": "2026-07-15",
            "entry_time_utc": "01:02:03",
            "protection_config": {
                "tp_target_pnl": 100,
                "sl_target_pnl": 0,
                "tsl_arm_pnl": 0,
                "tsl_trail_pnl": 0,
                "poll_secs": 10,
            },
        }
        state.update(overrides)
        self.state_file.write_text(json.dumps(state), encoding="utf-8")
        return state

    def read_state(self):
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def test_legacy_tsl_value_falls_back_for_both_new_controls(self):
        with patch.dict("os.environ", {"TSL_TARGET_PNL_TREND": "77"}, clear=True):
            settings = tp_monitor._slot_settings("trend")
        self.assertEqual(settings["tsl_arm_pnl"], 77)
        self.assertEqual(settings["tsl_trail_pnl"], 77)

        with patch.dict("os.environ", {
            "TSL_TARGET_PNL_TREND": "77",
            "TSL_ARM_PNL_TREND": "125",
            "TSL_TRAIL_PNL_TREND": "40",
        }, clear=True):
            settings = tp_monitor._slot_settings("trend")
        self.assertEqual(settings["tsl_arm_pnl"], 125)
        self.assertEqual(settings["tsl_trail_pnl"], 40)

    def test_sigterm_handler_keeps_open_exchange_orders(self):
        self.write_state(tsl_stop_order_id="sl-1", tp_stop_order_id="tp-1")
        with patch.object(tp_monitor.signal, "signal") as register:
            tp_monitor.install_signal_handlers()
        register.assert_called_once_with(signal.SIGTERM, tp_monitor._handle_termination)

        with patch.object(tp_monitor, "cancel_order") as cancel:
            with self.assertRaises(SystemExit):
                tp_monitor._handle_termination(signal.SIGTERM, None)
        cancel.assert_not_called()
        state = self.read_state()
        self.assertEqual(state["tsl_stop_order_id"], "sl-1")
        self.assertEqual(state["tp_stop_order_id"], "tp-1")
        health = json.loads(self.health_file.read_text(encoding="utf-8"))
        self.assertEqual(health["status"], "stopped")
        self.assertTrue(health["protection_retained"])

    def test_cleanup_requires_confirmed_close_or_explicit_action(self):
        self.write_state(
            tsl_stop_order_id="sl-1", tp_stop_order_id="tp-1",
            orphan_protection_order_ids=["old-1"],
        )
        with patch.object(tp_monitor, "cancel_order") as cancel:
            with self.assertRaises(PermissionError):
                tp_monitor.remove_exchange_protection(self.read_state())
        cancel.assert_not_called()

        with patch.object(tp_monitor, "cancel_order", return_value={"success": True}) as cancel:
            ok = tp_monitor.remove_exchange_protection(
                self.read_state(), explicit=True, reason="unit-test explicit action"
            )
        self.assertTrue(ok)
        self.assertEqual(cancel.call_count, 3)
        state = self.read_state()
        self.assertIsNone(state["tsl_stop_order_id"])
        self.assertIsNone(state["tp_stop_order_id"])
        self.assertEqual(state["last_tsl_stop_order_id"], "sl-1")
        self.assertEqual(state["last_tp_stop_order_id"], "tp-1")
        self.assertFalse(state["remove_protection_requested"])

    def test_failed_explicit_cleanup_retains_id_for_retry(self):
        self.write_state(tp_stop_order_id="tp-1", remove_protection_requested=True)
        with patch.object(tp_monitor, "cancel_order", return_value={"success": False}), \
             patch.object(tp_monitor, "get_order", return_value={}):
            ok = tp_monitor.remove_exchange_protection(
                self.read_state(), explicit=True, reason="unit-test"
            )
        self.assertFalse(ok)
        state = self.read_state()
        self.assertEqual(state["tp_stop_order_id"], "tp-1")
        self.assertTrue(state["remove_protection_requested"])

    def test_restart_reconciles_and_reuses_active_persisted_order(self):
        self.write_state(tp_stop_order_id="tp-1", tp_lots=10)
        active_order = {"id": "tp-1", "product_id": 101, "state": "open"}
        with patch.object(tp_monitor, "REMOVE_PROTECTION", False), \
             patch.object(tp_monitor, "install_signal_handlers"), \
             patch.object(tp_monitor, "get_exchange_size", return_value=10), \
             patch.object(tp_monitor, "get_order", return_value=active_order) as get_order, \
             patch.object(tp_monitor, "get_mark", return_value=1.0), \
             patch.object(tp_monitor, "place_stop_order") as place_stop, \
             patch.object(tp_monitor, "send_telegram"), \
             patch.object(tp_monitor.time, "sleep", side_effect=_StopLoop):
            with self.assertRaises(_StopLoop):
                tp_monitor.main()
        get_order.assert_called_with("tp-1")
        place_stop.assert_not_called()
        self.assertEqual(self.read_state()["tp_stop_order_id"], "tp-1")
        health = json.loads(self.health_file.read_text(encoding="utf-8"))
        self.assertEqual(health["status"], "healthy")

    def test_unsupported_exchange_protection_alerts_and_uses_local_fallback(self):
        self.write_state(protection_config={
            "tp_target_pnl": 100, "sl_target_pnl": 50,
            "tsl_arm_pnl": 0, "tsl_trail_pnl": 0, "poll_secs": 30,
        })
        unsupported = {"success": False, "error": {"code": "unsupported"}}
        with patch.object(tp_monitor, "REMOVE_PROTECTION", False), \
             patch.object(tp_monitor, "install_signal_handlers"), \
             patch.object(tp_monitor, "get_exchange_size", return_value=10), \
             patch.object(tp_monitor, "get_mark", return_value=1.0), \
             patch.object(tp_monitor, "place_stop_order", return_value=unsupported), \
             patch.object(tp_monitor, "send_telegram") as telegram, \
             patch.object(tp_monitor.time, "sleep", side_effect=_StopLoop):
            with self.assertRaises(_StopLoop):
                tp_monitor.main()
        telegram.assert_called_once()
        self.assertIn("local monitor fallback", telegram.call_args.args[0])
        health = json.loads(self.health_file.read_text(encoding="utf-8"))
        self.assertEqual(health["status"], "degraded")
        self.assertTrue(health["local_fallback_active"])
        self.assertFalse(health["exchange_protection_complete"])

    def test_close_does_not_remove_protection_until_zero_position_is_confirmed(self):
        self.write_state(tp_stop_order_id="tp-1")
        response = {"success": True, "result": {"id": "close-1", "average_fill_price": "2"}}
        with patch.object(tp_monitor, "get_exchange_size", side_effect=[10, None]), \
             patch.object(tp_monitor, "place_order", return_value=response), \
             patch.object(tp_monitor, "cancel_order") as cancel:
            closed = tp_monitor.close_position(self.read_state(), 2.0, 10.0)
        self.assertFalse(closed)
        cancel.assert_not_called()
        state = self.read_state()
        self.assertEqual(state["status"], "OPEN")
        self.assertEqual(state["pending_close_order_id"], "close-1")
        self.assertEqual(state["tp_stop_order_id"], "tp-1")

    def test_exit_history_records_actual_fees_and_net_pnl(self):
        self.write_state(entry_fee_usd=0.10)
        order = {
            "id": "close-1", "average_fill_price": "2.0", "commission": "0.20",
        }
        response = {"success": True, "result": dict(order)}
        with patch.object(tp_monitor, "get_exchange_size", side_effect=[10, 0]), \
             patch.object(tp_monitor, "place_order", return_value=response), \
             patch.object(tp_monitor, "get_order", return_value=order), \
             patch.object(tp_monitor, "send_telegram"):
            closed = tp_monitor.close_position(self.read_state(), 2.0, 10.0)
        self.assertTrue(closed)
        state = self.read_state()
        self.assertEqual(state["gross_pnl_usd"], 10.0)
        self.assertAlmostEqual(state["fees_usd"], 0.30)
        self.assertEqual(state["pnl_usd"], 9.70)
        history = json.loads(self.history_file.read_text(encoding="utf-8"))
        self.assertEqual(history[0]["gross_pnl_usd"], 10.0)
        self.assertAlmostEqual(history[0]["fees_usd"], 0.30)
        self.assertTrue(history[0]["fees_available"])

    def test_close_identity_is_durable_before_market_order_submit(self):
        self.write_state(tp_stop_order_id="tp-1")

        def submit(_product_id, _symbol, _side, _lots, **kwargs):
            persisted = self.read_state()
            client_id = kwargs["client_order_id"]
            self.assertEqual(persisted["pending_close_client_order_id"], client_id)
            self.assertEqual(persisted["pending_close_state"], "intent_persisted")
            self.assertEqual(persisted["pending_close_lots"], 10)
            return {"success": True, "result": {"id": "close-1"}}

        with patch.object(tp_monitor, "get_exchange_size", side_effect=[10, None]), \
             patch.object(tp_monitor, "place_order", side_effect=submit) as place:
            closed = tp_monitor.close_position(self.read_state(), 2.0, 10.0)

        self.assertFalse(closed)
        place.assert_called_once()
        state = self.read_state()
        self.assertEqual(
            place.call_args.kwargs["client_order_id"],
            state["pending_close_client_order_id"],
        )
        self.assertEqual(state["pending_close_order_id"], "close-1")
        self.assertEqual(state["tp_stop_order_id"], "tp-1")

    def test_response_loss_reconciles_exact_identity_and_verified_flat(self):
        self.write_state(tp_stop_order_id="tp-1", entry_fee_usd=0.10)
        exact = {
            "id": "close-1", "client_order_id": "durable-close-id",
            "state": "closed", "average_fill_price": "2.0", "commission": "0.20",
        }

        def response_lost(_product_id, _symbol, _side, _lots, **kwargs):
            exact["client_order_id"] = kwargs["client_order_id"]
            raise tp_monitor.requests.Timeout("response lost")

        with patch.object(tp_monitor, "get_exchange_size", side_effect=[10, 0]), \
             patch.object(tp_monitor, "place_order", side_effect=response_lost) as place, \
             patch.object(tp_monitor, "_lookup_pending_close",
                          return_value=(exact, True)) as lookup, \
             patch.object(tp_monitor, "send_telegram"):
            closed = tp_monitor.close_position(self.read_state(), 2.0, 10.0)

        self.assertTrue(closed)
        place.assert_called_once()
        lookup.assert_called_once()
        state = self.read_state()
        self.assertEqual(state["status"], "CLOSED")
        self.assertEqual(state["exit_order_id"], "close-1")
        self.assertEqual(state["exit_client_order_id"], exact["client_order_id"])
        self.assertIsNone(state["pending_close_client_order_id"])
        # The close routine does not cancel protection; its caller may do so
        # only after this verified-flat True result.
        self.assertEqual(state["tp_stop_order_id"], "tp-1")

    def test_ambiguous_response_loss_keeps_identity_and_blocks_duplicate(self):
        self.write_state(tp_stop_order_id="tp-1")
        with patch.object(tp_monitor, "get_exchange_size", side_effect=[10, 10]), \
             patch.object(tp_monitor, "place_order",
                          side_effect=tp_monitor.requests.Timeout("response lost")) as place, \
             patch.object(tp_monitor, "_lookup_pending_close",
                          return_value=({}, False)) as lookup:
            first_closed = tp_monitor.close_position(self.read_state(), 2.0, 10.0)

        self.assertFalse(first_closed)
        place.assert_called_once()
        lookup.assert_called_once()
        first_identity = self.read_state()["pending_close_client_order_id"]
        self.assertTrue(first_identity)
        self.assertEqual(self.read_state()["pending_close_state"], "ambiguous")

        with patch.object(tp_monitor, "get_exchange_size", return_value=10), \
             patch.object(tp_monitor, "place_order") as second_place, \
             patch.object(tp_monitor, "_lookup_pending_close",
                          return_value=({}, False)) as second_lookup:
            second_closed = tp_monitor.close_position(self.read_state(), 2.0, 10.0)

        self.assertFalse(second_closed)
        second_lookup.assert_called_once()
        second_place.assert_not_called()
        state = self.read_state()
        self.assertEqual(state["pending_close_client_order_id"], first_identity)
        self.assertEqual(state["tp_stop_order_id"], "tp-1")

    def test_conclusive_not_found_retries_same_client_identity(self):
        self.write_state(
            pending_close_client_order_id="close-client-1",
            pending_close_reason="take_profit",
            pending_close_state="not_found_retryable",
            pending_close_attempts=1,
            pending_close_created_utc="2026-07-15T01:00:00+00:00",
        )
        response = {"success": True, "result": {"id": "close-2"}}
        with patch.object(tp_monitor, "get_exchange_size", side_effect=[10, None]), \
             patch.object(tp_monitor, "_lookup_pending_close",
                          return_value=({}, True)) as lookup, \
             patch.object(tp_monitor, "place_order", return_value=response) as place:
            closed = tp_monitor.close_position(self.read_state(), 2.0, 10.0)

        self.assertFalse(closed)
        lookup.assert_called_once()
        place.assert_called_once()
        self.assertEqual(place.call_args.kwargs["client_order_id"], "close-client-1")
        state = self.read_state()
        self.assertEqual(state["pending_close_client_order_id"], "close-client-1")
        self.assertEqual(state["pending_close_attempts"], 2)
        self.assertEqual(state["pending_close_created_utc"], "2026-07-15T01:00:00+00:00")

    def test_failed_intent_persistence_never_submits_close(self):
        state = self.write_state()
        with patch.object(tp_monitor, "get_exchange_size", return_value=10), \
             patch.object(tp_monitor, "_persist_close_fields", return_value=False), \
             patch.object(tp_monitor, "place_order") as place:
            closed = tp_monitor._close_position_locked(state, 2.0, 10.0)
        self.assertFalse(closed)
        place.assert_not_called()

    def test_client_identity_absence_needs_open_and_history_success(self):
        open_orders = Mock()
        open_orders.json.return_value = {"success": True, "result": []}
        with patch.object(tp_monitor, "_sign", return_value={}), \
             patch.object(tp_monitor.requests, "get", side_effect=[
                 open_orders, tp_monitor.requests.Timeout("history unavailable"),
             ]):
            order, conclusive = tp_monitor.get_order_by_client_id("close-client-1", 101)
        self.assertEqual(order, {})
        self.assertFalse(conclusive)

    def test_protection_request_contains_reduce_only_owned_client_id(self):
        response = Mock()
        response.json.return_value = {"success": True, "result": {"id": "sl-1"}}
        with patch.object(tp_monitor, "_sign", return_value={}), \
             patch.object(tp_monitor.requests, "post", return_value=response) as post:
            result = tp_monitor.place_stop_order(101, "sell", 10, 1.5)
        payload = json.loads(post.call_args.kwargs["data"])
        self.assertTrue(payload["reduce_only"])
        self.assertTrue(payload["client_order_id"].startswith("nithi-tp-"))
        self.assertLessEqual(len(payload["client_order_id"]), 32)
        self.assertEqual(result["result"]["client_order_id"], payload["client_order_id"])


if __name__ == "__main__":
    unittest.main()
