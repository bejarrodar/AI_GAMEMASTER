from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    discord_token: str = ""
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/aigm"
    database_sslmode: str = "require"
    database_connect_timeout_s: int = 10
    llm_provider: str = "stub"
    sys_admin_token: str = ""

    model_config = SettingsConfigDict(env_prefix="AIGM_", env_file=".env", extra="ignore")


settings = Settings()
