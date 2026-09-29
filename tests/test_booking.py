from datetime import date, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Booking, BookingEvent, Customer
from app.domain.availability import find_available_slots, load_calendar
from app.domain.booking import (
    REF_ALPHABET,
    REF_LENGTH,
    BookingErr,
    BookingErrorCode,
    BookingOk,
    BookingResult,
    cancel_booking,
    create_booking,
    reschedule_booking,
    upcoming_bookings,
)
from app.domain.clock import FixedClock
from app.domain.enums import Actor, BookingStatus
from tests.factories import World, add_customer, add_time_off, build_world, local, new_booking

MONDAY = date(2026, 10, 5)
TUESDAY = MONDAY + timedelta(days=1)
WEDNESDAY = MONDAY + timedelta(days=2)


def ok(result: BookingResult) -> BookingOk:
    assert isinstance(result, BookingOk), result
    return result


def err(result: BookingResult) -> BookingErr:
    assert isinstance(result, BookingErr), result
    return result


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(local(MONDAY, 7))


@pytest.fixture
async def world(session: AsyncSession) -> World:
    world = await build_world(session)  # Ana: hvac; Ben: hvac + plumbing
    await add_customer(session, world, "+15550000001", "pat")
    await add_customer(session, world, "+15550000002", "sam")
    return world


async def booking_count(session: AsyncSession, status: str = BookingStatus.CONFIRMED) -> int:
    return await session.scalar(select(func.count()).where(Booking.status == status)) or 0


