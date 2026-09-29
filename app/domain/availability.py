"""Which arrival windows can be booked (D-007).

A window is available when all of these hold:

* it comes from one of the business's weekly window templates on that local date;
* it starts at least ``min_lead_minutes`` from now;
* its local date is within ``horizon_days`` of today (today counts as day 1);
* at least one active technician with the right trade has no time off and no confirmed booking
  overlapping it.

This module only *reads*. The booking service re-checks inside its own transaction, and the
exclusion constraint on ``bookings`` is the final word (D-006, D-008).
"""

import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Booking,
    Business,
    ServiceType,
    Technician,
    TechnicianTimeOff,
    WindowTemplate,
)
from app.domain.enums import BookingStatus
from app.domain.localtime import local_to_utc, local_today, slot_id, window_label, zone

DEFAULT_SLOT_LIMIT = 6


class PartOfDay(StrEnum):
    MORNING = "morning"  # starts before noon
    AFTERNOON = "afternoon"  # starts noon to 5pm
    EVENING = "evening"  # starts 5pm or later

    def contains(self, local_start: time) -> bool:
        if self is PartOfDay.MORNING:
            return local_start < time(12)
        if self is PartOfDay.AFTERNOON:
            return time(12) <= local_start < time(17)
        return local_start >= time(17)


@dataclass(frozen=True)
class Slot:
    start: datetime  # UTC
    end: datetime  # UTC
    slot_id: str  # local start with offset, e.g. 2026-09-29T10:00-04:00
    label: str  # "Tue Sep 29, 10am-12pm"


@dataclass(frozen=True)
class BusinessCalendar:
    """The slice of business configuration that availability depends on."""

    business_id: uuid.UUID
    tz: ZoneInfo
    min_lead: timedelta
    horizon_days: int
    # weekday (0 = Monday) -> sorted (start_local, end_local) pairs
    templates: dict[int, list[tuple[time, time]]]

    def bookable_days(self, now: datetime) -> tuple[date, date]:
        today = local_today(now, self.tz)
        return today, today + timedelta(days=self.horizon_days - 1)

    def windows_on(self, day: date) -> list[tuple[datetime, datetime]]:
        """Template windows for one local date, as UTC instants. Windows whose start or end
        falls in a DST gap are skipped (no real business opens at 2:30am)."""
        result = []
        for start_local, end_local in self.templates.get(day.weekday(), []):
            start = local_to_utc(day, start_local, self.tz)
            end = local_to_utc(day, end_local, self.tz)
            if start is not None and end is not None:
                result.append((start, end))
        return result

    def make_slot(self, start: datetime, end: datetime) -> Slot:
        return Slot(start, end, slot_id(start, self.tz), window_label(start, end, self.tz))


class SlotProblem(StrEnum):
    NOT_A_WINDOW = "not_a_window"  # no template window starts at that time
    TOO_SOON = "too_soon"  # in the past or inside the minimum lead time
    TOO_FAR = "too_far"  # beyond the booking horizon


def check_window(cal: BusinessCalendar, start: datetime, now: datetime) -> Slot | SlotProblem:
    """Validate a requested window start against the calendar (not against technicians)."""
    local_day = start.astimezone(cal.tz).date()
    match = next(((s, e) for s, e in cal.windows_on(local_day) if s == start), None)
    if match is None:
        return SlotProblem.NOT_A_WINDOW
    first_day, last_day = cal.bookable_days(now)
    if start < now + cal.min_lead:
        return SlotProblem.TOO_SOON
    if local_day > last_day or local_day < first_day:
        return SlotProblem.TOO_FAR
    return cal.make_slot(*match)


async def load_calendar(session: AsyncSession, business: Business) -> BusinessCalendar:
    rows = await session.execute(
        select(WindowTemplate.weekday, WindowTemplate.start_local, WindowTemplate.end_local)
        .where(WindowTemplate.business_id == business.id)
        .order_by(WindowTemplate.weekday, WindowTemplate.start_local)
    )
    templates: dict[int, list[tuple[time, time]]] = defaultdict(list)
    for weekday, start_local, end_local in rows:
        templates[weekday].append((start_local, end_local))
    return BusinessCalendar(
        business_id=business.id,
        tz=zone(business.timezone),
        min_lead=timedelta(minutes=business.min_lead_minutes),
        horizon_days=business.horizon_days,
        templates=dict(templates),
    )


async def get_service(
    session: AsyncSession, business_id: uuid.UUID, code: str
) -> ServiceType | None:
    return await session.scalar(
        select(ServiceType).where(
            ServiceType.business_id == business_id,
            ServiceType.code == code,
            ServiceType.active.is_(True),
        )
    )


