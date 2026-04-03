from __future__ import annotations

import json
from pathlib import Path

from aigm import config as config_module
from aigm.config import Settings


def test_settings_loads_external_secrets_from_json_file(tmp_path: Path, monkeypatch) -> None:
    secret_file = tmp_path / "secrets.json"
    secret_file.write_text(
        json.dumps(
            {
                "AIGM_DISCORD_TOKEN": "discord-from-json",
                "openai_api_key": "openai-from-json",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AIGM_SECRET_SOURCE", "json_file")
    monkeypatch.setenv("AIGM_SECRET_SOURCE_JSON_FILE", str(secret_file))
    monkeypatch.delenv("AIGM_DISCORD_TOKEN", raising=False)
    monkeypatch.delenv("AIGM_OPENAI_API_KEY", raising=False)

    cfg = Settings(_env_file=None)
    assert cfg.discord_token == "discord-from-json"
    assert cfg.openai_api_key == "openai-from-json"


def test_settings_loads_external_secrets_from_command(monkeypatch) -> None:
    class _Proc:
        def __init__(self, stdout: str) -> None:
            self.stdout = stdout

    def _fake_run(*_args, **_kwargs):
        return _Proc('{"sys_admin_token":"admin-from-cmd","AIGM_DATABASE_URL":"sqlite:///x.db"}')

    monkeypatch.setattr(config_module.subprocess, "run", _fake_run)
    monkeypatch.setenv("AIGM_SECRET_SOURCE", "command")
    monkeypatch.setenv("AIGM_SECRET_SOURCE_COMMAND", "echo mocked")
    monkeypatch.delenv("AIGM_SYS_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("AIGM_DATABASE_URL", raising=False)

    cfg = Settings(_env_file=None)
    assert cfg.sys_admin_token == "admin-from-cmd"
    assert cfg.database_url == "sqlite:///x.db"

