"""Application settings. Every secret and connection string comes from the environment."""

from functools import lru_cache

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+asyncpg://booking:booking@localhost:5432/booking_agent"
    # How long a worker may hold a conversation before another worker may re-claim it (D-024).
    conversation_lease_seconds: int = 120

    # LLM provider (D-014). Any OpenAI-compatible endpoint works; DeepSeek is the default.
    llm_base_url: str = "https://api.deepseek.com"
    llm_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("LLM_API_KEY", "DEEPSEEK_API_KEY")
    )
    llm_model: str = "deepseek-flash"
    # DeepSeek V4 models think by default; the agent runs non-thinking unless an experiment
    # turns this on (DESIGN section 8).
    llm_thinking: bool = False
    # Low temperature for consistent tool use; a Phase 4 experiment knob, not a settled value.
    llm_temperature: float | None = 0.3
    llm_max_tokens: int = 1024

    @property
    def llm_key(self) -> str | None:
        """The API key, or None when unset or blank (``DEEPSEEK_API_KEY=`` in .env)."""
        value = self.llm_api_key.get_secret_value().strip() if self.llm_api_key else ""
        return value or None

    # Agent loop limits (DESIGN section 4).
    agent_max_steps: int = 6
    agent_turn_budget_seconds: float = 45.0
    agent_call_timeout_seconds: float = 20.0

    # Web-chat simulator (dev only: it lets anyone text the agent as any phone number) and the
    # background queue worker.
    simulator_enabled: bool = True
    simulator_business_number: str = "+15555550100"
    worker_enabled: bool = True
    worker_poll_seconds: float = 2.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
