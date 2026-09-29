"""Create, reschedule, and cancel bookings.

Correctness comes from the database, not from this module being careful (D-006):

* Technician double-booking is impossible because of ``ex_bookings_technician_no_overlap``. This
  module picks a technician it *believes* is free, tries the write inside a savepoint, and on an
  exclusion violation (someone else won the race) moves to the next technician.
* A retried ``create_booking`` (same customer, service, and window) hits
  ``uq_bookings_customer_service_start`` and returns the booking that already exists.
* Writers queue per technician on a row lock so concurrent conflicting inserts can't deadlock
  each other (D-026). The lock is an optimisation for liveness; the constraint is the guarantee.

Failures that the caller (later: the LLM) can recover from are returned as ``BookingErr`` values,
never raised. Each carries a plain-English message and, where useful, alternative windows.

Functions here flush but never commit: the caller owns the transaction.
"""

import re
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import Insert, Update

from app.db.errors import is_deadlock, is_violation
from app.db.models import Booking, BookingEvent, Business, Customer, ServiceType, Technician
from app.domain.availability import (
    BusinessCalendar,
    Slot,
    SlotProblem,
    busy_intervals,
    check_window,
    find_available_slots,
    free_technicians,
    get_service,
    load_calendar,
    qualified_technicians,
    technician_load_on,
)
from app.domain.clock import Clock
from app.domain.enums import Actor, BookingEventType, BookingStatus, Urgency
from app.domain.localtime import local_to_utc

NO_OVERLAP = "ex_bookings_technician_no_overlap"
SAME_BOOKING = "uq_bookings_customer_service_start"
REF_TAKEN = "uq_bookings_business_id_ref"

# No 0/O, 1/I/L, 2/Z, 5/S, 8/B: refs get read aloud and retyped on phones.
REF_ALPHABET = "ACDEFGHJKMNPQRTUVWXY34679"
REF_LENGTH = 6
_MAX_WRITE_ATTEMPTS = 3
ALTERNATIVES_LIMIT = 3

_ZIP = re.compile(r"^(\d{5})(?:-\d{4})?$")


def random_ref() -> str:
    return "".join(secrets.choice(REF_ALPHABET) for _ in range(REF_LENGTH))


class BookingErrorCode(StrEnum):
    UNKNOWN_SERVICE = "unknown_service"
    SERVICE_NOT_BOOKABLE = "service_not_bookable"
    INVALID_ZIP = "invalid_zip"
    OUTSIDE_SERVICE_AREA = "outside_service_area"
    INVALID_SLOT = "invalid_slot"
    SLOT_UNAVAILABLE = "slot_unavailable"
    BOOKING_NOT_FOUND = "booking_not_found"
    NOT_MODIFIABLE = "not_modifiable"
    DUPLICATE_BOOKING = "duplicate_booking"


@dataclass(frozen=True)
class BookingView:
    id: uuid.UUID
    ref: str
    service_code: str
    service_label: str
    slot: Slot
    status: BookingStatus
    urgency: Urgency
    address: str
    zip: str
    problem_description: str
    technician_id: uuid.UUID  # internal; never shown to the customer


BookingOutcome = Literal[
    "created", "existing", "rescheduled", "unchanged", "cancelled", "already_cancelled"
]


@dataclass(frozen=True)
class BookingOk:
    outcome: BookingOutcome
    booking: BookingView


@dataclass(frozen=True)
class BookingErr:
    code: BookingErrorCode
    message: str
    alternatives: tuple[Slot, ...] = ()


BookingResult = BookingOk | BookingErr


@dataclass(frozen=True)
class NewBooking:
    service_code: str
    slot_start: datetime
    customer_name: str
    address: str
    zip: str
    problem_description: str
    urgency: Urgency = Urgency.ROUTINE


# --------------------------------------------------------------------------------------------
# helpers


