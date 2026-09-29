"""Build the production agent from settings."""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.loop import Agent, AgentConfig
from app.agent.providers.openai_compat import OpenAICompatibleProvider
from app.config import Settings
from app.domain.clock import Clock


def is_deepseek(base_url: str) -> bool:
    return "api.deepseek.com" in base_url


def make_provider(settings: Settings) -> OpenAICompatibleProvider:
    extra_body: dict[str, Any] = {}
    if is_deepseek(settings.llm_base_url):
        # V4 models think by default. Non-thinking is the agent default (DESIGN section 8).
        extra_body["thinking"] = {"type": "enabled" if settings.llm_thinking else "disabled"}
    return OpenAICompatibleProvider(
        base_url=settings.llm_base_url,
        api_key=settings.llm_key,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
        extra_body=extra_body,
        require_key=is_deepseek(settings.llm_base_url),
    )


def agent_config(settings: Settings) -> AgentConfig:
    return AgentConfig(
        model=settings.llm_model,
        max_steps=settings.agent_max_steps,
        turn_budget_seconds=settings.agent_turn_budget_seconds,
        call_timeout_seconds=settings.agent_call_timeout_seconds,
    )


def make_agent(
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    clock: Clock,
    provider: OpenAICompatibleProvider | None = None,
) -> Agent:
    return Agent(sessionmaker, provider or make_provider(settings), clock, agent_config(settings))
