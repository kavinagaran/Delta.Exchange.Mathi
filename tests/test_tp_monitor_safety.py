import json
import signal
import tempfile
import unittest
from contextlib import nullcontext
from datetime import datetime, timezone
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
            patch.object(tp_monitor, "USER_DIR", root),
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

    @staticmethod
    def continuity(size=6, entry=1.5, *, entries=6, exits=0, fill_ids=None):
        return {
            "verified": True, "status": "continuous", "signed_size": size,
            "position_cycle_id": "trend-cycle-test", "entry_mark": entry,
            "cycle_entry_lots_total": entries, "cycle_exit_lots_total": exits,
            "partial_exit_gross_pnl_usd": 0.0,
            "partial_exit_fees_usd": 0.0, "added_entry_fees_usd": 0.03,
            "fill_fees_complete": True, "fill_ids": fill_ids or ["fill-add"],
            "last_fill_id": (fill_ids or ["fill-add"])[-1],
            "verified_at_utc": "2026-07-17T00:00:00+00:00",
        }

    @staticmethod
    def protection_order(order_id, lots, kind):
        return {
            "id": order_id, "product_id": 101, "state": "pending",
            "size": lots, "unfilled_size": lots, "side": "sell",
            "reduce_only": True, "order_type": "market_order",
            "stop_order_type": ("take_profit_order" if kind == "tp"
                                else "stop_loss_order"),
            "stop_trigger_method": "mark_price",
            "stop_price": "18.2" if kind == "tp" else "0.1",
        }

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

    def test_realtime_position_endpoint_signs_query_and_distinguishes_failure(self):
        response = Mock()
        response.json.return_value = {
            "success": True,
            "result": {"product_id": 101, "size": "6", "entry_price": "1.25"},
        }
        with patch.object(tp_monitor, "_sign", return_value={}) as sign, \
             patch.object(tp_monitor.requests, "get", return_value=response) as get:
            position = tp_monitor.get_exchange_position(101)

        self.assertEqual(position["product_id"], 101)
        self.assertEqual(position["size"], "6")
        sign.assert_called_once_with("GET", "/v2/positions", "?product_id=101")
        self.assertEqual(get.call_args.kwargs["params"], {"product_id": 101})

        flat = Mock()
        flat.json.return_value = {
            "success": True,
            "result": {"product_id": 101, "size": 0, "entry_price": "0"},
        }
        malformed = Mock()
        malformed.json.return_value = {"success": True, "result": None}
        with patch.object(tp_monitor, "_sign", return_value={}), \
             patch.object(tp_monitor.requests, "get", side_effect=[flat, malformed]):
            self.assertEqual(tp_monitor.get_exchange_size(101), 0)
            self.assertIsNone(tp_monitor.get_exchange_size(101))

    def test_trend_adopts_matching_growth_and_resizes_full_exchange_protection(self):
        self.write_state(
            lots=3, owned_entry_lots=3, original_owned_entry_lots=3,
            entry_mark=1.0, entry_fees_usd=0.10,
            tp_stop_order_id="tp-old", tp_lots=3,
            tsl_stop_order_id="sl-old", stop_lots=3, stop_kind="tsl",
            tsl_peak=90.0, tsl_armed=True, tsl_floor=40.0,
            protection_config={
                "tp_target_pnl": 100, "sl_target_pnl": 50,
                "tsl_arm_pnl": 0, "tsl_trail_pnl": 0, "poll_secs": 10,
            },
        )
        active = lambda order_id: self.protection_order(
            order_id, 6, "tp" if str(order_id).startswith("tp") else "stop",
        )
        with patch.object(tp_monitor, "SLOT", "trend"), \
             patch.object(tp_monitor, "REMOVE_PROTECTION", False), \
             patch.object(tp_monitor, "install_signal_handlers"), \
             patch.object(tp_monitor, "get_exchange_size", return_value=6), \
             patch.object(tp_monitor, "get_exchange_position", return_value={
                 "product_id": 101, "size": 6, "entry_price": "1.5",
             }), \
             patch.object(tp_monitor, "_trend_cycle_continuity",
                          return_value=self.continuity()), \
             patch.object(tp_monitor, "get_order", side_effect=lambda oid: active(oid)), \
             patch.object(tp_monitor, "get_mark", return_value=1.4), \
             patch.object(tp_monitor, "edit_stop_price",
                          return_value={"success": True}) as edit, \
             patch.object(tp_monitor, "place_stop_order") as place, \
             patch.object(tp_monitor, "cancel_order", return_value={"success": True}) as cancel, \
             patch.object(tp_monitor, "audit_event") as audit, \
             patch.object(tp_monitor, "send_telegram") as telegram, \
             patch.object(tp_monitor.time, "sleep", side_effect=_StopLoop):
            with self.assertRaises(_StopLoop):
                tp_monitor.main()

        state = self.read_state()
        self.assertEqual(state["lots"], 6)
        self.assertEqual(state["protection_lots"], 6)
        self.assertEqual(state["owned_entry_lots"], 3)
        self.assertEqual(state["original_owned_entry_lots"], 3)
        self.assertEqual(state["externally_added_lots_adopted"], 3)
        self.assertEqual(state["entry_mark"], 1.5)
        self.assertEqual(state["entry_mark_source"], "exchange_realtime_aggregate")
        self.assertEqual(state["tp_stop_order_id"], "tp-old")
        self.assertEqual(state["tsl_stop_order_id"], "sl-old")
        self.assertEqual(state["tp_lots"], 6)
        self.assertEqual(state["stop_lots"], 6)
        self.assertEqual(state["tsl_peak"], 0.0)
        self.assertFalse(state["tsl_armed"])
        self.assertEqual(state["stop_kind"], "sl")
        self.assertEqual(state["tsl_rebase_reason"], "external_lot_adoption")
        self.assertEqual({call.args[0] for call in edit.call_args_list}, {"sl-old", "tp-old"})
        self.assertTrue(all(call.kwargs["size"] == 6 for call in edit.call_args_list))
        place.assert_not_called()
        cancel.assert_not_called()
        audit.assert_called_once()
        self.assertTrue(any("EXTERNAL LOTS PROTECTED" in call.args[0]
                            for call in telegram.call_args_list))
        health = json.loads(self.health_file.read_text(encoding="utf-8"))
        self.assertEqual(health["status"], "healthy")
        self.assertEqual(health["exchange_position_size"], 6)
        self.assertEqual(health["exchange_protected_lots"], 6)
        self.assertEqual(health["unprotected_same_product_lots"], 0)
        self.assertEqual(health["protection_revision"], 1)
        self.assertTrue(health["continuity_verified"])

    def test_live_score_position_never_adopts_same_product_growth(self):
        self.write_state(
            lots=3,
            owned_entry_lots=3,
            original_owned_entry_lots=3,
            ownership="trend_score_auto_live",
            entry_trigger="trend_engine_score_zone_auto",
            position_cycle_id="trend-cycle-test",
            entry_mark=1.0,
            entry_fees_usd=0.10,
            protection_config={
                "tp_target_pnl": 100,
                "sl_target_pnl": 50,
                "tsl_arm_pnl": 25,
                "tsl_trail_pnl": 10,
                "poll_secs": 10,
            },
        )
        with patch.object(tp_monitor, "SLOT", "trend"), \
             patch.object(tp_monitor, "REMOVE_PROTECTION", False), \
             patch.object(tp_monitor, "install_signal_handlers"), \
             patch.object(tp_monitor, "get_exchange_size", return_value=6), \
             patch.object(tp_monitor, "get_exchange_position", return_value={
                 "product_id": 101, "size": 6, "entry_price": "1.5",
             }), \
             patch.object(
                 tp_monitor,
                 "_trend_cycle_continuity",
                 return_value=self.continuity(),
             ), \
             patch.object(tp_monitor, "get_order") as get_order, \
             patch.object(tp_monitor, "place_stop_order") as place, \
             patch.object(tp_monitor, "edit_stop_price") as edit, \
             patch.object(tp_monitor, "send_telegram") as telegram, \
             patch.object(tp_monitor.time, "sleep", side_effect=_StopLoop):
            with self.assertRaises(_StopLoop):
                tp_monitor.main()

        state = self.read_state()
        self.assertEqual(state["lots"], 3)
        self.assertEqual(state["owned_entry_lots"], 3)
        self.assertNotIn("externally_added_lots_adopted", state)
        get_order.assert_not_called()
        place.assert_not_called()
        edit.assert_not_called()
        self.assertTrue(any(
            "fixed-size LIVE score ownership" in call.args[0]
            for call in telegram.call_args_list
        ))
        health = json.loads(self.health_file.read_text(encoding="utf-8"))
        self.assertEqual(health["status"], "degraded")
        self.assertEqual(health["adoption_status"], "blocked_score_fixed_size")
        self.assertEqual(health["exchange_position_size"], 6)
        self.assertEqual(health["protected_lots"], 3)
        self.assertEqual(health["unprotected_same_product_lots"], 3)
        self.assertFalse(health["protection_established"])

    def test_failed_resize_keeps_old_orders_and_reports_partial_coverage(self):
        self.write_state(
            lots=3, owned_entry_lots=3, entry_mark=1.0,
            tp_stop_order_id="tp-old", tp_lots=3,
            tsl_stop_order_id="sl-old", stop_lots=3, stop_kind="sl",
            protection_config={
                "tp_target_pnl": 100, "sl_target_pnl": 50,
                "tsl_arm_pnl": 0, "tsl_trail_pnl": 0, "poll_secs": 10,
            },
        )
        failure = {"success": False, "error": {"code": "temporary_failure"}}
        with patch.object(tp_monitor, "SLOT", "trend"), \
             patch.object(tp_monitor, "REMOVE_PROTECTION", False), \
             patch.object(tp_monitor, "install_signal_handlers"), \
             patch.object(tp_monitor, "get_exchange_size", return_value=6), \
             patch.object(tp_monitor, "get_exchange_position", return_value={
                 "product_id": 101, "size": 6, "entry_price": "1.5",
             }), \
             patch.object(tp_monitor, "_trend_cycle_continuity",
                          return_value=self.continuity()), \
             patch.object(tp_monitor, "get_order", side_effect=lambda oid:
                          self.protection_order(
                              oid, 3, "tp" if str(oid).startswith("tp") else "stop")), \
             patch.object(tp_monitor, "get_mark", return_value=1.4), \
             patch.object(tp_monitor, "edit_stop_price", return_value=failure), \
             patch.object(tp_monitor, "place_stop_order", return_value=failure), \
             patch.object(tp_monitor, "cancel_order") as cancel, \
             patch.object(tp_monitor, "audit_event"), \
             patch.object(tp_monitor, "send_telegram"), \
             patch.object(tp_monitor.time, "sleep", side_effect=_StopLoop):
            with self.assertRaises(_StopLoop):
                tp_monitor.main()

        state = self.read_state()
        self.assertEqual(state["lots"], 6)
        self.assertEqual(state["tp_stop_order_id"], "tp-old")
        self.assertEqual(state["tsl_stop_order_id"], "sl-old")
        cancel.assert_not_called()
        health = json.loads(self.health_file.read_text(encoding="utf-8"))
        self.assertEqual(health["status"], "degraded")
        self.assertEqual(health["exchange_protected_lots"], 0)
        self.assertEqual(health["unprotected_same_product_lots"], 6)
        self.assertTrue(health["local_fallback_active"])
        self.assertTrue(health["local_tp_fallback_active"])
        self.assertTrue(health["local_stop_fallback_active"])

    def test_undersized_order_ids_do_not_disable_local_full_position_exit(self):
        self.write_state(
            lots=3, owned_entry_lots=3, original_owned_entry_lots=3,
            entry_mark=1.0, tp_stop_order_id="tp-old", tp_lots=3,
            tsl_stop_order_id="sl-old", stop_lots=3, stop_kind="sl",
            protection_config={
                "tp_target_pnl": 1, "sl_target_pnl": 50,
                "tsl_arm_pnl": 0, "tsl_trail_pnl": 0, "poll_secs": 10,
            },
        )
        failure = {"success": False, "error": {"code": "temporary_failure"}}
        with patch.object(tp_monitor, "SLOT", "trend"), \
             patch.object(tp_monitor, "REMOVE_PROTECTION", False), \
             patch.object(tp_monitor, "install_signal_handlers"), \
             patch.object(tp_monitor, "get_exchange_size", return_value=6), \
             patch.object(tp_monitor, "get_exchange_position", return_value={
                 "product_id": 101, "size": 6, "entry_price": "1.5",
             }), \
             patch.object(tp_monitor, "_trend_cycle_continuity",
                          return_value=self.continuity()), \
             patch.object(tp_monitor, "get_order", side_effect=lambda oid:
                          self.protection_order(
                              oid, 3, "tp" if str(oid).startswith("tp") else "stop")), \
             patch.object(tp_monitor, "get_mark", return_value=2.0), \
             patch.object(tp_monitor, "edit_stop_price", return_value=failure), \
             patch.object(tp_monitor, "close_position", return_value=False) as close, \
             patch.object(tp_monitor, "audit_event"), \
             patch.object(tp_monitor, "send_telegram"), \
             patch.object(tp_monitor.time, "sleep", side_effect=_StopLoop):
            with self.assertRaises(_StopLoop):
                tp_monitor.main()
        close.assert_called_once()
        self.assertEqual(close.call_args.args[3], "take_profit")
        self.assertEqual(close.call_args.args[0]["lots"], 6)

    def test_strict_exchange_order_proof_rejects_every_unsafe_mismatch(self):
        valid = self.protection_order("tp-1", 6, "tp")
        proof = tp_monitor._protection_order_proof(
            valid, order_id="tp-1", product_id=101, close_side="sell",
            kind="tp", expected_lots=6, expected_stop_price=18.2,
        )
        self.assertTrue(proof["ok"])
        cases = {
            "wrong-id": {"id": "other"},
            "wrong-product": {"product_id": 999},
            "wrong-side": {"side": "buy"},
            "not-reduce-only": {"reduce_only": False},
            "wrong-kind": {"stop_order_type": "stop_loss_order"},
            "wrong-trigger": {"stop_trigger_method": "last_traded_price"},
            "terminal": {"state": "closed"},
            "undersized": {"size": 3, "unfilled_size": 3},
            "part-filled": {"unfilled_size": 5},
            "wrong-price": {"stop_price": "19.0"},
        }
        for label, change in cases.items():
            with self.subTest(label=label):
                rejected = tp_monitor._protection_order_proof(
                    {**valid, **change}, order_id="tp-1", product_id=101,
                    close_side="sell", kind="tp", expected_lots=6,
                    expected_stop_price=18.2,
                )
                self.assertFalse(rejected["ok"])
                self.assertEqual(rejected["covered_lots"], 0)

    def test_missing_trigger_method_requires_durable_mark_price_attestation(self):
        order = self.protection_order("tp-1", 6, "tp")
        order["client_order_id"] = "trend-tp-attested"
        order.pop("stop_trigger_method")

        unverified = tp_monitor._protection_order_proof(
            order, order_id="tp-1", client_order_id="trend-tp-attested",
            product_id=101, close_side="sell", kind="tp",
            expected_lots=6, expected_stop_price=18.2,
        )
        self.assertFalse(unverified["ok"])
        self.assertIn("not durably attested", unverified["reason"])

        attested = tp_monitor._protection_order_proof(
            order, order_id="tp-1", client_order_id="trend-tp-attested",
            product_id=101, close_side="sell", kind="tp",
            expected_lots=6, expected_stop_price=18.2,
            mark_trigger_attested=True,
        )
        self.assertTrue(attested["ok"])

        explicit_conflict = tp_monitor._protection_order_proof(
            {**order, "stop_trigger_method": "last_traded_price"},
            order_id="tp-1", client_order_id="trend-tp-attested",
            product_id=101, close_side="sell", kind="tp",
            expected_lots=6, expected_stop_price=18.2,
            mark_trigger_attested=True,
        )
        self.assertFalse(explicit_conflict["ok"])
        self.assertIn("not mark price", explicit_conflict["reason"])

    def test_legacy_unattested_tp_is_cancelled_then_replaced_with_attestation(self):
        self.write_state(
            lots=3, owned_entry_lots=3, original_owned_entry_lots=3,
            original_bot_entry_mark=1.0,
            tp_stop_order_id="tp-legacy", tp_lots=3,
            tp_client_order_id="nithi-tp-math-t-tp-legacy",
            protection_config={
                "tp_target_pnl": 51.6, "sl_target_pnl": 0,
                "tsl_arm_pnl": 0, "tsl_trail_pnl": 0, "poll_secs": 10,
            },
        )
        legacy = {
            **self.protection_order("tp-legacy", 3, "tp"),
            "client_order_id": "nithi-tp-math-t-tp-legacy",
        }
        legacy.pop("stop_trigger_method")
        replacement = {}

        def get_order(order_id):
            return replacement if order_id == "tp-new" else legacy

        def place(_product_id, _side, _lots, _price, _kind, **kwargs):
            replacement.update({
                **self.protection_order("tp-new", 3, "tp"),
                "client_order_id": kwargs["client_order_id"],
            })
            return {"success": True, "result": dict(replacement)}

        continuity = {
            **self.continuity(size=3, entry=1.0, entries=3, exits=0),
            "fill_ids": [], "last_fill_id": None,
        }
        with patch.object(tp_monitor, "SLOT", "trend"), \
             patch.object(tp_monitor, "REMOVE_PROTECTION", False), \
             patch.object(tp_monitor, "install_signal_handlers"), \
             patch.object(tp_monitor, "get_exchange_size", return_value=3), \
             patch.object(tp_monitor, "get_exchange_position", return_value={
                 "product_id": 101, "size": 3, "entry_price": "1.0",
             }), \
             patch.object(tp_monitor, "_trend_cycle_continuity", return_value=continuity), \
             patch.object(tp_monitor, "get_order", side_effect=get_order), \
             patch.object(tp_monitor, "cancel_order", return_value={"success": True}) as cancel, \
             patch.object(tp_monitor, "place_stop_order", side_effect=place) as submit, \
             patch.object(tp_monitor, "edit_stop_price") as edit, \
             patch.object(tp_monitor, "get_mark", return_value=1.1), \
             patch.object(tp_monitor, "send_telegram"), \
             patch.object(tp_monitor.time, "sleep", side_effect=_StopLoop):
            with self.assertRaises(_StopLoop):
                tp_monitor.main()

        cancel.assert_called_once_with("tp-legacy", 101)
        submit.assert_called_once()
        edit.assert_not_called()
        state = self.read_state()
        self.assertEqual(state["tp_stop_order_id"], "tp-new")
        self.assertEqual(state["last_tp_stop_order_id"], "tp-legacy")
        self.assertEqual(state["tp_trigger_method"], "mark_price")
        self.assertEqual(state["tp_lots"], 3)

    def test_legacy_unattested_tp_is_retained_when_cancel_is_unverified(self):
        self.write_state(
            lots=3, owned_entry_lots=3, original_owned_entry_lots=3,
            original_bot_entry_mark=1.0,
            tp_stop_order_id="tp-legacy", tp_lots=3,
            tp_client_order_id="nithi-tp-math-t-tp-legacy",
            protection_config={
                "tp_target_pnl": 51.6, "sl_target_pnl": 0,
                "tsl_arm_pnl": 0, "tsl_trail_pnl": 0, "poll_secs": 10,
            },
        )
        legacy = {
            **self.protection_order("tp-legacy", 3, "tp"),
            "client_order_id": "nithi-tp-math-t-tp-legacy",
        }
        legacy.pop("stop_trigger_method")
        continuity = {
            **self.continuity(size=3, entry=1.0, entries=3, exits=0),
            "fill_ids": [], "last_fill_id": None,
        }
        with patch.object(tp_monitor, "SLOT", "trend"), \
             patch.object(tp_monitor, "REMOVE_PROTECTION", False), \
             patch.object(tp_monitor, "install_signal_handlers"), \
             patch.object(tp_monitor, "get_exchange_size", return_value=3), \
             patch.object(tp_monitor, "get_exchange_position", return_value={
                 "product_id": 101, "size": 3, "entry_price": "1.0",
             }), \
             patch.object(tp_monitor, "_trend_cycle_continuity", return_value=continuity), \
             patch.object(tp_monitor, "get_order", return_value=legacy), \
             patch.object(tp_monitor, "cancel_order", return_value={"success": False}) as cancel, \
             patch.object(tp_monitor, "place_stop_order") as submit, \
             patch.object(tp_monitor, "edit_stop_price") as edit, \
             patch.object(tp_monitor, "get_mark", return_value=1.1), \
             patch.object(tp_monitor, "send_telegram"), \
             patch.object(tp_monitor.time, "sleep", side_effect=_StopLoop):
            with self.assertRaises(_StopLoop):
                tp_monitor.main()

        cancel.assert_called_once_with("tp-legacy", 101)
        submit.assert_not_called()
        edit.assert_not_called()
        state = self.read_state()
        self.assertEqual(state["tp_stop_order_id"], "tp-legacy")
        health = json.loads(self.health_file.read_text(encoding="utf-8"))
        self.assertTrue(health["local_tp_fallback_active"])
        self.assertFalse(health["exchange_protection_complete"])

    def test_fill_ledger_proves_addition_and_partial_reduction(self):
        state = self.write_state(
            lots=3, owned_entry_lots=3, original_owned_entry_lots=3,
            entry_mark=1.0, original_bot_entry_mark=1.0,
            entry_fees_usd=0.10, original_bot_entry_fee_usd=0.10,
            order_id="entry-order", order_ids=["entry-order"],
        )
        fills = [
            {"id": "add-1", "product_id": 101, "size": 3, "side": "buy",
             "price": "2.0", "commission": "0.03",
             "created_at": "2026-07-15T01:03:00Z", "order_id": "manual-add"},
            {"id": "reduce-1", "product_id": 101, "size": 2, "side": "sell",
             "price": "2.0", "commission": "0.02",
             "created_at": "2026-07-15T01:04:00Z", "order_id": "manual-reduce"},
        ]
        with patch.object(tp_monitor, "SLOT", "trend"):
            ledger = tp_monitor._trend_cycle_continuity(
                state, {"product_id": 101, "size": 4, "entry_price": "1.5"}, fills,
            )
        self.assertTrue(ledger["verified"])
        self.assertEqual(ledger["signed_size"], 4)
        self.assertEqual(ledger["cycle_entry_lots_total"], 6)
        self.assertEqual(ledger["cycle_exit_lots_total"], 2)
        self.assertAlmostEqual(ledger["partial_exit_gross_pnl_usd"], 1.0)
        self.assertEqual(ledger["partial_exit_fees_usd"], 0.02)

    def test_fill_ledger_detects_close_then_reopen_at_same_or_smaller_size(self):
        state = self.write_state(
            lots=3, owned_entry_lots=3, original_owned_entry_lots=3,
            entry_mark=1.0, original_bot_entry_mark=1.0,
            order_id="entry-order", order_ids=["entry-order"],
        )
        for reopened in (3, 2):
            fills = [
                {"id": f"flat-{reopened}", "product_id": 101, "size": 3,
                 "side": "sell", "price": "1.2", "commission": "0.01",
                 "created_at": "2026-07-15T01:03:00Z", "order_id": "manual-flat"},
                {"id": f"reopen-{reopened}", "product_id": 101, "size": reopened,
                 "side": "buy", "price": "1.0", "commission": "0.01",
                 "created_at": "2026-07-15T01:04:00Z", "order_id": "manual-reopen"},
            ]
            with self.subTest(reopened=reopened), \
                 patch.object(tp_monitor, "SLOT", "trend"):
                result = tp_monitor._trend_cycle_continuity(
                    state, {"product_id": 101, "size": reopened,
                            "entry_price": "1.0"}, fills,
                )
                self.assertFalse(result["verified"])
                self.assertEqual(result["status"], "broken_reopened")

    def test_proven_partial_reduction_rebases_and_invalidates_old_health(self):
        state = self.write_state(
            lots=6, protection_lots=6, owned_entry_lots=3,
            original_owned_entry_lots=3, entry_mark=1.5,
            original_bot_entry_mark=1.0, protection_revision=1,
            continuity_revision=1, protection_scope="trend_plus_same_product_external",
            externally_added_lots_adopted=3, original_bot_entry_fee_usd=0.10,
        )
        continuity = self.continuity(
            size=4, entry=1.5, entries=6, exits=2,
            fill_ids=["fill-add", "fill-reduce"],
        )
        continuity.update(partial_exit_gross_pnl_usd=1.0,
                          partial_exit_fees_usd=0.02)
        with patch.object(tp_monitor, "SLOT", "trend"), \
             patch.object(tp_monitor, "audit_event"):
            updated = tp_monitor._rebase_matching_trend_reduction(
                state, {"product_id": 101, "size": 4, "entry_price": "1.5"},
                6, continuity,
            )
        self.assertEqual(updated["lots"], 4)
        self.assertEqual(updated["protection_lots"], 4)
        self.assertEqual(updated["protection_revision"], 2)
        self.assertEqual(updated["cycle_exit_lots_total"], 2)
        self.assertEqual(updated["partial_exit_gross_pnl_usd"], 1.0)
        self.assertEqual(updated["lot_attribution_status"], "fungible_after_reduction")

    def test_adoption_lock_contention_makes_no_state_or_exchange_change(self):
        state = self.write_state(
            lots=3, owned_entry_lots=3, original_owned_entry_lots=3,
            entry_mark=1.0,
        )
        before = self.read_state()
        with patch.object(tp_monitor, "SLOT", "trend"), \
             patch.object(tp_monitor, "account_file_lock",
                          return_value=nullcontext(False)), \
             patch.object(tp_monitor, "audit_event") as audit:
            with self.assertRaisesRegex(RuntimeError, "lock is unavailable"):
                tp_monitor._adopt_matching_external_trend_lots(
                    state, {"product_id": 101, "size": 6, "entry_price": "1.5"},
                    3, self.continuity(),
                )
        self.assertEqual(self.read_state(), before)
        audit.assert_not_called()

    def test_fill_history_paginates_with_a_signed_exact_cursor(self):
        state = self.write_state(
            lots=3, owned_entry_lots=3, original_owned_entry_lots=3,
            entry_mark=1.0, order_id="entry-order",
        )
        first = Mock()
        first.json.return_value = {
            "success": True,
            "result": [{"id": "1"}],
            "meta": {"after": "cursor-1"},
        }
        second = Mock()
        second.json.return_value = {
            "success": True,
            "result": [{"id": "2"}],
            "meta": {"after": None},
        }
        with patch.object(tp_monitor.requests, "get", side_effect=[first, second]) as get, \
             patch.object(tp_monitor, "_sign", return_value={}) as sign:
            rows = tp_monitor.get_trend_cycle_fills(state)
        self.assertEqual([row["id"] for row in rows], ["1", "2"])
        self.assertNotIn("after", get.call_args_list[0].kwargs["params"])
        self.assertEqual(get.call_args_list[1].kwargs["params"]["after"], "cursor-1")
        self.assertIn("after=cursor-1", sign.call_args_list[1].args[2])

    def test_growth_rejects_reversal_pending_close_and_missing_entry_basis(self):
        base = self.write_state(
            lots=3, owned_entry_lots=3, entry_mark=1.0,
        )
        cases = [
            ("reversal", {"product_id": 101, "size": -6, "entry_price": "1.5"}, {}),
            ("pending-close", {"product_id": 101, "size": 6, "entry_price": "1.5"},
             {"pending_close_client_order_id": "close-1"}),
            ("missing-entry", {"product_id": 101, "size": 6, "entry_price": "0"}, {}),
        ]
        for label, position, extra in cases:
            with self.subTest(label=label):
                self.state_file.write_text(json.dumps({**base, **extra}), encoding="utf-8")
                before = self.read_state()
                with patch.object(tp_monitor, "SLOT", "trend"), \
                     patch.object(tp_monitor, "audit_event"):
                    with self.assertRaises(RuntimeError):
                        tp_monitor._adopt_matching_external_trend_lots(
                            before, position, previous_lots=3,
                        )
                self.assertEqual(self.read_state(), before)

    def test_direction_reversal_never_repurposes_existing_protection(self):
        self.write_state(
            lots=3, owned_entry_lots=3,
            tp_stop_order_id="tp-old", tp_lots=3,
        )
        with patch.object(tp_monitor, "SLOT", "trend"), \
             patch.object(tp_monitor, "REMOVE_PROTECTION", False), \
             patch.object(tp_monitor, "install_signal_handlers"), \
             patch.object(tp_monitor, "get_exchange_size", return_value=-3), \
             patch.object(tp_monitor, "get_exchange_position", return_value={
                 "product_id": 101, "size": -3, "entry_price": "1.0",
             }) as position, \
             patch.object(tp_monitor, "place_stop_order") as place, \
             patch.object(tp_monitor, "send_telegram"), \
             patch.object(tp_monitor.time, "sleep", side_effect=_StopLoop):
            with self.assertRaises(_StopLoop):
                tp_monitor.main()
        position.assert_called_once_with(101)
        place.assert_not_called()
        self.assertEqual(self.read_state()["tp_stop_order_id"], "tp-old")
        health = json.loads(self.health_file.read_text(encoding="utf-8"))
        self.assertEqual(health["adoption_status"], "blocked_direction_mismatch")

    def test_adopted_history_uses_aggregate_lots_and_preserves_bot_baseline(self):
        self.write_state(
            status="CLOSED", lots=6, protection_lots=6, max_protected_lots=6,
            owned_entry_lots=3, original_owned_entry_lots=3,
            externally_added_lots_adopted=3,
            protection_scope="trend_plus_same_product_external",
            exit_mark=2.0, gross_pnl_usd=3.0, pnl_usd=2.8,
            exit_time_utc="01:10:00", closed_lots=6,
        )
        self.assertTrue(tp_monitor.append_history(self.read_state()))
        row = json.loads(self.history_file.read_text(encoding="utf-8"))[0]
        self.assertEqual(row["lots"], 6)
        self.assertEqual(row["exit_lots"], 6)
        self.assertEqual(row["bot_entry_lots"], 3)
        self.assertEqual(row["externally_added_lots_adopted"], 3)
        self.assertEqual(tp_monitor._owned_close_lots(self.read_state()), 6)

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

    def test_cleanup_resolves_and_cancels_journalled_protection_identity(self):
        intent = {
            "client_order_id": "pending-tp-client",
            "product_id": 101,
            "side": "sell",
            "lots": 3,
            "stop_price": 18.2,
            "stop_order_type": "take_profit_order",
        }
        self.write_state(pending_tp_protection=intent)
        recovered = {
            "id": "pending-tp-order",
            "client_order_id": "pending-tp-client",
            "product_id": 101,
            "state": "pending",
        }
        with patch.object(
                tp_monitor, "get_order_by_client_id",
                return_value=(recovered, True)) as lookup, \
             patch.object(
                tp_monitor, "cancel_order",
                return_value={"success": True}) as cancel:
            ok = tp_monitor.remove_exchange_protection(
                self.read_state(), confirmed_closed=True,
                reason="unit-test flat cleanup",
            )

        self.assertTrue(ok)
        lookup.assert_called_once_with("pending-tp-client", 101)
        cancel.assert_called_once_with("pending-tp-order", 101)
        state = self.read_state()
        self.assertIsNone(state["pending_tp_protection"])
        self.assertEqual(
            state["last_pending_tp_protection_client_order_id"],
            "pending-tp-client",
        )
        self.assertEqual(
            state["last_pending_tp_protection_order_id"],
            "pending-tp-order",
        )

    def test_cleanup_retains_inconclusive_protection_journal(self):
        intent = {
            "client_order_id": "pending-stop-client",
            "product_id": 101,
            "side": "sell",
            "lots": 3,
            "stop_price": 0.1,
            "stop_order_type": "stop_loss_order",
        }
        self.write_state(pending_stop_protection=intent)
        with patch.object(
                tp_monitor, "get_order_by_client_id",
                return_value=({}, False)), \
             patch.object(tp_monitor, "cancel_order") as cancel:
            ok = tp_monitor.remove_exchange_protection(
                self.read_state(), explicit=True, reason="unit-test",
            )

        self.assertFalse(ok)
        cancel.assert_not_called()
        state = self.read_state()
        self.assertEqual(
            state["pending_stop_protection"]["client_order_id"],
            "pending-stop-client",
        )
        self.assertTrue(state["remove_protection_requested"])

    def test_restart_reconciles_and_reuses_active_persisted_order(self):
        self.write_state(tp_stop_order_id="tp-1", tp_lots=10)
        active_order = {
            "id": "tp-1", "product_id": 101, "state": "pending",
            "size": 10, "unfilled_size": 10, "side": "sell",
            "reduce_only": True, "order_type": "market_order",
            "stop_order_type": "take_profit_order",
            "stop_trigger_method": "mark_price", "stop_price": "11.0",
        }
        with patch.object(tp_monitor, "REMOVE_PROTECTION", False), \
             patch.object(tp_monitor, "install_signal_handlers"), \
             patch.object(tp_monitor, "get_exchange_size", return_value=10), \
             patch.object(tp_monitor, "get_exchange_position", return_value={
                 "product_id": 101, "size": 10, "entry_price": "1.0",
             }), \
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
             patch.object(tp_monitor, "get_exchange_position", return_value={
                 "product_id": 101, "size": 10, "entry_price": "1.0",
             }), \
             patch.object(tp_monitor, "get_mark", return_value=1.0), \
             patch.object(tp_monitor, "place_stop_order", return_value=unsupported), \
             patch.object(tp_monitor, "get_order_by_client_id",
                          return_value=({}, True)), \
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
        self.write_state(
            pending_close_order_id="unresolved-close",
            pending_close_client_order_id="unresolved-client",
            pending_close_submission_state="submission_unknown",
            pending_close_post_boundary=True,
            pending_close_attempts=1,
        )
        with patch.object(tp_monitor, "get_exchange_size", return_value=0), \
             patch.object(
                 tp_monitor, "_lookup_pending_close",
                 return_value=({}, False)), \
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
        self.assertEqual(state["exit_order_id"], "unresolved-close")
        self.assertEqual(state["exit_client_order_id"], "unresolved-client")
        self.assertIsNone(state["pending_close_submission_state"])
        self.assertIsNone(state["pending_close_client_order_id"])
        self.assertIsNone(state["pending_close_order_id"])
        self.assertFalse(state["pending_close_post_boundary"])
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

    def test_anchored_trend_terminal_protection_routes_to_complete_fill_ledger(self):
        self.write_state(
            lots=3, owned_entry_lots=3, original_owned_entry_lots=3,
            original_bot_entry_mark=1.0,
            continuity_anchor_utc="2026-07-15T01:02:03+00:00",
            tp_stop_order_id="tp-terminal",
        )
        terminal = {
            **self.protection_order("tp-terminal", 3, "tp"),
            "state": "closed", "unfilled_size": 0,
            "filled_size": 3, "average_fill_price": "2.0",
            "commission": "0.02",
        }
        with patch.object(tp_monitor, "SLOT", "trend"), \
             patch.object(tp_monitor, "REMOVE_PROTECTION", False), \
             patch.object(tp_monitor, "install_signal_handlers"), \
             patch.object(tp_monitor, "get_exchange_size", return_value=0), \
             patch.object(tp_monitor, "get_order", return_value=terminal), \
             patch.object(
                 tp_monitor, "_finalize_external_flat_close_locked",
                 return_value=True,
             ) as ledger, \
             patch.object(
                 tp_monitor, "remove_exchange_protection", return_value=True,
             ), \
             patch.object(tp_monitor, "send_telegram") as telegram:
            self.assertEqual(tp_monitor.main(), 0)

        ledger.assert_called_once()
        self.assertEqual(
            ledger.call_args.args[0]["exit_trigger"], "take_profit_trend",
        )
        telegram.assert_not_called()

    def test_complete_trend_fill_ledger_clears_consumed_close_journal(self):
        state = self.write_state(
            original_bot_entry_fee_usd=0.10,
            original_bot_entry_fee_source="exchange",
            continuity_anchor_utc="2026-07-15T01:02:03+00:00",
            pending_close_order_id="ledger-close",
            pending_close_client_order_id="ledger-close-client",
            pending_close_submission_state="acknowledged",
            pending_close_post_boundary=True,
            pending_close_attempts=1,
        )
        continuity = {
            "verified": True,
            "status": "closed",
            "verified_at_utc": "2026-07-15T01:03:00+00:00",
            "fill_ids": ["entry-fill", "exit-fill"],
            "last_fill_id": "exit-fill",
            "cycle_entry_lots_total": 10,
            "cycle_exit_lots_total": 10,
            "partial_exit_gross_pnl_usd": 2.0,
            "partial_exit_fees_usd": 0.20,
            "exit_mark": 1.20,
            "exit_order_ids": ["ledger-close"],
            "added_entry_fees_usd": 0.0,
            "fill_fees_complete": True,
        }
        with patch.object(tp_monitor, "SLOT", "trend"), \
             patch.object(
                 tp_monitor, "get_exchange_position",
                 return_value={"product_id": 101, "size": 0}), \
             patch.object(
                 tp_monitor, "_trend_cycle_continuity",
                 return_value=continuity):
            attempted, complete, error = (
                tp_monitor._finalize_trend_flat_fill_ledger(
                    state, datetime.now(timezone.utc),
                )
            )

        self.assertTrue(attempted)
        self.assertTrue(complete)
        self.assertEqual(error, "")
        closed = self.read_state()
        self.assertEqual(closed["status"], "CLOSED")
        self.assertIsNone(closed["pending_close_submission_state"])
        self.assertIsNone(closed["pending_close_client_order_id"])
        self.assertIsNone(closed["pending_close_order_id"])
        self.assertFalse(closed["pending_close_post_boundary"])

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

    def test_closed_manual_trend_pending_row_retries_fill_ledger(self):
        self.write_state(
            status="CLOSED",
            history_pending=True,
            history_logged=False,
            exit_trigger="manual_squareoff",
            exit_reconciliation_status="pending_fill_ledger",
            accounting_status="pending",
            partial_exit_accounting_status="fill_ledger_pending",
            continuity_anchor_utc="2026-07-15T01:02:03+00:00",
        )
        with patch.object(tp_monitor, "SLOT", "trend"), \
             patch.object(tp_monitor, "REMOVE_PROTECTION", False), \
             patch.object(tp_monitor, "install_signal_handlers"), \
             patch.object(tp_monitor, "get_exchange_size", return_value=0), \
             patch.object(
                 tp_monitor, "_finalize_external_flat_close", return_value=True,
             ) as reconcile, \
             patch.object(tp_monitor, "append_history") as append, \
             patch.object(
                 tp_monitor, "remove_exchange_protection", return_value=True,
             ):
            self.assertEqual(tp_monitor.main(), 0)

        reconcile.assert_called_once()
        append.assert_not_called()

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
            pending_close_order_id="stale-close",
            pending_close_client_order_id="stale-client",
            pending_close_submission_state="submission_unknown",
            pending_close_post_boundary=True,
            pending_close_attempts=1,
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
        self.assertIsNone(hydrated["pending_close_submission_state"])
        self.assertIsNone(hydrated["pending_close_client_order_id"])
        self.assertIsNone(hydrated["pending_close_order_id"])
        self.assertFalse(hydrated["pending_close_post_boundary"])

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
            self.assertEqual(
                persisted["pending_close_submission_state"], "submitting",
            )
            self.assertTrue(persisted["pending_close_post_boundary"])
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

    def test_conclusive_visibility_lag_after_response_loss_never_reposts(self):
        self.write_state(tp_stop_order_id="tp-1")
        with patch.object(tp_monitor, "get_exchange_size", side_effect=[10, 10]), \
             patch.object(
                 tp_monitor, "place_order",
                 side_effect=tp_monitor.requests.Timeout("response lost"),
             ) as first_place, \
             patch.object(
                 tp_monitor, "_lookup_pending_close", return_value=({}, True),
             ):
            first_closed = tp_monitor.close_position(
                self.read_state(), 2.0, 10.0,
            )

        self.assertFalse(first_closed)
        first_place.assert_called_once()
        pending = self.read_state()
        identity = pending["pending_close_client_order_id"]
        self.assertTrue(identity)
        self.assertEqual(
            pending["pending_close_submission_state"], "submission_unknown",
        )
        self.assertTrue(pending["pending_close_post_boundary"])
        self.assertEqual(
            pending["pending_close_state"], "post_boundary_unresolved",
        )

        with patch.object(tp_monitor, "get_exchange_size", return_value=10), \
             patch.object(
                 tp_monitor, "_lookup_pending_close", return_value=({}, True),
             ), \
             patch.object(tp_monitor, "place_order") as duplicate:
            second_closed = tp_monitor.close_position(
                self.read_state(), 2.0, 10.0,
            )

        self.assertFalse(second_closed)
        duplicate.assert_not_called()
        self.assertEqual(
            self.read_state()["pending_close_client_order_id"], identity,
        )

    def test_conclusive_not_found_submits_only_proven_pre_post_identity(self):
        self.write_state(
            pending_close_client_order_id="close-client-1",
            pending_close_reason="take_profit",
            pending_close_side="sell",
            pending_close_requested_lots=10,
            pending_close_start_size=10,
            pending_close_submission_state="prepared",
            pending_close_post_boundary=False,
            pending_close_state="intent_persisted",
            pending_close_attempts=0,
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
        self.assertEqual(state["pending_close_attempts"], 1)
        self.assertEqual(state["pending_close_created_utc"], "2026-07-15T01:00:00+00:00")
        self.assertEqual(state["pending_close_submission_state"], "acknowledged")
        self.assertTrue(state["pending_close_post_boundary"])

    def test_dashboard_post_boundary_close_is_never_reposted_during_visibility_lag(self):
        """A dashboard response-loss journal is authoritative in TP recovery."""
        self.write_state(
            pending_close_client_order_id="dashboard-close-1",
            pending_close_reason="trend_score_zone_change",
            pending_close_side="sell",
            pending_close_requested_lots=10,
            pending_close_start_size=10,
            pending_close_submission_state="submitting",
            pending_close_post_boundary=True,
            pending_close_started_at_utc="2026-07-15T01:00:00+00:00",
            pending_close_last_attempt_at_utc="2026-07-15T01:00:01+00:00",
        )
        with patch.object(tp_monitor, "get_exchange_size", return_value=10), \
             patch.object(tp_monitor, "_lookup_pending_close",
                          return_value=({}, True)) as lookup, \
             patch.object(tp_monitor, "place_order") as place:
            closed = tp_monitor.close_position(self.read_state(), 2.0, 10.0)

        self.assertFalse(closed)
        lookup.assert_called_once()
        place.assert_not_called()
        state = self.read_state()
        self.assertEqual(
            state["pending_close_client_order_id"], "dashboard-close-1",
        )
        self.assertEqual(state["pending_close_submission_state"], "submitting")
        self.assertTrue(state["pending_close_post_boundary"])
        self.assertEqual(state["pending_close_state"], "post_boundary_unresolved")

    def test_dashboard_acknowledged_close_is_never_reposted_when_lookup_lags(self):
        self.write_state(
            pending_close_client_order_id="dashboard-close-2",
            pending_close_order_id=88,
            pending_close_reason="trend_score_zone_change",
            pending_close_side="sell",
            pending_close_requested_lots=10,
            pending_close_start_size=10,
            pending_close_submission_state="acknowledged",
            pending_close_post_boundary=True,
            pending_close_started_at_utc="2026-07-15T01:00:00+00:00",
        )
        with patch.object(tp_monitor, "get_exchange_size", return_value=10), \
             patch.object(tp_monitor, "_lookup_pending_close",
                          return_value=({}, True)), \
             patch.object(tp_monitor, "place_order") as place:
            closed = tp_monitor.close_position(self.read_state(), 2.0, 10.0)

        self.assertFalse(closed)
        place.assert_not_called()
        self.assertEqual(
            self.read_state()["pending_close_client_order_id"],
            "dashboard-close-2",
        )

    def test_prepared_label_without_explicit_boundary_proof_is_not_submittable(self):
        self.write_state(
            pending_close_client_order_id="dashboard-close-ambiguous",
            pending_close_reason="trend_score_zone_change",
            pending_close_side="sell",
            pending_close_requested_lots=10,
            pending_close_start_size=10,
            pending_close_submission_state="prepared",
            pending_close_started_at_utc="2026-07-15T01:00:00+00:00",
        )
        with patch.object(tp_monitor, "get_exchange_size", return_value=10), \
             patch.object(tp_monitor, "_lookup_pending_close",
                          return_value=({}, True)), \
             patch.object(tp_monitor, "place_order") as place:
            closed = tp_monitor.close_position(self.read_state(), 2.0, 10.0)

        self.assertFalse(closed)
        place.assert_not_called()
        self.assertIn(
            "does not explicitly prove a pre-POST boundary",
            self.read_state()["pending_close_error"],
        )

    def test_main_reconciles_residual_pending_close_instead_of_gating_forever(self):
        self.write_state(
            lots=3, owned_entry_lots=3, original_owned_entry_lots=3,
            pending_close_client_order_id="trend-close-pending",
            pending_close_reason="manual_squareoff",
            pending_close_side="sell",
        )
        continuity = {
            **self.continuity(size=3, entry=1.0, entries=3, exits=0),
            "fill_ids": [], "last_fill_id": None,
        }
        with patch.object(tp_monitor, "SLOT", "trend"), \
             patch.object(tp_monitor, "REMOVE_PROTECTION", False), \
             patch.object(tp_monitor, "install_signal_handlers"), \
             patch.object(tp_monitor, "get_exchange_size", return_value=3), \
             patch.object(tp_monitor, "get_exchange_position", return_value={
                 "product_id": 101, "size": 3, "entry_price": "1.0",
             }), \
             patch.object(tp_monitor, "_trend_cycle_continuity", return_value=continuity), \
             patch.object(tp_monitor, "get_mark", return_value=1.1), \
             patch.object(tp_monitor, "_close_position_locked", return_value=False) as reconcile, \
             patch.object(tp_monitor, "edit_stop_price") as edit, \
             patch.object(tp_monitor, "place_stop_order") as place, \
             patch.object(tp_monitor.time, "sleep", side_effect=_StopLoop):
            with self.assertRaises(_StopLoop):
                tp_monitor.main()

        reconcile.assert_called_once()
        self.assertEqual(reconcile.call_args.args[0]["pending_close_client_order_id"],
                         "trend-close-pending")
        edit.assert_not_called()
        place.assert_not_called()
        health = json.loads(self.health_file.read_text(encoding="utf-8"))
        self.assertEqual(health["status"], "closing")

    def test_main_rebases_partial_pending_close_then_reconciles_residual(self):
        self.write_state(
            lots=6, protection_lots=6, owned_entry_lots=3,
            original_owned_entry_lots=3, entry_mark=1.5,
            original_bot_entry_mark=1.0,
            original_bot_entry_fee_usd=0.10,
            original_bot_entry_fee_source="exchange",
            pending_close_client_order_id="trend-close-partial",
            pending_close_reason="manual_squareoff", pending_close_side="sell",
        )
        continuity = self.continuity(
            size=3, entry=1.0, entries=6, exits=3,
            fill_ids=["add-1", "close-partial-1"],
        )
        with patch.object(tp_monitor, "SLOT", "trend"), \
             patch.object(tp_monitor, "REMOVE_PROTECTION", False), \
             patch.object(tp_monitor, "install_signal_handlers"), \
             patch.object(tp_monitor, "get_exchange_size", return_value=3), \
             patch.object(tp_monitor, "get_exchange_position", return_value={
                 "product_id": 101, "size": 3, "entry_price": "1.0",
             }), \
             patch.object(tp_monitor, "_trend_cycle_continuity", return_value=continuity), \
             patch.object(tp_monitor, "_close_position_locked", return_value=False) as close, \
             patch.object(tp_monitor, "edit_stop_price") as edit, \
             patch.object(tp_monitor, "place_stop_order") as place, \
             patch.object(tp_monitor, "audit_event"), \
             patch.object(tp_monitor.time, "sleep", side_effect=_StopLoop):
            with self.assertRaises(_StopLoop):
                tp_monitor.main()

        close.assert_called_once()
        persisted = self.read_state()
        self.assertEqual(persisted["lots"], 3)
        self.assertEqual(persisted["cycle_exit_lots_total"], 3)
        self.assertEqual(persisted["pending_close_client_order_id"],
                         "trend-close-partial")
        edit.assert_not_called()
        place.assert_not_called()

    def test_main_reconciles_flat_pending_close_before_generic_cleanup(self):
        self.write_state(
            lots=3, owned_entry_lots=3, original_owned_entry_lots=3,
            pending_close_order_id="trend-close-order",
            pending_close_client_order_id="trend-close-client",
            pending_close_reason="manual_squareoff",
            pending_close_side="sell",
        )
        with patch.object(tp_monitor, "SLOT", "trend"), \
             patch.object(tp_monitor, "REMOVE_PROTECTION", False), \
             patch.object(tp_monitor, "install_signal_handlers"), \
             patch.object(tp_monitor, "get_exchange_size", return_value=0), \
             patch.object(tp_monitor, "close_position", return_value=False) as reconcile, \
             patch.object(tp_monitor, "get_order") as get_order, \
             patch.object(tp_monitor, "remove_exchange_protection") as remove, \
             patch.object(tp_monitor.time, "sleep", side_effect=_StopLoop):
            with self.assertRaises(_StopLoop):
                tp_monitor.main()

        reconcile.assert_called_once()
        self.assertEqual(
            reconcile.call_args.args[0]["pending_close_client_order_id"],
            "trend-close-client",
        )
        self.assertEqual(reconcile.call_args.args[3], "manual_squareoff")
        get_order.assert_not_called()
        remove.assert_not_called()

    def test_main_reloads_dashboard_partial_reduction_before_resizing_orders(self):
        self.write_state(
            lots=6, owned_entry_lots=3, original_owned_entry_lots=3,
            entry_mark=1.0, tp_stop_order_id="tp-old", tp_lots=6,
            tsl_stop_order_id="sl-old", stop_lots=6, stop_kind="sl",
            protection_config={
                "tp_target_pnl": 100, "sl_target_pnl": 50,
                "tsl_arm_pnl": 0, "tsl_trail_pnl": 0, "poll_secs": 10,
            },
        )

        def dashboard_partial_reduction():
            state = self.read_state()
            state.update({
                "lots": 4, "protection_lots": 4,
                "protection_revision": 1, "continuity_revision": 1,
                "continuity_verified": False,
                "continuity_fill_ids": ["exit-1"],
                "cycle_entry_lots_total": 6, "cycle_exit_lots_total": 2,
                "partial_exit_gross_pnl_usd": 2.0,
                "partial_exit_fees_usd": 0.02,
            })
            self.state_file.write_text(json.dumps(state), encoding="utf-8")

        continuity = self.continuity(
            size=4, entry=1.0, entries=6, exits=2, fill_ids=["exit-1"],
        )
        edited = set()

        def get_order(order_id):
            kind = "tp" if str(order_id).startswith("tp") else "stop"
            order = self.protection_order(
                order_id, 4 if order_id in edited else 6, kind,
            )
            if kind == "tp":
                order["stop_price"] = "26.0"
            return order

        def edit_order(order_id, _product_id, _price, *, size=None):
            self.assertEqual(size, 4)
            edited.add(order_id)
            return {"success": True}

        with patch.object(tp_monitor, "SLOT", "trend"), \
             patch.object(tp_monitor, "REMOVE_PROTECTION", False), \
             patch.object(tp_monitor, "install_signal_handlers",
                          side_effect=dashboard_partial_reduction), \
             patch.object(tp_monitor, "get_exchange_size", return_value=4), \
             patch.object(tp_monitor, "get_exchange_position", return_value={
                 "product_id": 101, "size": 4, "entry_price": "1.0",
             }), \
             patch.object(tp_monitor, "_trend_cycle_continuity", return_value=continuity), \
             patch.object(tp_monitor, "_rebase_matching_trend_reduction_locked") as rebase, \
             patch.object(tp_monitor, "get_order", side_effect=get_order), \
             patch.object(tp_monitor, "edit_stop_price", side_effect=edit_order) as edit, \
             patch.object(tp_monitor, "get_mark", return_value=1.1), \
             patch.object(tp_monitor, "send_telegram"), \
             patch.object(tp_monitor.time, "sleep", side_effect=_StopLoop):
            with self.assertRaises(_StopLoop):
                tp_monitor.main()

        rebase.assert_not_called()
        self.assertEqual({call.args[0] for call in edit.call_args_list}, {"sl-old", "tp-old"})
        self.assertTrue(all(call.kwargs["size"] == 4 for call in edit.call_args_list))
        state = self.read_state()
        self.assertEqual(state["lots"], 4)
        self.assertEqual(state["stop_lots"], 4)
        self.assertEqual(state["tp_lots"], 4)

    def test_failed_intent_persistence_never_submits_close(self):
        state = self.write_state()
        with patch.object(tp_monitor, "get_exchange_size", return_value=10), \
             patch.object(tp_monitor, "_persist_close_fields", return_value=False), \
             patch.object(tp_monitor, "place_order") as place:
            closed = tp_monitor._close_position_locked(state, 2.0, 10.0)
        self.assertFalse(closed)
        place.assert_not_called()

    def test_trend_close_rechecks_cycle_after_journalling_before_post(self):
        state = self.write_state(
            lots=3, original_owned_entry_lots=3,
            original_bot_entry_mark=1.0, position_cycle_id="cycle-1",
        )
        with patch.object(tp_monitor, "SLOT", "trend"), \
             patch.object(tp_monitor, "get_exchange_size", return_value=3), \
             patch.object(tp_monitor, "_verify_trend_close_cycle", side_effect=[
                 (True, ""), (False, "the original cycle closed and reopened"),
             ]) as verify, \
             patch.object(tp_monitor, "place_order") as place:
            closed = tp_monitor._close_position_locked(state, 2.0, 3.0)

        self.assertFalse(closed)
        self.assertEqual(verify.call_count, 2)
        place.assert_not_called()
        persisted = self.read_state()
        self.assertTrue(persisted["pending_close_client_order_id"])
        self.assertEqual(persisted["pending_close_state"], "cycle_unverified")

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

    def test_health_snapshot_clears_stale_proof_and_freezes_explicit_identity(self):
        old_identity = self.write_state(
            protection_revision=7, continuity_revision=3,
            position_cycle_id="cycle-old",
        )
        tp_monitor.write_monitor_health(
            "healthy", persist_state=False, identity_state=old_identity,
            exchange_position_size=10, protected_lots=10,
            exchange_protected_lots=10, exchange_protection_complete=True,
            protection_established=True, continuity_verified=True,
            continuity_verified_size=10,
            stop_order_proof={"ok": True}, tp_order_proof={"ok": True},
        )
        self.state_file.write_text(json.dumps({
            **old_identity,
            "protection_revision": 8,
            "continuity_revision": 4,
            "position_cycle_id": "cycle-new",
        }), encoding="utf-8")

        tp_monitor.write_monitor_health(
            "degraded", persist_state=False, identity_state=old_identity,
            last_error="fresh proof is unavailable",
        )

        health = json.loads(self.health_file.read_text(encoding="utf-8"))
        self.assertEqual(health["status"], "degraded")
        self.assertEqual(health["protection_revision"], 7)
        self.assertEqual(health["continuity_revision"], 3)
        self.assertEqual(health["position_cycle_id"], "cycle-old")
        self.assertIsNone(health["exchange_position_size"])
        self.assertEqual(health["protected_lots"], 0)
        self.assertEqual(health["exchange_protected_lots"], 0)
        self.assertFalse(health["exchange_protection_complete"])
        self.assertFalse(health["protection_established"])
        self.assertFalse(health["continuity_verified"])
        self.assertIsNone(health["continuity_verified_size"])
        self.assertIsNone(health["stop_order_proof"])
        self.assertIsNone(health["tp_order_proof"])

    def test_strict_position_read_rejects_wrong_product_and_fractional_size(self):
        wrong_product = Mock()
        wrong_product.json.return_value = {
            "success": True,
            "result": {"product_id": 999, "size": "3", "entry_price": "1.0"},
        }
        with patch.object(tp_monitor, "_sign", return_value={}), \
             patch.object(tp_monitor.requests, "get", return_value=wrong_product):
            self.assertIsNone(tp_monitor.get_exchange_position(101))

        with patch.object(tp_monitor, "get_exchange_position", return_value={
            "product_id": 101, "size": "3.5", "entry_price": "1.0",
        }):
            self.assertIsNone(tp_monitor.get_exchange_size(101))

        state = self.write_state(
            lots=3, owned_entry_lots=3, original_owned_entry_lots=3,
            original_bot_entry_mark=1.0,
        )
        with patch.object(tp_monitor, "SLOT", "trend"):
            result = tp_monitor._trend_cycle_continuity(
                state,
                {"product_id": 101, "size": "3.5", "entry_price": "1.0"},
                [],
            )
        self.assertFalse(result["verified"])
        self.assertEqual(result["status"], "invalid_state")

    def test_protection_proof_uses_remaining_lots_after_partial_fill(self):
        partial = {
            **self.protection_order("tp-partial", 8, "tp"),
            "unfilled_size": 6,
            "filled_size": 2,
        }
        proof = tp_monitor._protection_order_proof(
            partial, order_id="tp-partial", product_id=101,
            close_side="sell", kind="tp", expected_lots=6,
            expected_stop_price=18.2,
        )
        self.assertTrue(proof["ok"])
        self.assertEqual(proof["covered_lots"], 6)

        explicit_zero_fill = {
            key: value for key, value in self.protection_order(
                "tp-zero", 6, "tp",
            ).items() if key != "unfilled_size"
        }
        explicit_zero_fill["filled_size"] = 0
        proof = tp_monitor._protection_order_proof(
            explicit_zero_fill, order_id="tp-zero", product_id=101,
            close_side="sell", kind="tp", expected_lots=6,
            expected_stop_price=18.2,
        )
        self.assertTrue(proof["ok"])

        no_remaining_evidence = dict(explicit_zero_fill)
        no_remaining_evidence.pop("filled_size")
        rejected = tp_monitor._protection_order_proof(
            no_remaining_evidence, order_id="tp-zero", product_id=101,
            close_side="sell", kind="tp", expected_lots=6,
            expected_stop_price=18.2,
        )
        self.assertFalse(rejected["ok"])
        self.assertIn("remaining size is unavailable", rejected["reason"])

        inconsistent = {**partial, "filled_size": 1}
        rejected = tp_monitor._protection_order_proof(
            inconsistent, order_id="tp-partial", product_id=101,
            close_side="sell", kind="tp", expected_lots=6,
            expected_stop_price=18.2,
        )
        self.assertFalse(rejected["ok"])
        self.assertIn("inconsistent", rejected["reason"])

    def test_partial_filled_protection_edit_preserves_total_order_size(self):
        order = {
            **self.protection_order("tp-partial", 6, "tp"),
            "unfilled_size": 4,
            "filled_size": 2,
        }
        edit_total, error = tp_monitor._protection_edit_total_size(
            order, order_id="tp-partial", product_id=101,
            close_side="sell", kind="tp", desired_remaining_lots=4,
        )
        self.assertEqual(error, "")
        self.assertEqual(edit_total, 6)

    def test_same_size_close_reopen_is_detached_before_any_protection_edit(self):
        self.write_state(
            lots=3, owned_entry_lots=3, original_owned_entry_lots=3,
            original_bot_entry_mark=1.0,
            tp_stop_order_id="tp-old", tp_lots=3,
            tsl_stop_order_id="sl-old", stop_lots=3, stop_kind="sl",
            protection_config={
                "tp_target_pnl": 100, "sl_target_pnl": 50,
                "tsl_arm_pnl": 0, "tsl_trail_pnl": 0, "poll_secs": 10,
            },
        )
        broken = {
            "verified": False, "status": "broken_reopened",
            "reason": "the original Trend position closed and reopened",
            "first_zero_fill_id": "flat-1", "reopen_fill_id": "reopen-1",
        }
        with patch.object(tp_monitor, "SLOT", "trend"), \
             patch.object(tp_monitor, "REMOVE_PROTECTION", False), \
             patch.object(tp_monitor, "install_signal_handlers"), \
             patch.object(tp_monitor, "get_exchange_size", return_value=3), \
             patch.object(tp_monitor, "get_exchange_position", return_value={
                 "product_id": 101, "size": 3, "entry_price": "1.0",
             }), \
             patch.object(tp_monitor, "_trend_cycle_continuity",
                          return_value=broken) as continuity, \
             patch.object(tp_monitor, "remove_exchange_protection",
                          return_value=True) as remove, \
             patch.object(tp_monitor, "edit_stop_price") as edit, \
             patch.object(tp_monitor, "place_stop_order") as place, \
             patch.object(tp_monitor, "send_telegram"):
            self.assertEqual(tp_monitor.main(), 0)

        continuity.assert_called_once()
        remove.assert_called_once()
        edit.assert_not_called()
        place.assert_not_called()
        state = self.read_state()
        self.assertEqual(state["status"], "OWNERSHIP_AMBIGUOUS")
        self.assertEqual(state["remaining_external_position_lots"], 3)
        self.assertEqual(state["continuity_status"], "broken_reopened")
        health = json.loads(self.health_file.read_text(encoding="utf-8"))
        self.assertEqual(health["status"], "ownership_ambiguous")
        self.assertFalse(health["protection_established"])

    def test_same_size_fill_refresh_keeps_missing_original_fee_pending(self):
        self.write_state(
            lots=3, owned_entry_lots=3, original_owned_entry_lots=3,
            original_bot_entry_mark=1.0,
            tp_stop_order_id="tp-old", tp_lots=3,
            tp_client_order_id="nithi-tp-tp-existing",
            tp_trigger_method="mark_price", continuity_fill_ids=[],
            protection_config={
                "tp_target_pnl": 51.6, "sl_target_pnl": 0,
                "tsl_arm_pnl": 0, "tsl_trail_pnl": 0, "poll_secs": 10,
            },
        )
        continuity = {
            **self.continuity(
                size=3, entry=1.0, entries=6, exits=3,
                fill_ids=["manual-add", "manual-reduce"],
            ),
            "partial_exit_gross_pnl_usd": 0.4,
            "partial_exit_fees_usd": 0.02,
        }
        order = {
            **self.protection_order("tp-old", 3, "tp"),
            "client_order_id": "nithi-tp-tp-existing",
        }
        with patch.object(tp_monitor, "SLOT", "trend"), \
             patch.object(tp_monitor, "REMOVE_PROTECTION", False), \
             patch.object(tp_monitor, "install_signal_handlers"), \
             patch.object(tp_monitor, "get_exchange_size", return_value=3), \
             patch.object(tp_monitor, "get_exchange_position", return_value={
                 "product_id": 101, "size": 3, "entry_price": "1.0",
             }), \
             patch.object(tp_monitor, "_trend_cycle_continuity", return_value=continuity), \
             patch.object(tp_monitor, "get_order", return_value=order), \
             patch.object(tp_monitor, "get_mark", return_value=1.1), \
             patch.object(tp_monitor, "place_stop_order") as place, \
             patch.object(tp_monitor, "send_telegram"), \
             patch.object(tp_monitor.time, "sleep", side_effect=_StopLoop):
            with self.assertRaises(_StopLoop):
                tp_monitor.main()

        place.assert_not_called()
        state = self.read_state()
        self.assertEqual(state["entry_fee_source"], "fill_ledger_fee_pending")
        self.assertFalse(state["fees_available"])
        self.assertTrue(state["fees_estimated"])

    def test_worker_resyncs_protection_ids_inside_lock_before_placing(self):
        self.write_state(
            lots=3, owned_entry_lots=3, original_owned_entry_lots=3,
            original_bot_entry_mark=1.0,
            protection_config={
                "tp_target_pnl": 51.6, "sl_target_pnl": 0,
                "tsl_arm_pnl": 0, "tsl_trail_pnl": 0, "poll_secs": 10,
            },
        )
        test_case = self

        class InjectFirstWorkerState:
            def __enter__(self):
                current = test_case.read_state()
                current.update({
                    "tp_stop_order_id": "tp-first-worker", "tp_lots": 3,
                    "tp_client_order_id": "nithi-tp-tp-first",
                    "tp_trigger_method": "mark_price",
                })
                test_case.state_file.write_text(json.dumps(current), encoding="utf-8")
                return True

            def __exit__(self, *_args):
                return False

        continuity = {
            **self.continuity(size=3, entry=1.0, entries=3, exits=0),
            "fill_ids": [], "last_fill_id": None,
        }
        order = {
            **self.protection_order("tp-first-worker", 3, "tp"),
            "client_order_id": "nithi-tp-tp-first",
        }
        with patch.object(tp_monitor, "SLOT", "trend"), \
             patch.object(tp_monitor, "REMOVE_PROTECTION", False), \
             patch.object(tp_monitor, "install_signal_handlers"), \
             patch.object(tp_monitor, "account_file_lock",
                          return_value=InjectFirstWorkerState()), \
             patch.object(tp_monitor, "get_exchange_size", return_value=3), \
             patch.object(tp_monitor, "get_exchange_position", return_value={
                 "product_id": 101, "size": 3, "entry_price": "1.0",
             }), \
             patch.object(tp_monitor, "_trend_cycle_continuity", return_value=continuity), \
             patch.object(tp_monitor, "get_order", return_value=order), \
             patch.object(tp_monitor, "get_mark", return_value=1.1), \
             patch.object(tp_monitor, "place_stop_order") as place, \
             patch.object(tp_monitor, "send_telegram"), \
             patch.object(tp_monitor.time, "sleep", side_effect=_StopLoop):
            with self.assertRaises(_StopLoop):
                tp_monitor.main()

        place.assert_not_called()
        self.assertEqual(self.read_state()["tp_stop_order_id"], "tp-first-worker")

    def test_disabled_stop_is_conclusively_removed_before_healthy_status(self):
        self.write_state(
            lots=3, owned_entry_lots=3, original_owned_entry_lots=3,
            original_bot_entry_mark=1.0,
            tp_stop_order_id="tp-old", tp_lots=3,
            tp_client_order_id="nithi-tp-tp-existing",
            tp_trigger_method="mark_price",
            tsl_stop_order_id="sl-old", stop_lots=3,
            stop_client_order_id="nithi-tp-stop-existing",
            stop_trigger_method="mark_price", stop_kind="sl",
            protection_config={
                "tp_target_pnl": 51.6, "sl_target_pnl": 0,
                "tsl_arm_pnl": 0, "tsl_trail_pnl": 0, "poll_secs": 10,
            },
        )
        continuity = {
            **self.continuity(size=3, entry=1.0, entries=3, exits=0),
            "fill_ids": [], "last_fill_id": None,
        }

        def order_for(order_id):
            kind = "tp" if order_id == "tp-old" else "stop"
            client = ("nithi-tp-tp-existing" if kind == "tp"
                      else "nithi-tp-stop-existing")
            return {**self.protection_order(order_id, 3, kind),
                    "client_order_id": client}

        with patch.object(tp_monitor, "SLOT", "trend"), \
             patch.object(tp_monitor, "REMOVE_PROTECTION", False), \
             patch.object(tp_monitor, "install_signal_handlers"), \
             patch.object(tp_monitor, "get_exchange_size", return_value=3), \
             patch.object(tp_monitor, "get_exchange_position", return_value={
                 "product_id": 101, "size": 3, "entry_price": "1.0",
             }), \
             patch.object(tp_monitor, "_trend_cycle_continuity", return_value=continuity), \
             patch.object(tp_monitor, "get_order", side_effect=order_for), \
             patch.object(tp_monitor, "get_mark", return_value=1.1), \
             patch.object(tp_monitor, "cancel_order",
                          return_value={"success": True}) as cancel, \
             patch.object(tp_monitor, "place_stop_order") as place, \
             patch.object(tp_monitor, "send_telegram"), \
             patch.object(tp_monitor.time, "sleep", side_effect=_StopLoop):
            with self.assertRaises(_StopLoop):
                tp_monitor.main()

        cancel.assert_called_once_with("sl-old", 101)
        place.assert_not_called()
        state = self.read_state()
        self.assertIsNone(state["tsl_stop_order_id"])
        self.assertEqual(state["last_tsl_stop_order_id"], "sl-old")
        health = json.loads(self.health_file.read_text(encoding="utf-8"))
        self.assertEqual(health["stop_order_proof"]["reason"], "stop is disabled")
        self.assertTrue(health["exchange_protection_complete"])

    def test_final_position_change_after_proofs_cannot_publish_healthy(self):
        self.write_state(
            lots=3, owned_entry_lots=3, original_owned_entry_lots=3,
            original_bot_entry_mark=1.0,
            tp_stop_order_id="tp-old", tp_lots=3,
            protection_config={
                "tp_target_pnl": 51.6, "sl_target_pnl": 0,
                "tsl_arm_pnl": 0, "tsl_trail_pnl": 0, "poll_secs": 10,
            },
        )
        continuity_result = {
            **self.continuity(size=3, entry=1.0, entries=3, exits=0),
            "fill_ids": [], "last_fill_id": None,
        }
        order = self.protection_order("tp-old", 3, "tp")
        with patch.object(tp_monitor, "SLOT", "trend"), \
             patch.object(tp_monitor, "REMOVE_PROTECTION", False), \
             patch.object(tp_monitor, "install_signal_handlers"), \
             patch.object(tp_monitor, "get_exchange_size", return_value=3), \
             patch.object(tp_monitor, "get_exchange_position", side_effect=[
                 {"product_id": 101, "size": 3, "entry_price": "1.0"},
                 {"product_id": 101, "size": 4, "entry_price": "1.25"},
             ]), \
             patch.object(tp_monitor, "_trend_cycle_continuity",
                          return_value=continuity_result) as continuity, \
             patch.object(tp_monitor, "get_order", return_value=order), \
             patch.object(tp_monitor, "get_mark", return_value=1.1), \
             patch.object(tp_monitor, "edit_stop_price") as edit, \
             patch.object(tp_monitor, "place_stop_order") as place, \
             patch.object(tp_monitor, "send_telegram"), \
             patch.object(tp_monitor.time, "sleep", side_effect=_StopLoop):
            with self.assertRaises(_StopLoop):
                tp_monitor.main()

        continuity.assert_called_once()
        edit.assert_not_called()
        place.assert_not_called()
        health = json.loads(self.health_file.read_text(encoding="utf-8"))
        self.assertEqual(health["status"], "degraded")
        self.assertIn("position changed", health["last_error"])
        self.assertFalse(health["exchange_protection_complete"])
        self.assertFalse(health["protection_established"])
        self.assertFalse(health["continuity_verified"])
        self.assertEqual(health["exchange_position_size"], 4)

    def test_pending_protection_intent_recovers_exact_order_without_resubmit(self):
        intent = {
            "client_order_id": "trend-tp-pending", "product_id": 101,
            "side": "sell", "lots": 3, "stop_price": 18.2,
            "stop_order_type": "take_profit_order",
            "stop_trigger_method": "mark_price",
            "created_at_utc": "2026-07-17T00:00:00+00:00",
        }
        self.write_state(
            lots=3, owned_entry_lots=3, original_owned_entry_lots=3,
            original_bot_entry_mark=1.0, pending_tp_protection=intent,
            protection_config={
                "tp_target_pnl": 51.6, "sl_target_pnl": 0,
                "tsl_arm_pnl": 0, "tsl_trail_pnl": 0, "poll_secs": 10,
            },
        )
        continuity_result = {
            **self.continuity(size=3, entry=1.0, entries=3, exits=0),
            "fill_ids": [], "last_fill_id": None,
        }
        recovered = {
            **self.protection_order("tp-recovered", 3, "tp"),
            "client_order_id": "trend-tp-pending",
        }
        with patch.object(tp_monitor, "SLOT", "trend"), \
             patch.object(tp_monitor, "REMOVE_PROTECTION", False), \
             patch.object(tp_monitor, "install_signal_handlers"), \
             patch.object(tp_monitor, "get_exchange_size", return_value=3), \
             patch.object(tp_monitor, "get_exchange_position", return_value={
                 "product_id": 101, "size": 3, "entry_price": "1.0",
             }), \
             patch.object(tp_monitor, "_trend_cycle_continuity",
                          return_value=continuity_result), \
             patch.object(tp_monitor, "get_order_by_client_id",
                          return_value=(recovered, True)) as lookup, \
             patch.object(tp_monitor, "get_order", return_value=recovered), \
             patch.object(tp_monitor, "get_mark", return_value=1.1), \
             patch.object(tp_monitor, "place_stop_order") as place, \
             patch.object(tp_monitor, "cancel_order") as cancel, \
             patch.object(tp_monitor, "send_telegram"), \
             patch.object(tp_monitor.time, "sleep", side_effect=_StopLoop):
            with self.assertRaises(_StopLoop):
                tp_monitor.main()

        lookup.assert_called_once_with("trend-tp-pending", 101)
        place.assert_not_called()
        cancel.assert_not_called()
        state = self.read_state()
        self.assertEqual(state["tp_stop_order_id"], "tp-recovered")
        self.assertEqual(state["tp_client_order_id"], "trend-tp-pending")
        self.assertIsNone(state["pending_tp_protection"])

    def test_pending_protection_intent_inconclusive_lookup_never_submits(self):
        intent = {
            "client_order_id": "trend-tp-pending", "product_id": 101,
            "side": "sell", "lots": 3, "stop_price": 18.2,
            "stop_order_type": "take_profit_order",
            "stop_trigger_method": "mark_price",
            "created_at_utc": "2026-07-17T00:00:00+00:00",
        }
        self.write_state(
            lots=3, owned_entry_lots=3, original_owned_entry_lots=3,
            original_bot_entry_mark=1.0, pending_tp_protection=intent,
            protection_config={
                "tp_target_pnl": 51.6, "sl_target_pnl": 0,
                "tsl_arm_pnl": 0, "tsl_trail_pnl": 0, "poll_secs": 10,
            },
        )
        continuity_result = {
            **self.continuity(size=3, entry=1.0, entries=3, exits=0),
            "fill_ids": [], "last_fill_id": None,
        }
        with patch.object(tp_monitor, "SLOT", "trend"), \
             patch.object(tp_monitor, "REMOVE_PROTECTION", False), \
             patch.object(tp_monitor, "install_signal_handlers"), \
             patch.object(tp_monitor, "get_exchange_size", return_value=3), \
             patch.object(tp_monitor, "get_exchange_position", return_value={
                 "product_id": 101, "size": 3, "entry_price": "1.0",
             }), \
             patch.object(tp_monitor, "_trend_cycle_continuity",
                          return_value=continuity_result), \
             patch.object(tp_monitor, "get_order_by_client_id",
                          return_value=({}, False)) as lookup, \
             patch.object(tp_monitor, "get_mark", return_value=1.1), \
             patch.object(tp_monitor, "place_stop_order") as place, \
             patch.object(tp_monitor, "send_telegram"), \
             patch.object(tp_monitor.time, "sleep", side_effect=_StopLoop):
            with self.assertRaises(_StopLoop):
                tp_monitor.main()

        lookup.assert_called_once_with("trend-tp-pending", 101)
        place.assert_not_called()
        state = self.read_state()
        self.assertEqual(
            state["pending_tp_protection"]["client_order_id"],
            "trend-tp-pending",
        )
        self.assertNotIn("tp_stop_order_id", state)
        health = json.loads(self.health_file.read_text(encoding="utf-8"))
        self.assertEqual(health["status"], "degraded")
        self.assertTrue(health["local_tp_fallback_active"])

    def test_pending_protection_intent_reuses_identity_after_conclusive_absence(self):
        intent = {
            "client_order_id": "trend-tp-pending", "product_id": 101,
            "side": "sell", "lots": 3, "stop_price": 18.2,
            "stop_order_type": "take_profit_order",
            "stop_trigger_method": "mark_price",
            "created_at_utc": "2026-07-17T00:00:00+00:00",
        }
        self.write_state(
            lots=3, owned_entry_lots=3, original_owned_entry_lots=3,
            original_bot_entry_mark=1.0, pending_tp_protection=intent,
            protection_config={
                "tp_target_pnl": 51.6, "sl_target_pnl": 0,
                "tsl_arm_pnl": 0, "tsl_trail_pnl": 0, "poll_secs": 10,
            },
        )
        continuity_result = {
            **self.continuity(size=3, entry=1.0, entries=3, exits=0),
            "fill_ids": [], "last_fill_id": None,
        }
        placed_order = {
            **self.protection_order("tp-new", 3, "tp"),
            "client_order_id": "trend-tp-pending",
        }
        with patch.object(tp_monitor, "SLOT", "trend"), \
             patch.object(tp_monitor, "REMOVE_PROTECTION", False), \
             patch.object(tp_monitor, "install_signal_handlers"), \
             patch.object(tp_monitor, "get_exchange_size", return_value=3), \
             patch.object(tp_monitor, "get_exchange_position", return_value={
                 "product_id": 101, "size": 3, "entry_price": "1.0",
             }), \
             patch.object(tp_monitor, "_trend_cycle_continuity",
                          return_value=continuity_result), \
             patch.object(tp_monitor, "get_order_by_client_id",
                          return_value=({}, True)), \
             patch.object(tp_monitor, "get_order", return_value=placed_order), \
             patch.object(tp_monitor, "get_mark", return_value=1.1), \
             patch.object(tp_monitor, "place_stop_order", return_value={
                 "success": True, "result": placed_order,
             }) as place, \
             patch.object(tp_monitor, "send_telegram"), \
             patch.object(tp_monitor.time, "sleep", side_effect=_StopLoop):
            with self.assertRaises(_StopLoop):
                tp_monitor.main()

        place.assert_called_once()
        self.assertEqual(
            place.call_args.kwargs["client_order_id"], "trend-tp-pending",
        )
        state = self.read_state()
        self.assertEqual(state["tp_stop_order_id"], "tp-new")
        self.assertEqual(state["tp_client_order_id"], "trend-tp-pending")
        self.assertIsNone(state["pending_tp_protection"])

    def test_flat_multifill_trend_ledger_preserves_complete_accounting(self):
        state = self.write_state(
            lots=3, owned_entry_lots=3, original_owned_entry_lots=3,
            entry_mark=1.0, original_bot_entry_mark=1.0,
            order_id="entry-order", order_ids=["entry-order"],
        )
        fills = [
            {
                "id": "exit-1", "product_id": 101, "size": 1,
                "side": "sell", "price": "2.0", "commission": "0.01",
                "created_at": "2026-07-15T01:03:00Z",
                "order_id": "manual-exit-1",
            },
            {
                "id": "exit-2", "product_id": 101, "size": 2,
                "side": "sell", "price": "3.0", "commission": "0.02",
                "created_at": "2026-07-15T01:04:00Z",
                "order_id": "manual-exit-2",
            },
        ]
        with patch.object(tp_monitor, "SLOT", "trend"):
            ledger = tp_monitor._trend_cycle_continuity(
                state, {"product_id": 101, "size": 0, "entry_price": "0"},
                fills,
            )

        self.assertTrue(ledger["verified"])
        self.assertEqual(ledger["status"], "closed")
        self.assertEqual(ledger["signed_size"], 0)
        self.assertEqual(ledger["cycle_entry_lots_total"], 3)
        self.assertEqual(ledger["cycle_exit_lots_total"], 3)
        self.assertAlmostEqual(ledger["partial_exit_gross_pnl_usd"], 5.0)
        self.assertAlmostEqual(ledger["partial_exit_fees_usd"], 0.03)
        self.assertAlmostEqual(ledger["exit_mark"], 8 / 3)
        self.assertEqual(ledger["exit_fill_ids"], ["exit-1", "exit-2"])
        self.assertEqual(
            ledger["exit_order_ids"], ["manual-exit-1", "manual-exit-2"],
        )
        self.assertTrue(ledger["fill_fees_complete"])


if __name__ == "__main__":
    unittest.main()
