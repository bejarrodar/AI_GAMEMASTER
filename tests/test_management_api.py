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
        self._create_bot_calls = 0
        self._idem: dict[str, tuple[str, int, dict]] = {}

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
        self._create_bot_calls += 1
        return {"id": self._create_bot_calls + 1, "name": payload.get("name", "new")}

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

    def list_auth_users(self) -> list[dict]:
        return [{"id": 1, "username": "admin", "roles": ["admin"]}]

    def list_auth_roles(self) -> list[dict]:
        return [{"id": 1, "name": "admin", "permissions": ["system.admin"]}]

    def list_auth_permissions(self) -> list[dict]:
        return [{"id": 1, "name": "system.admin"}]

    def list_agency_rules(self) -> list[dict]:
        return [{"rule_id": "pc_agency", "title": "PC Agency", "priority": "critical", "is_enabled": True, "body": "..."}]

    def upsert_agency_rule(self, _payload: dict) -> tuple[bool, str]:
        return True, "Upserted."

    def remove_agency_rule(self, _rule_id: str) -> tuple[bool, str]:
        return True, "Removed."

    def set_agency_rule_enabled(self, _rule_id: str, _is_enabled: bool) -> tuple[bool, str]:
        return True, "Updated."

    def crew_preview(self, _campaign_id: int, _payload: dict) -> dict:
        return {"narration": "preview", "accepted": [], "rejected": [], "crew_outputs": []}

    def crew_apply(self, _campaign_id: int, _payload: dict) -> dict:
        return {"narration": "applied", "details": {"accepted": [], "rejected": []}}

    def idempotency_get(self, *, method: str, path: str, key: str, fingerprint: str):
        cache_key = f"{method}|{path}|{key}"
        row = self._idem.get(cache_key)
        if row is None:
            return None
        cached_fingerprint, code, payload = row
        if cached_fingerprint != fingerprint:
            raise ValueError("idempotency key reuse with different payload")
        return code, payload

    def idempotency_put(self, *, method: str, path: str, key: str, fingerprint: str, status: int, payload: dict) -> None:
        cache_key = f"{method}|{path}|{key}"
        self._idem[cache_key] = (fingerprint, int(status), dict(payload))


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
        assert "openapi" in payload["endpoints"]
    finally:
        server.shutdown()
        server.server_close()