class TestCreate:
    async def test_books_a_window_and_records_everything(
        self, session: AsyncSession, world: World, clock: FixedClock
    ) -> None:
        pat = world.customers["pat"]
        result = ok(
            await create_booking(
                session, world.business, pat, new_booking(local(TUESDAY, 10)), clock
            )
        )

        assert result.outcome == "created"
        view = result.booking
        assert view.slot.label == "Tue Oct 6, 10am-12pm"
        assert view.service_code == "ac_repair"
        assert view.status is BookingStatus.CONFIRMED
        assert len(view.ref) == REF_LENGTH and set(view.ref) <= set(REF_ALPHABET)

        event = await session.scalar(select(BookingEvent).where(BookingEvent.booking_id == view.id))
        assert event is not None
        assert (event.event_type, event.actor) == ("created", Actor.AGENT)
        assert event.new_window is not None and event.new_window.lower == local(TUESDAY, 10)

        customer = await session.get(Customer, pat.id)
        assert customer is not None
        assert (customer.name, customer.zip) == ("Pat Customer", "07030")

    async def test_assigns_least_loaded_technician_ties_by_id(
        self, session: AsyncSession, world: World, clock: FixedClock
    ) -> None:
        pat, sam = world.customers["pat"], world.customers["sam"]
        ana, ben = world.techs["Ana"], world.techs["Ben"]

        first = ok(
            await create_booking(
                session, world.business, pat, new_booking(local(TUESDAY, 8)), clock
            )
        )
        second = ok(
            await create_booking(
                session, world.business, sam, new_booking(local(TUESDAY, 13)), clock
            )
        )
        third = ok(
            await create_booking(
                session,
                world.business,
                pat,
                new_booking(local(TUESDAY, 15), "furnace_repair"),
                clock,
            )
        )

        assert first.booking.technician_id == ana.id  # 0-0 tie, lower id
        assert second.booking.technician_id == ben.id  # Ana has 1 that day, Ben 0
        assert third.booking.technician_id == ana.id  # 1-1 tie again

    async def test_is_idempotent_for_the_same_request(
        self, session: AsyncSession, world: World, clock: FixedClock
    ) -> None:
        pat = world.customers["pat"]
        request = new_booking(local(TUESDAY, 10))

        first = ok(await create_booking(session, world.business, pat, request, clock))
        again = ok(await create_booking(session, world.business, pat, request, clock))

        assert (first.outcome, again.outcome) == ("created", "existing")
        assert again.booking.id == first.booking.id
        assert await booking_count(session) == 1

    async def test_full_window_returns_alternatives(
        self, session: AsyncSession, world: World, clock: FixedClock
    ) -> None:
        tuesday_10 = local(TUESDAY, 10)
        for phone in ("+15550000011", "+15550000012"):
            customer = await add_customer(session, world, phone)
            ok(
                await create_booking(
                    session, world.business, customer, new_booking(tuesday_10), clock
                )
            )

        result = err(
            await create_booking(
                session, world.business, world.customers["pat"], new_booking(tuesday_10), clock
            )
        )

        assert result.code is BookingErrorCode.SLOT_UNAVAILABLE
        assert [s.label for s in result.alternatives] == [
            "Tue Oct 6, 8-10am",
            "Tue Oct 6, 1-3pm",
            "Tue Oct 6, 3-5pm",
        ]

    async def test_time_off_is_respected(
        self, session: AsyncSession, world: World, clock: FixedClock
    ) -> None:
        await add_time_off(session, world.techs["Ana"], local(TUESDAY, 0), local(WEDNESDAY, 0))

        result = ok(
            await create_booking(
                session,
                world.business,
                world.customers["pat"],
                new_booking(local(TUESDAY, 10)),
                clock,
            )
        )

        assert result.booking.technician_id == world.techs["Ben"].id

    @pytest.mark.parametrize(
        ("hour", "minute", "code"),
        [
            (9, 0, BookingErrorCode.INVALID_SLOT),  # not a template window
            (10, 30, BookingErrorCode.INVALID_SLOT),
        ],
    )
    async def test_rejects_times_that_are_not_windows(
        self,
        session: AsyncSession,
        world: World,
        clock: FixedClock,
        hour: int,
        minute: int,
        code: BookingErrorCode,
    ) -> None:
        result = err(
            await create_booking(
                session,
                world.business,
                world.customers["pat"],
                new_booking(local(TUESDAY, hour, minute)),
                clock,
            )
        )
        assert result.code is code
        assert result.alternatives  # always something to offer instead

    async def test_rejects_windows_too_soon_or_too_far(
        self, session: AsyncSession, world: World, clock: FixedClock
    ) -> None:
        pat = world.customers["pat"]
        too_soon = err(
            await create_booking(session, world.business, pat, new_booking(local(MONDAY, 8)), clock)
        )
        too_far = err(
            await create_booking(
                session,
                world.business,
                pat,
                new_booking(local(MONDAY + timedelta(days=21), 8)),
                clock,
            )
        )
        assert too_soon.code is BookingErrorCode.SLOT_UNAVAILABLE
        assert too_far.code is BookingErrorCode.SLOT_UNAVAILABLE
        assert await booking_count(session) == 0

    async def test_service_and_area_checks(
        self, session: AsyncSession, world: World, clock: FixedClock
    ) -> None:
        pat, slot = world.customers["pat"], local(TUESDAY, 10)

        unknown = err(
            await create_booking(session, world.business, pat, new_booking(slot, "roofing"), clock)
        )
        install = err(
            await create_booking(
                session, world.business, pat, new_booking(slot, "hvac_install"), clock
            )
        )
        outside = err(
            await create_booking(
                session, world.business, pat, new_booking(slot, zip_code="10001"), clock
            )
        )
        bad_zip = err(
            await create_booking(
                session, world.business, pat, new_booking(slot, zip_code="7030"), clock
            )
        )

        assert unknown.code is BookingErrorCode.UNKNOWN_SERVICE
        assert install.code is BookingErrorCode.SERVICE_NOT_BOOKABLE
        assert outside.code is BookingErrorCode.OUTSIDE_SERVICE_AREA
        assert bad_zip.code is BookingErrorCode.INVALID_ZIP
        assert await booking_count(session) == 0

    async def test_zip_plus_four_is_accepted(
        self, session: AsyncSession, world: World, clock: FixedClock
    ) -> None:
        result = ok(
            await create_booking(
                session,
                world.business,
                world.customers["pat"],
                new_booking(local(TUESDAY, 10), zip_code="07030-1234"),
                clock,
            )
        )
        assert result.booking.zip == "07030"

    async def test_ref_collision_draws_a_new_ref(
        self, session: AsyncSession, world: World, clock: FixedClock
    ) -> None:
        refs = iter(["AAAAAA", "AAAAAA", "CCCCCC"])

        def make_ref() -> str:
            return next(refs)

        first = ok(
            await create_booking(
                session,
                world.business,
                world.customers["pat"],
                new_booking(local(TUESDAY, 8)),
                clock,
                make_ref=make_ref,
            )
        )
        second = ok(
            await create_booking(
                session,
                world.business,
                world.customers["sam"],
                new_booking(local(TUESDAY, 10)),
                clock,
                make_ref=make_ref,
            )
        )
        assert (first.booking.ref, second.booking.ref) == ("AAAAAA", "CCCCCC")


