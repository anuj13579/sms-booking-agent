"""The ORM models and the Alembic migrations must describe the same schema."""

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.base import Base


async def test_models_match_migrations(engine: AsyncEngine) -> None:
    def diff(connection: Connection) -> list[object]:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        return list(compare_metadata(context, Base.metadata))

    async with engine.connect() as conn:
        assert await conn.run_sync(diff) == []


async def test_integrity_constraints_exist(engine: AsyncEngine) -> None:
    """Autogenerate can't see exclusion constraints or expression indexes; check them by name."""
    async with engine.connect() as conn:
        constraints = set((await conn.execute(text("SELECT conname FROM pg_constraint"))).scalars())
        indexes = set((await conn.execute(text("SELECT indexname FROM pg_indexes"))).scalars())
    assert "ex_bookings_technician_no_overlap" in constraints
    assert {"uq_bookings_customer_service_start", "uq_conversations_open_per_channel"} <= indexes
