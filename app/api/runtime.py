"""Long-lived objects the HTTP handlers share, built once by the app factory."""

from dataclasses import dataclass

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.loop import Agent, AgentConfig
from app.agent.providers.base import LLMProvider
from app.config import Settings
from app.domain.clock import Clock
from app.domain.notifications import OwnerNotifier


@dataclass
class Runtime:
    settings: Settings
    sessionmaker: async_sessionmaker[AsyncSession]
    provider: LLMProvider
    agent_config: AgentConfig
    notifier: OwnerNotifier
    clock: Clock
    prompt_version: str
    api_key_configured: bool

    def agent(self) -> Agent:
        return Agent(self.sessionmaker, self.provider, self.clock, self.agent_config)


def get_runtime(request: Request) -> Runtime:
    runtime: Runtime = request.app.state.runtime
    return runtime
