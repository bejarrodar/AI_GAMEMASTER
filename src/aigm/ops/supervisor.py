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
from urllib.parse import parse_qs, urlparse

from sqlalchemy import text

from aigm.config import settings
from aigm.db.models import AdminAuditLog, SystemLog
from aigm.db.session import SessionLocal
from aigm.ops.component_store import ComponentStore
from aigm.ops.db_api_client import DBApiClient

try:
    from openai import OpenAI
except Exception:  # pragma: no cover - OpenAI dependency may be optional in some installs.
    OpenAI = None


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


def _read_env_file(env_path: Path) -> dict[str, str]:
    if not env_path.exists():
        return {}
    out: dict[str, str] = {}
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def _upsert_env_values(env_path: Path, updates: dict[str, str]) -> None:
    env_path.parent.mkdir(parents=True, exist_ok=True)
    if not env_path.exists():
        env_path.write_text("", encoding="utf-8")
    lines = env_path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if "=" not in stripped or stripped.startswith("#"):
            out.append(line)
            continue
        key, _value = stripped.split("=", 1)
        k = key.strip()
        if k in updates:
            out.append(f"{k}={updates[k]}")
            seen.add(k)
        else:
            out.append(line)
    for k, v in updates.items():
        if k not in seen:
            out.append(f"{k}={v}")
    env_path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


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


