from __future__ import annotations

import time

from aigm.config import settings
from aigm.ops.supervisor import RuntimeAlertMonitor


def _set(key: str, value):
    old = getattr(settings, key)
    setattr(settings, key, value)
    return old


def test_runtime_alert_monitor_detects_fallback_spike() -> None:
    old_window = _set("alert_fallback_window_s", 60)
    old_threshold = _set("alert_fallback_threshold", 2)
    try:
        monitor = RuntimeAlertMonitor()
        monitor.observe_line("[LLMAdapter] Ollama failed, using fallback: TimeoutError")
        monitor.observe_line("[LLMAdapter] OpenAI failed, using fallback: TimeoutError")
        alerts = monitor.evaluate(queue_depth=0)
        assert any(a.get("type") == "fallback_spike" for a in alerts)
    finally:
        setattr(settings, "alert_fallback_window_s", old_window)
        setattr(settings, "alert_fallback_threshold", old_threshold)


def test_runtime_alert_monitor_detects_latency_anomaly() -> None:
    old_window = _set("alert_latency_window_s", 60)
    old_threshold = _set("alert_latency_threshold_ms", 100)
    old_breaches = _set("alert_latency_breach_count", 2)
    try:
        monitor = RuntimeAlertMonitor()
        monitor.observe_metric("turn_success", {"latency_ms": "150"})
        monitor.observe_metric("turn_success", {"latency_ms": "220"})
        alerts = monitor.evaluate(queue_depth=0)
        assert any(a.get("type") == "latency_anomaly" for a in alerts)
    finally:
        setattr(settings, "alert_latency_window_s", old_window)
        setattr(settings, "alert_latency_threshold_ms", old_threshold)
        setattr(settings, "alert_latency_breach_count", old_breaches)


def test_runtime_alert_monitor_detects_stalled_turns() -> None:
    old_stall = _set("alert_turn_stall_s", 10)
    old_depth = _set("alert_turn_stall_queue_depth", 1)
    try:
        monitor = RuntimeAlertMonitor()
        monitor._last_turn_metric_ts = time.time() - 12.0  # noqa: SLF001 - intentional for deterministic test
        alerts = monitor.evaluate(queue_depth=1)
        assert any(a.get("type") == "turn_stalled" for a in alerts)
    finally:
        setattr(settings, "alert_turn_stall_s", old_stall)
        setattr(settings, "alert_turn_stall_queue_depth", old_depth)


def test_runtime_alert_monitor_cooldown() -> None:
    old_cooldown = _set("alert_runtime_cooldown_s", 30)
    try:
        monitor = RuntimeAlertMonitor()
        assert monitor.should_emit("fallback_spike") is True
        assert monitor.should_emit("fallback_spike") is False
    finally:
        setattr(settings, "alert_runtime_cooldown_s", old_cooldown)
