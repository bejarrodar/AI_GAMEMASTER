from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path


def _load_module(module_name: str, rel_path: str):
    path = Path(rel_path)
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_diagnostics_redaction_masks_secret_keys() -> None:
    mod = _load_module("diag_bundle", "scripts/diagnostics_bundle.py")
    payload = {
        "AIGM_DB_API_TOKEN": "abc",
        "nested": {"password": "x", "safe": "ok"},
        "AIGM_STREAMLIT_PORT": 9531,
    }
    redacted = mod._redact_map(payload)
    assert redacted["AIGM_DB_API_TOKEN"] == "***REDACTED***"
    assert redacted["nested"]["password"] == "***REDACTED***"
    assert redacted["nested"]["safe"] == "ok"
    assert redacted["AIGM_STREAMLIT_PORT"] == 9531


def test_nightly_soak_smoke_run_returns_summary_shape() -> None:
    cmd = [
        sys.executable,
        "scripts/nightly_soak.py",
        "--turns",
        "5",
        "--players",
        "2",
        "--mode",
        "story",
        "--sample-every",
        "2",
        "--max-failures",
        "10",
        "--max-p95-ms",
        "99999",
        "--max-memory-growth-mb",
        "99999",
        "--database-url",
        "sqlite:///:memory:",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr
    output = proc.stdout.lower()
    assert "latency_ms" in output
    assert "memory_mb" in output


def test_benchmark_modes_smoke_run_outputs_cases() -> None:
    cmd = [
        sys.executable,
        "scripts/benchmark_modes.py",
        "--turns",
        "5",
        "--players",
        "2",
        "--thread-prefix",
        "bench-smoke",
        "--database-url",
        "sqlite:///:memory:",
        "--max-failures",
        "10",
        "--max-p95-ms",
        "99999",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr
    out = proc.stdout.lower()
    assert '"label": "dnd"' in out
    assert '"label": "story"' in out
    assert '"label": "crew"' in out
