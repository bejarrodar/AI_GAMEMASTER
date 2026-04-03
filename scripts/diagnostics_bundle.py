from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from urllib import error, request

from aigm.config import settings


REDACT_PATTERNS = ("token", "password", "secret", "api_key", "key", "dsn")


def _redact_key(key: str) -> bool:
    lower = key.lower()
    return any(p in lower for p in REDACT_PATTERNS)


def _redact_map(values: dict) -> dict:
    out: dict = {}
    for k, v in values.items():
        if _redact_key(str(k)):
            out[str(k)] = "***REDACTED***"
        elif isinstance(v, dict):
            out[str(k)] = _redact_map(v)
        else:
            out[str(k)] = v
    return out


def _fetch_json(url: str, token: str = "", timeout_s: int = 6) -> dict:
    headers = {}
    if token.strip():
        headers["Authorization"] = f"Bearer {token.strip()}"
    req = request.Request(url=url, method="GET", headers=headers)
    try:
        with request.urlopen(req, timeout=timeout_s) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            return {"ok": True, "status": int(resp.status), "url": url, "payload": payload}
    except error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8")
        except Exception:
            body = str(exc)
        return {"ok": False, "status": int(exc.code), "url": url, "error": body}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": None, "url": url, "error": str(exc)}


def _fetch_text(url: str, timeout_s: int = 6) -> dict:
    req = request.Request(url=url, method="GET")
    try:
        with request.urlopen(req, timeout=timeout_s) as resp:
            payload = resp.read().decode("utf-8", errors="replace")
            return {"ok": True, "status": int(resp.status), "url": url, "payload": payload}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": None, "url": url, "error": str(exc)}


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def _collect_log_files(log_dir: Path, out_dir: Path, max_files: int, max_bytes_per_file: int) -> list[str]:
    copied: list[str] = []
    if not log_dir.exists():
        return copied
    candidates = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[: max(1, max_files)]
    dst_dir = out_dir / "log_files"
    dst_dir.mkdir(parents=True, exist_ok=True)
    for src in candidates:
        raw = src.read_bytes()
        if len(raw) > max_bytes_per_file:
            raw = raw[-max_bytes_per_file:]
        dst = dst_dir / src.name
        dst.write_bytes(raw)
        copied.append(str(dst.relative_to(out_dir)))
    return copied


def create_diagnostics_bundle(output_dir: Path, log_limit: int, include_log_files: bool) -> Path:
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    root = output_dir / f"diagnostics_{timestamp}"
    root.mkdir(parents=True, exist_ok=True)

    health_url = f"http://127.0.0.1:{int(settings.healthcheck_port)}/health"
    metrics_url = f"http://127.0.0.1:{int(settings.healthcheck_port)}/metrics"
    mgmt_url = f"http://127.0.0.1:{int(settings.management_api_port)}/api/v1/health"
    db_health_url = f"http://127.0.0.1:{int(settings.db_api_port)}/db/v1/health"
    db_logs_url = f"http://127.0.0.1:{int(settings.db_api_port)}/db/v1/logs/system?limit={max(1, int(log_limit))}"

    runtime_config = settings.model_dump() if hasattr(settings, "model_dump") else dict(settings.__dict__)
    env_subset = {k: os.environ.get(k, "") for k in os.environ if k.startswith("AIGM_")}
    _write_json(root / "config_runtime.json", _redact_map(runtime_config))
    _write_json(root / "config_env_subset.json", _redact_map(env_subset))

    _write_json(root / "health_runtime.json", _fetch_json(health_url))
    _write_json(root / "health_management_api.json", _fetch_json(mgmt_url, token=settings.sys_admin_token))
    _write_json(root / "health_db_api.json", _fetch_json(db_health_url, token=settings.db_api_token))
    _write_json(root / "logs_system_recent.json", _fetch_json(db_logs_url, token=settings.db_api_token))

    metrics = _fetch_text(metrics_url)
    (root / "metrics.txt").write_text(
        metrics.get("payload", metrics.get("error", "")),
        encoding="utf-8",
    )

    copied_files: list[str] = []
    if include_log_files:
        copied_files = _collect_log_files(
            log_dir=Path(settings.log_dir),
            out_dir=root,
            max_files=5,
            max_bytes_per_file=512 * 1024,
        )

    manifest = {
        "created_at": f"{timestamp}",
        "bundle_dir": str(root),
        "sources": {
            "health_runtime": health_url,
            "health_management_api": mgmt_url,
            "health_db_api": db_health_url,
            "metrics": metrics_url,
            "db_logs": db_logs_url,
        },
        "copied_log_files": copied_files,
    }
    _write_json(root / "manifest.json", manifest)

    zip_path = output_dir / f"{root.name}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file in root.rglob("*"):
            if file.is_file():
                zf.write(file, arcname=str(file.relative_to(root)))
    return zip_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a diagnostics bundle (config, health, logs, metrics, errors).")
    parser.add_argument("--output-dir", default="artifacts", help="Directory to write bundle zip and temp files.")
    parser.add_argument("--log-limit", type=int, default=300, help="Recent system-log rows to request from DB API.")
    parser.add_argument(
        "--include-log-files",
        action="store_true",
        help="Include recent local *.log files from AIGM_LOG_DIR (last 5, tailed).",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="aigm_diag_") as tmp:
        tmp_dir = Path(tmp)
        zip_path = create_diagnostics_bundle(
            output_dir=tmp_dir,
            log_limit=max(1, int(args.log_limit)),
            include_log_files=bool(args.include_log_files),
        )
        final_zip = output_dir / zip_path.name
        shutil.copy2(zip_path, final_zip)
        print(f"[diagnostics] bundle created: {final_zip}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
