from __future__ import annotations

import json
import threading
import time
from urllib import error
from urllib import parse, request

from aigm.config import settings


class DBApiRequestError(RuntimeError):
    def __init__(
        self,
        *,
        method: str,
        path: str,
        status: int,
        error_code: str,
        message: str,
        details: dict | None = None,
    ) -> None:
        super().__init__(f"{method.upper()} {path} -> {status} {error_code}: {message}")
        self.method = method.upper()
        self.path = path
        self.status = int(status)
        self.error_code = str(error_code)
        self.message = str(message)
        self.details = dict(details or {})


class DBApiClient:
    def __init__(self, base_url: str, token: str = "", timeout_s: int = 10) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()
        self.timeout_s = max(1, int(timeout_s))
        self._consecutive_failures = 0
        self._opened_until_ts = 0.0
        self._request_ctx = threading.local()

    def set_correlation_id(self, correlation_id: str | None) -> None:
        value = str(correlation_id or "").strip()
        setattr(self._request_ctx, "correlation_id", value)

    def clear_correlation_id(self) -> None:
        try:
            delattr(self._request_ctx, "correlation_id")
        except AttributeError:
            pass

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        correlation_id = str(getattr(self._request_ctx, "correlation_id", "") or "").strip()
        if correlation_id:
            headers["X-Correlation-ID"] = correlation_id
        return headers

    @staticmethod
    def _is_retryable_status(status: int) -> bool:
        return int(status) in {408, 409, 425, 429, 500, 502, 503, 504}

    def _circuit_open(self) -> bool:
        return float(self._opened_until_ts) > time.time()

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._opened_until_ts = 0.0

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        threshold = max(1, int(settings.service_api_circuit_breaker_failure_threshold))
        if self._consecutive_failures >= threshold:
            self._opened_until_ts = time.time() + max(1, int(settings.service_api_circuit_breaker_reset_s))
            self._consecutive_failures = 0

    @staticmethod
    def _extract_error_payload(raw: str) -> tuple[str, str, dict]:
        if not raw.strip():
            return "upstream_error", "Upstream API request failed.", {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return "upstream_error", raw.strip(), {}
        if not isinstance(parsed, dict):
            return "upstream_error", str(parsed), {}
        error_code = str(parsed.get("error_code", "") or "upstream_error")
        message = str(parsed.get("error_message", "") or parsed.get("error", "") or "Upstream API request failed.")
        details = parsed.get("error_details", {})
        if not isinstance(details, dict):
            details = {"raw_details": details}
        return error_code, message, details

    def _request(self, method: str, path: str, payload: dict | None = None, query: dict | None = None) -> dict:
        if self._circuit_open():
            raise DBApiRequestError(
                method=method,
                path=path,
                status=503,
                error_code="service_api_circuit_open",
                message="DB API client circuit breaker is open.",
                details={"opened_until_ts": self._opened_until_ts},
            )
        url = f"{self.base_url}{path}"
        if query:
            q = parse.urlencode({k: v for k, v in query.items() if v is not None and v != ""})
            if q:
                url = f"{url}?{q}"
        data = None if payload is None else json.dumps(payload, ensure_ascii=True).encode("utf-8")
        req = request.Request(url=url, method=method.upper(), data=data, headers=self._headers())
        attempts = max(0, int(settings.service_api_http_max_retries)) + 1
        backoff_s = max(0.0, float(settings.service_api_http_retry_backoff_s))
        last_error: DBApiRequestError | None = None
        for attempt in range(attempts):
            try:
                with request.urlopen(req, timeout=self.timeout_s) as resp:
                    body = resp.read().decode("utf-8")
                self._record_success()
                if not body.strip():
                    return {}
                return json.loads(body)
            except error.HTTPError as exc:
                raw = ""
                try:
                    raw = exc.read().decode("utf-8")
                except Exception:
                    raw = str(exc)
                code, msg, details = self._extract_error_payload(raw)
                last_error = DBApiRequestError(
                    method=method,
                    path=path,
                    status=int(exc.code),
                    error_code=code,
                    message=msg,
                    details=details,
                )
                self._record_failure()
                if (attempt + 1) >= attempts or not self._is_retryable_status(int(exc.code)):
                    raise last_error
                if backoff_s > 0:
                    time.sleep(backoff_s * (2**attempt))
            except (error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                last_error = DBApiRequestError(
                    method=method,
                    path=path,
                    status=503,
                    error_code="service_api_unavailable",
                    message=str(exc),
                    details={},
                )
                self._record_failure()
                if (attempt + 1) >= attempts:
                    raise last_error
                if backoff_s > 0:
                    time.sleep(backoff_s * (2**attempt))
        if last_error is not None:
            raise last_error
        raise DBApiRequestError(
            method=method,
            path=path,
            status=500,
            error_code="unknown_error",
            message="DB API request failed.",
            details={},
        )

    def health(self) -> dict:
        return self._request("GET", "/db/v1/health")

    def list_bots(self, *, enabled_only: bool | None = None) -> list[dict]:
        query = {}
        if enabled_only is not None:
            query["enabled"] = "true" if enabled_only else "false"
        payload = self._request("GET", "/db/v1/bots", query=query)
        return list(payload.get("rows", []))

    def create_bot(self, payload: dict) -> dict:
        return self._request("POST", "/db/v1/bots", payload=payload)

    def update_bot(self, bot_id: int, payload: dict) -> dict:
        return self._request("PUT", f"/db/v1/bots/{int(bot_id)}", payload=payload)

    def delete_bot(self, bot_id: int) -> dict:
        return self._request("DELETE", f"/db/v1/bots/{int(bot_id)}")

    def list_system_logs(self, *, limit: int = 100, service: str = "", level: str = "") -> list[dict]:
        payload = self._request(
            "GET",
            "/db/v1/logs/system",
            query={"limit": int(limit), "service": service, "level": level},
        )
        return list(payload.get("rows", []))

    def ingest_system_logs(self, rows: list[dict]) -> int:
        payload = self._request("POST", "/db/v1/logs/system/batch", payload={"rows": rows})
        return int(payload.get("inserted", 0) or 0)

    def list_audit_logs(self, *, limit: int = 100) -> list[dict]:
        payload = self._request("GET", "/db/v1/logs/audit", query={"limit": int(limit)})
        return list(payload.get("rows", []))

    def table_counts(self) -> dict:
        return self._request("GET", "/db/v1/debug/table-counts")

    def campaign_by_thread(self, thread_id: str) -> dict | None:
        payload = self._request("GET", "/db/v1/campaigns/by-thread", query={"thread_id": thread_id})
        return payload.get("row")

    def campaign_by_id(self, campaign_id: int) -> dict | None:
        payload = self._request("GET", "/db/v1/campaigns/by-id", query={"campaign_id": int(campaign_id)})
        return payload.get("row")

    def upsert_campaign_by_thread(self, *, thread_id: str, mode: str, state: dict) -> dict:
        payload = self._request(
            "POST",
            "/db/v1/campaigns/upsert-thread",
            payload={"thread_id": thread_id, "mode": mode, "state": state},
        )
        return dict(payload.get("row", {}))

    def campaign_rules(self, campaign_id: int) -> dict[str, str]:
        payload = self._request("GET", "/db/v1/campaigns/rules", query={"campaign_id": int(campaign_id)})
        return dict(payload.get("rules", {}))

    def set_campaign_rule(self, campaign_id: int, rule_key: str, rule_value: str) -> dict:
        return self._request(
            "POST",
            "/db/v1/campaigns/rules/set",
            payload={"campaign_id": int(campaign_id), "rule_key": rule_key, "rule_value": rule_value},
        )

    def reserve_idempotency(self, *, campaign_id: int | None, discord_message_id: str, actor_discord_user_id: str) -> bool:
        payload = self._request(
            "POST",
            "/db/v1/idempotency/reserve",
            payload={
                "campaign_id": campaign_id,
                "discord_message_id": discord_message_id,
                "actor_discord_user_id": actor_discord_user_id,
            },
        )
        return bool(payload.get("reserved", False))

    def create_dead_letter_event(
        self,
        *,
        event_type: str,
        campaign_id: int | None,
        discord_thread_id: str = "",
        discord_message_id: str = "",
        actor_discord_user_id: str = "",
        actor_display_name: str = "",
        user_input: str = "",
        error_message: str = "",
        payload: dict | None = None,
        max_attempts: int = 3,
    ) -> dict:
        return self._request(
            "POST",
            "/db/v1/dead-letters",
            payload={
                "event_type": event_type,
                "campaign_id": campaign_id,
                "discord_thread_id": discord_thread_id,
                "discord_message_id": discord_message_id,
                "actor_discord_user_id": actor_discord_user_id,
                "actor_display_name": actor_display_name,
                "user_input": user_input,
                "error_message": error_message,
                "payload": dict(payload or {}),
                "max_attempts": int(max_attempts),
            },
        )

    def list_dead_letter_events(self, *, status: str = "", limit: int = 50) -> list[dict]:
        query: dict[str, str | int] = {"limit": int(limit)}
        if status:
            query["status"] = status
        payload = self._request("GET", "/db/v1/dead-letters", query=query)
        return list(payload.get("rows", []))

    def list_campaigns(self, *, limit: int = 200) -> list[dict]:
        payload = self._request("GET", "/db/v1/campaigns", query={"limit": int(limit)})
        return list(payload.get("rows", []))

    def list_turn_logs(self, *, campaign_id: int, limit: int = 50) -> list[dict]:
        payload = self._request(
            "GET",
            "/db/v1/turns",
            query={"campaign_id": int(campaign_id), "limit": int(limit)},
        )
        return list(payload.get("rows", []))

    def list_item_knowledge(self, *, limit: int = 200) -> list[dict]:
        payload = self._request("GET", "/db/v1/knowledge/items", query={"limit": int(limit)})
        return list(payload.get("rows", []))

    def list_item_relevance(self, *, limit: int = 300) -> list[dict]:
        payload = self._request("GET", "/db/v1/knowledge/item-relevance", query={"limit": int(limit)})
        return list(payload.get("rows", []))

    def list_effect_knowledge(self, *, limit: int = 200) -> list[dict]:
        payload = self._request("GET", "/db/v1/knowledge/effects", query={"limit": int(limit)})
        return list(payload.get("rows", []))

    def list_effect_relevance(self, *, limit: int = 300) -> list[dict]:
        payload = self._request("GET", "/db/v1/knowledge/effect-relevance", query={"limit": int(limit)})
        return list(payload.get("rows", []))

    def list_dice_rolls(self, *, limit: int = 100, campaign_id: int | None = None) -> list[dict]:
        query: dict[str, int] = {"limit": int(limit)}
        if campaign_id is not None:
            query["campaign_id"] = int(campaign_id)
        payload = self._request("GET", "/db/v1/dice-rolls", query=query)
        return list(payload.get("rows", []))
