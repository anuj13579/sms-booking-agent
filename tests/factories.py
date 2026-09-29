"""Small, explicit test fixtures. Each test builds exactly the business it needs."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Business,
    Customer,
    ServiceType,
    Technician,
    TechnicianTimeOff,
    WindowTemplate,
)
from app.domain.booking import NewBooking
from app.domain.customers import get_or_create_customer
from app.domain.enums import Urgency
from app.domain.localtime import local_to_utc

NY = ZoneInfo("America/New_York")

# Weekdays 8-10, 10-12, 1-3, 3-5; Saturday 9-11, 11-1; closed Sunday.
WEEKDAY_WINDOWS = [
    (time(8), time(10)),
    (time(10), time(12)),
    (time(13), time(15)),
    (time(15), time(17)),
]
SATURDAY_WINDOWS = [(time(9), time(11)), (time(11), time(13))]

SERVICES = [
    # code, label, trade, bookable
    ("ac_repair", "AC repair", "hvac", True),
    ("furnace_repair", "Furnace repair", "hvac", True),
    ("hvac_install", "HVAC system install", "hvac", False),
    ("leak_repair", "Leak repair", "plumbing", True),
    ("outlet_repair", "Outlet or switch repair", "electrical", True),
]


def local(day: date, hour: int, minute: int = 0) -> datetime:
    """A New York wall time as a UTC instant."""
    instant = local_to_utc(day, time(hour, minute), NY)
    assert instant is not None
    return instant


@dataclass
class World:
    business: Business
    services: dict[str, ServiceType]
    techs: dict[str, Technician]
    customers: dict[str, Customer] = field(default_factory=dict)


async def build_world(
    session: AsyncSession,
    techs: Sequence[tuple[str, Sequence[str]]] = (("Ana", ["hvac"]), ("Ben", ["hvac", "plumbing"])),
    *,
    min_lead_minutes: int = 120,
    horizon_days: int = 14,
    service_area_zips: Sequence[str] = ("07030", "07302"),
) -> World:
    business = Business(
        name="Brightwater Test Co",
        timezone="America/New_York",
        sms_number=f"+1555555{uuid.uuid4().int % 10000:04d}",
        owner_name="Dana",
        owner_phone="+15555550101",
        min_lead_minutes=min_lead_minutes,
        horizon_days=horizon_days,
        service_area_zips=list(service_area_zips),
    )
    session.add(business)
    await session.flush()

    for weekday in range(5):
        for start, end in WEEKDAY_WINDOWS:
            session.add(
                WindowTemplate(
                    business_id=business.id, weekday=weekday, start_local=start, end_local=end
                )
            )
    for start, end in SATURDAY_WINDOWS:
        session.add(
            WindowTemplate(business_id=business.id, weekday=5, start_local=start, end_local=end)
        )

    services = {}
    for code, label, trade, bookable in SERVICES:
        services[code] = ServiceType(
            business_id=business.id, code=code, label=label, trade=trade, bookable=bookable
        )
        session.add(services[code])

    # Ascending UUIDs in list order, so "ties broken by id" is predictable in assertions.
    base = uuid.uuid4().int >> 64 << 64
    tech_rows = {}
    for index, (name, trades) in enumerate(techs):
        tech_rows[name] = Technician(
            id=uuid.UUID(int=base + index + 1),
            business_id=business.id,
            name=name,
            trades=list(trades),
        )
        session.add(tech_rows[name])
    await session.flush()
    return World(business, services, tech_rows)


async def add_customer(session: AsyncSession, world: World, phone: str, name: str = "") -> Customer:
    customer = await get_or_create_customer(session, world.business.id, phone)
    world.customers[name or phone] = customer
    return customer


async def add_time_off(
    session: AsyncSession, tech: Technician, start: datetime, end: datetime
) -> None:
    session.add(TechnicianTimeOff(technician_id=tech.id, period=Range(start, end, bounds="[)")))
    await session.flush()


def new_booking(
    slot_start: datetime,
    service_code: str = "ac_repair",
    *,
    zip_code: str = "07030",
    name: str = "Pat Customer",
    urgency: Urgency = Urgency.ROUTINE,
) -> NewBooking:
    return NewBooking(
        service_code=service_code,
        slot_start=slot_start,
        customer_name=name,
        address="12 River St, Hoboken NJ",
        zip=zip_code,
        problem_description="AC blowing warm air",
        urgency=urgency,
    )
