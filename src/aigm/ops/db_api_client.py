from __future__ import annotations

import json
from urllib import parse, request


class DBApiClient:
    def __init__(self, base_url: str, token: str = "", timeout_s: int = 10) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()
        self.timeout_s = max(1, int(timeout_s))

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(self, method: str, path: str, payload: dict | None = None, query: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        if query:
            q = parse.urlencode({k: v for k, v in query.items() if v is not None and v != ""})
            if q:
                url = f"{url}?{q}"
        data = None if payload is None else json.dumps(payload, ensure_ascii=True).encode("utf-8")
        req = request.Request(url=url, method=method.upper(), data=data, headers=self._headers())
        with request.urlopen(req, timeout=self.timeout_s) as resp:
            body = resp.read().decode("utf-8")
        if not body.strip():
            return {}
        return json.loads(body)

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

    def list_audit_logs(self, *, limit: int = 100) -> list[dict]:
        payload = self._request("GET", "/db/v1/logs/audit", query={"limit": int(limit)})
        return list(payload.get("rows", []))

    def table_counts(self) -> dict:
        return self._request("GET", "/db/v1/debug/table-counts")