def _view(booking: Booking, service: ServiceType, cal: BusinessCalendar) -> BookingView:
    window = booking.time_window
    assert window.lower is not None and window.upper is not None
    return BookingView(
        id=booking.id,
        ref=booking.ref,
        service_code=service.code,
        service_label=service.label,
        slot=cal.make_slot(window.lower, window.upper),
        status=BookingStatus(booking.status),
        urgency=Urgency(booking.urgency),
        address=booking.address,
        zip=booking.zip,
        problem_description=booking.problem_description,
        technician_id=booking.technician_id,
    )


async def _load_view(
    session: AsyncSession, booking_id: uuid.UUID, cal: BusinessCalendar
) -> BookingView:
    row = (
        await session.execute(
            select(Booking, ServiceType)
            .join(ServiceType, ServiceType.id == Booking.service_type_id)
            .where(Booking.id == booking_id)
            .execution_options(populate_existing=True)
        )
    ).one()
    return _view(row[0], row[1], cal)


def _local_day_bounds(cal: BusinessCalendar, instant: datetime) -> tuple[datetime, datetime]:
    day = instant.astimezone(cal.tz).date()
    start = local_to_utc(day, datetime.min.time(), cal.tz)
    end = local_to_utc(day + timedelta(days=1), datetime.min.time(), cal.tz)
    # Midnight always exists in the zones we serve (US DST switches at 02:00).
    assert start is not None and end is not None
    return start, end


async def _alternatives(
    session: AsyncSession,
    cal: BusinessCalendar,
    service: ServiceType,
    now: datetime,
    near: datetime,
    exclude_booking_id: uuid.UUID | None = None,
) -> tuple[Slot, ...]:
    """Up to three open windows: from the requested date onwards, else the earliest overall."""
    from_day = near.astimezone(cal.tz).date()
    slots = await find_available_slots(
        session,
        cal,
        service,
        now,
        earliest=from_day,
        limit=ALTERNATIVES_LIMIT,
        exclude_booking_id=exclude_booking_id,
    )
    if not slots:
        slots = await find_available_slots(
            session,
            cal,
            service,
            now,
            limit=ALTERNATIVES_LIMIT,
            exclude_booking_id=exclude_booking_id,
        )
    return tuple(slots)


async def _slot_error(
    session: AsyncSession,
    cal: BusinessCalendar,
    service: ServiceType,
    now: datetime,
    requested: datetime,
    problem: SlotProblem,
    exclude_booking_id: uuid.UUID | None = None,
) -> BookingErr:
    alternatives = await _alternatives(session, cal, service, now, requested, exclude_booking_id)
    if problem is SlotProblem.NOT_A_WINDOW:
        return BookingErr(
            BookingErrorCode.INVALID_SLOT,
            "That time is not one of the business's arrival windows. "
            "Use a slot_id returned by get_availability.",
            alternatives,
        )
    reason = (
        "is too soon or already past"
        if problem is SlotProblem.TOO_SOON
        else "is beyond how far ahead the business books"
    )
    return BookingErr(
        BookingErrorCode.SLOT_UNAVAILABLE,
        f"That window {reason}. Offer an alternative.",
        alternatives,
    )


async def _technicians_by_preference(
    session: AsyncSession,
    cal: BusinessCalendar,
    trade: str,
    slot: Slot,
    exclude_booking_id: uuid.UUID | None = None,
    keep_technician_id: uuid.UUID | None = None,
) -> list[uuid.UUID]:
    """Free, qualified technicians for a window, best first.

    Least-loaded that local day, ties by id (deterministic tests). On a reschedule, the currently
    assigned technician goes first if still free, so the owner's schedule churns as little as
    possible.
    """
    techs = await qualified_technicians(session, cal.business_id, trade)
    busy = await busy_intervals(
        session, [t.id for t in techs], slot.start, slot.end, exclude_booking_id
    )
    free = free_technicians((slot.start, slot.end), techs, busy)
    day_start, day_end = _local_day_bounds(cal, slot.start)
    load = await technician_load_on(
        session, [t.id for t in free], day_start, day_end, exclude_booking_id
    )
    free.sort(key=lambda t: (t.id != keep_technician_id, load.get(t.id, 0), t.id))
    return [t.id for t in free]


