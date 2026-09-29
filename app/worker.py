"""The background queue poller (D-012, D-024).

Channels normally process a message right after storing it. The poller is the safety net: it
answers anything still ``received``, such as messages whose worker crashed mid-turn (the lease
expires and the conversation becomes claimable again).
"""

import asyncio
import contextlib
import logging
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.clock import Clock
from app.domain.notifications import OwnerNotifier
from app.pipeline.inbound import TurnAgent, process_next

logger = logging.getLogger(__name__)


async def run_worker(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    agent: TurnAgent,
    notifier: OwnerNotifier,
    clock: Clock,
    stop: asyncio.Event,
    poll_seconds: float = 2.0,
    lease: timedelta = timedelta(seconds=120),
) -> None:
    while not stop.is_set():
        outcome = None
        try:
            outcome = await process_next(
                sessionmaker, agent=agent, notifier=notifier, clock=clock, lease=lease
            )
        except Exception:
            logger.exception("queue worker step failed")
        if outcome is None:  # idle (or failed): wait before polling again
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), poll_seconds)
