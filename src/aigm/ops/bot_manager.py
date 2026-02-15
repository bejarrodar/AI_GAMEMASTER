from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

from aigm.config import settings
from aigm.db.models import BotConfig
from aigm.db.session import SessionLocal


def _safe_name(value: str) -> str:
    return "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in value).strip("_") or "bot"


@dataclass
class ManagedBotProc:
    key: str
    label: str
    popen: subprocess.Popen


class BotManager:
    def __init__(self, cwd: str, poll_s: int = 5) -> None:
        self.cwd = cwd
        self.poll_s = max(2, poll_s)
        self.procs: dict[str, ManagedBotProc] = {}
        self._stop = False

    def _db_enabled_configs(self) -> list[BotConfig]:
        with SessionLocal() as db:
            return db.query(BotConfig).filter(BotConfig.is_enabled.is_(True)).order_by(BotConfig.id.asc()).all()

    def _desired(self) -> list[tuple[str, str, str | None, int | None]]:
        rows = self._db_enabled_configs()
        desired: list[tuple[str, str, str | None, int | None]] = []
        for row in rows:
            if not row.discord_token.strip():
                continue
            desired.append((f"db:{row.id}", row.name.strip() or f"bot-{row.id}", row.discord_token.strip(), row.id))
        if not desired and settings.discord_token.strip():
            desired.append(("env:default", "default", settings.discord_token.strip(), None))
        return desired

    def _spawn(self, key: str, label: str, token: str, bot_config_id: int | None) -> ManagedBotProc:
        args = [sys.executable, "-m", "aigm.bot", "--token", token, "--bot-label", label]
        if bot_config_id is not None:
            args.extend(["--bot-config-id", str(bot_config_id)])
        proc = subprocess.Popen(
            args,
            cwd=self.cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        return ManagedBotProc(key=key, label=label, popen=proc)

    def _pipe_printer(self, proc: ManagedBotProc, stream, stream_name: str) -> None:
        try:
            for line in iter(stream.readline, ""):
                if not line:
                    break
                print(f"[bot-manager][{proc.label}][{stream_name}] {line.rstrip()}", flush=True)
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def _start_proc(self, key: str, label: str, token: str, bot_config_id: int | None) -> None:
        managed = self._spawn(key=key, label=label, token=token, bot_config_id=bot_config_id)
        self.procs[key] = managed
        threading.Thread(target=self._pipe_printer, args=(managed, managed.popen.stdout, "stdout"), daemon=True).start()
        threading.Thread(target=self._pipe_printer, args=(managed, managed.popen.stderr, "stderr"), daemon=True).start()
        print(f"[bot-manager] started bot '{label}' ({key})", flush=True)

    def _stop_proc(self, key: str) -> None:
        proc = self.procs.pop(key, None)
        if not proc:
            return
        if proc.popen.poll() is None:
            proc.popen.terminate()
            time.sleep(0.5)
            if proc.popen.poll() is None:
                proc.popen.kill()
        print(f"[bot-manager] stopped bot '{proc.label}' ({key})", flush=True)

    def request_stop(self, _sig=None, _frame=None) -> None:
        self._stop = True

    def run(self) -> int:
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)
        print("[bot-manager] starting", flush=True)
        while not self._stop:
            try:
                desired = self._desired()
            except Exception as exc:  # noqa: BLE001
                print(f"[bot-manager] failed loading bot configs: {exc}", flush=True)
                desired = []

            desired_map = {key: (label, token, bot_config_id) for key, label, token, bot_config_id in desired}

            for key in list(self.procs.keys()):
                proc = self.procs[key]
                if key not in desired_map:
                    self._stop_proc(key)
                    continue
                label, _, _ = desired_map[key]
                if proc.popen.poll() is not None:
                    print(f"[bot-manager] bot '{proc.label}' exited with code {proc.popen.poll()}, restarting", flush=True)
                    self._stop_proc(key)
                    token = desired_map[key][1]
                    bot_config_id = desired_map[key][2]
                    if token:
                        self._start_proc(key, label, token, bot_config_id)

            for key, (label, token, bot_config_id) in desired_map.items():
                if key in self.procs:
                    continue
                if token:
                    self._start_proc(key, label, token, bot_config_id)

            time.sleep(self.poll_s)

        for key in list(self.procs.keys()):
            self._stop_proc(key)
        print("[bot-manager] stopped", flush=True)
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run multiple Discord bots from DB bot_configs table.")
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--poll-s", type=int, default=5)
    args = parser.parse_args()
    manager = BotManager(cwd=args.cwd, poll_s=args.poll_s)
    return manager.run()


if __name__ == "__main__":
    raise SystemExit(main())