async def _find_same(
    session: AsyncSession, customer_id: uuid.UUID, service_type_id: uuid.UUID, start: datetime
) -> Booking | None:
    return await session.scalar(
        select(Booking).where(
            Booking.customer_id == customer_id,
            Booking.service_type_id == service_type_id,
            Booking.status == BookingStatus.CONFIRMED,
            func.lower(Booking.time_window) == start,
        )
    )


async def _get_customer_booking(
    session: AsyncSession, business: Business, customer: Customer, ref: str
) -> Booking | None:
    """Look a booking up by ref, **scoped to the customer in the conversation** (D-010). A prompt
    injection that names someone else's ref gets ``booking_not_found``. Row-locked so concurrent
    reschedule/cancel of the same booking serialise."""
    return await session.scalar(
        select(Booking)
        .where(
            Booking.business_id == business.id,
            Booking.customer_id == customer.id,
            Booking.ref == ref.strip().upper(),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _event(
    booking_id: uuid.UUID,
    event_type: BookingEventType,
    actor: Actor,
    *,
    old: tuple[Range[datetime], uuid.UUID] | None = None,
    new: tuple[Range[datetime], uuid.UUID] | None = None,
    detail: dict[str, str] | None = None,
) -> BookingEvent:
    return BookingEvent(
        booking_id=booking_id,
        event_type=event_type,
        actor=actor,
        old_window=old[0] if old else None,
        old_technician_id=old[1] if old else None,
        new_window=new[0] if new else None,
        new_technician_id=new[1] if new else None,
        detail=detail,
    )


def _range(slot: Slot) -> Range[datetime]:
    return Range(slot.start, slot.end, bounds="[)")


class _Conflict(StrEnum):
    TECHNICIAN_TAKEN = "technician_taken"
    SAME_BOOKING = "same_booking"
    REF_TAKEN = "ref_taken"
    DEADLOCK = "deadlock"


_CONSTRAINT_CONFLICTS = {
    NO_OVERLAP: _Conflict.TECHNICIAN_TAKEN,
    SAME_BOOKING: _Conflict.SAME_BOOKING,
    REF_TAKEN: _Conflict.REF_TAKEN,
}


async def _try_write(
    session: AsyncSession, technician_id: uuid.UUID, statement: Insert | Update
) -> tuple[object | None, _Conflict | None]:
    """Write one booking row for ``technician_id`` inside a savepoint.

    A recognised conflict rolls back only the savepoint and comes back as data; the surrounding
    transaction stays usable. Anything else propagates.

    The ``FOR NO KEY UPDATE`` lock on the technician row is for *liveness*, not correctness
    (D-026). The exclusion constraint alone already makes double-booking impossible, but when
    several transactions insert conflicting rows at once they can wait on each other in a cycle,
    and Postgres only breaks each cycle after ``deadlock_timeout`` (1 s). Queueing writers per
    technician removes those cycles: the next writer waits for the previous one to commit, then
    sees its row and gets a clean exclusion violation. If some future code path writes without
    the lock, nothing becomes incorrect; deadlocks just come back, and are handled here too.
    The lock is taken inside the savepoint, so a failed attempt releases it immediately.
    """
    try:
        async with session.begin_nested():
            await session.execute(
                select(Technician.id)
                .where(Technician.id == technician_id)
                .with_for_update(key_share=True)
            )
            result = await session.execute(statement)
            # Every INSERT here uses RETURNING; UPDATEs return nothing.
            value = result.scalar_one() if isinstance(statement, Insert) else None
    except DBAPIError as exc:
        if is_deadlock(exc):
            return None, _Conflict.DEADLOCK
        for name, conflict in _CONSTRAINT_CONFLICTS.items():
            if is_violation(exc, name):
                return None, conflict
        raise
    return value, None


# --------------------------------------------------------------------------------------------
# public API


async def create_booking(
    session: AsyncSession,
    business: Business,
    customer: Customer,
    request: NewBooking,
    clock: Clock,
    *,
    actor: Actor = Actor.AGENT,
    make_ref: Callable[[], str] = random_ref,
) -> BookingResult:
    now = clock.now()
    cal = await load_calendar(session, business)

    service = await get_service(session, business.id, request.service_code)
    if service is None:
        return BookingErr(
            BookingErrorCode.UNKNOWN_SERVICE,
            f"No active service with code {request.service_code!r}.",
        )
    if not service.bookable:
        return BookingErr(
            BookingErrorCode.SERVICE_NOT_BOOKABLE,
            f"{service.label} needs an on-site estimate from the owner and can't be booked as a "
            "single visit. Escalate to the owner as a quote request.",
        )

    zip_match = _ZIP.match(request.zip.strip())
    if zip_match is None:
        return BookingErr(BookingErrorCode.INVALID_ZIP, "ZIP code must be 5 digits.")
    zip5 = zip_match.group(1)
    if business.service_area_zips and zip5 not in business.service_area_zips:
        return BookingErr(
            BookingErrorCode.OUTSIDE_SERVICE_AREA,
            f"ZIP {zip5} is outside the service area. Don't book; tell the customer politely.",
        )

    checked = check_window(cal, request.slot_start, now)
    if isinstance(checked, SlotProblem):
        return await _slot_error(session, cal, service, now, request.slot_start, checked)
    slot = checked

    existing = await _find_same(session, customer.id, service.id, slot.start)
    if existing is not None:
        return BookingOk("existing", _view(existing, service, cal))

    candidates = await _technicians_by_preference(session, cal, service.trade, slot)
    for technician_id in candidates:
        booking_id, conflict = None, None
        for _ in range(_MAX_WRITE_ATTEMPTS):
            booking_id, conflict = await _try_write(
                session,
                technician_id,
                insert(Booking)
                .values(
                    business_id=business.id,
                    ref=make_ref(),
                    customer_id=customer.id,
                    technician_id=technician_id,
                    service_type_id=service.id,
                    time_window=_range(slot),
                    status=BookingStatus.CONFIRMED,
                    urgency=request.urgency,
                    address=request.address.strip(),
                    zip=zip5,
                    problem_description=request.problem_description.strip(),
                )
                .returning(Booking.id),
            )
            # A clashing ref (astronomically rare) or a deadlock victim: same technician again.
            if conflict not in (_Conflict.REF_TAKEN, _Conflict.DEADLOCK):
                break
        if conflict is _Conflict.SAME_BOOKING:
            # A concurrent duplicate of this exact request committed first.
            same = await _find_same(session, customer.id, service.id, slot.start)
            assert same is not None
            return BookingOk("existing", _view(same, service, cal))
        if conflict is not None:
            continue  # lost the race for this technician; try the next one
        assert isinstance(booking_id, uuid.UUID)
        session.add(
            _event(booking_id, BookingEventType.CREATED, actor, new=(_range(slot), technician_id))
        )
        customer.name = request.customer_name.strip() or customer.name
        customer.address = request.address.strip() or customer.address
        customer.zip = zip5
        await session.flush()
        return BookingOk("created", await _load_view(session, booking_id, cal))

    return BookingErr(
        BookingErrorCode.SLOT_UNAVAILABLE,
        "That window was just taken. Offer one of the alternatives.",
        await _alternatives(session, cal, service, now, slot.start),
    )


async def reschedule_booking(
    session: AsyncSession,
    business: Business,
    customer: Customer,
    ref: str,
    new_slot_start: datetime,
    clock: Clock,
    *,
    actor: Actor = Actor.AGENT,
) -> BookingResult:
    now = clock.now()
    cal = await load_calendar(session, business)
    booking = await _get_customer_booking(session, business, customer, ref)
    if booking is None:
        return BookingErr(
            BookingErrorCode.BOOKING_NOT_FOUND, f"This customer has no booking with ref {ref!r}."
        )
    service = await session.get(ServiceType, booking.service_type_id)
    assert service is not None
    problem = _modifiable_problem(booking, now)
    if problem:
        return BookingErr(BookingErrorCode.NOT_MODIFIABLE, problem)

    checked = check_window(cal, new_slot_start, now)
    if isinstance(checked, SlotProblem):
        return await _slot_error(
            session, cal, service, now, new_slot_start, checked, exclude_booking_id=booking.id
        )
    slot = checked
    old_window, old_tech = booking.time_window, booking.technician_id
    if old_window.lower == slot.start:
        return BookingOk("unchanged", _view(booking, service, cal))

    candidates = await _technicians_by_preference(
        session,
        cal,
        service.trade,
        slot,
        exclude_booking_id=booking.id,
        keep_technician_id=old_tech,
    )
    for technician_id in candidates:
        conflict = None
        for _ in range(_MAX_WRITE_ATTEMPTS):
            # A Core UPDATE rather than mutating the ORM object: if the savepoint rolls back,
            # there is no half-changed in-memory Booking to reconcile.
            _, conflict = await _try_write(
                session,
                technician_id,
                update(Booking)
                .where(Booking.id == booking.id)
                .values(technician_id=technician_id, time_window=_range(slot))
                .execution_options(synchronize_session=False),
            )
            if conflict is not _Conflict.DEADLOCK:
                break
        if conflict is _Conflict.SAME_BOOKING:
            return BookingErr(
                BookingErrorCode.DUPLICATE_BOOKING,
                "The customer already has this service booked in that window.",
            )
        if conflict is not None:
            continue
        session.add(
            _event(
                booking.id,
                BookingEventType.RESCHEDULED,
                actor,
                old=(old_window, old_tech),
                new=(_range(slot), technician_id),
            )
        )
        await session.flush()
        return BookingOk("rescheduled", await _load_view(session, booking.id, cal))

    return BookingErr(
        BookingErrorCode.SLOT_UNAVAILABLE,
        "That window is taken. Offer one of the alternatives.",
        await _alternatives(session, cal, service, now, slot.start, booking.id),
    )


async def cancel_booking(
    session: AsyncSession,
    business: Business,
    customer: Customer,
    ref: str,
    clock: Clock,
    *,
    reason: str | None = None,
    actor: Actor = Actor.AGENT,
) -> BookingResult:
    now = clock.now()
    cal = await load_calendar(session, business)
    booking = await _get_customer_booking(session, business, customer, ref)
    if booking is None:
        return BookingErr(
            BookingErrorCode.BOOKING_NOT_FOUND, f"This customer has no booking with ref {ref!r}."
        )
    service = await session.get(ServiceType, booking.service_type_id)
    assert service is not None
    if booking.status == BookingStatus.CANCELLED:
        return BookingOk("already_cancelled", _view(booking, service, cal))
    problem = _modifiable_problem(booking, now)
    if problem:
        return BookingErr(BookingErrorCode.NOT_MODIFIABLE, problem)

    booking.status = BookingStatus.CANCELLED
    booking.cancel_reason = reason
    session.add(
        _event(
            booking.id,
            BookingEventType.CANCELLED,
            actor,
            old=(booking.time_window, booking.technician_id),
            detail={"reason": reason} if reason else None,
        )
    )
    await session.flush()
    return BookingOk("cancelled", await _load_view(session, booking.id, cal))


def _modifiable_problem(booking: Booking, now: datetime) -> str | None:
    if booking.status == BookingStatus.CANCELLED:
        return "That booking is already cancelled."
    if booking.status == BookingStatus.COMPLETED:
        return "That booking is already completed."
    start = booking.time_window.lower
    assert start is not None
    if start <= now:
        return "That visit has already started or passed. Escalate to the owner."
    return None


async def upcoming_bookings(
    session: AsyncSession, business: Business, customer: Customer, clock: Clock
) -> list[BookingView]:
    """The customer's confirmed bookings that haven't ended yet, read fresh every turn (D-005)."""
    cal = await load_calendar(session, business)
    rows = await session.execute(
        select(Booking, ServiceType)
        .join(ServiceType, ServiceType.id == Booking.service_type_id)
        .where(
            Booking.customer_id == customer.id,
            Booking.status == BookingStatus.CONFIRMED,
            func.upper(Booking.time_window) > clock.now(),
        )
        .order_by(func.lower(Booking.time_window))
    )
    return [_view(b, s, cal) for b, s in rows]
