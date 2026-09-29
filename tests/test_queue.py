"""The Postgres work queue and per-conversation leases (D-012, D-024)."""

import asyncio
import uuid
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.clock import FixedClock
from app.domain.enums import Channel
from app.pipeline import queue
from app.pipeline.inbound import receive_inbound
from tests.factories import World, build_world, local

LEASE = timedelta(seconds=120)


async def setup(
    sessionmaker: async_sessionmaker[AsyncSession], phones: list[str]
) -> tuple[World, FixedClock, list[uuid.UUID]]:
    clock = FixedClock(local(date(2026, 10, 5), 9))
    conversation_ids = []
    async with sessionmaker() as session, session.begin():
        world = await build_world(session)
        for phone in phones:
            received = await receive_inbound(
                session,
                world.business,
                channel=Channel.SMS,
                from_number=phone,
                body="hi",
                clock=clock,
            )
            assert received.conversation_id is not None
            conversation_ids.append(received.conversation_id)
            clock.advance(timedelta(seconds=1))
    return world, clock, conversation_ids


async def test_claims_oldest_pending_first_and_skips_leased(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _, clock, (first, second) = await setup(sessionmaker, ["+15550000001", "+15550000002"])

    async with sessionmaker() as session, session.begin():
        claimed_a = await queue.claim_next(session, clock.now(), LEASE)
    async with sessionmaker() as session, session.begin():
        claimed_b = await queue.claim_next(session, clock.now(), LEASE)
    async with sessionmaker() as session, session.begin():
        claimed_c = await queue.claim_next(session, clock.now(), LEASE)

    assert claimed_a is not None and claimed_a[0] == first
    assert claimed_b is not None and claimed_b[0] == second
    assert claimed_c is None


async def test_expired_lease_is_reclaimed_and_stale_release_is_refused(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _, clock, (conversation_id,) = await setup(sessionmaker, ["+15550000001"])

    async with sessionmaker() as session, session.begin():
        crashed = await queue.claim(session, conversation_id, clock.now(), LEASE)
    clock.advance(LEASE)  # the first worker died; its lease runs out
    async with sessionmaker() as session, session.begin():
        rescuer = await queue.claim(session, conversation_id, clock.now(), LEASE)
        assert rescuer is not None and rescuer != crashed
        assert crashed is not None
        assert not await queue.release(session, conversation_id, crashed)  # too late
        assert await queue.release(session, conversation_id, rescuer)


async def test_concurrent_workers_never_share_a_conversation(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _, clock, ids = await setup(sessionmaker, [f"+1555000000{i}" for i in range(3)])

    async def worker() -> tuple[uuid.UUID, uuid.UUID] | None:
        async with sessionmaker() as session, session.begin():
            return await queue.claim_next(session, clock.now(), LEASE)

    results = await asyncio.gather(*(worker() for _ in range(6)))

    claimed = [r[0] for r in results if r is not None]
    assert sorted(claimed) == sorted(ids)
    assert results.count(None) == 3
