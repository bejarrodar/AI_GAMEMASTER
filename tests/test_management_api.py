from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer
from urllib import error, request

from aigm.ops import supervisor


class _Logger:
    def write(self, *_args, **_kwargs):
        return


class _HealthState:
    def snapshot(self):
        return {
            "ok": True,
            "timestamp": "2026-02-15T00:00:00Z",
            "checks": {
                "db": {"ok": True, "detail": "reachable"},
                "ollama": {"ok": True, "detail": "reachable"},
                "streamlit": {"ok": True, "detail": "reachable"},
            },
        }


class _State:
    def __init__(self):
        self.api_token = ""
        self.logger = _Logger()
        self.health_state = _HealthState()
        self.streamlit_port = 9531
        self.health_port = 9540
        self.db_api = type("DBAPI", (), {"base_url": "http://127.0.0.1:9542"})()

    def auth_ok(self, _authorization: str) -> bool:
        return True

    def get_llm_config(self) -> dict:
        return {"runtime": {"provider": "stub"}, "persisted_env": {}}

    def get_web_config(self) -> dict:
        return {"runtime": {"streamlit_port": 9531}, "persisted_env": {}}

    def list_bot_configs(self) -> list[dict]:
        return [{"id": 1, "name": "default", "is_enabled": True, "token_masked": "****"}]

    def get_system_logs(self, *, limit: int, service: str, level: str) -> list[dict]:
        return [{"id": 1, "service": service or "api", "level": level or "INFO", "limit": limit}]

    def get_audit_logs(self, *, limit: int) -> list[dict]:
        return [{"id": 1, "action": "test", "limit": limit}]

    def create_bot_config(self, payload: dict) -> dict:
        return {"id": 2, "name": payload.get("name", "new")}

    def update_llm_config(self, _payload: dict) -> dict:
        return {"updated_keys": ["AIGM_LLM_PROVIDER"], "restart_required": True}

    def update_web_config(self, _payload: dict) -> dict:
        return {"updated_keys": ["AIGM_STREAMLIT_PORT"], "restart_required": True}

    def update_bot_config(self, bot_id: int, _payload: dict) -> dict:
        return {"id": bot_id, "name": "updated"}

    def delete_bot_config(self, bot_id: int) -> dict:
        return {"id": bot_id, "deleted": True}

    def check_db(self) -> tuple[bool, str]:
        return True, "reachable"

    def check_ollama(self) -> tuple[bool, str]:
        return True, "reachable"

    def check_openai(self) -> tuple[bool, str]:
        return False, "not_configured"


def test_management_meta_shape() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), supervisor.make_management_handler(_State()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/api/v1/meta"
        with request.urlopen(url, timeout=3) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        assert payload["ok"] is True
        assert payload["service"] == "aigm_management_api"
        assert "endpoints" in payload
        assert "config_llm" in payload["endpoints"]
    finally:
        server.shutdown()
        server.server_close()


def test_management_health_includes_required_checks() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), supervisor.make_management_handler(_State()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/api/v1/health"
        with request.urlopen(url, timeout=3) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        assert payload["ok"] is True
        checks = payload["health"]["checks"]
        assert set(checks.keys()) >= {"db", "ollama", "streamlit"}
    finally:
        server.shutdown()
        server.server_close()


def test_management_debug_db_endpoint_shape() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), supervisor.make_management_handler(_State()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/api/v1/debug/checks/db"
        req = request.Request(url, method="POST", data=b"{}", headers={"Content-Type": "application/json"})
        with request.urlopen(req, timeout=3) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        assert payload["ok"] is True
        assert payload["check"] == "db"
        assert "detail" in payload
    finally:
        server.shutdown()
        server.server_close()


def test_management_requires_bearer_token_when_configured() -> None:
    state = _State()
    state.api_token = "abc123"
    state.auth_ok = lambda authorization: authorization == "Bearer abc123"
    server = ThreadingHTTPServer(("127.0.0.1", 0), supervisor.make_management_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/api/v1/meta"
        try:
            with request.urlopen(url, timeout=3):
                pass
            raise AssertionError("expected unauthorized response")
        except error.HTTPError as exc:
            assert exc.code == 401
    finally:
        server.shutdown()
        server.server_close()
