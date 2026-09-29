"""Seed the fictional pilot business, Brightwater Home Services (America/New_York).

    uv run python -m app.seed

Idempotent: if a business with this SMS number already exists, nothing is written.
"""

import asyncio
from datetime import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Business, ServiceType, Technician, WindowTemplate
from app.db.session import make_engine, make_sessionmaker

SMS_NUMBER = "+15555550100"  # 555-01xx numbers are reserved for fiction

# Hoboken, Jersey City, and the towns just north along the Hudson.
SERVICE_AREA_ZIPS = [
    "07030", "07302", "07304", "07305", "07306", "07307", "07310", "07311",
    "07086", "07087", "07093", "07047", "07002",
]  # fmt: skip

WEEKDAY_WINDOWS = [
    (time(8), time(10)),
    (time(10), time(12)),
    (time(13), time(15)),
    (time(15), time(17)),
]
SATURDAY_WINDOWS = [(time(9), time(11)), (time(11), time(13))]

# code, label, trade, bookable. Non-bookable jobs span several visits or need an estimate first;
# the agent routes them to the owner as a quote request (D-007).
SERVICES = [
    ("ac_repair", "AC repair", "hvac", True),
    ("heating_repair", "Furnace or heating repair", "hvac", True),
    ("hvac_maintenance", "HVAC tune-up", "hvac", True),
    ("hvac_install", "New HVAC system", "hvac", False),
    ("leak_repair", "Leak repair", "plumbing", True),
    ("drain_clog", "Clogged drain", "plumbing", True),
    ("toilet_repair", "Toilet repair", "plumbing", True),
    ("water_heater_repair", "Water heater repair", "plumbing", True),
    ("water_heater_install", "New water heater", "plumbing", False),
    ("outlet_switch_repair", "Outlet or switch repair", "electrical", True),
    ("lighting", "Light fixture install or repair", "electrical", True),
    ("power_issue", "Breaker or power problem", "electrical", True),
    ("panel_upgrade", "Electrical panel upgrade", "electrical", False),
    ("ev_charger_install", "EV charger install", "electrical", False),
]

TECHNICIANS = [
    ("Marcus Hill", ["hvac"]),
    ("Luis Ortega", ["hvac", "plumbing"]),
    ("Priya Shah", ["plumbing"]),
    ("Jenna Brooks", ["electrical"]),
    ("Sam Carter", ["electrical", "hvac"]),
]


async def seed_brightwater(session: AsyncSession) -> Business:
    existing = await session.scalar(select(Business).where(Business.sms_number == SMS_NUMBER))
    if existing is not None:
        return existing

    business = Business(
        name="Brightwater Home Services",
        timezone="America/New_York",
        sms_number=SMS_NUMBER,
        owner_name="Dana",
        owner_phone="+15555550101",
        min_lead_minutes=120,
        horizon_days=14,
        service_area_zips=SERVICE_AREA_ZIPS,
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
    for code, label, trade, bookable in SERVICES:
        session.add(
            ServiceType(
                business_id=business.id, code=code, label=label, trade=trade, bookable=bookable
            )
        )
    for name, trades in TECHNICIANS:
        session.add(Technician(business_id=business.id, name=name, trades=trades))
    await session.flush()
    return business


async def main() -> None:
    engine = make_engine()
    async with make_sessionmaker(engine)() as session, session.begin():
        existed = await session.scalar(select(Business.id).where(Business.sms_number == SMS_NUMBER))
        business = await seed_brightwater(session)
        verb = "Already seeded" if existed else "Seeded"
        print(f"{verb}: {business.name} ({business.sms_number}), id {business.id}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
