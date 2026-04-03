from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer
from urllib import error, request

from aigm.ops import db_api


class _State:
    def __init__(self) -> None:
        self.dead_letters: list[dict] = []

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

    def ingest_system_logs(self, rows: list[dict]) -> dict:
        return {"inserted": len(rows)}

    def list_audit_logs(self, limit: int) -> list[dict]:
        return [{"id": 1, "limit": limit}]

    def table_counts(self) -> dict:
        return {"campaigns": 1, "players": 1, "turn_logs": 1, "bot_configs": 1, "system_logs": 1}

    def campaign_by_thread(self, thread_id: str) -> dict | None:
        if thread_id == "missing":
            return None
        return {"id": 10, "discord_thread_id": thread_id, "mode": "dnd", "state": {"scene": "x"}, "version": 1}

    def campaign_by_id(self, campaign_id: int) -> dict | None:
        if campaign_id == 999:
            return None
        return {"id": campaign_id, "discord_thread_id": "thread-123", "mode": "dnd", "state": {"scene": "x"}, "version": 1}

    def upsert_campaign_by_thread(self, thread_id: str, mode: str, state: dict) -> dict:
        return {"id": 11, "discord_thread_id": thread_id, "mode": mode, "state": state, "version": 1}

    def campaign_rules(self, campaign_id: int) -> dict[str, str]:
        return {"thread_name": "test-thread", "campaign_id": str(campaign_id)}

    def set_campaign_rule(self, campaign_id: int, key: str, value: str) -> dict:
        return {"campaign_id": campaign_id, "rule_key": key, "rule_value": value}

    def reserve_processed_message(self, campaign_id: int | None, discord_message_id: str, actor_discord_user_id: str) -> dict:
        if discord_message_id == "dup":
            return {"reserved": False}
        return {"reserved": True, "campaign_id": campaign_id, "actor": actor_discord_user_id}

    def list_item_knowledge(self, limit: int = 200) -> list[dict]:
        return [{"item_key": "torch", "observation_count": limit}]

    def list_global_item_relevance(self, limit: int = 300) -> list[dict]:
        return [{"item_key": "torch", "context_tag": "night", "score": 0.9, "interaction_count": limit}]

    def list_effect_knowledge(self, limit: int = 200) -> list[dict]:
        return [{"effect_key": "poison", "observation_count": limit}]

    def list_global_effect_relevance(self, limit: int = 300) -> list[dict]:
        return [{"effect_key": "poison", "context_tag": "combat", "score": 0.8, "interaction_count": limit}]

    def list_dice_roll_logs(self, limit: int = 100, campaign_id: int | None = None) -> list[dict]:
        return [{"id": 1, "campaign_id": campaign_id, "expression": "d20", "total": 14, "limit": limit}]

    def create_dead_letter_event(self, payload: dict) -> dict:
        event_id = len(self.dead_letters) + 1
        row = {
            "id": event_id,
            "event_type": str(payload.get("event_type", "")),
            "status": "open",
            "campaign_id": payload.get("campaign_id"),
            "actor_discord_user_id": str(payload.get("actor_discord_user_id", "")),
        }
        self.dead_letters.append(row)
        return {"id": event_id, "status": "open"}

    def list_dead_letter_events(self, status: str = "", limit: int = 50) -> list[dict]:
        rows = list(self.dead_letters)
        if status:
            rows = [r for r in rows if str(r.get("status", "")) == status]
        return rows[: max(1, int(limit))]


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
            payload = json.loads(exc.read().decode("utf-8"))
            assert payload["ok"] is False
            assert payload["error_code"] == "unauthorized"
            assert payload["error_message"] == "unauthorized"
            assert isinstance(payload["error_details"], dict)
    finally:
        server.shutdown()
        server.server_close()


def test_db_api_state_fails_closed_when_token_required_and_missing(monkeypatch) -> None:
    monkeypatch.setattr(db_api.settings, "db_api_token", "")
    monkeypatch.setattr(db_api.settings, "db_api_require_token", True)
    state = db_api.DBAPIState()
    assert state.auth_ok("") is False
    assert state.auth_ok("Bearer anything") is False


