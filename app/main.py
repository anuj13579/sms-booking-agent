"""FastAPI app factory.

    uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000

Then open http://127.0.0.1:8000 for the web-chat simulator.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.factory import agent_config, is_deepseek, make_provider
from app.agent.prompts import prompt_version
from app.agent.providers.base import LLMProvider
from app.api import simulator
from app.api.runtime import Runtime
from app.config import Settings, get_settings
from app.db.session import make_engine, make_sessionmaker
from app.domain.clock import Clock, SystemClock
from app.domain.notifications import LoggingNotifier, OwnerNotifier
from app.worker import run_worker

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    provider: LLMProvider | None = None,
    notifier: OwnerNotifier | None = None,
    clock: Clock | None = None,
) -> FastAPI:
    """Everything injectable, so tests can run the real app against a FakeProvider."""
    settings = settings or get_settings()
    engine = None if sessionmaker is not None else make_engine(settings.database_url)
    if sessionmaker is None:
        assert engine is not None
        sessionmaker = make_sessionmaker(engine)
    owned_provider = make_provider(settings) if provider is None else None
    runtime = Runtime(
        settings=settings,
        sessionmaker=sessionmaker,
        provider=provider or owned_provider,  # type: ignore[arg-type]
        agent_config=agent_config(settings),
        notifier=notifier or LoggingNotifier(),
        clock=clock or SystemClock(),
        prompt_version=prompt_version(),
        api_key_configured=settings.llm_key is not None or not is_deepseek(settings.llm_base_url),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        stop = asyncio.Event()
        worker = None
        if settings.worker_enabled:
            worker = asyncio.create_task(
                run_worker(
                    runtime.sessionmaker,
                    agent=runtime.agent(),
                    notifier=runtime.notifier,
                    clock=runtime.clock,
                    stop=stop,
                    poll_seconds=settings.worker_poll_seconds,
                    lease=timedelta(seconds=settings.conversation_lease_seconds),
                )
            )
        if not runtime.api_key_configured:
            logger.warning("No LLM API key configured: every agent turn will fall back.")
        try:
            yield
        finally:
            stop.set()
            if worker is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await worker
            if owned_provider is not None:
                await owned_provider.aclose()
            if engine is not None:
                await engine.dispose()

    app = FastAPI(title="SMS Booking Agent", lifespan=lifespan)
    app.state.runtime = runtime

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    if settings.simulator_enabled:
        app.include_router(simulator.router)
    return app
