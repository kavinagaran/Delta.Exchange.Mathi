"""M/A/E trade-origin labeling across tracked, paper and exchange trades."""
import json
from pathlib import Path

import pytest

import dashboard

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def isolated_user(tmp_path, monkeypatch):
    users = tmp_path / "users"
    account = users / "alice"
    (account / "dry_run").mkdir(parents=True)
    monkeypatch.setattr(dashboard, "USERS_DIR", users)
    monkeypatch.setattr(dashboard, "DASH_USER", "alice")
    monkeypatch.setattr(dashboard, "BOT_USER", "alice")
    return account


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text(json.dumps(rows), encoding="utf-8")


# ── Classification ─────────────────────────────────────────────────────


@pytest.mark.parametrize("record,label", [
    ({"entry_trigger": "manual_cockpit_buy_ce"}, "M"),
    ({"entry_trigger": "manual_cockpit_sell_move"}, "M"),
    ({"entry_classification": "manual_cockpit"}, "M"),
    ({"ownership": "manual_cockpit_live"}, "M"),
    ({"ownership": "manual_cockpit_dry_run"}, "M"),
    ({"signal_key": "manual-cockpit|buy_pe|abcd"}, "M"),
    ({"entry_trigger": dashboard.TREND_SCORE_AUTO_TRIGGER}, "A"),
    ({"entry_trigger": "trend_engine_phase1_confirmed"}, "A"),
    ({"entry_trigger": "trend_auto"}, "A"),
    ({"entry_trigger": "trend_alignment"}, "A"),
    ({"ownership": "trend_bot"}, "A"),
    ({"ownership": "trend_engine_dry_run"}, "A"),
    ({"ownership": "trend_score_auto_live"}, "A"),
    ({"entry_trigger": "exchange_sync"}, "E"),
    ({"entry_trigger": "trend_recovered"}, "E"),
    ({"ownership": "external"}, "E"),
    ({"symbol": "MV-BTC-112000-020926"}, "A"),
    ({"symbol": "C-BTC-112000-020926"}, "—"),
    ({}, "—"),
    (None, "—"),
])
def test_trade_origin_label(record, label):
    assert dashboard._trade_origin_label(record) == label


# ── API injection ──────────────────────────────────────────────────────


def test_merged_trades_carry_origin_labels(isolated_user, monkeypatch):
    manual = {
        "symbol": "C-BTC-112000-020926", "entry_date": "2026-09-01",
        "entry_time_utc": "2026-09-01T06:00:00Z",
        "entry_trigger": "manual_cockpit_buy_ce", "pnl_usd": 5.0,
    }
    synced = {
        "symbol": "P-BTC-111000-020926", "entry_date": "2026-09-01",
        "entry_time_utc": "2026-09-01T07:00:00Z",
        "entry_trigger": "exchange_sync", "pnl_usd": -2.0,
    }
    legacy_mv = {
        "symbol": "MV-BTC-111500-020926", "entry_date": "2026-09-01",
        "entry_time_utc": "04:36:36", "pnl_usd": 1.0,
    }
    _write(isolated_user / "trade_history.json", [manual, synced, legacy_mv])
    untracked = {
        "symbol": "C-BTC-113000-020926", "product_id": 1,
        "date": "2026-09-01", "entry_time": "09:00:00",
        "exit_time": "10:00:00", "side": "LONG", "lots": 10.0,
        "entry_mark": 100.0, "exit_mark": 90.0, "pnl_usd": -1.0,
    }
    monkeypatch.setattr(
        dashboard, "_fetch_reconstructed_trades", lambda: [untracked])

    merged = dashboard._all_trades_merged()
    labels = {t["symbol"]: t["origin_label"] for t in merged}

    assert labels == {
        "C-BTC-112000-020926": "M",
        "P-BTC-111000-020926": "E",
        "MV-BTC-111500-020926": "A",
        "C-BTC-113000-020926": "M",
    }


def test_dry_run_trades_carry_origin_labels(isolated_user, monkeypatch):
    rows = [
        {"symbol": "C-BTC-112000-020926", "entry_date": "2026-09-01",
         "entry_time_utc": "2026-09-01T05:00:00Z", "execution_mode": "dry_run",
         "entry_trigger": "manual_cockpit_buy_ce", "pnl_usd": 3.0},
        {"symbol": "P-BTC-111000-020926", "entry_date": "2026-09-01",
         "entry_time_utc": "2026-09-01T05:30:00Z", "execution_mode": "dry_run",
         "entry_trigger": dashboard.TREND_SCORE_AUTO_TRIGGER, "pnl_usd": -1.0},
    ]
    _write(isolated_user / "dry_run" / "trade_history.json", rows)
    monkeypatch.setattr(dashboard, "_import_legacy_dry_records", lambda: None)

    dry = dashboard._dry_run_trades()

    assert {t["symbol"]: t["origin_label"] for t in dry} == {
        "C-BTC-112000-020926": "M",
        "P-BTC-111000-020926": "A",
    }


