from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from aigm.ops.db_api_client import DBApiClient


def test_db_api_client_forwards_correlation_id_header() -> None:
    captured: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            captured["correlation_id"] = str(self.headers.get("X-Correlation-ID", "") or "")
            body = json.dumps({"ok": True, "rows": []}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = DBApiClient(base_url=f"http://127.0.0.1:{server.server_port}", token="", timeout_s=3)
        client.set_correlation_id("corr-db-client-123")
        _ = client.list_system_logs(limit=1)
        assert captured.get("correlation_id") == "corr-db-client-123"
    finally:
        server.shutdown()
        server.server_close()


def test_db_api_client_dead_letter_methods_hit_expected_paths() -> None:
    calls: list[tuple[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            calls.append(("GET", self.path))
            body = json.dumps({"ok": True, "rows": []}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802
            calls.append(("POST", self.path))
            body = json.dumps({"ok": True, "id": 7, "status": "open"}).encode("utf-8")
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = DBApiClient(base_url=f"http://127.0.0.1:{server.server_port}", token="", timeout_s=3)
        created = client.create_dead_letter_event(event_type="turn_job", campaign_id=1, actor_discord_user_id="u1")
        rows = client.list_dead_letter_events(status="open", limit=5)
        assert int(created["id"]) == 7
        assert isinstance(rows, list)
        assert ("POST", "/db/v1/dead-letters") in calls
        assert any(method == "GET" and path.startswith("/db/v1/dead-letters") for method, path in calls)
    finally:
        server.shutdown()
        server.server_close()
