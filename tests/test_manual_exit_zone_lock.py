from __future__ import annotations

import json

import pytest

from manual_exit_zone_lock import (
    apply_manual_exit_zone_lock,
    position_automatic_zone,
)
from trend_score_auto import CE_2_ITM, HOLD, PE_2_ITM, SHORT_MOVE


def _state(action: str, *, cycle: str = "cycle-1") -> dict:
    symbol, side = {
        "buy_ce": ("C-BTC-80000-060926", "long"),
        "sell_ce": ("C-BTC-80000-060926", "short"),
        "buy_pe": ("P-BTC-80000-060926", "long"),
        "sell_pe": ("P-BTC-80000-060926", "short"),
        "buy_move": ("MV-BTC-80000-060926", "long"),
        "sell_move": ("MV-BTC-80000-060926", "short"),
    }[action]
    return {
        "status": "CLOSED",
        "slot": "trend",
        "symbol": symbol,
        "side": side,
        "ownership": "manual_cockpit_live",
        "entry_trigger": f"manual_cockpit_{action}",
        "entry_classification": "manual_cockpit",
        "manual_cockpit_action": action,
        "position_cycle_id": cycle,
        "exit_trigger": "manual_squareoff",
    }


def _snapshot(zone: str) -> dict:
    scores = {
        CE_2_ITM: 41.0,
        PE_2_ITM: -41.0,
        SHORT_MOVE: 0.0,
        HOLD: 40.0,
    }
    return {
        "data_quality": "OK",
        "trend_score": scores[zone],
        "zone": zone,
        "signal_id": f"signal-{zone}",
    }


@pytest.mark.parametrize(("action", "zone"), [
    ("buy_ce", CE_2_ITM),
    ("sell_pe", CE_2_ITM),
    ("buy_pe", PE_2_ITM),
    ("sell_ce", PE_2_ITM),
    ("sell_move", SHORT_MOVE),
    ("buy_move", None),
])
def test_manual_position_direction_maps_to_automatic_zone(action, zone):
    assert position_automatic_zone(_state(action)) == zone


@pytest.mark.parametrize(("action", "zone"), [
    ("buy_ce", CE_2_ITM),
    ("sell_pe", CE_2_ITM),
    ("buy_pe", PE_2_ITM),
    ("sell_ce", PE_2_ITM),
    ("sell_move", SHORT_MOVE),
])
def test_same_direction_exit_arms_zone_lock(tmp_path, action, zone):
    result = apply_manual_exit_zone_lock(
        tmp_path, _state(action), snapshot=_snapshot(zone),
        account_lock_held=True,
    )
    ledger = json.loads((tmp_path / "trend_score_auto_ledger.json").read_text())
    assert result["matched"] is True
    assert result["locked"] is True
    assert ledger["setup_lock"]["target_zone"] == zone
    assert ledger["setup_lock"]["source_action"] == "MANUAL_EXIT_MATCH"


def test_opposite_direction_records_evaluation_without_lock(tmp_path):
    result = apply_manual_exit_zone_lock(
        tmp_path, _state("buy_ce"), snapshot=_snapshot(PE_2_ITM),
        account_lock_held=True,
    )
    ledger = json.loads((tmp_path / "trend_score_auto_ledger.json").read_text())
    assert result["matched"] is False
    assert result["locked"] is False
    assert ledger.get("setup_lock") is None


def test_long_move_never_matches_short_move_automation(tmp_path):
    result = apply_manual_exit_zone_lock(
        tmp_path, _state("buy_move"), snapshot=_snapshot(SHORT_MOVE),
        account_lock_held=True,
    )
    assert result["matched"] is False
    assert result["locked"] is False


def test_hold_decision_does_not_arm_a_directional_lock(tmp_path):
    result = apply_manual_exit_zone_lock(
        tmp_path, _state("buy_ce"), snapshot=_snapshot(HOLD),
        account_lock_held=True,
    )
    assert result["matched"] is False
    assert result["locked"] is False


def test_bot_and_external_closes_are_out_of_scope(tmp_path):
    bot = _state("buy_ce")
    bot.update({
        "ownership": "trend_score_auto_live",
        "entry_trigger": "trend_engine_score_zone_auto",
        "entry_classification": "rules_based_score_auto",
        "manual_cockpit_action": None,
    })
    external = _state("buy_ce")
    external.update({
        "ownership": "external_protection_only",
        "entry_trigger": "exchange_sync",
        "entry_classification": "external",
        "manual_cockpit_action": None,
    })
    assert apply_manual_exit_zone_lock(
        tmp_path, bot, snapshot=_snapshot(CE_2_ITM), account_lock_held=True,
    )["eligible"] is False
    assert apply_manual_exit_zone_lock(
        tmp_path, external, snapshot=_snapshot(CE_2_ITM), account_lock_held=True,
    )["eligible"] is False
    assert not (tmp_path / "trend_score_auto_ledger.json").exists()


def test_invalid_committed_snapshot_never_arms_lock(tmp_path):
    result = apply_manual_exit_zone_lock(
        tmp_path,
        _state("sell_move"),
        snapshot={
            "data_quality": "DEGRADED", "trend_score": 0,
            "zone": SHORT_MOVE, "signal_id": "signal-degraded",
        },
        account_lock_held=True,
    )
    assert result["locked"] is False
    assert "data quality" in result["reason"]


def test_one_exit_is_evaluated_only_once_even_after_manual_reset(tmp_path):
    state = _state("buy_ce")
    first = apply_manual_exit_zone_lock(
        tmp_path, state, snapshot=_snapshot(CE_2_ITM), account_lock_held=True,
    )
    path = tmp_path / "trend_score_auto_ledger.json"
    ledger = json.loads(path.read_text())
    ledger["setup_lock"] = None
    path.write_text(json.dumps(ledger))

    second = apply_manual_exit_zone_lock(
        tmp_path, state, snapshot=_snapshot(CE_2_ITM), account_lock_held=True,
    )
    final = json.loads(path.read_text())
    assert first["locked"] is True
    assert second["already_evaluated"] is True
    assert second["locked"] is False
    assert second["previously_locked"] is True
    assert final["setup_lock"] is None
