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
            "exit_detected_at_utc": "2026-07-15T01:10:00Z",
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
            "id": "close-1", "state": "closed", "size": 10,
            "unfilled_size": 0, "average_fill_price": "2.0",
            "commission": "0.20",
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

    def test_external_close_resolver_requires_post_entry_opposite_full_fill(self):
        state = self.write_state(tp_stop_order_id="tp-close")
        response = Mock()
        response.json.return_value = {"success": True, "result": [
            {"id": "pre-entry", "product_id": 101, "side": "sell", "state": "closed",
             "size": 10, "average_fill_price": "0.9",
             "created_at": "2026-07-15T01:01:59Z"},
            {"id": "wrong-side", "product_id": 101, "side": "buy", "state": "closed",
             "size": 10, "average_fill_price": "0.8",
             "created_at": "2026-07-15T01:03:00Z"},
            {"id": "partial-later", "product_id": 101, "side": "sell", "state": "closed",
             "size": 5, "average_fill_price": "0.7",
             "created_at": "2026-07-15T01:05:00Z"},
            {"id": "manual-later", "product_id": 101, "side": "sell", "state": "closed",
             "size": 10, "average_fill_price": "0.6",
             "created_at": "2026-07-15T01:04:00Z"},
            {"id": "safe-reduce-later", "product_id": 101, "side": "sell", "state": "closed",
             "size": 10, "unfilled_size": 0, "average_fill_price": "0.55",
             "reduce_only": True,
             "created_at": "2026-07-15T01:06:00Z"},
            {"id": "oversized-latest", "product_id": 101, "side": "sell", "state": "closed",
             "size": 20, "unfilled_size": 0, "average_fill_price": "0.45",
             "reduce_only": True,
             "created_at": "2026-07-15T01:07:00Z"},
            {"id": "after-detection", "product_id": 101, "side": "sell", "state": "closed",
             "size": 10, "unfilled_size": 0, "average_fill_price": "0.40",
             "reduce_only": True,
             "created_at": "2026-07-15T01:11:00Z"},
            {"id": "tp-close", "product_id": 101, "side": "sell", "state": "closed",
             "size": 10, "unfilled_size": 0, "average_fill_price": "0.5",
             "created_at": "2026-07-15T01:03:00Z"},
        ]}
        with patch.object(tp_monitor, "_sign", return_value={}), \
             patch.object(tp_monitor.requests, "get", return_value=response) as get:
            order, conclusive, error = tp_monitor._resolve_external_close_order(state)
            self.assertTrue(conclusive)
            self.assertEqual(error, "")
            self.assertEqual(order["id"], "tp-close")
            self.assertEqual(get.call_args.kwargs["params"], {
                "page_size": 50, "product_ids": "101",
            })

            # Without a persisted protection identity, the later manual order is
            # rejected because it does not explicitly prove reduce-only intent.
            state.pop("tp_stop_order_id")
            order, conclusive, error = tp_monitor._resolve_external_close_order(state)
            self.assertTrue(conclusive)
            self.assertEqual(error, "")
            self.assertEqual(order["id"], "safe-reduce-later")

    def test_external_flat_close_uses_history_fill_and_fee_aware_net_loss(self):
        self.write_state(entry_fee_usd=0.10)
        close_order = {
            "id": "external-close", "client_order_id": "manual-close",
            "product_id": 101, "side": "sell", "state": "closed", "size": 10,
            "unfilled_size": 0, "average_fill_price": "0.5", "commission": "0.20",
            "created_at": "2026-07-15T01:03:00Z",
        }
        with patch.object(tp_monitor, "get_exchange_size", return_value=0), \
             patch.object(tp_monitor, "_resolve_external_close_order",
                          return_value=(close_order, True, "")):
            closed = tp_monitor.close_position(self.read_state(), 0.5, -5.0)

        self.assertTrue(closed)
        state = self.read_state()
        self.assertEqual(state["status"], "CLOSED")
        self.assertEqual(state["exit_order_id"], "external-close")
        self.assertEqual(state["exit_mark"], 0.5)
        self.assertEqual(state["gross_pnl_usd"], -5.0)
        self.assertEqual(state["pnl_usd"], -5.3)
        self.assertAlmostEqual(state["fees_usd"], 0.30)
        self.assertTrue(state["pnl_includes_fees"])
        self.assertFalse(state["history_pending"])
        history = json.loads(self.history_file.read_text(encoding="utf-8"))
        self.assertEqual(history[0]["pnl_usd"], -5.3)
        self.assertEqual(history[0]["accounting_status"], "complete")

    def test_external_flat_unknown_accounting_stays_null_and_pending(self):
        self.write_state()
        with patch.object(tp_monitor, "get_exchange_size", return_value=0), \
             patch.object(tp_monitor, "_resolve_external_close_order", return_value=(
                 {}, False, "order history unavailable",
             )):
            closed = tp_monitor.close_position(self.read_state(), 1.0, 0.0)

        self.assertTrue(closed)
        state = self.read_state()
        self.assertEqual(state["status"], "CLOSED")
        self.assertIsNone(state["exit_mark"])
        self.assertIsNone(state["gross_pnl_usd"])
        self.assertIsNone(state["pnl_usd"])
        self.assertIsNone(state["fees_usd"])
        self.assertTrue(state["history_pending"])
        self.assertFalse(state["history_logged"])
        self.assertEqual(state["exit_reconciliation_status"], "pending_order_history")
        history = json.loads(self.history_file.read_text(encoding="utf-8"))
        self.assertIsNone(history[0]["pnl_usd"])
        self.assertEqual(history[0]["accounting_status"], "pending")

    def test_verified_flat_close_never_uses_live_mark_as_missing_fill(self):
        state = self.write_state(
            pending_close_order_id="close-without-fill",
            pending_close_client_order_id="close-client",
        )
        with patch.object(tp_monitor, "_resolve_external_close_order", return_value=(
            {}, False, "terminal history has not exposed the fill yet",
        )):
            closed = tp_monitor._finalize_confirmed_market_close(
                state, {"id": "close-without-fill", "state": "closed"},
                mark=9.0, lots=10, reason="take_profit",
            )

        self.assertTrue(closed)
        persisted = self.read_state()
        self.assertIsNone(persisted["exit_mark"])
        self.assertIsNone(persisted["gross_pnl_usd"])
        self.assertIsNone(persisted["pnl_usd"])
        self.assertTrue(persisted["history_pending"])

    def test_verified_flat_non_exact_close_order_never_prices_full_position(self):
        cases = (
            ("partial", 10, 4),
            ("oversized", 12, 0),
        )
        for label, size, unfilled in cases:
            with self.subTest(label=label):
                self.history_file.unlink(missing_ok=True)
                state = self.write_state(
                    pending_close_order_id=f"{label}-close",
                    pending_close_client_order_id=f"{label}-client",
                )
                non_exact = {
                    "id": f"{label}-close", "client_order_id": f"{label}-client",
                    "state": "closed", "size": size, "unfilled_size": unfilled,
                    "average_fill_price": "2.0", "commission": "0.12",
                }
                with patch.object(tp_monitor, "_resolve_external_close_order", return_value=(
                    {}, False, "an exact owned-lot close fill is not visible yet",
                )):
                    closed = tp_monitor._finalize_confirmed_market_close(
                        state, non_exact, mark=2.0, lots=10, reason="take_profit",
                    )

                self.assertTrue(closed)
                persisted = self.read_state()
                self.assertEqual(persisted["status"], "CLOSED")
                self.assertIsNone(persisted["exit_mark"])
                self.assertIsNone(persisted["gross_pnl_usd"])
                self.assertIsNone(persisted["pnl_usd"])
                self.assertTrue(persisted["history_pending"])

    def test_partial_terminal_protection_order_routes_to_strict_reconciliation(self):
        self.write_state(tp_stop_order_id="tp-partial", entry_fee_usd=0.10)
        partial = {
            "id": "tp-partial", "state": "closed", "size": 10,
            "unfilled_size": 4, "average_fill_price": "2.0",
            "commission": "0.12",
        }
        with patch.object(tp_monitor, "REMOVE_PROTECTION", False), \
             patch.object(tp_monitor, "install_signal_handlers"), \
             patch.object(tp_monitor, "get_exchange_size", return_value=0), \
             patch.object(tp_monitor, "get_order", return_value=partial), \
             patch.object(tp_monitor, "remove_exchange_protection", return_value=True), \
             patch.object(tp_monitor, "_resolve_external_close_order", return_value=(
                 {}, False, "an exact owned-lot close fill is not visible yet",
             )), \
             patch.object(tp_monitor, "send_telegram") as telegram:
            self.assertEqual(tp_monitor.main(), 0)

        telegram.assert_not_called()
        persisted = self.read_state()
        self.assertEqual(persisted["status"], "CLOSED")
        self.assertIsNone(persisted["exit_mark"])
        self.assertIsNone(persisted["gross_pnl_usd"])
        self.assertIsNone(persisted["pnl_usd"])
        self.assertTrue(persisted["history_pending"])

    def test_closed_pending_worker_retries_once_then_repairs_on_later_run(self):
        self.write_state(
            status="CLOSED", history_pending=True, history_logged=False,
            exit_trigger="closed_externally", exit_mark=None,
            gross_pnl_usd=None, pnl_usd=None, fees_usd=None,
            entry_fee_usd=0.10,
        )
        close_order = {
            "id": "external-close", "client_order_id": "external-client",
            "product_id": 101, "side": "sell", "state": "closed",
            "size": 10, "unfilled_size": 0,
            "average_fill_price": "0.5", "commission": "0.20",
            "reduce_only": True, "created_at": "2026-07-15T01:03:00Z",
        }
        with patch.object(tp_monitor, "REMOVE_PROTECTION", False), \
             patch.object(tp_monitor, "install_signal_handlers"), \
             patch.object(tp_monitor, "remove_exchange_protection", return_value=True), \
             patch.object(tp_monitor, "get_exchange_size", return_value=0) as get_size, \
             patch.object(tp_monitor, "place_stop_order") as place_stop, \
             patch.object(tp_monitor, "_resolve_external_close_order", side_effect=[
                 ({}, False, "order history temporarily unavailable"),
                 (close_order, True, ""),
             ]) as resolve:
            self.assertEqual(tp_monitor.main(), 0)
            first = self.read_state()
            self.assertTrue(first["history_pending"])
            self.assertIsNone(first["pnl_usd"])

            self.assertEqual(tp_monitor.main(), 0)

        self.assertEqual(resolve.call_count, 2)
        self.assertEqual(get_size.call_count, 2)
        place_stop.assert_not_called()
        final = self.read_state()
        self.assertFalse(final["history_pending"])
        self.assertEqual(final["exit_order_id"], "external-close")
        self.assertEqual(final["gross_pnl_usd"], -5.0)
        self.assertEqual(final["pnl_usd"], -5.3)
        history = json.loads(self.history_file.read_text(encoding="utf-8"))
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["accounting_status"], "complete")
        self.assertEqual(history[0]["pnl_usd"], -5.3)

    def test_closed_worker_never_cancels_without_verified_zero_exposure(self):
        for live_size in (None, 4):
            with self.subTest(live_size=live_size):
                self.write_state(
                    status="CLOSED", history_pending=True,
                    exit_trigger="closed_externally", tp_stop_order_id="tp-keep",
                )
                self.health_file.unlink(missing_ok=True)
                with patch.object(tp_monitor, "REMOVE_PROTECTION", False), \
                     patch.object(tp_monitor, "install_signal_handlers"), \
                     patch.object(tp_monitor, "get_exchange_size", return_value=live_size), \
                     patch.object(tp_monitor, "remove_exchange_protection") as remove, \
                     patch.object(tp_monitor, "_finalize_external_flat_close") as reconcile, \
                     patch.object(tp_monitor, "place_stop_order") as place_stop:
                    self.assertEqual(tp_monitor.main(), 0)

                remove.assert_not_called()
                reconcile.assert_not_called()
                place_stop.assert_not_called()
                self.assertEqual(self.read_state()["tp_stop_order_id"], "tp-keep")
                health = json.loads(self.health_file.read_text(encoding="utf-8"))
                self.assertEqual(health["status"], "degraded")
                self.assertEqual(health["exchange_position_size"], live_size)
                self.assertTrue(health["protection_established"])

    def test_complete_accounting_repairs_incomplete_history_duplicate(self):
        self.write_state(
            status="CLOSED", exit_mark=0.5, gross_pnl_usd=-5.0, pnl_usd=-5.3,
            entry_fee_usd=0.1, exit_fee_usd=0.2, fees_usd=0.3,
            fees_available=True, fees_complete=True, pnl_includes_fees=True,
            exit_order_id="external-close", exit_time_utc="01:03:00",
            exit_trigger="closed_externally", closed_lots=10,
        )
        self.history_file.write_text(json.dumps([{
            "symbol": "C-BTC-TEST", "entry_date": "2026-07-15",
            "entry_time_utc": "01:02:03", "exit_mark": 0,
            "gross_pnl_usd": 0, "pnl_usd": 0, "fees_usd": None,
            "pnl_includes_fees": False, "accounting_status": "pending",
        }]), encoding="utf-8")

        self.assertTrue(tp_monitor.append_history(self.read_state()))

        history = json.loads(self.history_file.read_text(encoding="utf-8"))
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["exit_order_id"], "external-close")
        self.assertEqual(history[0]["exit_mark"], 0.5)
        self.assertEqual(history[0]["gross_pnl_usd"], -5.0)
        self.assertEqual(history[0]["pnl_usd"], -5.3)
        self.assertEqual(history[0]["accounting_status"], "complete")
        self.assertFalse(self.read_state()["history_pending"])

    def test_complete_history_hydrates_state_without_a_new_order_lookup(self):
        self.write_state(
            status="CLOSED", history_pending=True, history_logged=False,
            exit_trigger="closed_externally", exit_mark=None, pnl_usd=None,
        )
        self.history_file.write_text(json.dumps([{
            "slot": "evening", "symbol": "C-BTC-TEST",
            "entry_date": "2026-07-15", "entry_time_utc": "01:02:03",
            "exit_date": "2026-07-15", "exit_time_utc": "01:03:00",
            "exit_mark": 0.5, "gross_pnl_usd": -5.0, "pnl_usd": -5.3,
            "entry_fee_usd": 0.1, "exit_fee_usd": 0.2, "fees_usd": 0.3,
            "pnl_includes_fees": True, "accounting_status": "complete",
            "exit_order_id": "history-close",
            "exit_reconciliation_status": "resolved_order_history",
        }]), encoding="utf-8")

        with patch.object(tp_monitor, "_resolve_external_close_order") as resolve:
            self.assertTrue(tp_monitor._finalize_external_flat_close(self.read_state()))

        resolve.assert_not_called()
        hydrated = self.read_state()
        self.assertFalse(hydrated["history_pending"])
        self.assertTrue(hydrated["history_logged"])
        self.assertEqual(hydrated["exit_order_id"], "history-close")
        self.assertEqual(hydrated["pnl_usd"], -5.3)

    def test_legacy_non_fee_and_dry_run_history_do_not_remain_pending(self):
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run):
                self.history_file.unlink(missing_ok=True)
                self.write_state(
                    status="CLOSED", exit_mark=1.25, pnl_usd=2.5,
                    exit_time_utc="01:03:00", exit_trigger="legacy_close",
                    dry_run=dry_run,
                )
                self.assertTrue(tp_monitor.append_history(self.read_state()))
                state = self.read_state()
                self.assertFalse(state["history_pending"])
                self.assertTrue(state["history_logged"])
                row = json.loads(self.history_file.read_text(encoding="utf-8"))[0]
                self.assertEqual(row["accounting_status"], "complete")

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
            "state": "closed", "size": 10, "unfilled_size": 0,
            "average_fill_price": "2.0", "commission": "0.20",
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
