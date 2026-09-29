"""Races, run for real: separate connections, separate transactions, all at once (D-006).

A barrier holds every request after it has picked a technician it believes is free and before
it writes. That removes luck from the test: every transaction has already "checked" and found
the window open, so only the exclusion constraint can stop a double-booking.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

import pytest
from sqlalchemy import func, insert, select, text
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Booking
from app.domain import booking as booking_module
from app.domain.booking import (
    BookingErr,
    BookingErrorCode,
    BookingOk,
    BookingResult,
    create_booking,
)
from app.domain.clock import FixedClock
from tests.factories import World, add_customer, build_world, local, new_booking

TUESDAY = date(2026, 10, 6)
CLOCK = FixedClock(local(date(2026, 10, 5), 7))

OVERLAPS = text(
    """
    SELECT count(*) FROM bookings a JOIN bookings b
      ON a.technician_id = b.technician_id AND a.id < b.id AND a.time_window && b.time_window
    WHERE a.status = 'confirmed' AND b.status = 'confirmed'
    """
)


async def setup(
    sessionmaker: async_sessionmaker[AsyncSession], techs: int, customers: int
) -> World:
    async with sessionmaker() as session, session.begin():
        world = await build_world(session, techs=[(f"T{i}", ["hvac"]) for i in range(techs)])
        for i in range(customers):
            await add_customer(session, world, f"+1555000{i:04d}", f"c{i}")
    return world


def synchronise_writes(monkeypatch: pytest.MonkeyPatch, parties: int) -> None:
    barrier = asyncio.Barrier(parties)
    original: Callable[..., Awaitable[Any]] = booking_module._technicians_by_preference

    async def pick_then_wait(*args: Any, **kwargs: Any) -> Any:
        chosen = await original(*args, **kwargs)
        await barrier.wait()
        return chosen

    monkeypatch.setattr(booking_module, "_technicians_by_preference", pick_then_wait)


async def book(
    sessionmaker: async_sessionmaker[AsyncSession], world: World, customer: str, hour: int = 10
) -> BookingResult:
    async with sessionmaker() as session, session.begin():
        return await create_booking(
            session,
            world.business,
            world.customers[customer],
            new_booking(local(TUESDAY, hour)),
            CLOCK,
        )


async def overlap_count(sessionmaker: async_sessionmaker[AsyncSession]) -> int:
    async with sessionmaker() as session:
        return await session.scalar(OVERLAPS) or 0


async def test_many_customers_one_technician_one_window(
    sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await setup(sessionmaker, techs=1, customers=8)
    synchronise_writes(monkeypatch, 8)

    results = await asyncio.gather(*(book(sessionmaker, world, f"c{i}") for i in range(8)))

    created = [r for r in results if isinstance(r, BookingOk)]
    rejected = [r for r in results if isinstance(r, BookingErr)]
    assert len(created) == 1
    assert len(rejected) == 7
    assert {r.code for r in rejected} == {BookingErrorCode.SLOT_UNAVAILABLE}
    assert all("Tue Oct 6, 10am-12pm" not in [a.label for a in r.alternatives] for r in rejected)
    assert await overlap_count(sessionmaker) == 0


async def test_losers_fall_through_to_the_next_free_technician(
    sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await setup(sessionmaker, techs=3, customers=5)
    synchronise_writes(monkeypatch, 5)

    results = await asyncio.gather(*(book(sessionmaker, world, f"c{i}") for i in range(5)))

    created = [r for r in results if isinstance(r, BookingOk)]
    assert len(created) == 3  # everyone picked T0 first; the constraint sent them onwards
    assert len({r.booking.technician_id for r in created}) == 3
    assert sum(isinstance(r, BookingErr) for r in results) == 2
    assert await overlap_count(sessionmaker) == 0


async def test_duplicate_concurrent_request_books_once(
    sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A webhook retry racing its original: two identical create_booking calls at once."""
    world = await setup(sessionmaker, techs=2, customers=1)
    synchronise_writes(monkeypatch, 2)

    results = await asyncio.gather(book(sessionmaker, world, "c0"), book(sessionmaker, world, "c0"))

    assert all(isinstance(r, BookingOk) for r in results)
    outcomes = sorted(r.outcome for r in results if isinstance(r, BookingOk))
    assert outcomes == ["created", "existing"]
    ids = {r.booking.id for r in results if isinstance(r, BookingOk)}
    assert len(ids) == 1
    async with sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(Booking)) == 1


async def test_mixed_load_never_double_books(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """No barrier: 30 customers, 3 technicians, 4 windows, natural interleaving."""
    world = await setup(sessionmaker, techs=3, customers=30)
    hours = [8, 10, 13, 15]

    results = await asyncio.gather(
        *(book(sessionmaker, world, f"c{i}", hours[i % 4]) for i in range(30))
    )

    assert sum(isinstance(r, BookingOk) for r in results) == 12  # 3 techs x 4 windows
    assert await overlap_count(sessionmaker) == 0


async def test_deadlock_victim_rolls_back_only_its_savepoint(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Force the deadlock the exclusion constraint can produce (D-026) and check the victim
    comes back as a DEADLOCK conflict with its transaction still usable.

    The first two rows are written without the technician lock, standing in for a future code
    path that forgets it: the lock prevents deadlocks, and this proves we survive without it."""
    world = await setup(sessionmaker, techs=2, customers=4)
    ana, ben = world.techs["T0"].id, world.techs["T1"].id
    window = new_booking(local(TUESDAY, 10))

    def row(tech_id: object, customer: str) -> Any:
        return (
            insert(Booking)
            .values(
                business_id=world.business.id,
                ref=customer.upper().ljust(6, "X"),
                customer_id=world.customers[customer].id,
                technician_id=tech_id,
                service_type_id=world.services["ac_repair"].id,
                time_window=Range(window.slot_start, local(TUESDAY, 12), bounds="[)"),
                address="a",
                zip="07030",
                problem_description="p",
            )
            .returning(Booking.id)
        )

    async with sessionmaker() as a, sessionmaker() as b:
        await a.begin()
        await b.begin()
        await a.execute(row(ana, "c0"))  # A holds Ana 10-12 (uncommitted)
        await b.execute(row(ben, "c1"))  # B holds Ben 10-12 (uncommitted)
        # Each now wants the other's technician: A waits on B, B waits on A.
        tasks = {
            asyncio.create_task(booking_module._try_write(a, ben, row(ben, "c2"))): a,
            asyncio.create_task(booking_module._try_write(b, ana, row(ana, "c3"))): b,
        }
        done, pending = await asyncio.wait(tasks, timeout=15, return_when=asyncio.FIRST_COMPLETED)
        assert len(done) == 1 and len(pending) == 1
        victim_task, survivor_task = done.pop(), pending.pop()
        _, conflict = victim_task.result()
        assert conflict is booking_module._Conflict.DEADLOCK

        # The victim's transaction survived its savepoint rollback: still usable.
        victim = tasks[victim_task]
        assert await victim.scalar(text("SELECT 1")) == 1
        # Once the victim's transaction ends, the survivor's write goes through.
        await victim.rollback()
        value, conflict = await asyncio.wait_for(survivor_task, timeout=15)
        assert conflict is None and value is not None
        await tasks[survivor_task].rollback()