class ManagementState:
    def __init__(
        self,
        *,
        logger: UnifiedLogger,
        health_state: HealthState,
        env_path: Path,
        streamlit_port: int,
        health_port: int,
        db_api_url: str,
    ) -> None:
        self.logger = logger
        self.health_state = health_state
        self.env_path = env_path
        self.streamlit_port = int(streamlit_port)
        self.health_port = int(health_port)
        token = settings.sys_admin_token.strip()
        self.api_token = token
        self._lock = threading.Lock()
        self.store = ComponentStore("management_api")
        db_api_url = str(self.store.get("db_api_url", db_api_url)).strip() or db_api_url
        db_api_token = str(self.store.get("db_api_token", settings.db_api_token)).strip() or settings.db_api_token
        self.db_api = DBApiClient(base_url=db_api_url, token=db_api_token, timeout_s=12)
        self.store.set("db_api_url", db_api_url)
        self.store.set("db_api_token", db_api_token)

    def auth_ok(self, authorization: str) -> bool:
        token = self.api_token.strip()
        if not token:
            return True
        value = (authorization or "").strip()
        expected = f"Bearer {token}"
        return value == expected

    def _audit(self, action: str, metadata: dict | None = None) -> None:
        self.logger.write(
            "api",
            "INFO",
            action,
            source="management_api",
            metadata=metadata or {},
        )

    def get_llm_config(self) -> dict:
        file_values = _read_env_file(self.env_path)
        return {
            "runtime": {
                "provider": settings.llm_provider,
                "ollama_url": settings.ollama_url,
                "ollama_model": settings.ollama_model,
                "ollama_model_narration": settings.ollama_model_narration,
                "ollama_model_intent": settings.ollama_model_intent,
                "ollama_model_review": settings.ollama_model_review,
                "openai_base_url": settings.openai_base_url,
                "openai_model": settings.openai_model,
                "openai_model_narration": settings.openai_model_narration,
                "openai_model_intent": settings.openai_model_intent,
                "openai_model_review": settings.openai_model_review,
                "openai_timeout_s": settings.openai_timeout_s,
                "ollama_timeout_s": settings.ollama_timeout_s,
                "ollama_gen_temperature": settings.ollama_gen_temperature,
                "ollama_json_temperature": settings.ollama_json_temperature,
                "ollama_gen_num_predict": settings.ollama_gen_num_predict,
                "ollama_json_num_predict": settings.ollama_json_num_predict,
                "llm_json_mode_strict": settings.llm_json_mode_strict,
                "has_openai_api_key": bool(settings.openai_api_key.strip()),
            },
            "persisted_env": {
                "AIGM_LLM_PROVIDER": file_values.get("AIGM_LLM_PROVIDER", ""),
                "AIGM_OLLAMA_URL": file_values.get("AIGM_OLLAMA_URL", ""),
                "AIGM_OLLAMA_MODEL": file_values.get("AIGM_OLLAMA_MODEL", ""),
                "AIGM_OLLAMA_MODEL_NARRATION": file_values.get("AIGM_OLLAMA_MODEL_NARRATION", ""),
                "AIGM_OLLAMA_MODEL_INTENT": file_values.get("AIGM_OLLAMA_MODEL_INTENT", ""),
                "AIGM_OLLAMA_MODEL_REVIEW": file_values.get("AIGM_OLLAMA_MODEL_REVIEW", ""),
                "AIGM_OPENAI_BASE_URL": file_values.get("AIGM_OPENAI_BASE_URL", ""),
                "AIGM_OPENAI_MODEL": file_values.get("AIGM_OPENAI_MODEL", ""),
                "AIGM_OPENAI_MODEL_NARRATION": file_values.get("AIGM_OPENAI_MODEL_NARRATION", ""),
                "AIGM_OPENAI_MODEL_INTENT": file_values.get("AIGM_OPENAI_MODEL_INTENT", ""),
                "AIGM_OPENAI_MODEL_REVIEW": file_values.get("AIGM_OPENAI_MODEL_REVIEW", ""),
                "AIGM_OPENAI_TIMEOUT_S": file_values.get("AIGM_OPENAI_TIMEOUT_S", ""),
                "AIGM_OLLAMA_TIMEOUT_S": file_values.get("AIGM_OLLAMA_TIMEOUT_S", ""),
                "AIGM_OLLAMA_GEN_TEMPERATURE": file_values.get("AIGM_OLLAMA_GEN_TEMPERATURE", ""),
                "AIGM_OLLAMA_JSON_TEMPERATURE": file_values.get("AIGM_OLLAMA_JSON_TEMPERATURE", ""),
                "AIGM_OLLAMA_GEN_NUM_PREDICT": file_values.get("AIGM_OLLAMA_GEN_NUM_PREDICT", ""),
                "AIGM_OLLAMA_JSON_NUM_PREDICT": file_values.get("AIGM_OLLAMA_JSON_NUM_PREDICT", ""),
                "AIGM_LLM_JSON_MODE_STRICT": file_values.get("AIGM_LLM_JSON_MODE_STRICT", ""),
                "AIGM_OPENAI_API_KEY": "***" if file_values.get("AIGM_OPENAI_API_KEY") else "",
            },
        }

    def update_llm_config(self, payload: dict) -> dict:
        map_keys = {
            "provider": "AIGM_LLM_PROVIDER",
            "ollama_url": "AIGM_OLLAMA_URL",
            "ollama_model": "AIGM_OLLAMA_MODEL",
            "ollama_model_narration": "AIGM_OLLAMA_MODEL_NARRATION",
            "ollama_model_intent": "AIGM_OLLAMA_MODEL_INTENT",
            "ollama_model_review": "AIGM_OLLAMA_MODEL_REVIEW",
            "openai_base_url": "AIGM_OPENAI_BASE_URL",
            "openai_model": "AIGM_OPENAI_MODEL",
            "openai_model_narration": "AIGM_OPENAI_MODEL_NARRATION",
            "openai_model_intent": "AIGM_OPENAI_MODEL_INTENT",
            "openai_model_review": "AIGM_OPENAI_MODEL_REVIEW",
            "openai_timeout_s": "AIGM_OPENAI_TIMEOUT_S",
            "ollama_timeout_s": "AIGM_OLLAMA_TIMEOUT_S",
            "ollama_gen_temperature": "AIGM_OLLAMA_GEN_TEMPERATURE",
            "ollama_json_temperature": "AIGM_OLLAMA_JSON_TEMPERATURE",
            "ollama_gen_num_predict": "AIGM_OLLAMA_GEN_NUM_PREDICT",
            "ollama_json_num_predict": "AIGM_OLLAMA_JSON_NUM_PREDICT",
            "llm_json_mode_strict": "AIGM_LLM_JSON_MODE_STRICT",
            "openai_api_key": "AIGM_OPENAI_API_KEY",
        }
        updates: dict[str, str] = {}
        for src, dst in map_keys.items():
            if src not in payload:
                continue
            val = payload[src]
            if isinstance(val, bool):
                updates[dst] = "true" if val else "false"
            else:
                updates[dst] = str(val)
        if updates:
            with self._lock:
                _upsert_env_values(self.env_path, updates)
            self._audit("llm_config_updated", {"keys": sorted(updates.keys())})
        return {
            "updated_keys": sorted(updates.keys()),
            "restart_required": True,
            "message": "Configuration persisted to .env. Restart services to apply runtime settings.",
        }

    def get_web_config(self) -> dict:
        env = _read_env_file(self.env_path)
        return {
            "runtime": {
                "streamlit_port": self.streamlit_port,
                "healthcheck_port": self.health_port,
                "healthcheck_url": settings.healthcheck_url,
                "log_dir": settings.log_dir,
            },
            "persisted_env": {
                "AIGM_STREAMLIT_PORT": env.get("AIGM_STREAMLIT_PORT", ""),
                "AIGM_HEALTHCHECK_PORT": env.get("AIGM_HEALTHCHECK_PORT", ""),
                "AIGM_HEALTHCHECK_URL": env.get("AIGM_HEALTHCHECK_URL", ""),
                "AIGM_LOG_DIR": env.get("AIGM_LOG_DIR", ""),
            },
        }

    def update_web_config(self, payload: dict) -> dict:
        map_keys = {
            "streamlit_port": "AIGM_STREAMLIT_PORT",
            "healthcheck_port": "AIGM_HEALTHCHECK_PORT",
            "healthcheck_url": "AIGM_HEALTHCHECK_URL",
            "log_dir": "AIGM_LOG_DIR",
        }
        updates: dict[str, str] = {}
        for src, dst in map_keys.items():
            if src in payload:
                updates[dst] = str(payload[src])
        if updates:
            with self._lock:
                _upsert_env_values(self.env_path, updates)
            self._audit("web_config_updated", {"keys": sorted(updates.keys())})
        return {
            "updated_keys": sorted(updates.keys()),
            "restart_required": True,
            "message": "Configuration persisted to .env. Restart services to apply runtime settings.",
        }

    @staticmethod
    def _mask_token(token: str) -> str:
        value = token.strip()
        if len(value) <= 8:
            return "*" * len(value)
        return f"{value[:4]}...{value[-4:]}"

    def list_bot_configs(self) -> list[dict]:
        rows = self.db_api.list_bots(enabled_only=None)
        return [
            {
                "id": int(row.get("id", 0) or 0),
                "name": str(row.get("name", "") or ""),
                "is_enabled": bool(row.get("is_enabled", False)),
                "notes": str(row.get("notes", "") or ""),
                "token_masked": self._mask_token(str(row.get("discord_token", "") or "")),
                "updated_at": row.get("updated_at"),
            }
            for row in rows
        ]

    def create_bot_config(self, payload: dict) -> dict:
        created = self.db_api.create_bot(payload)
        self._audit("bot_config_created", {"id": created.get("id"), "name": created.get("name")})
        return {"id": created.get("id"), "name": created.get("name")}

    def update_bot_config(self, bot_id: int, payload: dict) -> dict:
        updated = self.db_api.update_bot(int(bot_id), payload)
        self._audit("bot_config_updated", {"id": updated.get("id"), "name": updated.get("name")})
        return {"id": updated.get("id"), "name": updated.get("name")}

    def delete_bot_config(self, bot_id: int) -> dict:
        deleted = self.db_api.delete_bot(int(bot_id))
        self._audit("bot_config_deleted", {"id": deleted.get("id")})
        return {"id": deleted.get("id"), "deleted": bool(deleted.get("deleted", False))}

    def get_system_logs(self, *, limit: int, service: str, level: str) -> list[dict]:
        return self.db_api.list_system_logs(limit=limit, service=service, level=level)

    def get_audit_logs(self, *, limit: int) -> list[dict]:
        return self.db_api.list_audit_logs(limit=limit)

    def check_db(self) -> tuple[bool, str]:
        try:
            payload = self.db_api.health()
            checks = payload.get("checks", {}) or {}
            db_check = checks.get("db", {})
            return bool(db_check.get("ok", False)), str(db_check.get("detail", "unknown"))
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    @staticmethod
    def check_ollama() -> tuple[bool, str]:
        try:
            req = request.Request(f"{settings.ollama_url.rstrip('/')}/api/tags", method="GET")
            with request.urlopen(req, timeout=8) as resp:
                _ = resp.read()
            return True, "reachable"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    @staticmethod
    def check_openai() -> tuple[bool, str]:
        if OpenAI is None:
            return False, "openai package not installed"
        if not settings.openai_api_key.strip():
            return False, "AIGM_OPENAI_API_KEY is not configured"
        try:
            kwargs: dict = {"api_key": settings.openai_api_key.strip()}
            if settings.openai_base_url.strip():
                kwargs["base_url"] = settings.openai_base_url.strip()
            client = OpenAI(**kwargs)
            _ = client.models.list()
            return True, "reachable"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)


