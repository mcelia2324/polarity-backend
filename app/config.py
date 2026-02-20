from __future__ import annotations

import os

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_timezone: str = "UTC"
    send_hour: int = 8
    send_minute: int = 0

    # Accepts either a full DATABASE_URL or individual components.
    # Components are preferred in Cloud Run (password from Secret Manager).
    database_url: str | None = None
    database_host: str | None = None
    database_port: int = 5432
    database_name: str = "polarity"
    database_user: str = "polarity"
    database_password: str | None = None

    openai_api_key: str | None = None
    openai_model: str = "gpt-5.2"

    apns_key_id: str | None = None
    apns_team_id: str | None = None
    apns_bundle_id: str | None = None
    apns_auth_key: str | None = None
    apns_use_sandbox: bool = True

    cron_secret: str | None = None

    class Config:
        env_file = ".env"
        case_sensitive = False

    def get_database_url(self) -> str:
        """Return the async database URL, preferring components over full URL."""
        if self.database_host and self.database_password:
            return (
                f"postgresql+asyncpg://{self.database_user}:{self.database_password}"
                f"@{self.database_host}:{self.database_port}/{self.database_name}"
            )
        if self.database_url:
            return self.database_url
        # Fallback for local docker-compose development
        return "postgresql+asyncpg://polarity:polarity@db:5432/polarity"


settings = Settings()
