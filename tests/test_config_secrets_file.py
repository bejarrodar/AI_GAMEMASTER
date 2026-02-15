from __future__ import annotations

from pathlib import Path

from aigm.config import Settings


def test_settings_loads_discord_token_from_file_env(tmp_path: Path, monkeypatch) -> None:
    secret_file = tmp_path / "discord_token.txt"
    secret_file.write_text("token-from-file\n", encoding="utf-8")
    monkeypatch.setenv("AIGM_DISCORD_TOKEN_FILE", str(secret_file))
    monkeypatch.delenv("AIGM_DISCORD_TOKEN", raising=False)

    cfg = Settings(_env_file=None)
    assert cfg.discord_token == "token-from-file"

