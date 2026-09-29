from datetime import date, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.availability import (
    PartOfDay,
    SlotProblem,
    check_window,
    find_available_slots,
    load_calendar,
)
from app.domain.booking import create_booking
from app.domain.clock import FixedClock
from tests.factories import add_customer, add_time_off, build_world, local, new_booking

MONDAY = date(2026, 10, 5)
TUESDAY = MONDAY + timedelta(days=1)


async def test_earliest_first_with_labels_and_limit(session: AsyncSession) -> None:
    world = await build_world(session)
    clock = FixedClock(local(MONDAY, 7))
    cal = await load_calendar(session, world.business)

    slots = await find_available_slots(session, cal, world.services["ac_repair"], clock.now())

    assert [s.label for s in slots] == [
        "Mon Oct 5, 10am-12pm",  # 8-10 is inside the 2 h minimum lead
        "Mon Oct 5, 1-3pm",
        "Mon Oct 5, 3-5pm",
        "Tue Oct 6, 8-10am",
        "Tue Oct 6, 10am-12pm",
        "Tue Oct 6, 1-3pm",
    ]
    assert slots[0].slot_id == "2026-10-05T10:00-04:00"
    assert slots[0].start == local(MONDAY, 10)
    assert slots[0].end == local(MONDAY, 12)


async def test_min_lead_excludes_windows_starting_too_soon(session: AsyncSession) -> None:
    world = await build_world(session, min_lead_minutes=120)
    cal = await load_calendar(session, world.business)
    service = world.services["ac_repair"]

    at_1100 = await find_available_slots(session, cal, service, local(MONDAY, 11), latest=MONDAY)
    at_1101 = await find_available_slots(session, cal, service, local(MONDAY, 11, 1), latest=MONDAY)

    assert [s.label for s in at_1100] == ["Mon Oct 5, 1-3pm", "Mon Oct 5, 3-5pm"]  # exactly 2 h
    assert [s.label for s in at_1101] == ["Mon Oct 5, 3-5pm"]


async def test_horizon_counts_today_as_day_one(session: AsyncSession) -> None:
    world = await build_world(session, horizon_days=3)
    cal = await load_calendar(session, world.business)

    slots = await find_available_slots(
        session, cal, world.services["ac_repair"], local(MONDAY, 7), limit=100
    )

    assert {s.start.astimezone(cal.tz).date() for s in slots} == {
        MONDAY,
        TUESDAY,
        MONDAY + timedelta(days=2),
    }


async def test_closed_days_and_saturday_windows(session: AsyncSession) -> None:
    world = await build_world(session)
    cal = await load_calendar(session, world.business)
    saturday, sunday = date(2026, 10, 10), date(2026, 10, 11)

    slots = await find_available_slots(
        session,
        cal,
        world.services["ac_repair"],
        local(MONDAY, 7),
        earliest=saturday,
        latest=sunday,
    )

    assert [s.label for s in slots] == ["Sat Oct 10, 9-11am", "Sat Oct 10, 11am-1pm"]


async def test_window_stays_open_until_every_qualified_tech_is_busy(session: AsyncSession) -> None:
    world = await build_world(session)  # Ana (hvac) and Ben (hvac, plumbing)
    clock = FixedClock(local(MONDAY, 7))
    cal = await load_calendar(session, world.business)
    service = world.services["ac_repair"]
    tuesday_8 = local(TUESDAY, 8)

    async def tuesday_labels() -> list[str]:
        slots = await find_available_slots(
            session, cal, service, clock.now(), earliest=TUESDAY, latest=TUESDAY
        )
        return [s.label for s in slots]

    first = await add_customer(session, world, "+15550000001")
    await create_booking(session, world.business, first, new_booking(tuesday_8), clock)
    assert "Tue Oct 6, 8-10am" in await tuesday_labels()

    second = await add_customer(session, world, "+15550000002")
    await create_booking(session, world.business, second, new_booking(tuesday_8), clock)
    assert "Tue Oct 6, 8-10am" not in await tuesday_labels()


async def test_only_qualified_active_technicians_count(session: AsyncSession) -> None:
    world = await build_world(session, techs=[("Ana", ["hvac"]), ("Pia", ["plumbing"])])
    cal = await load_calendar(session, world.business)
    now = local(MONDAY, 7)

    electrical = await find_available_slots(session, cal, world.services["outlet_repair"], now)
    assert electrical == []  # nobody is an electrician

    world.techs["Pia"].active = False
    await session.flush()
    plumbing = await find_available_slots(session, cal, world.services["leak_repair"], now)
    assert plumbing == []


