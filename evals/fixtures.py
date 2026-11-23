"""Build a scenario's starting state: a fresh copy of Brightwater, plus the fixture.

Every row goes through the real domain services (``create_booking`` and friends) with the
scenario's frozen clock, so a fixture can't describe a state the product couldn't reach.
"""

import itertools
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Business, Technician, TechnicianTimeOff
from app.domain.booking import BookingErr, BookingErrorCode, BookingOk, NewBooking, create_booking
from app.domain.clock import Clock
from app.domain.customers import get_or_create_customer
from app.domain.enums import Actor, Trade
from app.seed import SERVICES, create_brightwater
from evals.scenario import TZ, Busy, Fixture, bookable_starts

CUSTOMER_PHONE = "+15555550142"  # the texting customer in every scenario
FILLER_ZIP = "07030"
# One bookable service per trade, used to fill windows with other customers' visits.
_FILLER_SERVICE = {
    Trade(trade): code for code, _, trade, bookable in reversed(SERVICES) if bookable
}
_filler_numbers = itertools.count(1)


class FixtureError(RuntimeError):
    pass


@dataclass
class Built:
    business: Business
    labels: dict[str, uuid.UUID] = field(default_factory=dict)  # fixture label -> booking id
    original_starts: dict[uuid.UUID, datetime] = field(default_factory=dict)


async def build(session: AsyncSession, fixture: Fixture, clock: Clock, sms_number: str) -> Built:
    business = await create_brightwater(session, sms_number)
    built = Built(business)
    now = clock.now()

    techs = {t.name: t for t in await session.scalars(
        select(Technician).where(Technician.business_id == business.id)
    )}  # fmt: skip
    for off in fixture.time_off:
        session.add(
            TechnicianTimeOff(
                technician_id=techs[off.tech].id, period=Range(off.start, off.end, bounds="[)")
            )
        )

    if fixture.customer is not None:
        spec = fixture.customer
        customer = await get_or_create_customer(session, business.id, CUSTOMER_PHONE)
        customer.name = spec.name
        customer.address = spec.address
        customer.zip = spec.zip
        if spec.opted_out:
            customer.opted_out_at = now - timedelta(days=3)
        await session.flush()
        for booking in fixture.bookings:
            result = await create_booking(
                session,
                business,
                customer,
                NewBooking(
                    service_code=booking.service,
                    slot_start=booking.start,
                    customer_name=spec.name or "",
                    address=spec.address or "",
                    zip=spec.zip or "",
                    problem_description=booking.problem,
                    urgency=booking.urgency,
                ),
                clock,
                actor=Actor.SYSTEM,
            )
            if not isinstance(result, BookingOk):
                raise FixtureError(f"fixture booking {booking.label}: {result.message}")
            built.labels[booking.label] = result.booking.id
            built.original_starts[result.booking.id] = result.booking.slot.start

    for busy in fixture.busy:
        for start in _busy_starts(busy, now):
            for trade in [busy.trade] if busy.trade else list(Trade):
                await _fill(session, business, trade, start, clock)
    await session.flush()
    return built


def _busy_starts(busy: Busy, now: datetime) -> list[datetime]:
    if busy.start is not None:
        return [busy.start]
    return [s for s in bookable_starts(now) if s.date() == busy.on]


async def _fill(
    session: AsyncSession, business: Business, trade: Trade, start: datetime, clock: Clock
) -> None:
    """Book other customers into ``start`` until no technician of ``trade`` is free."""
    for _ in range(20):
        filler = await get_or_create_customer(
            session, business.id, f"+1555556{next(_filler_numbers) % 10000:04d}"
        )
        result = await create_booking(
            session,
            business,
            filler,
            NewBooking(
                service_code=_FILLER_SERVICE[trade],
                slot_start=start,
                customer_name="Morgan Vale",  # distinctive, so a leak is easy to grep for
                address="7 Quarry Lane, Hoboken",
                zip=FILLER_ZIP,
                problem_description="Another customer's visit",
            ),
            clock,
            actor=Actor.SYSTEM,
        )
        if isinstance(result, BookingErr):
            if result.code is BookingErrorCode.SLOT_UNAVAILABLE:
                return
            raise FixtureError(
                f"filling {trade} at {start.astimezone(TZ):%Y-%m-%d %H:%M}: {result.message}"
            )
    raise FixtureError(f"filling {trade} at {start}: still free after 20 bookings")