def test_db_api_rate_limit_scope() -> None:
    state = _State()
    state.request_limiter = db_api.APIRateLimiter(window_s=60, max_requests=1)
    server = ThreadingHTTPServer(("127.0.0.1", 0), db_api.make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        first = _req(f"{base}/db/v1/health")
        assert first["ok"] is True
        try:
            _req(f"{base}/db/v1/health")
            raise AssertionError("expected rate_limited response")
        except error.HTTPError as exc:
            assert exc.code == 429
            payload = json.loads(exc.read().decode("utf-8"))
            assert payload["error_code"] == "rate_limited"
            assert payload["error_details"]["scope"] == "db_api"
    finally:
        server.shutdown()
        server.server_close()


def test_db_api_bad_limit_returns_400() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), db_api.make_handler(_State()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        req = request.Request(f"{base}/db/v1/logs/system?limit=abc", headers={"Authorization": "Bearer ok"})
        try:
            with request.urlopen(req, timeout=3):
                pass
            raise AssertionError("expected bad_request response")
        except error.HTTPError as exc:
            assert exc.code == 400
            payload = json.loads(exc.read().decode("utf-8"))
            assert payload["error_code"] == "bad_request"
            assert payload["error_message"] == "limit must be an integer"
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


def test_db_api_echoes_correlation_id_header() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), db_api.make_handler(_State()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        req = request.Request(f"{base}/db/v1/health", headers={"Authorization": "Bearer ok", "X-Correlation-ID": "db-corr-1"})
        with request.urlopen(req, timeout=3) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            assert payload["ok"] is True
            assert resp.headers.get("X-Correlation-ID") == "db-corr-1"
    finally:
        server.shutdown()
        server.server_close()


def test_db_api_system_log_batch_ingest_endpoint() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), db_api.make_handler(_State()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        result = _req(
            f"{base}/db/v1/logs/system/batch",
            method="POST",
            payload={"rows": [{"service": "x", "level": "INFO", "message": "hello", "source": "runtime", "metadata": {}}]},
        )
        assert result["ok"] is True
        assert int(result["inserted"]) == 1
    finally:
        server.shutdown()
        server.server_close()


def test_db_api_gameplay_campaign_and_idempotency_endpoints() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), db_api.make_handler(_State()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        row = _req(f"{base}/db/v1/campaigns/by-thread?thread_id=thread-123")
        assert row["ok"] is True
        assert row["row"]["discord_thread_id"] == "thread-123"
        row_by_id = _req(f"{base}/db/v1/campaigns/by-id?campaign_id=10")
        assert row_by_id["ok"] is True
        assert row_by_id["row"]["id"] == 10

        missing = _req(f"{base}/db/v1/campaigns/by-thread?thread_id=missing")
        assert missing["ok"] is True
        assert missing["row"] is None

        upserted = _req(
            f"{base}/db/v1/campaigns/upsert-thread",
            method="POST",
            payload={"thread_id": "thread-999", "mode": "story", "state": {"scene": "start"}},
        )
        assert upserted["ok"] is True
        assert upserted["row"]["mode"] == "story"

        rules = _req(f"{base}/db/v1/campaigns/rules?campaign_id=10")
        assert rules["ok"] is True
        assert rules["rules"]["thread_name"] == "test-thread"

        set_rule = _req(
            f"{base}/db/v1/campaigns/rules/set",
            method="POST",
            payload={"campaign_id": 10, "rule_key": "thread_name", "rule_value": "new-name"},
        )
        assert set_rule["ok"] is True
        assert set_rule["rule_key"] == "thread_name"

        reserved = _req(
            f"{base}/db/v1/idempotency/reserve",
            method="POST",
            payload={"campaign_id": 10, "discord_message_id": "m1", "actor_discord_user_id": "u1"},
        )
        assert reserved["ok"] is True
        assert reserved["reserved"] is True

        dup = _req(
            f"{base}/db/v1/idempotency/reserve",
            method="POST",
            payload={"campaign_id": 10, "discord_message_id": "dup", "actor_discord_user_id": "u1"},
        )
        assert dup["ok"] is True
        assert dup["reserved"] is False
    finally:
        server.shutdown()
        server.server_close()


def test_db_api_knowledge_and_dice_endpoints() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), db_api.make_handler(_State()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        items = _req(f"{base}/db/v1/knowledge/items?limit=5")
        item_rel = _req(f"{base}/db/v1/knowledge/item-relevance?limit=7")
        effects = _req(f"{base}/db/v1/knowledge/effects?limit=9")
        effect_rel = _req(f"{base}/db/v1/knowledge/effect-relevance?limit=11")
        dice = _req(f"{base}/db/v1/dice-rolls?limit=3&campaign_id=10")
        assert items["ok"] is True and isinstance(items["rows"], list)
        assert item_rel["ok"] is True and isinstance(item_rel["rows"], list)
        assert effects["ok"] is True and isinstance(effects["rows"], list)
        assert effect_rel["ok"] is True and isinstance(effect_rel["rows"], list)
        assert dice["ok"] is True and isinstance(dice["rows"], list)
    finally:
        server.shutdown()
        server.server_close()


def test_db_api_dead_letter_endpoints() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), db_api.make_handler(_State()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        created = _req(
            f"{base}/db/v1/dead-letters",
            method="POST",
            payload={"event_type": "turn_job", "campaign_id": 10, "actor_discord_user_id": "u1"},
        )
        assert created["ok"] is True
        assert int(created["id"]) == 1
        listed = _req(f"{base}/db/v1/dead-letters?status=open&limit=5")
        assert listed["ok"] is True
        assert len(listed["rows"]) == 1
        assert listed["rows"][0]["event_type"] == "turn_job"
    finally:
        server.shutdown()
        server.server_close()


def test_db_api_error_envelope_for_bad_request() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), db_api.make_handler(_State()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            _req(f"{base}/db/v1/campaigns/by-thread")
            raise AssertionError("expected bad_request response")
        except error.HTTPError as exc:
            assert exc.code == 400
            payload = json.loads(exc.read().decode("utf-8"))
            assert payload["ok"] is False
            assert payload["error_code"] == "bad_request"
            assert payload["error_message"] == "thread_id is required"
            assert isinstance(payload["error_details"], dict)
            assert payload["error_details"].get("path") == "/db/v1/campaigns/by-thread"
    finally:
        server.shutdown()
        server.server_close()