async def test_time_off_blocks_overlapping_windows(session: AsyncSession) -> None:
    world = await build_world(session, techs=[("Ana", ["hvac"])])
    cal = await load_calendar(session, world.business)
    # Dentist 11:00-13:30 Tuesday blocks 10-12 and 1-3, not 8-10 or 3-5.
    await add_time_off(session, world.techs["Ana"], local(TUESDAY, 11), local(TUESDAY, 13, 30))

    slots = await find_available_slots(
        session,
        cal,
        world.services["ac_repair"],
        local(MONDAY, 7),
        earliest=TUESDAY,
        latest=TUESDAY,
    )

    assert [s.label for s in slots] == ["Tue Oct 6, 8-10am", "Tue Oct 6, 3-5pm"]


async def test_part_of_day(session: AsyncSession) -> None:
    world = await build_world(session)
    cal = await load_calendar(session, world.business)
    service, now = world.services["ac_repair"], local(MONDAY, 7)

    mornings = await find_available_slots(
        session, cal, service, now, earliest=TUESDAY, latest=TUESDAY, part_of_day=PartOfDay.MORNING
    )
    afternoons = await find_available_slots(
        session,
        cal,
        service,
        now,
        earliest=TUESDAY,
        latest=TUESDAY,
        part_of_day=PartOfDay.AFTERNOON,
    )
    evenings = await find_available_slots(
        session, cal, service, now, earliest=TUESDAY, latest=TUESDAY, part_of_day=PartOfDay.EVENING
    )

    assert [s.label for s in mornings] == ["Tue Oct 6, 8-10am", "Tue Oct 6, 10am-12pm"]
    assert [s.label for s in afternoons] == ["Tue Oct 6, 1-3pm", "Tue Oct 6, 3-5pm"]
    assert evenings == []


async def test_windows_across_the_fall_back_transition(session: AsyncSession) -> None:
    world = await build_world(session)
    cal = await load_calendar(session, world.business)
    friday, monday = date(2026, 10, 30), date(2026, 11, 2)

    slots = await find_available_slots(
        session,
        cal,
        world.services["ac_repair"],
        local(date(2026, 10, 29), 12),
        earliest=friday,
        latest=monday,
        part_of_day=PartOfDay.MORNING,
        limit=100,
    )

    by_label = {s.label: s for s in slots}
    assert by_label["Fri Oct 30, 8-10am"].start.hour == 12  # 08:00 EDT = 12:00 UTC
    assert by_label["Mon Nov 2, 8-10am"].start.hour == 13  # 08:00 EST = 13:00 UTC
    assert by_label["Sat Oct 31, 9-11am"].slot_id == "2026-10-31T09:00-04:00"
    assert by_label["Mon Nov 2, 8-10am"].slot_id == "2026-11-02T08:00-05:00"
    assert all(s.end - s.start == timedelta(hours=2) for s in slots)


async def test_windows_across_the_spring_forward_transition(session: AsyncSession) -> None:
    world = await build_world(session)
    cal = await load_calendar(session, world.business)
    friday, monday = date(2027, 3, 12), date(2027, 3, 15)

    slots = await find_available_slots(
        session,
        cal,
        world.services["ac_repair"],
        local(date(2027, 3, 11), 12),
        earliest=friday,
        latest=monday,
        part_of_day=PartOfDay.MORNING,
        limit=100,
    )

    by_label = {s.label: s for s in slots}
    assert by_label["Fri Mar 12, 8-10am"].start.hour == 13  # EST
    assert by_label["Mon Mar 15, 8-10am"].start.hour == 12  # EDT
    assert all(s.end - s.start == timedelta(hours=2) for s in slots)


async def test_check_window(session: AsyncSession) -> None:
    world = await build_world(session, horizon_days=14)
    cal = await load_calendar(session, world.business)
    now = local(MONDAY, 9)

    ok = check_window(cal, local(TUESDAY, 10), now)
    assert not isinstance(ok, SlotProblem)
    assert ok.label == "Tue Oct 6, 10am-12pm"

    assert check_window(cal, local(TUESDAY, 9), now) is SlotProblem.NOT_A_WINDOW
    assert check_window(cal, local(MONDAY, 10), now) is SlotProblem.TOO_SOON
    assert check_window(cal, local(MONDAY - timedelta(days=7), 10), now) is SlotProblem.TOO_SOON
    last_friday = check_window(cal, local(MONDAY + timedelta(days=11), 10), now)  # day 12 of 14
    assert not isinstance(last_friday, SlotProblem)
    assert check_window(cal, local(MONDAY + timedelta(days=14), 10), now) is SlotProblem.TOO_FAR


async def test_check_window_rejects_naive_datetimes(session: AsyncSession) -> None:
    world = await build_world(session)
    cal = await load_calendar(session, world.business)
    with pytest.raises(ValueError, match="naive"):
        check_window(cal, datetime(2026, 10, 6, 10), local(MONDAY, 9))
