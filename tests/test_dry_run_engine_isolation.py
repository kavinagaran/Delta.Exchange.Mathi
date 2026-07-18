import json
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

import Delta_Straddle_Live as bot
from risk_controls import evaluate_entry


def _mode_dirs(tmp_path):
    account = tmp_path / "users" / "mathi"
    dry = account / "dry_run"
    dry.mkdir(parents=True)
    return account, dry


def _open_paper_state():
    return {
        "slot": "evening",
        "status": "OPEN",
        "dry_run": True,
        "execution_mode": "dry_run",
        "side": "long",
        "entry_date": "2026-07-18",
        "entry_time_utc": "12:05:00",
        "symbol": "MV-BTC-64000-190726",
        "product_id": 123,
        "strike": 64000,
        "settlement": "2026-07-19T12:00:00Z",
        "contract_value": 0.001,
        "lots": 10,
        "owned_entry_lots": 10,
        "entry_mark": 100.0,
        "btc_at_entry": 64000.0,
        "total_cost_usd": 1.0,
        "entry_fee_usd": 0.0,
        "entry_fee_source": "dry_run",
    }


def test_storage_namespace_keeps_live_root_and_dry_subdirectory(tmp_path):
    account, dry = _mode_dirs(tmp_path)
    with patch.multiple(
        bot,
        ACCOUNT_DIR=account,
        LIVE_DATA_DIR=account,
        DRY_DATA_DIR=dry,
    ):
        with bot._storage_namespace(False):
            assert bot.DATA_DIR == account
            assert bot.STATE_FILE == account / "straddle_state.json"
            assert bot.HISTORY_FILE == account / "trade_history.json"
        with bot._storage_namespace(True):
            assert bot.DATA_DIR == dry
            assert bot.MORNING_STATE_FILE == dry / "morning_state.json"
            assert bot.HISTORY_FILE == dry / "trade_history.json"


def test_legacy_paper_state_in_live_root_is_never_treated_as_real(tmp_path):
    account, dry = _mode_dirs(tmp_path)
    (account / "straddle_state.json").write_text(
        json.dumps(_open_paper_state()), encoding="utf-8")
    with patch.multiple(
        bot,
        ACCOUNT_DIR=account,
        LIVE_DATA_DIR=account,
        DRY_DATA_DIR=dry,
    ):
        with bot._storage_namespace(False):
            assert bot.load_state() is None


def test_paper_close_uses_public_marks_and_only_dry_history(tmp_path):
    account, dry = _mode_dirs(tmp_path)
    with patch.multiple(
        bot,
        ACCOUNT_DIR=account,
        LIVE_DATA_DIR=account,
        DRY_DATA_DIR=dry,
        DRY_RUN=False,
    ), bot._storage_namespace(True):
        bot.save_state(_open_paper_state())
        with patch.object(bot, "get_mv_mark", return_value=125.0), \
             patch.object(bot, "get_btc_price", return_value=65000.0), \
             patch.object(bot, "get_mv_position") as get_position, \
             patch.object(bot, "place_market_order") as place_order, \
             patch.object(bot, "_post") as authenticated_post, \
             patch.object(bot, "_sign") as sign:
            result = bot.exit_job()

        assert result["dry_run"] is True
        get_position.assert_not_called()
        place_order.assert_not_called()
        authenticated_post.assert_not_called()
        sign.assert_not_called()

    closed = json.loads((dry / "straddle_state.json").read_text(encoding="utf-8"))
    history = json.loads((dry / "trade_history.json").read_text(encoding="utf-8"))
    assert closed["status"] == "CLOSED"
    assert closed["execution_mode"] == "dry_run"
    assert closed["pnl_usd"] == pytest.approx(0.25)
    assert closed["history_pending"] is False
    assert len(history) == 1
    assert history[0]["dry_run"] is True
    assert history[0]["execution_mode"] == "dry_run"
    assert history[0]["pnl_usd"] == pytest.approx(0.25)
    assert not (account / "trade_history.json").exists()


