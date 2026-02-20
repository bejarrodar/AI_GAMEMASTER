from __future__ import annotations

import asyncio
from dataclasses import dataclass

import discord

from aigm import bot as bot_module


@dataclass
class _Campaign:
    id: int = 1
    mode: str = "dnd"
    state: dict = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.state is None:
            self.state = {"scene": "test"}


@dataclass
class _Player:
    id: int = 1


class _DummySession:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None


class _Author:
    def __init__(self, user_id: str = "u1", name: str = "Tester") -> None:
        self.bot = False
        self.id = user_id
        self.display_name = name


class _ThreadChannel:
    def __init__(self) -> None:
        self.type = discord.ChannelType.public_thread
        self.id = 12345
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(str(text))


class _Message:
    def __init__(self, content: str, channel: _ThreadChannel, author: _Author | None = None) -> None:
        self.content = content
        self.channel = channel
        self.author = author or _Author()
        self.id = 999


class _LLMStub:
    def __init__(self, matched_command: str | None = None) -> None:
        self._matched = matched_command

    def infer_discord_command(self, _content: str, _possible: list[str]) -> dict:
        return {"matched_command": self._matched, "confidence": 0.9, "reason": "test"}


class _ServiceStub:
    def __init__(self, *, game_started: bool, suggestion: str | None = None) -> None:
        self._game_started = game_started
        self.llm = _LLMStub(matched_command=suggestion)

    def seed_default_gameplay_knowledge(self, _db) -> None:
        return

    def get_or_create_campaign(self, _db, thread_id: str, mode: str, thread_name: str):  # noqa: ARG002
        return _Campaign()

    def reserve_discord_message_idempotency(self, _db, campaign, discord_message_id: str, actor_discord_user_id: str):  # noqa: ARG002
        return True

    def ensure_player(self, _db, campaign, actor_id: str, actor_display_name: str):  # noqa: ARG002
        return _Player()

    def list_rules(self, _db, _campaign) -> dict[str, str]:
        return {"game_started": "true" if self._game_started else "false"}


def test_possible_commands_contains_expected_surface() -> None:
    cmds = bot_module._possible_commands()
    assert "!gmhelp" in cmds
    assert "!startgame" in cmds
    assert "!startstory" in cmds
    assert "!help" not in cmds


def test_discord_contract_gmhelp_response(monkeypatch) -> None:
    monkeypatch.setattr(bot_module, "SessionLocal", lambda: _DummySession())
    monkeypatch.setattr(bot_module, "service", _ServiceStub(game_started=False))
    bot_module.authenticated_admin_ids.clear()
    channel = _ThreadChannel()
    msg = _Message("!gmhelp", channel)
    asyncio.run(bot_module.on_message(msg))
    assert channel.sent
    payload = channel.sent[-1]
    assert "GameMaster Commands" in payload
    assert "!gmhelp" in payload
    assert "!startgame" in payload


def test_discord_contract_rejects_game_commands_before_start(monkeypatch) -> None:
    monkeypatch.setattr(bot_module, "SessionLocal", lambda: _DummySession())
    monkeypatch.setattr(bot_module, "service", _ServiceStub(game_started=False))
    bot_module.authenticated_admin_ids.clear()
    channel = _ThreadChannel()
    msg = _Message("!roll d20", channel)
    asyncio.run(bot_module.on_message(msg))
    assert channel.sent
    assert "Use `!startgame` or `!startstory` first." in channel.sent[-1]


def test_discord_contract_unknown_command_suggests_closest(monkeypatch) -> None:
    monkeypatch.setattr(bot_module, "SessionLocal", lambda: _DummySession())
    monkeypatch.setattr(bot_module, "service", _ServiceStub(game_started=True, suggestion="!mycharacter"))
    bot_module.authenticated_admin_ids.clear()
    channel = _ThreadChannel()
    msg = _Message("!mycharcter I am a rogue", channel)
    asyncio.run(bot_module.on_message(msg))
    assert channel.sent
    payload = channel.sent[-1]
    assert "Unknown command `!mycharcter`." in payload
    assert "Closest valid command: `!mycharacter`" in payload


def test_typing_indicator_loop_triggers_typing_until_stopped() -> None:
    class _TypingChannel:
        def __init__(self) -> None:
            self.calls = 0

        async def trigger_typing(self):
            self.calls += 1

    async def _run() -> int:
        channel = _TypingChannel()
        stop = asyncio.Event()
        task = asyncio.create_task(bot_module._typing_indicator_loop(channel, stop, interval_s=0.05))
        await asyncio.sleep(0.12)
        stop.set()
        await task
        return channel.calls

    calls = asyncio.run(_run())
    assert calls >= 1
