from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

import Delta_Straddle_Live as bot


@pytest.fixture
def telegram_ledger(tmp_path):
    bot._TELEGRAM_EVENT_MEMORY.clear()
    with patch.object(bot, "ACCOUNT_DIR", tmp_path), \
         patch.object(bot, "TELEGRAM_EVENT_FILE", tmp_path / "telegram_event_alerts.json"), \
         patch.object(bot, "TELEGRAM_ON", True), \
         patch.object(bot, "TELEGRAM_TOKEN", "token"), \
         patch.object(bot, "TELEGRAM_CHATID", "chat"):
        yield tmp_path
    bot._TELEGRAM_EVENT_MEMORY.clear()


def test_same_entry_event_alerts_once_even_when_error_changes(telegram_ledger):
    key = bot._entry_failure_event_key(
        "morning", "2026-07-22", "sideways")
    with patch.object(bot, "send_telegram", return_value=True) as send:
        assert bot.send_telegram_once(key, "portfolio risk blocked") is True
        assert bot.send_telegram_once(key, "no executable depth") is False

    send.assert_called_once_with("portfolio risk blocked")


def test_event_claim_survives_process_memory_reset(telegram_ledger):
    key = bot._entry_failure_event_key(
        "morning", "2026-07-22", "sideways")
    with patch.object(bot, "send_telegram", return_value=True) as send:
        assert bot.send_telegram_once(key, "first") is True
        bot._TELEGRAM_EVENT_MEMORY.clear()  # Simulates a service restart.
        assert bot.send_telegram_once(key, "retry after restart") is False

    send.assert_called_once_with("first")


def test_new_date_is_a_new_entry_event(telegram_ledger):
    first = bot._entry_failure_event_key(
        "morning", "2026-07-22", "sideways")
    following = bot._entry_failure_event_key(
        "morning", "2026-07-23", "sideways")
    with patch.object(bot, "send_telegram", return_value=True) as send:
        assert bot.send_telegram_once(first, "day one") is True
        assert bot.send_telegram_once(following, "day two") is True

    assert send.call_count == 2


def test_failed_delivery_remains_claimed_for_at_most_once_policy(telegram_ledger):
    key = bot._entry_failure_event_key(
        "evening", "2026-07-22", "scheduled")
    with patch.object(bot, "send_telegram", return_value=False) as send:
        assert bot.send_telegram_once(key, "network timeout") is False
        assert bot.send_telegram_once(key, "retry") is False

    send.assert_called_once_with("network timeout")


def test_concurrent_claimers_send_only_one_alert(telegram_ledger):
    key = bot._entry_failure_event_key(
        "morning", "2026-07-22", "scheduled")
    with patch.object(bot, "send_telegram", return_value=True) as send:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda text: bot.send_telegram_once(key, text),
                ("worker one", "worker two"),
            ))

    assert sorted(results) == [False, True]
    assert send.call_count == 1


def test_position_failure_key_is_stable_across_partial_exit_retries():
    state = {
        "product_id": 91,
        "entry_date": "2026-07-22",
        "entry_time_utc": "04:15:00",
        "symbol": "MV-BTC-TEST",
        "lots": 1000,
    }
    first = bot._position_failure_event_key(
        "exit-failed", "morning", False, state)
    state["lots"] = 400
    second = bot._position_failure_event_key(
        "exit-failed", "morning", False, state)

    assert first == second


def test_replacement_pending_journal_creates_a_new_event():
    original = {
        "started_at_utc": "2026-07-22T04:15:00+00:00",
        "product_id": 91,
        "symbol": "MV-BTC-TEST",
        "side": "sell",
        "requested_lots": 1000,
    }
    replacement = dict(
        original, started_at_utc="2026-07-22T05:15:00+00:00")

    assert bot._pending_entry_event_key(
        "pending-entry-recovery-failed", "morning", original
    ) != bot._pending_entry_event_key(
        "pending-entry-recovery-failed", "morning", replacement
    )