# ── Performance ledger join ────────────────────────────────────────────


def test_performance_index_and_delta_row_labels(isolated_user):
    tracked_auto = {
        "symbol": "C-BTC-112000-020926", "entry_date": "2026-09-01",
        "entry_time_utc": "06:00:10",
        "entry_trigger": dashboard.TREND_SCORE_AUTO_TRIGGER,
    }
    tracked_manual = {
        "symbol": "P-BTC-111000-020926", "entry_date": "2026-09-01",
        "entry_time_utc": "07:15:00",
        "entry_trigger": "manual_cockpit_buy_pe",
    }
    _write(isolated_user / "trade_history.json",
           [tracked_auto, tracked_manual])

    index = dashboard._performance_origin_index()

    def label(symbol, entry_at_utc, date="2026-09-01"):
        return dashboard._delta_row_origin_label(
            {"symbol": symbol, "date": date, "entry_at_utc": entry_at_utc},
            index)

    # ±5s clock match inherits the tracked record's label.
    assert label("C-BTC-112000-020926", "2026-09-01T06:00:12Z") == "A"
    assert label("P-BTC-111000-020926", "2026-09-01T07:14:58Z") == "M"
    # Outside the tolerance window there is no match.
    assert label("C-BTC-112000-020926", "2026-09-01T06:01:12Z") == "M"
    # Unmatched MOVE rows belong to the legacy straddle bot.
    assert label("MV-BTC-111500-020926", "2026-09-01T08:00:00Z") == "A"
    # Anything else on the ledger was placed outside the automation.
    assert label("C-BTC-113000-020926", "2026-09-01T09:00:00Z") == "M"


def test_performance_endpoint_returns_origin_labels(isolated_user, monkeypatch):
    _write(isolated_user / "trade_history.json", [])
    fills = [{
        "product_symbol": "C-BTC-112000-020926",
        "product_id": 1, "side": "buy", "size": "10", "price": "100",
        "created_at": "1788609600000000",  # 2026-09-05T12:00:00Z
    }, {
        "product_symbol": "MV-BTC-111500-020926",
        "product_id": 2, "side": "buy", "size": "5", "price": "50",
        "created_at": "1788613200000000",  # 2026-09-05T13:00:00Z
    }]
    monkeypatch.setattr(
        dashboard, "_fetch_complete_delta_fills", lambda: fills)
    monkeypatch.setattr(
        dashboard, "_delta_contract_value", lambda product_id: 0.001)
    monkeypatch.setattr(
        dashboard, "_find_account", lambda username: {"username": username})

    with dashboard.app.test_client() as client:
        with client.session_transaction() as sess:
            sess["user"] = "alice"
        payload = client.get("/api/performance/delta-trades").get_json()

    assert payload["ok"] is True
    labels = {row["symbol"]: row["origin_label"] for row in payload["records"]}
    assert labels["C-BTC-112000-020926"] == "M"
    assert labels["MV-BTC-111500-020926"] == "A"


# ── Display wiring ─────────────────────────────────────────────────────


def test_web_surfaces_render_origin_badges():
    app_js = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
    app_css = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")
    assert "function originBadge(trade)" in app_js
    assert "ORIGIN_BADGE_META" in app_js
    assert ".badge.origin.manual" in app_css
    assert ".badge.origin.auto" in app_css
    for template in ("trades.html", "dry_run.html", "overview.html"):
        html = (ROOT / "templates" / template).read_text(encoding="utf-8")
        assert "originBadge(trade)" in html, template


def test_flutter_surfaces_render_origin_chips():
    kit = (ROOT / "mv_btc_bot" / "lib" / "widgets" / "kit.dart") \
        .read_text(encoding="utf-8")
    assert "class OriginChip" in kit
    assert "origin_label" in kit
    for screen in ("performance_screen.dart", "today_screen.dart",
                   "dry_run_screen.dart"):
        dart = (ROOT / "mv_btc_bot" / "lib" / "screens" / screen) \
            .read_text(encoding="utf-8")
        assert "OriginChip.forTrade" in dart, screen
