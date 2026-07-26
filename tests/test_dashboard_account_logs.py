import json

import dashboard


def test_logs_use_the_active_accounts_audit_feed(tmp_path, monkeypatch):
    users = tmp_path / "users"
    audit = users / "nithi" / "strategy_audit.jsonl"
    audit.parent.mkdir(parents=True)
    audit.write_text(
        "\n".join([
            json.dumps({
                "at_utc": "2026-07-26T14:00:00Z",
                "event": "trend_score_auto_entry_opened",
                "zone": "CE_2_ITM",
                "symbol": "C-BTC-64200-270726",
                "lots": 1000,
                "direction_score": 36.5,
                "exchange_api_called": False,
            }),
            json.dumps({
                "at_utc": "2026-07-26T14:05:00Z",
                "event": "trend_score_auto_error",
                "error": "score +23.0 is in the hold band; no new action",
            }),
            "not-json",
        ]) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(dashboard, "USERS_DIR", users)
    monkeypatch.setattr(dashboard, "DASH_USER", "nithi")
    monkeypatch.setattr(dashboard, "BOT_USER", "nithi")

    with dashboard.app.test_request_context("/api/logs?n=100"):
        payload = dashboard.api_logs().get_json()

    assert payload["source"] == "account_activity"
    assert payload["lines"] == [
        "2026-07-26 07:30:00 PM IST · TRADE OPENED · CE 2 ITM · "
        "C-BTC-64200-270726 · 1,000 lots · score +36.5 · DRY RUN",
        "2026-07-26 07:35:00 PM IST · HOLD · score +23.0 is in the hold band; no new action",
    ]


def test_logs_page_explains_empty_account_activity():
    source = (dashboard.BASE / "templates" / "logs.html").read_text(
        encoding="utf-8")

    assert "Account activity" in source
    assert "No account activity yet" in source
    assert "log-context" in source
