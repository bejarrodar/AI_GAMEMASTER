from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import error, request

from pathlib import Path

from aigm.ops import supervisor


class _DummySession:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def execute(self, _query):
        return 1


class _DummyResp:
    def __init__(self, body: bytes = b"{}") -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def read(self) -> bytes:
        return self._body


class _DummyProc:
    def poll(self):
        return None


def test_health_snapshot_has_required_checks(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(supervisor, "SessionLocal", lambda: _DummySession())
    monkeypatch.setattr(supervisor.request, "urlopen", lambda *_args, **_kwargs: _DummyResp(b'{"models": []}'))

    logger = supervisor.UnifiedLogger(log_dir=tmp_path / "logs")
    state = supervisor.HealthState(
        streamlit_url="http://127.0.0.1:9531",
        ollama_url="http://127.0.0.1:11434",
        logger=logger,
    )
    state.set_proc("bot_manager", _DummyProc())
    snap = state.snapshot()

    assert "ok" in snap
    assert "checks" in snap
    checks = snap["checks"]
    assert "db" in checks
    assert "ollama" in checks
    assert "streamlit" in checks
    assert "process_bot_manager" in checks


def test_health_handler_returns_json_shape() -> None:
    class _State:
        def snapshot(self):
            return {
                "ok": False,
                "timestamp": "2026-02-15T00:00:00Z",
                "checks": {
                    "db": {"ok": True, "detail": "reachable"},
                    "ollama": {"ok": False, "detail": "unreachable"},
                    "streamlit": {"ok": True, "detail": "reachable"},
                },
            }

    server = ThreadingHTTPServer(("127.0.0.1", 0), supervisor.make_health_handler(_State()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/health"
        try:
            with request.urlopen(url, timeout=3) as resp:
                body = resp.read().decode("utf-8")
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            assert exc.code == 503
        payload = json.loads(body)
        assert set(payload.keys()) >= {"ok", "timestamp", "checks"}
        assert set(payload["checks"].keys()) >= {"db", "ollama", "streamlit"}
    finally:
        server.shutdown()
        server.server_close()


def test_metrics_endpoint_returns_prometheus_text() -> None:
    class _State:
        def __init__(self) -> None:
            self._payload = {
                "ok": True,
                "timestamp": "2026-02-15T00:00:00Z",
                "checks": {
                    "db": {"ok": True, "detail": "reachable"},
                    "ollama": {"ok": True, "detail": "reachable"},
                    "streamlit": {"ok": True, "detail": "reachable"},
                },
            }
            self._requests = 0

        def snapshot(self):
            return self._payload

        def record_health_request(self, _ok: bool):
            self._requests += 1

        def metrics_text(self):
            self.record_health_request(True)
            return (
                "aigm_health_requests_total 1\n"
                "aigm_health_failures_total 0\n"
                'aigm_health_check_ok{check="db"} 1\n'
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), supervisor.make_health_handler(_State()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/metrics"
        with request.urlopen(url, timeout=3) as resp:
            body = resp.read().decode("utf-8")
            content_type = resp.headers.get("Content-Type", "")
        assert "text/plain" in content_type
        assert "aigm_health_requests_total" in body
        assert "aigm_health_failures_total" in body
        assert 'aigm_health_check_ok{check="db"}' in body
    finally:
        server.shutdown()
        server.server_close()


def test_parse_aigm_metric_line() -> None:
    parsed = supervisor.parse_aigm_metric_line("[bot-manager][x][stdout] [aigm-metric] turn_success latency_ms=123.45")
    assert parsed is not None
    assert parsed["name"] == "turn_success"
    assert parsed["fields"]["latency_ms"] == "123.45"

    parsed2 = supervisor.parse_aigm_metric_line("[aigm-metric] turn_failure reason=stale_update")
    assert parsed2 is not None
    assert parsed2["name"] == "turn_failure"
    assert parsed2["fields"]["reason"] == "stale_update"


def test_post_json_webhook() -> None:
    received: dict = {}

    class _WebhookHandler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            received["json"] = json.loads(body)
            self.send_response(200)
            self.end_headers()

        def log_message(self, _format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _WebhookHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/hook"
        payload = {"event": "test", "ok": True}
        supervisor.post_json_webhook(url, payload, timeout_s=3)
        assert received["json"] == payload
    finally:
        server.shutdown()
        server.server_close()


def test_traceback_parsing_helpers() -> None:
    assert supervisor.is_traceback_start("Traceback (most recent call last):") is True
    assert supervisor.is_traceback_start("ValueError: boom") is False

    assert supervisor.is_traceback_line("Traceback (most recent call last):") is True
    assert supervisor.is_traceback_line('  File "x.py", line 1, in <module>') is True
    assert supervisor.is_traceback_line("    raise ValueError('x')") is True
    assert supervisor.is_traceback_line("ValueError: bad input") is True
    assert supervisor.is_traceback_line("During handling of the above exception, another exception occurred:") is True
    assert supervisor.is_traceback_line("The above exception was the direct cause of the following exception:") is True
    assert supervisor.is_traceback_line("[2026-02-15] INFO service started") is False
