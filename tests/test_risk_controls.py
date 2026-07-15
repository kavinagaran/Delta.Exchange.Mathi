import json
import os
import time
from datetime import datetime, timedelta, timezone

import risk_controls
from risk_controls import (
    account_file_lock,
    account_entry_lock,
    cfg_float,
    cfg_int,
    dedupe_trades,
    evaluate_entry,
    position_risk_usd,
    risk_based_lots,
    trading_date,
)


def _write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def test_ist_trading_day_crosses_utc_boundary():
    now = datetime(2026, 7, 14, 20, 0, tzinfo=timezone.utc)
    assert trading_date(now, 330) == "2026-07-15"


def test_history_dedupes_same_order():
    rows = [{"order_id": 7, "pnl_usd": 1}, {"order_id": 7, "pnl_usd": 1}]
    assert len(dedupe_trades(rows)) == 1


def test_global_daily_cap_covers_all_slots(tmp_path):
    _write(tmp_path / "trade_history.json", [
        {"slot": "morning", "trading_date": "2026-07-15", "order_id": 1, "pnl_usd": 10},
        {"slot": "trend", "trading_date": "2026-07-15", "order_id": 2, "pnl_usd": 10},
    ])
    d = evaluate_entry(tmp_path, 10, {"MAX_TRADES_PER_DAY_GLOBAL": 2},
                       datetime(2026, 7, 15, 4, tzinfo=timezone.utc))
    assert not d.allowed
    assert "daily trade cap" in d.reason


def test_open_position_counts_toward_global_daily_cap(tmp_path):
    _write(tmp_path / "trend_state.json", {
        "status": "OPEN", "trading_date": "2026-07-15", "order_id": 9,
        "total_cost_usd": 10,
    })
    d = evaluate_entry(tmp_path, 10, {"MAX_TRADES_PER_DAY_GLOBAL": 1},
                       datetime(2026, 7, 15, 4, tzinfo=timezone.utc))
    assert not d.allowed
    assert d.trades_today == 1


def test_unrealized_loss_is_in_daily_loss_lock(tmp_path):
    d = evaluate_entry(tmp_path, 10, {"MAX_DAILY_LOSS_USD": 100},
                       datetime(2026, 7, 15, 4, tzinfo=timezone.utc),
                       unrealized_pnl_usd=-120)
    assert not d.allowed
    assert "daily loss lock" in d.reason


def test_open_risk_cap_counts_other_strategy(tmp_path):
    _write(tmp_path / "trend_state.json", {
        "status": "OPEN", "side": "long", "total_cost_usd": 80,
    })
    d = evaluate_entry(tmp_path, 30, {"MAX_OPEN_RISK_USD": 100},
                       datetime(2026, 7, 15, 4, tzinfo=timezone.utc))
    assert not d.allowed
    assert d.open_risk_usd == 80


def test_unknown_open_risk_fails_closed(tmp_path):
    _write(tmp_path / "trend_state.json", {
        "status": "OPEN", "side": "short", "trading_date": "2026-07-15",
        "order_id": 4,
    })
    d = evaluate_entry(tmp_path, 10, {},
                       datetime(2026, 7, 15, 4, tzinfo=timezone.utc))
    assert not d.allowed
    assert "no verifiable risk" in d.reason


def test_post_loss_cooldown(tmp_path):
    now = datetime(2026, 7, 15, 4, tzinfo=timezone.utc)
    _write(tmp_path / "trade_history.json", [{
        "order_id": 1, "trading_date": "2026-07-15", "pnl_usd": -10,
        "exit_at_utc": (now - timedelta(minutes=5)).isoformat(),
    }])
    d = evaluate_entry(tmp_path, 10, {"LOSS_COOLDOWN_MINUTES": 30}, now)
    assert not d.allowed
    assert d.cooldown_remaining_sec > 0


def test_risk_lots_is_minimum_of_every_cap():
    assert risk_based_lots(1000, 900, 400, 5000, 100, 50, .20, .02, .03) == 400
    assert risk_based_lots(1000, 900, 800, 5000, 100, 50, .20, .02, .03) == 400


def test_short_without_stop_is_blocked():
    assert risk_based_lots(100, 100, 100, 100, 100, 0, 1, 0, 0, short=True) == 0


def test_entry_lock_is_cross_process_style_mutex(tmp_path):
    with account_entry_lock(tmp_path, "first") as first:
        assert first
        with account_entry_lock(tmp_path, "second") as second:
            assert not second
    with account_entry_lock(tmp_path, "third") as third:
        assert third


