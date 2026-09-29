import asyncio
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Conversation, Customer, Message
from app.domain.clock import FixedClock
from app.domain.conversations import add_inbound, open_conversation
from app.domain.customers import InvalidPhoneError, normalize_phone
from app.domain.enums import Channel, ConversationMode
from app.pipeline.inbound import receive_inbound
from tests.factories import World, add_customer, build_world, local

MONDAY = date(2026, 10, 5)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("(555) 010-0199", "+15550100199"),
        ("555.010.0199", "+15550100199"),
        ("1-555-010-0199", "+15550100199"),
        ("+1 555 010 0199", "+15550100199"),
        ("+44 20 7946 0958", "+442079460958"),
    ],
)
def test_normalize_phone(raw: str, expected: str) -> None:
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", ["", "12345", "555-0199", "+0123456789", "call me"])
def test_normalize_phone_rejects(raw: str) -> None:
    with pytest.raises(InvalidPhoneError):
        normalize_phone(raw)


@pytest.fixture
async def world(session: AsyncSession) -> World:
    world = await build_world(session)
    await add_customer(session, world, "+15550000001", "pat")
    return world


class TestSessions:
    """D-021 and D-025."""

    async def test_reuses_the_open_conversation_within_24_hours(
        self, session: AsyncSession, world: World
    ) -> None:
        pat, clock = world.customers["pat"], FixedClock(local(MONDAY, 9))
        first = await open_conversation(
            session, world.business.id, pat.id, Channel.SMS, clock.now()
        )
        await add_inbound(session, first, "hi", clock.now())

        clock.advance(timedelta(hours=23, minutes=59))
        again = await open_conversation(
            session, world.business.id, pat.id, Channel.SMS, clock.now()
        )

        assert again.id == first.id

    async def test_starts_fresh_after_24_hours_idle(
        self, session: AsyncSession, world: World
    ) -> None:
        pat, clock = world.customers["pat"], FixedClock(local(MONDAY, 9))
        first = await open_conversation(
            session, world.business.id, pat.id, Channel.SMS, clock.now()
        )
        await add_inbound(session, first, "hi", clock.now())

        clock.advance(timedelta(hours=24))
        fresh = await open_conversation(
            session, world.business.id, pat.id, Channel.SMS, clock.now()
        )

        assert fresh.id != first.id
        await session.refresh(first)
        assert first.mode == ConversationMode.CLOSED
        assert fresh.mode == ConversationMode.BOT

    async def test_handoff_never_times_out(self, session: AsyncSession, world: World) -> None:
        pat, clock = world.customers["pat"], FixedClock(local(MONDAY, 9))
        first = await open_conversation(
            session, world.business.id, pat.id, Channel.SMS, clock.now()
        )
        first.mode = ConversationMode.HANDOFF
        await session.flush()

        clock.advance(timedelta(days=3))
        again = await open_conversation(
            session, world.business.id, pat.id, Channel.SMS, clock.now()
        )

        assert again.id == first.id
        assert again.mode == ConversationMode.HANDOFF

    async def test_channels_are_separate_conversations(
        self, session: AsyncSession, world: World
    ) -> None:
        pat, now = world.customers["pat"], local(MONDAY, 9)
        sms = await open_conversation(session, world.business.id, pat.id, Channel.SMS, now)
        web = await open_conversation(session, world.business.id, pat.id, Channel.WEBCHAT, now)
        assert sms.id != web.id


async def test_webhook_retry_is_stored_once(session: AsyncSession, world: World) -> None:
    clock = FixedClock(local(MONDAY, 9))
    kwargs = {"channel": Channel.SMS, "from_number": "+15550000001", "body": "hi", "clock": clock}

    first = await receive_inbound(session, world.business, provider_sid="SM1", **kwargs)
    retry = await receive_inbound(session, world.business, provider_sid="SM1", **kwargs)

    assert not first.duplicate
    assert retry.duplicate
    assert await session.scalar(select(func.count()).select_from(Message)) == 1


async def test_simultaneous_first_messages_from_a_new_number(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Two texts from an unknown number land at the same instant: one customer, one conversation."""
    async with sessionmaker() as session, session.begin():
        world = await build_world(session)
    clock = FixedClock(local(MONDAY, 9))

    async def receive(body: str, sid: str) -> None:
        async with sessionmaker() as session, session.begin():
            await receive_inbound(
                session,
                world.business,
                channel=Channel.SMS,
                from_number="(555) 000-0042",
                body=body,
                clock=clock,
                provider_sid=sid,
            )

    await asyncio.gather(*(receive(f"part {i}", f"SM{i}") for i in range(6)))

    async with sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(Customer)) == 1
        assert await session.scalar(select(func.count()).select_from(Conversation)) == 1
        assert await session.scalar(select(func.count()).select_from(Message)) == 6
