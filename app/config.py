"""Application settings. Every secret and connection string comes from the environment."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+asyncpg://booking:booking@localhost:5432/booking_agent"
    # How long a worker may hold a conversation before another worker may re-claim it (D-024).
    conversation_lease_seconds: int = 120


@lru_cache
def get_settings() -> Settings:
    return Settings()
