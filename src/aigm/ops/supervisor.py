from __future__ import annotations

import argparse
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import request

from sqlalchemy import text

from aigm.config import settings
from aigm.db.models import AdminAuditLog, SystemLog
from aigm.db.session import SessionLocal


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def infer_level(line: str) -> str:
    upper = line.upper()
    if "ERROR" in upper or "TRACEBACK" in upper or "EXCEPTION" in upper:
        return "ERROR"
    if "WARN" in upper:
        return "WARNING"
    if "DEBUG" in upper:
        return "DEBUG"
    return "INFO"


def parse_aigm_metric_line(line: str) -> dict | None:
    text = (line or "").strip()
    marker = "[aigm-metric]"
    idx = text.find(marker)
    if idx < 0:
        return None
    payload = text[idx + len(marker) :].strip()
    if not payload:
        return None
    parts = payload.split()
    if not parts:
        return None
    name = parts[0]
    fields: dict[str, str] = {}
    for token in parts[1:]:
        if "=" not in token:
            continue
        k, v = token.split("=", 1)
        fields[k.strip()] = v.strip()
    return {"name": name, "fields": fields}


def post_json_webhook(url: str, payload: dict, timeout_s: int = 8) -> None:
    req = request.Request(
        url=url,
        method="POST",
        data=json.dumps(payload, ensure_ascii=True).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with request.urlopen(req, timeout=timeout_s) as _resp:
        return


@dataclass
class ProcRef:
    name: str
    popen: subprocess.Popen


class UnifiedLogger:
    def __init__(self, log_dir: Path) -> None:
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.combined_path = self.log_dir / "combined.log"
        self.service_paths = {
            "supervisor": self.log_dir / "supervisor.log",
            "bot_manager": self.log_dir / "bot_manager.log",
            "streamlit": self.log_dir / "streamlit.log",
            "health": self.log_dir / "health.log",
        }
        self._lock = threading.Lock()
        self._db_pending: list[dict] = []
        self._last_flush = time.time()
        self._max_bytes = max(1024, int(settings.log_file_max_bytes))
        self._backup_count = max(1, int(settings.log_file_backup_count))
        self._batch_size = max(1, int(settings.log_db_batch_size))
        self._flush_interval_s = max(1, int(settings.log_db_flush_interval_s))

    def _rotate_if_needed(self, path: Path) -> None:
        if not path.exists():
            return
        try:
            size = path.stat().st_size
        except OSError:
            return
        if size < self._max_bytes:
            return
        oldest = Path(f"{path}.{self._backup_count}")
        if oldest.exists():
            oldest.unlink(missing_ok=True)
        for i in range(self._backup_count - 1, 0, -1):
            src = Path(f"{path}.{i}")
            dst = Path(f"{path}.{i + 1}")
            if src.exists():
                src.replace(dst)
        path.replace(Path(f"{path}.1"))

    def _flush_db_locked(self, force: bool = False) -> None:
        now = time.time()
        if not self._db_pending:
            self._last_flush = now
            return
        if not force and len(self._db_pending) < self._batch_size and (now - self._last_flush) < self._flush_interval_s:
            return
        batch = self._db_pending
        self._db_pending = []
        self._last_flush = now
        try:
            with SessionLocal() as db:
                db.add_all(
                    [
                        SystemLog(
                            service=item["service"],
                            level=item["level"],
                            message=item["message"],
                            source=item["source"],
                            log_metadata=item["metadata"],
                        )
                        for item in batch
                    ]
                )
                db.commit()
        except Exception:
            # DB logging should never crash supervisor.
            pass

    def write(self, service: str, level: str, message: str, source: str = "runtime", metadata: dict | None = None) -> None:
        # Emit one normalized JSON line to both filesystem sinks and DB sink.
        payload = {
            "ts": utc_now_iso(),
            "service": service,
            "level": level,
            "message": message.rstrip("\n"),
            "source": source,
            "metadata": metadata or {},
        }
        line = json.dumps(payload, ensure_ascii=True)
        with self._lock:
            self._rotate_if_needed(self.combined_path)
            with self.combined_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
            service_path = self.service_paths.get(service, self.log_dir / f"{service}.log")
            self._rotate_if_needed(service_path)
            with service_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
            self._db_pending.append(payload)
            self._flush_db_locked(force=False)

    def flush(self, force: bool = False) -> None:
        with self._lock:
            self._flush_db_locked(force=force)


class HealthState:
    def __init__(self, streamlit_url: str, ollama_url: str, logger: UnifiedLogger) -> None:
        self.streamlit_url = streamlit_url.rstrip("/")
        self.ollama_url = ollama_url.rstrip("/")
        self.logger = logger
        self._lock = threading.Lock()
        self._procs: dict[str, subprocess.Popen] = {}
        self._metrics = {
            "health_requests_total": 0,
            "health_failures_total": 0,
            "snapshot_count_total": 0,
            "snapshot_duration_seconds_sum": 0.0,
            "snapshot_duration_seconds_last": 0.0,
            "turn_success_total": 0,
            "turn_failure_total": 0,
            "turn_latency_seconds_sum": 0.0,
            "turn_latency_seconds_count": 0,
            "log_queue_depth": 0,
        }
        self._last_snapshot: dict | None = None

    def set_proc(self, name: str, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs[name] = proc

    def snapshot(self) -> dict:
        # Aggregate runtime dependency checks used by both API responses and periodic health logs.
        start = time.perf_counter()
        checks: dict[str, dict] = {}

        try:
            with SessionLocal() as db:
                db.execute(text("SELECT 1"))
            checks["db"] = {"ok": True, "detail": "reachable"}
        except Exception as exc:
            checks["db"] = {"ok": False, "detail": str(exc)}

        try:
            max_age = max(1, int(settings.secret_rotation_max_age_days))
            with SessionLocal() as db:
                last_rotation = (
                    db.query(AdminAuditLog)
                    .filter(AdminAuditLog.action.in_(["secret_rotated_local", "secret_rotated"]))
                    .order_by(AdminAuditLog.created_at.desc())
                    .first()
                )
            if last_rotation is None:
                checks["secret_rotation"] = {
                    "ok": False,
                    "detail": "No secret rotation audit event found.",
                }
            else:
                age_days = (datetime.now(timezone.utc) - last_rotation.created_at.replace(tzinfo=timezone.utc)).days
                checks["secret_rotation"] = {
                    "ok": age_days <= max_age,
                    "detail": f"last_rotation_days={age_days}, max_age_days={max_age}",
                }
        except Exception as exc:
            checks["secret_rotation"] = {"ok": False, "detail": str(exc)}

        try:
            req = request.Request(f"{self.ollama_url}/api/tags", method="GET")
            with request.urlopen(req, timeout=5) as resp:
                _ = resp.read()
            checks["ollama"] = {"ok": True, "detail": "reachable"}
        except Exception as exc:
            checks["ollama"] = {"ok": False, "detail": str(exc)}

        try:
            req = request.Request(f"{self.streamlit_url}/_stcore/health", method="GET")
            with request.urlopen(req, timeout=5) as resp:
                _ = resp.read()
            checks["streamlit"] = {"ok": True, "detail": "reachable"}
        except Exception as exc:
            checks["streamlit"] = {"ok": False, "detail": str(exc)}

        with self._lock:
            for name, proc in self._procs.items():
                running = proc.poll() is None
                checks[f"process_{name}"] = {"ok": running, "detail": "running" if running else f"exit={proc.poll()}"}

        overall = all(v.get("ok", False) for v in checks.values())
        payload = {
            "ok": overall,
            "timestamp": utc_now_iso(),
            "checks": checks,
        }
        elapsed = max(0.0, time.perf_counter() - start)
        with self._lock:
            self._metrics["snapshot_count_total"] += 1
            self._metrics["snapshot_duration_seconds_sum"] += elapsed
            self._metrics["snapshot_duration_seconds_last"] = elapsed
            self._last_snapshot = payload
        return payload

    def record_health_request(self, ok: bool) -> None:
        with self._lock:
            self._metrics["health_requests_total"] += 1
            if not ok:
                self._metrics["health_failures_total"] += 1

    def record_turn_metric(self, metric_name: str, fields: dict[str, str] | None = None) -> None:
        data = fields or {}
        with self._lock:
            if metric_name == "turn_success":
                self._metrics["turn_success_total"] += 1
                try:
                    latency_ms = float(data.get("latency_ms", "0") or "0")
                except ValueError:
                    latency_ms = 0.0
                if latency_ms > 0:
                    self._metrics["turn_latency_seconds_sum"] += latency_ms / 1000.0
                    self._metrics["turn_latency_seconds_count"] += 1
            elif metric_name == "turn_failure":
                self._metrics["turn_failure_total"] += 1

    def set_log_queue_depth(self, depth: int) -> None:
        with self._lock:
            self._metrics["log_queue_depth"] = max(0, int(depth))

    def metrics_text(self) -> str:
        snap = self.snapshot()
        self.record_health_request(bool(snap.get("ok", False)))
        with self._lock:
            metrics = dict(self._metrics)
        lines = [
            "# HELP aigm_health_requests_total Total HTTP health/metrics requests served.",
            "# TYPE aigm_health_requests_total counter",
            f"aigm_health_requests_total {int(metrics['health_requests_total'])}",
            "# HELP aigm_health_failures_total Total failed health snapshots returned by API.",
            "# TYPE aigm_health_failures_total counter",
            f"aigm_health_failures_total {int(metrics['health_failures_total'])}",
            "# HELP aigm_health_snapshot_count_total Total health snapshots computed.",
            "# TYPE aigm_health_snapshot_count_total counter",
            f"aigm_health_snapshot_count_total {int(metrics['snapshot_count_total'])}",
            "# HELP aigm_health_snapshot_duration_seconds_sum Sum of health snapshot durations in seconds.",
            "# TYPE aigm_health_snapshot_duration_seconds_sum counter",
            f"aigm_health_snapshot_duration_seconds_sum {float(metrics['snapshot_duration_seconds_sum']):.6f}",
            "# HELP aigm_health_snapshot_duration_seconds_last Last health snapshot duration in seconds.",
            "# TYPE aigm_health_snapshot_duration_seconds_last gauge",
            f"aigm_health_snapshot_duration_seconds_last {float(metrics['snapshot_duration_seconds_last']):.6f}",
            "# HELP aigm_turn_success_total Total successfully processed turns.",
            "# TYPE aigm_turn_success_total counter",
            f"aigm_turn_success_total {int(metrics['turn_success_total'])}",
            "# HELP aigm_turn_failure_total Total failed turns.",
            "# TYPE aigm_turn_failure_total counter",
            f"aigm_turn_failure_total {int(metrics['turn_failure_total'])}",
            "# HELP aigm_turn_latency_seconds_sum Sum of successful turn latencies in seconds.",
            "# TYPE aigm_turn_latency_seconds_sum counter",
            f"aigm_turn_latency_seconds_sum {float(metrics['turn_latency_seconds_sum']):.6f}",
            "# HELP aigm_turn_latency_seconds_count Count of successful turn latency samples.",
            "# TYPE aigm_turn_latency_seconds_count counter",
            f"aigm_turn_latency_seconds_count {int(metrics['turn_latency_seconds_count'])}",
            "# HELP aigm_log_queue_depth Current supervisor log fan-in queue depth.",
            "# TYPE aigm_log_queue_depth gauge",
            f"aigm_log_queue_depth {int(metrics['log_queue_depth'])}",
        ]
        for check_name, check in (snap.get("checks", {}) or {}).items():
            safe_name = "".join(c if c.isalnum() else "_" for c in check_name.lower()).strip("_")
            lines.append(f'aigm_health_check_ok{{check="{safe_name}"}} {1 if check.get("ok", False) else 0}')
        return "\n".join(lines) + "\n"


def make_health_handler(state: HealthState):
    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/metrics":
                if hasattr(state, "metrics_text"):
                    body = state.metrics_text().encode("utf-8")
                else:
                    body = b""
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path not in ("/health", "/healthz"):
                self.send_response(404)
                self.end_headers()
                return
            payload = state.snapshot()
            if hasattr(state, "record_health_request"):
                state.record_health_request(bool(payload["ok"]))
            body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
            self.send_response(200 if payload["ok"] else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *args):
            # silence default stderr logging
            return

    return HealthHandler


def stream_reader(service: str, stream, out_q: queue.Queue) -> None:
    try:
        for line in iter(stream.readline, ""):
            if not line:
                break
            out_q.put((service, line.rstrip("\n")))
    finally:
        try:
            stream.close()
        except Exception:
            pass


def start_process(name: str, args: list[str], cwd: str) -> ProcRef:
    try:
        proc = subprocess.Popen(
            args,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        raise RuntimeError(f"Failed to start process '{name}': {exc}") from exc
    return ProcRef(name=name, popen=proc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run AI GameMaster supervisor with health API and unified logging.")
    parser.add_argument("--streamlit-port", type=int, default=settings.streamlit_port)
    parser.add_argument("--health-port", type=int, default=settings.healthcheck_port)
    parser.add_argument("--log-dir", default=settings.log_dir)
    parser.add_argument("--cwd", default=os.getcwd())
    args = parser.parse_args()

    python_exe = sys.executable
    logger = UnifiedLogger(Path(args.log_dir))
    logger.write("supervisor", "INFO", "Supervisor starting.", metadata={"pid": os.getpid()})
    if settings.secret_source.strip().lower() != "none":
        try:
            with SessionLocal() as db:
                db.add(
                    AdminAuditLog(
                        actor_source="runtime",
                        actor_id="supervisor",
                        actor_display="supervisor",
                        action="secret_source_accessed",
                        target=settings.secret_source.strip().lower(),
                        audit_metadata={
                            "source": settings.secret_source.strip().lower(),
                            "aws_secret_id_set": bool(settings.secret_source_aws_secret_id.strip()),
                        },
                    )
                )
                db.commit()
        except Exception:
            pass

    try:
        bot_manager_ref = start_process(
            "bot_manager",
            [python_exe, "-m", "aigm.ops.bot_manager", "--cwd", args.cwd],
            cwd=args.cwd,
        )
        streamlit_ref = start_process(
            "streamlit",
            [
                python_exe,
                "-m",
                "streamlit",
                "run",
                "streamlit_app.py",
                "--server.port",
                str(args.streamlit_port),
                "--server.headless",
                "true",
            ],
            cwd=args.cwd,
        )
    except RuntimeError as exc:
        logger.write("supervisor", "ERROR", str(exc))
        return 1

    health_state = HealthState(
        streamlit_url=f"http://127.0.0.1:{args.streamlit_port}",
        ollama_url=settings.ollama_url,
        logger=logger,
    )
    health_state.set_proc("bot_manager", bot_manager_ref.popen)
    health_state.set_proc("streamlit", streamlit_ref.popen)

    server = ThreadingHTTPServer(("0.0.0.0", args.health_port), make_health_handler(health_state))
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    logger.write(
        "supervisor",
        "INFO",
        "Health API started.",
        metadata={"url": f"http://127.0.0.1:{args.health_port}/health"},
    )

    # Buffered fan-in queue from bot + streamlit stdout/stderr readers.
    q: queue.Queue = queue.Queue()
    threads = [
        threading.Thread(target=stream_reader, args=("bot_manager", bot_manager_ref.popen.stdout, q), daemon=True),
        threading.Thread(target=stream_reader, args=("bot_manager", bot_manager_ref.popen.stderr, q), daemon=True),
        threading.Thread(target=stream_reader, args=("streamlit", streamlit_ref.popen.stdout, q), daemon=True),
        threading.Thread(target=stream_reader, args=("streamlit", streamlit_ref.popen.stderr, q), daemon=True),
    ]
    for t in threads:
        t.start()

    stop_flag = {"value": False}

    def request_stop(_sig=None, _frame=None):
        stop_flag["value"] = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    last_health_log = 0.0
    consecutive_health_failures = 0
    last_alert_sent_ts = 0.0
    health_interval = max(5, int(settings.health_log_interval_s))
    alert_threshold = max(1, int(settings.health_alert_consecutive_failures))
    alert_webhook = settings.health_alert_webhook_url.strip()
    alert_cooldown_s = max(10, int(settings.health_alert_webhook_cooldown_s))
    while not stop_flag["value"]:
        try:
            service, line = q.get(timeout=0.5)
            metric = parse_aigm_metric_line(line)
            if metric:
                health_state.record_turn_metric(metric["name"], metric.get("fields", {}))
            logger.write(service, infer_level(line), line, source="subprocess")
        except queue.Empty:
            pass

        health_state.set_log_queue_depth(q.qsize())
        now = time.time()
        # Periodic structured snapshots make health regressions visible in UI and files.
        if now - last_health_log >= health_interval:
            snap = health_state.snapshot()
            ok = bool(snap["ok"])
            level = "INFO" if ok else "WARNING"
            logger.write("health", level, "Periodic health snapshot.", metadata=snap)
            if ok:
                consecutive_health_failures = 0
            else:
                consecutive_health_failures += 1
                if consecutive_health_failures >= alert_threshold:
                    alert_payload = {
                        "event": "aigm_health_alert",
                        "timestamp": utc_now_iso(),
                        "consecutive_failures": consecutive_health_failures,
                        "alert_threshold": alert_threshold,
                        "checks": snap.get("checks", {}),
                    }
                    logger.write(
                        "health",
                        "ERROR",
                        "Health failure alert threshold reached.",
                        metadata=alert_payload,
                    )
                    if alert_webhook and (now - last_alert_sent_ts) >= alert_cooldown_s:
                        try:
                            post_json_webhook(alert_webhook, alert_payload, timeout_s=8)
                            logger.write(
                                "health",
                                "INFO",
                                "Health alert webhook delivered.",
                                metadata={"webhook": "configured", "cooldown_s": alert_cooldown_s},
                            )
                            last_alert_sent_ts = now
                        except Exception as exc:
                            logger.write(
                                "health",
                                "ERROR",
                                "Health alert webhook delivery failed.",
                                metadata={"error": str(exc), "webhook": "configured"},
                            )
            last_health_log = now

        logger.flush(force=False)

        bot_manager_exit = bot_manager_ref.popen.poll()
        if bot_manager_exit is not None:
            logger.write("supervisor", "ERROR", "Bot manager process exited.", metadata={"exit_code": bot_manager_exit})
            stop_flag["value"] = True
        streamlit_exit = streamlit_ref.popen.poll()
        if streamlit_exit is not None:
            logger.write(
                "supervisor",
                "ERROR",
                "Streamlit process exited.",
                metadata={"exit_code": streamlit_exit},
            )
            stop_flag["value"] = True

    logger.write("supervisor", "INFO", "Supervisor stopping.")
    for proc in (bot_manager_ref.popen, streamlit_ref.popen):
        if proc.poll() is None:
            proc.terminate()
    time.sleep(1)
    for proc in (bot_manager_ref.popen, streamlit_ref.popen):
        if proc.poll() is None:
            proc.kill()

    logger.flush(force=True)
    server.shutdown()
    server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
