"""Kill switches must LATCH: fire, survive a restart, and clear only via an
explicit resume (Trend_Engine.md criterion 11 / §24)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from btc_trend_engine.risk.drawdown_guard import triggers_from_decision
from btc_trend_engine.risk.kill_switch import KillSwitchName, KillSwitchStore

T0 = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path: Path) -> KillSwitchStore:
    return KillSwitchStore(tmp_path / "risk_kill_switches.json")


def test_a_fresh_store_has_nothing_active(store):
    assert not store.any_active()
    assert store.active_names() == []


def test_fired_switch_is_active_and_recorded(store):
    state = store.fire(KillSwitchName.MAX_DAILY_LOSS, "daily loss -520", now=T0)
    assert store.any_active()
    assert store.active_names() == ["max_daily_loss"]
    assert state.fired_at_utc == "2026-07-25T12:00:00Z"
    assert state.reason == "daily loss -520"


def test_latch_survives_a_new_store_instance_ie_a_process_restart(store):
    store.fire(KillSwitchName.DATA_QUALITY, "feed stale", now=T0)
    reopened = KillSwitchStore(store.path)
    assert reopened.any_active()
    assert reopened.active_names() == ["data_quality"]


def test_refiring_bumps_the_count_but_never_resets_fired_at(store):
    store.fire(KillSwitchName.DATA_QUALITY, "first", now=T0)
    later = store.fire(KillSwitchName.DATA_QUALITY, "second", now=T0 + timedelta(minutes=30))
    assert later.fired_at_utc == "2026-07-25T12:00:00Z"
    assert later.last_seen_utc == "2026-07-25T12:30:00Z"
    assert later.fired_count == 2
    assert later.reason == "second"


def test_a_switch_clears_only_via_an_explicit_resume(store):
    store.fire(KillSwitchName.CONSECUTIVE_LOSSES, "3 in a row", now=T0)
    assert store.any_active()
    assert store.resume(KillSwitchName.CONSECUTIVE_LOSSES, now=T0) is True
    assert not store.any_active()


def test_resuming_an_inactive_switch_reports_that_it_was_not_active(store):
    assert store.resume(KillSwitchName.MANUAL, now=T0) is False


def test_resume_all_clears_every_switch_and_is_audited(store):
    store.fire(KillSwitchName.MANUAL, "operator", now=T0)
    store.fire(KillSwitchName.DATA_QUALITY, "stale", now=T0)
    cleared = store.resume_all(now=T0)
    assert cleared == ["data_quality", "manual"]
    assert not store.any_active()
    events = [(e["event"], e["name"]) for e in store.history()]
    assert ("fire", "manual") in events
    assert ("resume", "data_quality") in events


def test_a_corrupt_state_file_is_treated_as_no_latch_not_a_crash(store):
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{not json", encoding="utf-8")
    # Reading must not raise. A corrupt file cannot be trusted to *prove* a
    # latch, and the engine's fail-closed posture lives in risk_manager
    # (which refuses entry on any active switch), not in this store.
    assert store.active_names() == []
    store.fire(KillSwitchName.MANUAL, "recovered", now=T0)
    assert store.active_names() == ["manual"]


# ── drawdown_guard -> kill-switch trigger recommendations ────────────────
def test_daily_loss_breach_recommends_the_daily_loss_switch():
    assert triggers_from_decision(
        daily_pnl_usd=-600, consecutive_losses=0,
        max_daily_loss_usd=500, max_consecutive_losses=3) == ["max_daily_loss"]


def test_consecutive_loss_breach_recommends_that_switch():
    assert triggers_from_decision(
        daily_pnl_usd=0, consecutive_losses=3,
        max_daily_loss_usd=500, max_consecutive_losses=3) == ["consecutive_losses"]


def test_no_breach_recommends_nothing():
    assert triggers_from_decision(
        daily_pnl_usd=-100, consecutive_losses=1,
        max_daily_loss_usd=500, max_consecutive_losses=3) == []


def test_a_zero_limit_disables_that_trigger_rather_than_firing_constantly():
    assert triggers_from_decision(
        daily_pnl_usd=-9999, consecutive_losses=99,
        max_daily_loss_usd=0, max_consecutive_losses=0) == []