def test_real_close_is_not_simulated_when_process_mode_is_dry(tmp_path):
    account, dry = _mode_dirs(tmp_path)
    real_state = {
        **_open_paper_state(),
        "dry_run": False,
        "execution_mode": "real",
    }
    with patch.multiple(
        bot,
        ACCOUNT_DIR=account,
        LIVE_DATA_DIR=account,
        DRY_DATA_DIR=dry,
        DRY_RUN=True,
    ), bot._storage_namespace(False):
        bot.save_state(real_state)
        with patch.object(bot, "_close_position_job") as real_close, \
             patch.object(bot, "_close_dry_run_position_job") as paper_close:
            bot.exit_job()

    real_close.assert_called_once()
    paper_close.assert_not_called()


def test_start_monitor_keys_off_state_mode_not_process_mode(tmp_path):
    account, dry = _mode_dirs(tmp_path)
    with patch.multiple(
        bot,
        ACCOUNT_DIR=account,
        LIVE_DATA_DIR=account,
        DRY_DATA_DIR=dry,
        DRY_RUN=False,
    ), bot._storage_namespace(True):
        bot.save_state(_open_paper_state())
        with patch.object(bot.subprocess, "Popen") as spawn:
            assert bot.start_tp_monitor("evening") is True
        spawn.assert_not_called()


def test_dry_risk_uses_paper_rows_while_live_filter_stays_unchanged(tmp_path):
    today = datetime(2026, 7, 18, tzinfo=timezone.utc)
    (tmp_path / "trade_history.json").write_text(json.dumps([
        {
            "slot": "evening",
            "entry_date": "2026-07-18",
            "trading_date": "2026-07-18",
            "entry_time_utc": "12:05:00",
            "symbol": "MV-BTC-PAPER",
            "lots": 10,
            "dry_run": True,
            "pnl_usd": -5,
        }
    ]), encoding="utf-8")
    config = {"MAX_TRADES_PER_DAY_GLOBAL": 1, "MAX_OPEN_RISK_USD": 500}

    paper = evaluate_entry(
        tmp_path, 10, config, today, dry_run=True)
    live = evaluate_entry(
        tmp_path, 10, config, today)

    assert paper.allowed is False
    assert paper.trades_today == 1
    assert "daily trade cap" in paper.reason
    assert live.allowed is True
    assert live.trades_today == 0


def test_scheduled_entry_mode_is_rechecked_inside_account_lock(tmp_path):
    account, _ = _mode_dirs(tmp_path)
    config_path = account / "config.json"
    config_path.write_text(json.dumps({"DRY_RUN": "true"}), encoding="utf-8")
    events = []

    @contextmanager
    def entry_lock(path, owner):
        assert path == account
        events.append("lock")
        yield True

    def mode_check():
        events.append("mode")

    def entry():
        events.append("entry")
        return "ok"

    with patch.multiple(
        bot,
        ACCOUNT_DIR=account,
        CFG_FILE=config_path,
        PROCESS_DRY_RUN=True,
    ), patch.object(bot, "account_entry_lock", entry_lock), \
         patch.object(bot, "_assert_entry_mode_current", mode_check), \
         patch.object(bot, "_entry_job_locked", entry):
        assert bot.entry_job() == "ok"

    assert events == ["lock", "mode", "entry"]


def test_mode_change_blocks_new_entry_before_strategy_work(tmp_path):
    account, _ = _mode_dirs(tmp_path)
    config_path = account / "config.json"
    config_path.write_text(json.dumps({"DRY_RUN": "false"}), encoding="utf-8")
    with patch.multiple(
        bot,
        ACCOUNT_DIR=account,
        CFG_FILE=config_path,
        PROCESS_DRY_RUN=True,
    ):
        with pytest.raises(RuntimeError, match="bot reload is required"):
            bot._assert_entry_mode_current()
