"""The database guarantees on their own, bypassing all application code (D-006).

If a future code path forgets every check in app/domain, these still hold.
"""

import uuid
from collections.abc import Awaitable
from datetime import date, datetime

import pytest
from sqlalchemy import insert, update
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.errors import EXCLUSION_VIOLATION, UNIQUE_VIOLATION, constraint_name, sqlstate
from app.db.models import Booking, Conversation, Customer, Escalation, Message, Technician
from tests.factories import World, add_customer, build_world, local

TUESDAY = date(2026, 10, 6)


def window(start_hour: int, end_hour: int) -> Range[datetime]:
    return Range(local(TUESDAY, start_hour), local(TUESDAY, end_hour), bounds="[)")


async def raw_booking(
    session: AsyncSession,
    world: World,
    *,
    tech: str = "Ana",
    customer: str = "pat",
    service: str = "ac_repair",
    time_window: Range[datetime] | None = None,
    status: str = "confirmed",
) -> None:
    await session.execute(
        insert(Booking).values(
            business_id=world.business.id,
            ref=uuid.uuid4().hex[:6].upper(),
            customer_id=world.customers[customer].id,
            technician_id=world.techs[tech].id,
            service_type_id=world.services[service].id,
            time_window=time_window or window(10, 12),
            status=status,
            address="1 Main St",
            zip="07030",
            problem_description="test",
        )
    )


async def expect_violation(
    session: AsyncSession, name: str, state: str, coro: Awaitable[object]
) -> None:
    with pytest.raises(IntegrityError) as caught:
        async with session.begin_nested():
            await coro
    assert constraint_name(caught.value) == name
    assert sqlstate(caught.value) == state


@pytest.fixture
async def world(session: AsyncSession) -> World:
    world = await build_world(session)
    await add_customer(session, world, "+15550000001", "pat")
    await add_customer(session, world, "+15550000002", "sam")
    return world


async def test_technician_cannot_have_overlapping_confirmed_bookings(
    session: AsyncSession, world: World
) -> None:
    await raw_booking(session, world, customer="pat", time_window=window(10, 12))
    await expect_violation(
        session,
        "ex_bookings_technician_no_overlap",
        EXCLUSION_VIOLATION,
        raw_booking(session, world, customer="sam", time_window=window(11, 13)),
    )


async def test_back_to_back_windows_do_not_overlap(session: AsyncSession, world: World) -> None:
    await raw_booking(session, world, customer="pat", time_window=window(8, 10))
    await raw_booking(session, world, customer="sam", time_window=window(10, 12))  # [8,10) [10,12)


async def test_cancelled_bookings_do_not_block(session: AsyncSession, world: World) -> None:
    await raw_booking(session, world, customer="pat", status="cancelled")
    await raw_booking(session, world, customer="sam")


async def test_different_technicians_may_overlap(session: AsyncSession, world: World) -> None:
    await raw_booking(session, world, tech="Ana", customer="pat")
    await raw_booking(session, world, tech="Ben", customer="sam")


async def test_same_customer_service_and_start_is_unique_even_across_technicians(
    session: AsyncSession, world: World
) -> None:
    await raw_booking(session, world, tech="Ana")
    await expect_violation(
        session,
        "uq_bookings_customer_service_start",
        UNIQUE_VIOLATION,
        raw_booking(session, world, tech="Ben"),
    )


async def test_reschedule_in_place_does_not_conflict_with_itself(
    session: AsyncSession, world: World
) -> None:
    await raw_booking(session, world, time_window=window(10, 12))
    await session.execute(update(Booking).values(time_window=window(11, 13)))


async def test_open_ended_or_empty_windows_are_rejected(
    session: AsyncSession, world: World
) -> None:
    open_ended = Range(local(TUESDAY, 10), None, bounds="[)")
    await expect_violation(
        session,
        "ck_bookings_time_window_finite",
        "23514",
        raw_booking(session, world, time_window=open_ended),
    )


