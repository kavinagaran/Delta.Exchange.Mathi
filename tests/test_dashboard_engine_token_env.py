"""Dashboard and Trend Engine must resolve duplicate token rows identically."""
from pathlib import Path

import dashboard


def test_dashboard_uses_the_engine_first_declaration_for_duplicate_token(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "OTHER=value\nENGINE_TOKEN = engine-first\nENGINE_TOKEN=dashboard-last\n",
        encoding="utf-8",
    )

    assert dashboard._first_dotenv_value(env_file, "ENGINE_TOKEN") == "engine-first"
    assert dashboard._first_dotenv_value(env_file, "MISSING") is None