class TestReschedule:
    @pytest.fixture
    async def booked(self, session: AsyncSession, world: World, clock: FixedClock) -> BookingOk:
        return ok(
            await create_booking(
                session,
                world.business,
                world.customers["pat"],
                new_booking(local(TUESDAY, 8)),
                clock,
            )
        )

    async def test_moves_the_booking_and_keeps_the_technician(
        self, session: AsyncSession, world: World, clock: FixedClock, booked: BookingOk
    ) -> None:
        result = ok(
            await reschedule_booking(
                session,
                world.business,
                world.customers["pat"],
                booked.booking.ref,
                local(WEDNESDAY, 13),
                clock,
            )
        )

        assert result.outcome == "rescheduled"
        assert result.booking.id == booked.booking.id
        assert result.booking.slot.label == "Wed Oct 7, 1-3pm"
        assert result.booking.technician_id == booked.booking.technician_id
        event = await session.scalar(
            select(BookingEvent).where(BookingEvent.event_type == "rescheduled")
        )
        assert event is not None
        assert event.old_window is not None and event.old_window.lower == local(TUESDAY, 8)
        assert event.new_window is not None and event.new_window.lower == local(WEDNESDAY, 13)

    async def test_switches_technician_when_the_current_one_is_busy(
        self, session: AsyncSession, world: World, clock: FixedClock, booked: BookingOk
    ) -> None:
        ana, ben = world.techs["Ana"], world.techs["Ben"]
        assert booked.booking.technician_id == ana.id
        ok(
            await create_booking(
                session,
                world.business,
                world.customers["sam"],
                new_booking(local(WEDNESDAY, 13)),
                clock,
            )
        )

        result = ok(
            await reschedule_booking(
                session,
                world.business,
                world.customers["pat"],
                booked.booking.ref,
                local(WEDNESDAY, 13),
                clock,
            )
        )

        assert result.booking.technician_id == ben.id

    async def test_same_window_is_a_no_op(
        self, session: AsyncSession, world: World, clock: FixedClock, booked: BookingOk
    ) -> None:
        result = ok(
            await reschedule_booking(
                session,
                world.business,
                world.customers["pat"],
                booked.booking.ref,
                local(TUESDAY, 8),
                clock,
            )
        )
        assert result.outcome == "unchanged"

    async def test_full_window_returns_alternatives_and_leaves_booking_alone(
        self, session: AsyncSession, world: World, clock: FixedClock, booked: BookingOk
    ) -> None:
        for phone in ("+15550000011", "+15550000012"):
            other = await add_customer(session, world, phone)
            ok(
                await create_booking(
                    session, world.business, other, new_booking(local(WEDNESDAY, 10)), clock
                )
            )

        result = err(
            await reschedule_booking(
                session,
                world.business,
                world.customers["pat"],
                booked.booking.ref,
                local(WEDNESDAY, 10),
                clock,
            )
        )

        assert result.code is BookingErrorCode.SLOT_UNAVAILABLE
        assert result.alternatives
        booking = await session.get(Booking, booked.booking.id)
        assert booking is not None and booking.time_window.lower == local(TUESDAY, 8)

    async def test_ref_lookup_is_case_insensitive(
        self, session: AsyncSession, world: World, clock: FixedClock, booked: BookingOk
    ) -> None:
        result = ok(
            await reschedule_booking(
                session,
                world.business,
                world.customers["pat"],
                f"  {booked.booking.ref.lower()} ",
                local(WEDNESDAY, 8),
                clock,
            )
        )
        assert result.outcome == "rescheduled"