async def test_provider_sid_dedupes_webhook_retries(session: AsyncSession, world: World) -> None:
    conversation = Conversation(
        business_id=world.business.id,
        customer_id=world.customers["pat"].id,
        channel="sms",
        started_at=local(TUESDAY, 9),
        last_activity_at=local(TUESDAY, 9),
    )
    session.add(conversation)
    await session.flush()

    def message() -> Awaitable[object]:
        return session.execute(
            insert(Message).values(
                conversation_id=conversation.id,
                direction="in",
                author="customer",
                body="hi",
                provider_sid="SM123",
                status="received",
                created_at=local(TUESDAY, 9),
            )
        )

    await message()
    await expect_violation(session, "uq_messages_provider_sid", UNIQUE_VIOLATION, message())


async def test_one_open_conversation_per_customer_and_channel(
    session: AsyncSession, world: World
) -> None:
    def conversation(mode: str, channel: str = "sms") -> Awaitable[object]:
        return session.execute(
            insert(Conversation).values(
                business_id=world.business.id,
                customer_id=world.customers["pat"].id,
                channel=channel,
                mode=mode,
                started_at=local(TUESDAY, 9),
                last_activity_at=local(TUESDAY, 9),
            )
        )

    await conversation("closed")
    await conversation("closed")  # any number of closed ones
    await conversation("bot")
    await conversation("bot", channel="webchat")
    await expect_violation(
        session, "uq_conversations_open_per_channel", UNIQUE_VIOLATION, conversation("handoff")
    )


@pytest.mark.parametrize(
    ("values", "constraint"),
    [
        (
            {"direction": "in", "author": "agent", "status": "received"},
            "ck_messages_author_matches_direction",
        ),
        (
            {"direction": "out", "author": "system", "status": "received"},
            "ck_messages_status_matches_direction",
        ),
    ],
)
async def test_message_checks(
    session: AsyncSession, world: World, values: dict[str, str], constraint: str
) -> None:
    conversation = Conversation(
        business_id=world.business.id,
        customer_id=world.customers["pat"].id,
        channel="sms",
        started_at=local(TUESDAY, 9),
        last_activity_at=local(TUESDAY, 9),
    )
    session.add(conversation)
    await session.flush()
    await expect_violation(
        session,
        constraint,
        "23514",
        session.execute(
            insert(Message).values(
                conversation_id=conversation.id, body="x", created_at=local(TUESDAY, 9), **values
            )
        ),
    )


async def test_emergency_escalation_requires_a_hazard(session: AsyncSession, world: World) -> None:
    conversation = Conversation(
        business_id=world.business.id,
        customer_id=world.customers["pat"].id,
        channel="sms",
        started_at=local(TUESDAY, 9),
        last_activity_at=local(TUESDAY, 9),
    )
    session.add(conversation)
    await session.flush()

    def escalation(reason: str, hazard: str | None) -> Awaitable[object]:
        return session.execute(
            insert(Escalation).values(
                conversation_id=conversation.id,
                reason=reason,
                hazard=hazard,
                mode="handoff",
                source="agent",
                summary="x",
                created_at=local(TUESDAY, 9),
            )
        )

    await expect_violation(
        session, "ck_escalations_hazard_iff_emergency", "23514", escalation("emergency", None)
    )
    await expect_violation(
        session, "ck_escalations_hazard_iff_emergency", "23514", escalation("price_quote", "gas")
    )


async def test_trade_and_phone_checks(session: AsyncSession, world: World) -> None:
    await expect_violation(
        session,
        "ck_technicians_trades_valid",
        "23514",
        session.execute(
            insert(Technician).values(business_id=world.business.id, name="X", trades=["roofing"])
        ),
    )
    await expect_violation(
        session,
        "ck_customers_phone_e164_format",
        "23514",
        session.execute(
            insert(Customer).values(business_id=world.business.id, phone_e164="555-0100")
        ),
    )