def test_management_openapi_shape() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), supervisor.make_management_handler(_State()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/api/v1/openapi.json"
        with request.urlopen(url, timeout=3) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        assert payload["openapi"] == "3.0.3"
        assert "paths" in payload
        assert "/api/v1/meta" in payload["paths"]
        assert "/api/v1/openapi.json" in payload["paths"]
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


def test_management_echoes_correlation_id_header() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), supervisor.make_management_handler(_State()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/api/v1/meta"
        req = request.Request(url, headers={"X-Correlation-ID": "corr-123"})
        with request.urlopen(req, timeout=3) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            assert payload["ok"] is True
            assert resp.headers.get("X-Correlation-ID") == "corr-123"
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
            payload = json.loads(exc.read().decode("utf-8"))
            assert payload["ok"] is False
            assert payload["error_code"] == "unauthorized"
            assert payload["error_message"] == "unauthorized"
            assert isinstance(payload["error_details"], dict)
    finally:
        server.shutdown()
        server.server_close()


def test_management_login_is_reachable_without_bearer_token() -> None:
    state = _State()
    state.api_token = "abc123"
    state.auth_ok = lambda authorization: authorization == "Bearer abc123"
    state.auth_login = lambda username, password: {"ok": username == "admin" and password == "secret", "user": {"username": username}}
    server = ThreadingHTTPServer(("127.0.0.1", 0), supervisor.make_management_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/api/v1/auth/login"
        req = request.Request(
            url,
            method="POST",
            data=json.dumps({"username": "admin", "password": "secret"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with request.urlopen(req, timeout=3) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        assert payload["ok"] is True
        assert payload["user"]["username"] == "admin"
    finally:
        server.shutdown()
        server.server_close()


def test_management_error_envelope_for_not_found() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), supervisor.make_management_handler(_State()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/api/v1/not-a-route"
        try:
            with request.urlopen(url, timeout=3):
                pass
            raise AssertionError("expected not_found response")
        except error.HTTPError as exc:
            assert exc.code == 404
            payload = json.loads(exc.read().decode("utf-8"))
            assert payload["ok"] is False
            assert payload["error"] == "not_found"
            assert payload["error_code"] == "not_found"
            assert payload["error_message"] == "not_found"
            assert isinstance(payload["error_details"], dict)
    finally:
        server.shutdown()
        server.server_close()


def test_management_auth_endpoints_shape() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), supervisor.make_management_handler(_State()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        users_url = f"http://127.0.0.1:{server.server_port}/api/v1/auth/users"
        roles_url = f"http://127.0.0.1:{server.server_port}/api/v1/auth/roles"
        perms_url = f"http://127.0.0.1:{server.server_port}/api/v1/auth/permissions"
        with request.urlopen(users_url, timeout=3) as resp:
            users_payload = json.loads(resp.read().decode("utf-8"))
        with request.urlopen(roles_url, timeout=3) as resp:
            roles_payload = json.loads(resp.read().decode("utf-8"))
        with request.urlopen(perms_url, timeout=3) as resp:
            perms_payload = json.loads(resp.read().decode("utf-8"))
        assert users_payload["ok"] is True
        assert roles_payload["ok"] is True
        assert perms_payload["ok"] is True
        assert isinstance(users_payload["users"], list)
        assert isinstance(roles_payload["roles"], list)
        assert isinstance(perms_payload["permissions"], list)
    finally:
        server.shutdown()
        server.server_close()


def test_management_agency_and_crew_endpoints_shape() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), supervisor.make_management_handler(_State()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        with request.urlopen(f"{base}/api/v1/agency/rules", timeout=3) as resp:
            rows_payload = json.loads(resp.read().decode("utf-8"))
        assert rows_payload["ok"] is True
        assert isinstance(rows_payload["rows"], list)

        req_upsert = request.Request(
            f"{base}/api/v1/agency/rules/upsert",
            method="POST",
            data=json.dumps({"rule_id": "x", "title": "x", "priority": "high", "body": "x"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with request.urlopen(req_upsert, timeout=3) as resp:
            upsert_payload = json.loads(resp.read().decode("utf-8"))
        assert upsert_payload["ok"] is True

        req_toggle = request.Request(
            f"{base}/api/v1/agency/rules/pc_agency/enabled",
            method="PUT",
            data=json.dumps({"is_enabled": False}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with request.urlopen(req_toggle, timeout=3) as resp:
            toggle_payload = json.loads(resp.read().decode("utf-8"))
        assert toggle_payload["ok"] is True

        req_delete = request.Request(f"{base}/api/v1/agency/rules/pc_agency", method="DELETE")
        with request.urlopen(req_delete, timeout=3) as resp:
            delete_payload = json.loads(resp.read().decode("utf-8"))
        assert delete_payload["ok"] is True

        req_preview = request.Request(
            f"{base}/api/v1/campaigns/1/crew/preview",
            method="POST",
            data=json.dumps({"actor": "u1", "user_input": "hello", "crew_definition_json": "{}"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with request.urlopen(req_preview, timeout=3) as resp:
            preview_payload = json.loads(resp.read().decode("utf-8"))
        assert preview_payload["ok"] is True
        assert "narration" in preview_payload

        req_apply = request.Request(
            f"{base}/api/v1/campaigns/1/crew/apply",
            method="POST",
            data=json.dumps(
                {"actor": "u1", "actor_display_name": "U1", "user_input": "hello", "crew_definition_json": "{}"}
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with request.urlopen(req_apply, timeout=3) as resp:
            apply_payload = json.loads(resp.read().decode("utf-8"))
        assert apply_payload["ok"] is True
        assert "details" in apply_payload
    finally:
        server.shutdown()
        server.server_close()


def test_management_mutation_idempotency_key_replays_response() -> None:
    state = _State()
    server = ThreadingHTTPServer(("127.0.0.1", 0), supervisor.make_management_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        payload = {"name": "bot-a", "discord_token": "tok", "is_enabled": True, "notes": ""}
        headers = {"Content-Type": "application/json", "Idempotency-Key": "abc-1"}
        req1 = request.Request(
            f"{base}/api/v1/bots",
            method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
        )
        with request.urlopen(req1, timeout=3) as resp:
            p1 = json.loads(resp.read().decode("utf-8"))
        req2 = request.Request(
            f"{base}/api/v1/bots",
            method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
        )
        with request.urlopen(req2, timeout=3) as resp:
            p2 = json.loads(resp.read().decode("utf-8"))
        assert p1 == p2
        assert state._create_bot_calls == 1
    finally:
        server.shutdown()
        server.server_close()


def test_management_debug_rate_limit_scope() -> None:
    state = _State()
    state.request_limiter = supervisor.APIRateLimiter(window_s=60, max_requests=100)
    state.mutation_limiter = supervisor.APIRateLimiter(window_s=60, max_requests=100)
    state.debug_limiter = supervisor.APIRateLimiter(window_s=60, max_requests=1)
    server = ThreadingHTTPServer(("127.0.0.1", 0), supervisor.make_management_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        req1 = request.Request(f"{base}/api/v1/debug/checks/db", method="POST", data=b"{}", headers={"Content-Type": "application/json"})
        with request.urlopen(req1, timeout=3) as resp:
            payload1 = json.loads(resp.read().decode("utf-8"))
        assert payload1["ok"] is True

        req2 = request.Request(f"{base}/api/v1/debug/checks/db", method="POST", data=b"{}", headers={"Content-Type": "application/json"})
        try:
            with request.urlopen(req2, timeout=3):
                pass
            raise AssertionError("expected debug rate limit")
        except error.HTTPError as exc:
            assert exc.code == 429
            payload2 = json.loads(exc.read().decode("utf-8"))
            assert payload2["error_code"] == "rate_limited"
            assert payload2["error_details"]["scope"] == "management_api_debug"
    finally:
        server.shutdown()
        server.server_close()
