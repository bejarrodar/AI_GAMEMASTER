from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from urllib import request

from aigm.ops import db_api, supervisor
from aigm.ops.db_api_client import DBApiClient


class _InMemoryDBState:
    def __init__(self) -> None:
        self._token = "dbtok"
        self._lock = threading.Lock()
        self._next_id = 1
        self._bots: list[dict] = []
        self._syslogs: list[dict] = []

    def auth_ok(self, authorization: str) -> bool:
        return (authorization or "").strip() == f"Bearer {self._token}"

    def health(self) -> dict:
        return {"ok": True, "checks": {"db": {"ok": True, "detail": "reachable"}}}

    def list_bots(self, enabled: bool | None = None) -> list[dict]:
        with self._lock:
            rows = list(self._bots)
        if enabled is not None:
            rows = [r for r in rows if bool(r.get("is_enabled", False)) is bool(enabled)]
        return rows

    def create_bot(self, payload: dict) -> dict:
        with self._lock:
            row = {
                "id": self._next_id,
                "name": str(payload.get("name", "")).strip(),
                "discord_token": str(payload.get("discord_token", "")).strip(),
                "is_enabled": bool(payload.get("is_enabled", True)),
                "notes": str(payload.get("notes", "")),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            self._next_id += 1
            self._bots.append(row)
            self._syslogs.append(
                {
                    "id": len(self._syslogs) + 1,
                    "service": "bot-manager",
                    "level": "INFO",
                    "message": f"created bot {row['name']}",
                    "source": "integration_test",
                    "metadata": {"bot_id": row["id"]},
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        return {"id": row["id"], "name": row["name"]}

    def update_bot(self, bot_id: int, payload: dict) -> dict:
        with self._lock:
            row = next((r for r in self._bots if int(r["id"]) == int(bot_id)), None)
            if row is None:
                raise ValueError("bot config not found")
            if "name" in payload and str(payload["name"]).strip():
                row["name"] = str(payload["name"]).strip()
            if "discord_token" in payload and str(payload["discord_token"]).strip():
                row["discord_token"] = str(payload["discord_token"]).strip()
            if "is_enabled" in payload:
                row["is_enabled"] = bool(payload["is_enabled"])
            if "notes" in payload:
                row["notes"] = str(payload["notes"])
            row["updated_at"] = datetime.now(timezone.utc).isoformat()
            self._syslogs.append(
                {
                    "id": len(self._syslogs) + 1,
                    "service": "bot-manager",
                    "level": "INFO",
                    "message": f"updated bot {row['name']}",
                    "source": "integration_test",
                    "metadata": {"bot_id": row["id"]},
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        return {"id": row["id"], "name": row["name"]}

    def delete_bot(self, bot_id: int) -> dict:
        with self._lock:
            idx = next((i for i, r in enumerate(self._bots) if int(r["id"]) == int(bot_id)), None)
            if idx is None:
                raise ValueError("bot config not found")
            row = self._bots.pop(idx)
            self._syslogs.append(
                {
                    "id": len(self._syslogs) + 1,
                    "service": "bot-manager",
                    "level": "INFO",
                    "message": f"deleted bot {row['name']}",
                    "source": "integration_test",
                    "metadata": {"bot_id": row["id"]},
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        return {"id": int(bot_id), "deleted": True}

    def list_system_logs(self, limit: int, service: str, level: str) -> list[dict]:
        with self._lock:
            rows = list(self._syslogs)
        if service:
            rows = [r for r in rows if str(r.get("service", "")) == service]
        if level:
            rows = [r for r in rows if str(r.get("level", "")).upper() == level.upper()]
        return rows[-max(1, int(limit)) :][::-1]

    def list_audit_logs(self, limit: int) -> list[dict]:
        return [{"id": 1, "action": "integration_test", "limit": int(limit)}]


class _Logger:
    def write(self, *_args, **_kwargs):
        return


class _HealthState:
    def snapshot(self) -> dict:
        return {
            "ok": True,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "checks": {
                "db": {"ok": True, "detail": "reachable"},
                "ollama": {"ok": True, "detail": "reachable"},
                "streamlit": {"ok": True, "detail": "reachable"},
            },
        }


class _IntegrationManagementState:
    def __init__(self, db_api_url: str, db_api_token: str) -> None:
        self.api_token = ""
        self.logger = _Logger()
        self.health_state = _HealthState()
        self.streamlit_port = 9531
        self.health_port = 9540
        self.db_api = DBApiClient(db_api_url, token=db_api_token, timeout_s=5)

    def auth_ok(self, _authorization: str) -> bool:
        return True

    def list_bot_configs(self) -> list[dict]:
        rows = self.db_api.list_bots(enabled_only=None)
        return [
            {
                "id": int(r.get("id", 0)),
                "name": str(r.get("name", "")),
                "is_enabled": bool(r.get("is_enabled", False)),
                "notes": str(r.get("notes", "")),
                "token_masked": "***",
            }
            for r in rows
        ]

    def create_bot_config(self, payload: dict) -> dict:
        return self.db_api.create_bot(payload)

    def update_bot_config(self, bot_id: int, payload: dict) -> dict:
        return self.db_api.update_bot(int(bot_id), payload)

    def delete_bot_config(self, bot_id: int) -> dict:
        return self.db_api.delete_bot(int(bot_id))

    def get_system_logs(self, *, limit: int, service: str, level: str) -> list[dict]:
        return self.db_api.list_system_logs(limit=limit, service=service, level=level)

    def get_audit_logs(self, *, limit: int) -> list[dict]:
        return self.db_api.list_audit_logs(limit=limit)

    def get_llm_config(self) -> dict:
        return {"runtime": {"provider": "stub"}, "persisted_env": {}}

    def get_web_config(self) -> dict:
        return {"runtime": {"streamlit_port": 9531}, "persisted_env": {}}

    def check_db(self) -> tuple[bool, str]:
        payload = self.db_api.health()
        ok = bool(payload.get("ok", False))
        return ok, "reachable" if ok else "unreachable"

    def check_ollama(self) -> tuple[bool, str]:
        return True, "reachable"

    def check_openai(self) -> tuple[bool, str]:
        return False, "not_configured"


def _mreq(base: str, path: str, method: str = "GET", payload: dict | None = None) -> dict:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = request.Request(
        f"{base}{path}",
        method=method,
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with request.urlopen(req, timeout=4) as resp:
        return json.loads(resp.read().decode("utf-8"))


def test_management_db_api_bot_crud_health_logs_integration() -> None:
    db_state = _InMemoryDBState()
    db_server = ThreadingHTTPServer(("127.0.0.1", 0), db_api.make_handler(db_state))
    db_thread = threading.Thread(target=db_server.serve_forever, daemon=True)
    db_thread.start()
    mgmt_state = _IntegrationManagementState(
        db_api_url=f"http://127.0.0.1:{db_server.server_port}",
        db_api_token="dbtok",
    )
    mgmt_server = ThreadingHTTPServer(("127.0.0.1", 0), supervisor.make_management_handler(mgmt_state))
    mgmt_thread = threading.Thread(target=mgmt_server.serve_forever, daemon=True)
    mgmt_thread.start()
    try:
        base = f"http://127.0.0.1:{mgmt_server.server_port}"
        created = _mreq(
            base,
            "/api/v1/bots",
            method="POST",
            payload={"name": "integration-bot", "discord_token": "tok", "is_enabled": True, "notes": "n"},
        )
        assert created["ok"] is True
        bot_id = int(created["created"]["id"])

        listed = _mreq(base, "/api/v1/bots")
        assert listed["ok"] is True
        assert any(str(r.get("name", "")) == "integration-bot" for r in listed["bots"])

        updated = _mreq(
            base,
            f"/api/v1/bots/{bot_id}",
            method="PUT",
            payload={"name": "integration-bot", "discord_token": "tok", "is_enabled": False, "notes": "n2"},
        )
        assert updated["ok"] is True

        listed_after = _mreq(base, "/api/v1/bots")
        row = next(r for r in listed_after["bots"] if int(r["id"]) == bot_id)
        assert bool(row["is_enabled"]) is False

        logs = _mreq(base, "/api/v1/logs/system?limit=10&service=bot-manager")
        assert logs["ok"] is True
        assert isinstance(logs["rows"], list)
        assert len(logs["rows"]) >= 1

        health = _mreq(base, "/api/v1/health")
        assert health["ok"] is True
        assert set(health["health"]["checks"].keys()) >= {"db", "ollama", "streamlit"}

        deleted = _mreq(base, f"/api/v1/bots/{bot_id}", method="DELETE")
        assert deleted["ok"] is True

        listed_final = _mreq(base, "/api/v1/bots")
        assert all(int(r["id"]) != bot_id for r in listed_final["bots"])
    finally:
        mgmt_server.shutdown()
        mgmt_server.server_close()
        db_server.shutdown()
        db_server.server_close()