def test_active_pid_lock_is_not_stolen_even_when_aged(tmp_path):
    with account_entry_lock(tmp_path, "active", stale_after_sec=1) as first:
        assert first
        old = time.time() - 60
        os.utime(tmp_path / ".entry.lock", (old, old))
        with account_entry_lock(tmp_path, "contender", stale_after_sec=1) as second:
            assert not second


def test_dead_stale_named_lock_is_reclaimed(tmp_path, monkeypatch):
    path = tmp_path / ".trade_history.lock"
    _write(path, {"owner": "dead-writer", "pid": 999999, "ts": 0})
    old = time.time() - 60
    os.utime(path, (old, old))
    monkeypatch.setattr(risk_controls, "_pid_is_alive", lambda pid: False)

    with account_file_lock(
        tmp_path, "trade_history", "new-writer", stale_after_sec=1
    ) as acquired:
        assert acquired
    assert not path.exists()


def test_unreadable_stale_lock_fails_closed(tmp_path):
    path = tmp_path / ".trade_history.lock"
    path.write_text("not-json", encoding="utf-8")
    old = time.time() - 60
    os.utime(path, (old, old))

    with account_file_lock(
        tmp_path, "trade_history", "new-writer", stale_after_sec=1
    ) as acquired:
        assert not acquired
    assert path.exists()


def test_numeric_zero_config_values_are_not_replaced_by_defaults():
    config = {"FLOAT_ZERO": 0, "INT_ZERO": "0"}
    assert cfg_float(config, "FLOAT_ZERO", 12.5) == 0.0
    assert cfg_int(config, "INT_ZERO", 12) == 0


def test_dry_run_history_and_state_are_excluded_from_live_risk(tmp_path):
    _write(tmp_path / "trade_history.json", [{
        "order_id": 1,
        "trading_date": "2026-07-15",
        "pnl_usd": -1000,
        "dry_run": True,
    }])
    _write(tmp_path / "trend_state.json", {
        "status": "OPEN",
        "trading_date": "2026-07-15",
        "total_cost_usd": 1000,
        "dry_run": True,
    })
    decision = evaluate_entry(
        tmp_path,
        10,
        {
            "MAX_TRADES_PER_DAY_GLOBAL": 1,
            "MAX_DAILY_LOSS_USD": 100,
            "MAX_OPEN_RISK_USD": 100,
        },
        datetime(2026, 7, 15, 4, tzinfo=timezone.utc),
    )
    assert decision.allowed
    assert decision.trades_today == 0
    assert decision.daily_pnl_usd == 0
    assert decision.open_risk_usd == 0


def test_long_risk_uses_worst_of_catastrophe_and_explicit_stop():
    assert position_risk_usd({
        "status": "OPEN",
        "side": "long",
        "total_cost_usd": 125,
        "sl_target_pnl": 40,
    }, 0) == 125
    assert position_risk_usd({
        "status": "OPEN",
        "side": "long",
        "total_cost_usd": 25,
        "sl_target_pnl": 80,
    }, 0) == 80


def test_closed_state_and_matching_history_are_counted_once(tmp_path):
    row = {
        "status": "CLOSED", "slot": "trend", "client_order_id": "trend-abc",
        "trading_date": "2026-07-15", "entry_date": "2026-07-15",
        "entry_time_utc": "04:00:00", "symbol": "C-BTC-X", "lots": 10,
        "pnl_usd": -40, "pnl_includes_fees": True,
    }
    _write(tmp_path / "trend_state.json", row)
    _write(tmp_path / "trade_history.json", [{**row, "status": "CLOSED"}])
    decision = evaluate_entry(
        tmp_path, 10,
        {"MAX_TRADES_PER_DAY_GLOBAL": 2, "MAX_DAILY_LOSS_USD": 50},
        datetime(2026, 7, 15, 4, tzinfo=timezone.utc),
    )
    assert decision.allowed
    assert decision.trades_today == 1
    assert decision.daily_pnl_usd == -40


def test_corrupt_history_blocks_risk_evaluation(tmp_path):
    (tmp_path / "trade_history.json").write_text("not-json", encoding="utf-8")
    try:
        evaluate_entry(tmp_path, 10, {}, datetime(2026, 7, 15, tzinfo=timezone.utc))
    except risk_controls.RiskDataError as exc:
        assert "history" in str(exc)
    else:
        raise AssertionError("corrupt risk history must fail closed")


def test_corrupt_state_blocks_risk_evaluation(tmp_path):
    (tmp_path / "trend_state.json").write_text("[]", encoding="utf-8")
    try:
        evaluate_entry(tmp_path, 10, {}, datetime(2026, 7, 15, tzinfo=timezone.utc))
    except risk_controls.RiskDataError as exc:
        assert "state" in str(exc)
    else:
        raise AssertionError("corrupt risk state must fail closed")