async def qualified_technicians(
    session: AsyncSession, business_id: uuid.UUID, trade: str
) -> list[Technician]:
    result = await session.scalars(
        select(Technician)
        .where(
            Technician.business_id == business_id,
            Technician.active.is_(True),
            Technician.trades.contains([trade]),
        )
        .order_by(Technician.id)
    )
    return list(result)


Interval = tuple[datetime, datetime]


async def busy_intervals(
    session: AsyncSession,
    technician_ids: Sequence[uuid.UUID],
    span_start: datetime,
    span_end: datetime,
    exclude_booking_id: uuid.UUID | None = None,
) -> dict[uuid.UUID, list[Interval]]:
    """Confirmed bookings and time off per technician that overlap [span_start, span_end)."""
    busy: dict[uuid.UUID, list[Interval]] = defaultdict(list)
    if not technician_ids:
        return busy
    span = Range(span_start, span_end, bounds="[)")

    booking_filter = [
        Booking.technician_id.in_(technician_ids),
        Booking.status == BookingStatus.CONFIRMED,
        Booking.time_window.overlaps(span),
    ]
    if exclude_booking_id is not None:
        booking_filter.append(Booking.id != exclude_booking_id)
    bookings = await session.execute(
        select(Booking.technician_id, Booking.time_window).where(and_(*booking_filter))
    )
    time_off = await session.execute(
        select(TechnicianTimeOff.technician_id, TechnicianTimeOff.period).where(
            TechnicianTimeOff.technician_id.in_(technician_ids),
            TechnicianTimeOff.period.overlaps(span),
        )
    )
    for tech_id, window in [*bookings, *time_off]:
        assert window.lower is not None and window.upper is not None  # CHECK: finite ranges
        busy[tech_id].append((window.lower, window.upper))
    return busy


def _overlaps(a: Interval, b: Interval) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def free_technicians(
    window: Interval, technicians: Iterable[Technician], busy: dict[uuid.UUID, list[Interval]]
) -> list[Technician]:
    return [t for t in technicians if not any(_overlaps(window, b) for b in busy.get(t.id, []))]


async def find_available_slots(
    session: AsyncSession,
    cal: BusinessCalendar,
    service: ServiceType,
    now: datetime,
    *,
    earliest: date | None = None,
    latest: date | None = None,
    part_of_day: PartOfDay | None = None,
    limit: int = DEFAULT_SLOT_LIMIT,
    exclude_booking_id: uuid.UUID | None = None,
) -> list[Slot]:
    """Earliest-first list of bookable windows for a service, at most ``limit`` long."""
    first_day, last_day = cal.bookable_days(now)
    start_day = max(first_day, earliest or first_day)
    end_day = min(last_day, latest or last_day)
    if start_day > end_day or limit <= 0:
        return []

    candidates: list[Interval] = []
    day = start_day
    while day <= end_day:
        for start, end in cal.windows_on(day):
            if start < now + cal.min_lead:
                continue
            if part_of_day and not part_of_day.contains(start.astimezone(cal.tz).time()):
                continue
            candidates.append((start, end))
        day += timedelta(days=1)
    if not candidates:
        return []

    techs = await qualified_technicians(session, cal.business_id, service.trade)
    busy = await busy_intervals(
        session,
        [t.id for t in techs],
        candidates[0][0],
        max(end for _, end in candidates),
        exclude_booking_id=exclude_booking_id,
    )
    slots = []
    for window in candidates:
        if free_technicians(window, techs, busy):
            slots.append(cal.make_slot(*window))
            if len(slots) == limit:
                break
    return slots


async def technician_load_on(
    session: AsyncSession,
    technician_ids: Sequence[uuid.UUID],
    day_start: datetime,
    day_end: datetime,
    exclude_booking_id: uuid.UUID | None = None,
) -> dict[uuid.UUID, int]:
    """Confirmed bookings per technician whose window starts in [day_start, day_end)."""
    if not technician_ids:
        return {}
    filters = [
        Booking.technician_id.in_(technician_ids),
        Booking.status == BookingStatus.CONFIRMED,
        func.lower(Booking.time_window) >= day_start,
        func.lower(Booking.time_window) < day_end,
    ]
    if exclude_booking_id is not None:
        filters.append(Booking.id != exclude_booking_id)
    rows = await session.execute(
        select(Booking.technician_id, func.count()).where(*filters).group_by(Booking.technician_id)
    )
    return {tech_id: count for tech_id, count in rows}