def make_management_handler(state: ManagementState):
    class ManagementHandler(BaseHTTPRequestHandler):
        def _send_json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0:
                return {}
            raw = self.rfile.read(length).decode("utf-8")
            if not raw.strip():
                return {}
            return json.loads(raw)

        def _require_auth(self) -> bool:
            auth = self.headers.get("Authorization", "")
            if state.auth_ok(auth):
                return True
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return False

        def do_GET(self):  # noqa: N802
            if not self._require_auth():
                return
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            try:
                if path == "/api/v1/meta":
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "service": "aigm_management_api",
                            "timestamp": utc_now_iso(),
                            "runtime": {
                                "streamlit_port": state.streamlit_port,
                                "healthcheck_port": state.health_port,
                                "has_auth_token": bool(state.api_token),
                                "db_api_url": state.db_api.base_url,
                            },
                            "endpoints": {
                                "config_llm": "/api/v1/config/llm",
                                "config_web": "/api/v1/config/web",
                                "bots": "/api/v1/bots",
                                "logs_system": "/api/v1/logs/system",
                                "logs_audit": "/api/v1/logs/audit",
                                "debug_checks": "/api/v1/debug/checks/*",
                                "db_api_health": f"{state.db_api.base_url}/db/v1/health",
                            },
                        },
                    )
                    return
                if path == "/api/v1/health":
                    snap = state.health_state.snapshot()
                    self._send_json(200 if snap.get("ok") else 503, {"ok": bool(snap.get("ok")), "health": snap})
                    return
                if path == "/api/v1/config/llm":
                    self._send_json(200, {"ok": True, "config": state.get_llm_config()})
                    return
                if path == "/api/v1/config/web":
                    self._send_json(200, {"ok": True, "config": state.get_web_config()})
                    return
                if path == "/api/v1/bots":
                    self._send_json(200, {"ok": True, "bots": state.list_bot_configs()})
                    return
                if path == "/api/v1/logs/system":
                    limit = max(1, min(500, int((query.get("limit", ["100"]) or ["100"])[0])))
                    service = str((query.get("service", [""]) or [""])[0]).strip()
                    level = str((query.get("level", [""]) or [""])[0]).strip().upper()
                    rows = state.get_system_logs(limit=limit, service=service, level=level)
                    self._send_json(200, {"ok": True, "rows": rows})
                    return
                if path == "/api/v1/logs/audit":
                    limit = max(1, min(500, int((query.get("limit", ["100"]) or ["100"])[0])))
                    rows = state.get_audit_logs(limit=limit)
                    self._send_json(200, {"ok": True, "rows": rows})
                    return
                self._send_json(404, {"ok": False, "error": "not_found"})
            except Exception as exc:  # noqa: BLE001
                state.logger.write("api", "ERROR", "GET request failed", source="management_api", metadata={"error": str(exc)})
                self._send_json(500, {"ok": False, "error": str(exc)})

        def do_POST(self):  # noqa: N802
            if not self._require_auth():
                return
            parsed = urlparse(self.path)
            path = parsed.path
            try:
                payload = self._read_json()
                if path == "/api/v1/bots":
                    created = state.create_bot_config(payload)
                    self._send_json(201, {"ok": True, "created": created})
                    return
                if path == "/api/v1/debug/checks/db":
                    ok, detail = state.check_db()
                    self._send_json(200 if ok else 503, {"ok": ok, "check": "db", "detail": detail})
                    return
                if path == "/api/v1/debug/checks/ollama":
                    ok, detail = state.check_ollama()
                    self._send_json(200 if ok else 503, {"ok": ok, "check": "ollama", "detail": detail})
                    return
                if path == "/api/v1/debug/checks/openai":
                    ok, detail = state.check_openai()
                    self._send_json(200 if ok else 503, {"ok": ok, "check": "openai", "detail": detail})
                    return
                self._send_json(404, {"ok": False, "error": "not_found"})
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                state.logger.write("api", "ERROR", "POST request failed", source="management_api", metadata={"error": str(exc)})
                self._send_json(500, {"ok": False, "error": str(exc)})

        def do_PUT(self):  # noqa: N802
            if not self._require_auth():
                return
            parsed = urlparse(self.path)
            path = parsed.path
            try:
                payload = self._read_json()
                if path == "/api/v1/config/llm":
                    result = state.update_llm_config(payload)
                    self._send_json(200, {"ok": True, **result})
                    return
                if path == "/api/v1/config/web":
                    result = state.update_web_config(payload)
                    self._send_json(200, {"ok": True, **result})
                    return
                if path.startswith("/api/v1/bots/"):
                    bot_id = int(path.rsplit("/", 1)[-1])
                    updated = state.update_bot_config(bot_id, payload)
                    self._send_json(200, {"ok": True, "updated": updated})
                    return
                self._send_json(404, {"ok": False, "error": "not_found"})
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                state.logger.write("api", "ERROR", "PUT request failed", source="management_api", metadata={"error": str(exc)})
                self._send_json(500, {"ok": False, "error": str(exc)})

        def do_DELETE(self):  # noqa: N802
            if not self._require_auth():
                return
            parsed = urlparse(self.path)
            path = parsed.path
            try:
                if path.startswith("/api/v1/bots/"):
                    bot_id = int(path.rsplit("/", 1)[-1])
                    deleted = state.delete_bot_config(bot_id)
                    self._send_json(200, {"ok": True, "deleted": deleted})
                    return
                self._send_json(404, {"ok": False, "error": "not_found"})
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                state.logger.write(
                    "api",
                    "ERROR",
                    "DELETE request failed",
                    source="management_api",
                    metadata={"error": str(exc)},
                )
                self._send_json(500, {"ok": False, "error": str(exc)})

        def log_message(self, _format, *args):
            return

    return ManagementHandler


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
    parser.add_argument("--management-port", type=int, default=settings.management_api_port)
    parser.add_argument("--db-api-port", type=int, default=settings.db_api_port)
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
        db_api_ref = start_process(
            "db_api",
            [python_exe, "-m", "aigm.ops.db_api", "--port", str(args.db_api_port)],
            cwd=args.cwd,
        )
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
    health_state.set_proc("db_api", db_api_ref.popen)
    health_state.set_proc("bot_manager", bot_manager_ref.popen)
    health_state.set_proc("streamlit", streamlit_ref.popen)
    logger.write(
        "supervisor",
        "INFO",
        "DB API process started.",
        metadata={"url": f"http://127.0.0.1:{args.db_api_port}/db/v1/health"},
    )

    server = ThreadingHTTPServer(("0.0.0.0", args.health_port), make_health_handler(health_state))
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    logger.write(
        "supervisor",
        "INFO",
        "Health API started.",
        metadata={"url": f"http://127.0.0.1:{args.health_port}/health"},
    )

    management_state = ManagementState(
        logger=logger,
        health_state=health_state,
        env_path=Path(args.cwd) / ".env",
        streamlit_port=args.streamlit_port,
        health_port=args.health_port,
        db_api_url=f"http://127.0.0.1:{args.db_api_port}",
    )
    management_server = ThreadingHTTPServer(("0.0.0.0", args.management_port), make_management_handler(management_state))
    management_thread = threading.Thread(target=management_server.serve_forever, daemon=True)
    management_thread.start()
    logger.write(
        "supervisor",
        "INFO",
        "Management API started.",
        metadata={"url": f"http://127.0.0.1:{args.management_port}/api/v1/meta"},
    )

    # Buffered fan-in queue from bot + streamlit stdout/stderr readers.
    q: queue.Queue = queue.Queue()
    threads = [
        threading.Thread(target=stream_reader, args=("db_api", db_api_ref.popen.stdout, q), daemon=True),
        threading.Thread(target=stream_reader, args=("db_api", db_api_ref.popen.stderr, q), daemon=True),
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

        db_api_exit = db_api_ref.popen.poll()
        if db_api_exit is not None:
            logger.write("supervisor", "ERROR", "DB API process exited.", metadata={"exit_code": db_api_exit})
            stop_flag["value"] = True
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
    for proc in (db_api_ref.popen, bot_manager_ref.popen, streamlit_ref.popen):
        if proc.poll() is None:
            proc.terminate()
    time.sleep(1)
    for proc in (db_api_ref.popen, bot_manager_ref.popen, streamlit_ref.popen):
        if proc.poll() is None:
            proc.kill()

    logger.flush(force=True)
    server.shutdown()
    server.server_close()
    management_server.shutdown()
    management_server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
