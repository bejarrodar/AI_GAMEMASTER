from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer
from urllib import error, request

from aigm.ops import db_api


class _State:
    def auth_ok(self, authorization: str) -> bool:
        return authorization == "Bearer ok"

    def health(self) -> dict:
        return {"ok": True, "checks": {"db": {"ok": True, "detail": "reachable"}}}

    def list_bots(self, enabled: bool | None = None) -> list[dict]:
        return [{"id": 1, "name": "default", "discord_token": "abc", "is_enabled": enabled if enabled is not None else True}]

    def create_bot(self, _payload: dict) -> dict:
        return {"id": 2, "name": "new"}

    def update_bot(self, bot_id: int, _payload: dict) -> dict:
        return {"id": bot_id, "name": "updated"}

    def delete_bot(self, bot_id: int) -> dict:
        return {"id": bot_id, "deleted": True}

    def list_system_logs(self, limit: int, service: str, level: str) -> list[dict]:
        return [{"id": 1, "limit": limit, "service": service, "level": level}]

    def list_audit_logs(self, limit: int) -> list[dict]:
        return [{"id": 1, "limit": limit}]

    def table_counts(self) -> dict:
        return {"campaigns": 1, "players": 1, "turn_logs": 1, "bot_configs": 1, "system_logs": 1}


def _req(url: str, method: str = "GET", payload: dict | None = None) -> dict:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = request.Request(
        url,
        method=method,
        data=body,
        headers={"Authorization": "Bearer ok", "Content-Type": "application/json"},
    )
    with request.urlopen(req, timeout=3) as resp:
        return json.loads(resp.read().decode("utf-8"))


def test_db_api_requires_auth() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), db_api.make_handler(_State()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        try:
            with request.urlopen(f"http://127.0.0.1:{server.server_port}/db/v1/health", timeout=3):
                pass
            raise AssertionError("expected unauthorized response")
        except error.HTTPError as exc:
            assert exc.code == 401
    finally:
        server.shutdown()
        server.server_close()


def test_db_api_bots_and_health_endpoints() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), db_api.make_handler(_State()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        health = _req(f"{base}/db/v1/health")
        assert health["ok"] is True
        bots = _req(f"{base}/db/v1/bots?enabled=true")
        assert bots["ok"] is True
        assert bots["rows"][0]["is_enabled"] is True
        created = _req(f"{base}/db/v1/bots", method="POST", payload={"name": "x", "discord_token": "y"})
        assert created["id"] == 2
    finally:
        server.shutdown()
        server.server_close()
