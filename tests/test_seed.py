from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Business, ServiceType
from app.domain.availability import find_available_slots, load_calendar
from app.seed import seed_brightwater


async def test_seed_is_idempotent(session: AsyncSession) -> None:
    first = await seed_brightwater(session)
    second = await seed_brightwater(session)
    assert first.id == second.id
    assert await session.scalar(select(func.count()).select_from(Business)) == 1


async def test_every_bookable_service_has_availability(session: AsyncSession) -> None:
    business = await seed_brightwater(session)
    cal = await load_calendar(session, business)
    services = (
        await session.scalars(select(ServiceType).where(ServiceType.business_id == business.id))
    ).all()
    monday_morning = datetime(2026, 10, 5, 11, tzinfo=UTC)

    for service in services:
        slots = await find_available_slots(session, cal, service, monday_morning)
        assert slots, f"no technician can do {service.code}"
    assert {s.code for s in services if not s.bookable} == {
        "hvac_install",
        "water_heater_install",
        "panel_upgrade",
        "ev_charger_install",
    }
