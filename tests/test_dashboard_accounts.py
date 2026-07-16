import json
from pathlib import Path

import dashboard


def _write_account(users: Path, username: str, display_name: str) -> None:
    directory = users / username
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "account.json").write_text(json.dumps({
        "username": username,
        "display_name": display_name,
        "pw_hash": "test-only",
        "api_key": f"key-{username}",
        "api_secret": f"secret-{username}",
    }), encoding="utf-8")


def test_primary_account_is_explicit_and_coexistent_account_keeps_bot_identity(
        tmp_path, monkeypatch):
    users = tmp_path / "users"
    users.mkdir()
    _write_account(users, "mathi", "Mathi")
    _write_account(users, "nithi", "Nithiyanandam")
    monkeypatch.setattr(dashboard, "USERS_DIR", users)
    monkeypatch.setattr(dashboard, "DASH_USER", "mathi")
    monkeypatch.setattr(dashboard, "BOT_USER", "mathi")
    monkeypatch.setenv("PRIMARY_ACCOUNT_USER", "nithi")

    with dashboard.app.test_request_context("/api/accounts"):
        rows = dashboard.api_accounts_list().get_json()

    assert [row["username"] for row in rows] == ["nithi", "mathi"]
    assert rows[0]["primary"] is True
    assert rows[0]["role"] == "primary"
    assert rows[0]["bot"] is False
    assert rows[1]["primary"] is False
    assert rows[1]["role"] == "coexistent"
    assert rows[1]["bot"] is True


def test_primary_account_cannot_be_deleted(tmp_path, monkeypatch):
    users = tmp_path / "users"
    users.mkdir()
    _write_account(users, "nithi", "Nithiyanandam")
    monkeypatch.setattr(dashboard, "USERS_DIR", users)
    monkeypatch.setattr(dashboard, "BOT_USER", "mathi")
    monkeypatch.setenv("PRIMARY_ACCOUNT_USER", "nithi")

    with dashboard.app.test_request_context("/api/accounts/nithi", method="DELETE"):
        response, status = dashboard.api_accounts_delete("nithi")

    assert status == 400
    assert response.get_json()["error"] == "The primary account cannot be deleted"
    assert (users / "nithi" / "account.json").exists()


def test_primary_role_defaults_to_bot_user(monkeypatch):
    monkeypatch.delenv("PRIMARY_ACCOUNT_USER", raising=False)
    monkeypatch.setattr(dashboard, "BOT_USER", "mathi")
    monkeypatch.setattr(dashboard, "DASH_USER", "other")

    assert dashboard._primary_account_user() == "mathi"


def test_accounts_page_renders_primary_and_coexistent_roles():
    html = (Path(dashboard.BASE) / "templates" / "accounts.html").read_text(
        encoding="utf-8")

    assert "<th>Role</th>" in html
    assert "PRIMARY</span>" in html
    assert "COEXISTENT</span>" in html
    assert "a.primary || a.bot" in html
