from __future__ import annotations

from urllib import error

from aigm.adapters.llm import LLMAdapter
from aigm.config import settings
from aigm.ops.db_api_client import DBApiClient, DBApiRequestError


def test_chaos_db_api_outage_retries_then_fails(monkeypatch) -> None:
    calls = {"count": 0}

    def _raise_url_error(*_args, **_kwargs):
        calls["count"] += 1
        raise error.URLError("db outage")

    monkeypatch.setattr("aigm.ops.db_api_client.request.urlopen", _raise_url_error)
    monkeypatch.setattr(settings, "service_api_http_max_retries", 1)
    monkeypatch.setattr(settings, "service_api_http_retry_backoff_s", 0.0)
    monkeypatch.setattr(settings, "service_api_circuit_breaker_failure_threshold", 10)
    monkeypatch.setattr(settings, "service_api_circuit_breaker_reset_s", 1)

    client = DBApiClient(base_url="http://127.0.0.1:9", timeout_s=1)
    try:
        client.health()
        raise AssertionError("expected DBApiRequestError")
    except DBApiRequestError as exc:
        assert exc.error_code == "service_api_unavailable"
    assert calls["count"] == 2


def test_chaos_db_api_circuit_breaker_opens_after_failures(monkeypatch) -> None:
    def _raise_url_error(*_args, **_kwargs):
        raise error.URLError("db outage")

    monkeypatch.setattr("aigm.ops.db_api_client.request.urlopen", _raise_url_error)
    monkeypatch.setattr(settings, "service_api_http_max_retries", 0)
    monkeypatch.setattr(settings, "service_api_http_retry_backoff_s", 0.0)
    monkeypatch.setattr(settings, "service_api_circuit_breaker_failure_threshold", 2)
    monkeypatch.setattr(settings, "service_api_circuit_breaker_reset_s", 60)

    client = DBApiClient(base_url="http://127.0.0.1:9", timeout_s=1)
    for _ in range(2):
        try:
            client.health()
        except DBApiRequestError:
            pass
    try:
        client.health()
        raise AssertionError("expected circuit-open DBApiRequestError")
    except DBApiRequestError as exc:
        assert exc.error_code == "service_api_circuit_open"


def test_chaos_ollama_outage_generate_fallback(monkeypatch) -> None:
    def _boom(*_args, **_kwargs):
        raise error.URLError("ollama down")

    monkeypatch.setattr(settings, "llm_provider", "ollama")
    monkeypatch.setattr(LLMAdapter, "_generate_with_ollama", _boom)
    adapter = LLMAdapter()
    out = adapter.generate(
        user_input="I search the market for herbs.",
        state_json='{"scene":"town"}',
        mode="dnd",
        context_json="{}",
        system_prompt="",
    )
    assert isinstance(out.narration, str) and out.narration.strip()


def test_chaos_slow_llm_timeout_generate_fallback(monkeypatch) -> None:
    def _timeout(*_args, **_kwargs):
        raise TimeoutError("llm timeout")

    monkeypatch.setattr(settings, "llm_provider", "ollama")
    monkeypatch.setattr(LLMAdapter, "_generate_with_ollama", _timeout)
    adapter = LLMAdapter()
    out = adapter.generate(
        user_input="I dance in the town square.",
        state_json='{"scene":"town"}',
        mode="story",
        context_json="{}",
        system_prompt="",
    )
    assert isinstance(out.narration, str) and out.narration.strip()