class TestCancel:
    async def test_cancel_frees_the_window_and_is_idempotent(
        self, session: AsyncSession, world: World, clock: FixedClock
    ) -> None:
        world_one_tech = await build_world(session, techs=[("Solo", ["hvac"])])
        pat = await add_customer(session, world_one_tech, "+15550000099")
        booked = ok(
            await create_booking(
                session, world_one_tech.business, pat, new_booking(local(TUESDAY, 10)), clock
            )
        )
        cal = await load_calendar(session, world_one_tech.business)
        service = world_one_tech.services["ac_repair"]

        async def tuesday_labels() -> list[str]:
            slots = await find_available_slots(
                session, cal, service, clock.now(), earliest=TUESDAY, latest=TUESDAY
            )
            return [s.label for s in slots]

        assert "Tue Oct 6, 10am-12pm" not in await tuesday_labels()

        first = ok(
            await cancel_booking(
                session, world_one_tech.business, pat, booked.booking.ref, clock, reason="fixed it"
            )
        )
        again = ok(
            await cancel_booking(session, world_one_tech.business, pat, booked.booking.ref, clock)
        )

        assert (first.outcome, again.outcome) == ("cancelled", "already_cancelled")
        assert first.booking.status is BookingStatus.CANCELLED
        assert "Tue Oct 6, 10am-12pm" in await tuesday_labels()
        events = (
            await session.scalars(
                select(BookingEvent.event_type).where(BookingEvent.booking_id == booked.booking.id)
            )
        ).all()
        assert sorted(events) == ["cancelled", "created"]  # the repeat wrote nothing

    async def test_cannot_change_a_visit_that_has_started(
        self, session: AsyncSession, world: World, clock: FixedClock
    ) -> None:
        pat = world.customers["pat"]
        booked = ok(
            await create_booking(
                session, world.business, pat, new_booking(local(TUESDAY, 10)), clock
            )
        )
        clock.set(local(TUESDAY, 10, 5))

        cancel = err(await cancel_booking(session, world.business, pat, booked.booking.ref, clock))
        move = err(
            await reschedule_booking(
                session, world.business, pat, booked.booking.ref, local(WEDNESDAY, 10), clock
            )
        )

        assert cancel.code is BookingErrorCode.NOT_MODIFIABLE
        assert move.code is BookingErrorCode.NOT_MODIFIABLE

    async def test_cancelled_booking_cannot_be_rescheduled(
        self, session: AsyncSession, world: World, clock: FixedClock
    ) -> None:
        pat = world.customers["pat"]
        booked = ok(
            await create_booking(
                session, world.business, pat, new_booking(local(TUESDAY, 10)), clock
            )
        )
        ok(await cancel_booking(session, world.business, pat, booked.booking.ref, clock))

        result = err(
            await reschedule_booking(
                session, world.business, pat, booked.booking.ref, local(WEDNESDAY, 10), clock
            )
        )
        assert result.code is BookingErrorCode.NOT_MODIFIABLE


class TestSessionScoping:
    """D-010: a customer can only touch their own bookings, whatever the model is told."""

    async def test_another_customers_ref_is_not_found(
        self, session: AsyncSession, world: World, clock: FixedClock
    ) -> None:
        pat, sam = world.customers["pat"], world.customers["sam"]
        pats = ok(
            await create_booking(
                session, world.business, pat, new_booking(local(TUESDAY, 10)), clock
            )
        )

        cancel = err(await cancel_booking(session, world.business, sam, pats.booking.ref, clock))
        move = err(
            await reschedule_booking(
                session, world.business, sam, pats.booking.ref, local(WEDNESDAY, 10), clock
            )
        )

        assert cancel.code is BookingErrorCode.BOOKING_NOT_FOUND
        assert move.code is BookingErrorCode.BOOKING_NOT_FOUND
        assert await booking_count(session) == 1


async def test_upcoming_bookings_are_confirmed_future_and_ordered(
    session: AsyncSession, world: World, clock: FixedClock
) -> None:
    pat = world.customers["pat"]
    later = ok(
        await create_booking(session, world.business, pat, new_booking(local(WEDNESDAY, 10)), clock)
    )
    sooner = ok(
        await create_booking(session, world.business, pat, new_booking(local(TUESDAY, 13)), clock)
    )
    gone = ok(
        await create_booking(session, world.business, pat, new_booking(local(TUESDAY, 8)), clock)
    )
    ok(await cancel_booking(session, world.business, pat, gone.booking.ref, clock))

    upcoming = await upcoming_bookings(session, world.business, pat, clock)
    assert [b.ref for b in upcoming] == [sooner.booking.ref, later.booking.ref]

    clock.set(local(TUESDAY, 15, 1))  # sooner has ended
    upcoming = await upcoming_bookings(session, world.business, pat, clock)
    assert [b.ref for b in upcoming] == [later.booking.ref]
