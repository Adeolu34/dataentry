from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5433/data_entry_bot"

    # Redis
    redis_url: str = "redis://localhost:6380/0"

    # Portal
    portal_url: str = ""
    portal_user: str = ""
    portal_pass: str = ""
    portal_name: str = "Portal"

    # Directories
    input_files_dir: str = "/app/input_files"
    receipts_dir: str = "/app/receipts"
    screenshots_dir: str = "/app/screenshots"

    # Logging
    log_level: str = "INFO"

    # SMTP / alerts
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass: str = ""
    alert_email: str = ""

    # Bot behaviour
    max_retries: int = 3
    retry_delay_seconds: int = 5
    headless_browser: bool = True
    batch_size: int = 50

    @property
    def celery_broker_url(self) -> str:
        return self.redis_url

    @property
    def celery_result_backend(self) -> str:
        return self.redis_url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
