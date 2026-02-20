import os
import json
import subprocess
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    discord_token: str = ""
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/aigm"
    database_sslmode: str = "require"
    database_connect_timeout_s: int = 10
    database_auto_init: bool = True
    database_use_alembic: bool = False
    llm_provider: str = "stub"
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5:7b-instruct"
    ollama_model_narration: str = ""
    ollama_model_intent: str = ""
    ollama_model_review: str = ""
    openai_api_key: str = ""
    openai_base_url: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_model_narration: str = ""
    openai_model_intent: str = ""
    openai_model_review: str = ""
    openai_timeout_s: int = 90
    ollama_timeout_s: int = 90
    ollama_gen_temperature: float = 0.35
    ollama_json_temperature: float = 0.1
    ollama_gen_num_predict: int = 320
    ollama_json_num_predict: int = 280
    llm_json_mode_strict: bool = True
    llm_http_max_retries: int = 2
    llm_http_retry_backoff_s: float = 0.75
    command_suggestion_min_confidence: float = 0.72
    story_fast_review_bypass: bool = True
    review_precheck_enabled: bool = True
    context_recent_turns: int = 6
    context_max_facts: int = 12
    context_turn_line_max_chars: int = 180
    context_token_budget_chars: int = 7000
    context_truncation_diagnostics: bool = True
    context_memory_summary_turns: int = 20
    context_memory_max_entries: int = 5
    sys_admin_token: str = ""
    auth_enforce: bool = False
    auth_bootstrap_admin_username: str = ""
    auth_bootstrap_admin_password: str = ""
    streamlit_port: int = 9531
    healthcheck_port: int = 9540
    management_api_port: int = 9541
    db_api_port: int = 9542
    db_api_url: str = "http://127.0.0.1:9542"
    db_api_token: str = ""
    gameplay_use_db_api: bool = False
    component_state_dir: str = "./component_state"
    healthcheck_url: str = "http://127.0.0.1:9540/health"
    health_log_interval_s: int = 30
    health_alert_consecutive_failures: int = 3
    health_alert_webhook_url: str = ""
    health_alert_webhook_cooldown_s: int = 300
    backup_encryption_passphrase: str = ""
    secret_source: str = "none"
    secret_source_json_file: str = ""
    secret_source_command: str = ""
    secret_source_aws_secret_id: str = ""
    secret_source_aws_region: str = ""
    secret_rotation_max_age_days: int = 30
    log_dir: str = "./logs"
    log_file_max_bytes: int = 10_485_760
    log_file_backup_count: int = 5
    log_db_batch_size: int = 50
    log_db_flush_interval_s: int = 2
    discord_rate_limit_window_s: int = 10
    discord_rate_limit_max_messages: int = 6
    turn_conflict_retries: int = 1
    turn_worker_queue_max: int = 200
    turn_worker_count: int = 2
    llm_circuit_breaker_failure_threshold: int = 5
    llm_circuit_breaker_reset_s: int = 60
    management_api_rate_limit_window_s: int = 60
    management_api_rate_limit_max_requests: int = 180
    management_api_mutation_rate_limit_window_s: int = 60
    management_api_mutation_rate_limit_max_requests: int = 60
    alert_turn_stall_s: int = 120
    alert_turn_stall_queue_depth: int = 1
    alert_fallback_window_s: int = 300
    alert_fallback_threshold: int = 5
    alert_latency_window_s: int = 300
    alert_latency_threshold_ms: int = 20000
    alert_latency_breach_count: int = 3
    alert_runtime_cooldown_s: int = 120
    management_api_idempotency_ttl_s: int = 3600
    management_api_idempotency_max_entries: int = 2000
    service_api_http_max_retries: int = 2
    service_api_http_retry_backoff_s: float = 0.5
    service_api_circuit_breaker_failure_threshold: int = 5
    service_api_circuit_breaker_reset_s: int = 60

    model_config = SettingsConfigDict(env_prefix="AIGM_", env_file=".env", extra="ignore")

    def model_post_init(self, __context) -> None:
        super().model_post_init(__context)
        self._apply_external_secret_source()
        self._apply_file_secrets()

    @staticmethod
    def _secret_field_map() -> dict[str, str]:
        return {
            "discord_token": "AIGM_DISCORD_TOKEN",
            "openai_api_key": "AIGM_OPENAI_API_KEY",
            "sys_admin_token": "AIGM_SYS_ADMIN_TOKEN",
            "database_url": "AIGM_DATABASE_URL",
            "backup_encryption_passphrase": "AIGM_BACKUP_ENCRYPTION_PASSPHRASE",
        }

    def _apply_external_secret_source(self) -> None:
        source = (self.secret_source or "none").strip().lower()
        if source == "none":
            return
        payload = self._load_external_secret_payload(source)
        if not payload:
            return
        field_map = self._secret_field_map()
        # Accept either field names or env key names in payload.
        for field_name, env_key in field_map.items():
            # Explicit env vars take precedence over external secret sources.
            explicit_env = os.getenv(env_key, "").strip()
            if explicit_env:
                continue
            value = payload.get(field_name)
            if value is None:
                value = payload.get(env_key)
            if value is None:
                continue
            val = str(value).strip()
            if not val:
                continue
            object.__setattr__(self, field_name, val)

    def _load_external_secret_payload(self, source: str) -> dict:
        try:
            if source == "json_file":
                path = self.secret_source_json_file.strip()
                if not path:
                    return {}
                return json.loads(Path(path).read_text(encoding="utf-8"))
            if source == "command":
                cmd = self.secret_source_command.strip()
                if not cmd:
                    return {}
                proc = subprocess.run(
                    cmd,
                    shell=True,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                return json.loads(proc.stdout or "{}")
            if source == "aws_secrets_manager":
                secret_id = self.secret_source_aws_secret_id.strip()
                if not secret_id:
                    return {}
                cmd = [
                    "aws",
                    "secretsmanager",
                    "get-secret-value",
                    "--secret-id",
                    secret_id,
                    "--query",
                    "SecretString",
                    "--output",
                    "text",
                ]
                region = self.secret_source_aws_region.strip()
                if region:
                    cmd.extend(["--region", region])
                proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
                secret_str = (proc.stdout or "").strip()
                if not secret_str:
                    return {}
                return json.loads(secret_str)
        except Exception:
            return {}
        return {}

    def _apply_file_secrets(self) -> None:
        # Supports production secret injection via env vars like AIGM_DISCORD_TOKEN_FILE.
        mapping = {
            "discord_token": "AIGM_DISCORD_TOKEN_FILE",
            "openai_api_key": "AIGM_OPENAI_API_KEY_FILE",
            "sys_admin_token": "AIGM_SYS_ADMIN_TOKEN_FILE",
            "database_url": "AIGM_DATABASE_URL_FILE",
            "backup_encryption_passphrase": "AIGM_BACKUP_ENCRYPTION_PASSPHRASE_FILE",
        }
        for field_name, env_name in mapping.items():
            file_path = os.getenv(env_name, "").strip()
            if not file_path:
                continue
            try:
                secret = Path(file_path).read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if not secret:
                continue
            env_key = self._secret_field_map().get(field_name, "")
            explicit_env = os.getenv(env_key, "").strip() if env_key else ""
            if explicit_env:
                continue
            object.__setattr__(self, field_name, secret)


settings = Settings()
