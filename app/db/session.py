from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings


def make_engine(url: str | None = None, **kwargs: object) -> AsyncEngine:
    return create_async_engine(url or get_settings().database_url, **kwargs)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: domain services return plain values built from ORM rows after the
    # caller commits; expiring would trigger lazy loads, which async sessions forbid.
    return async_sessionmaker(engine, expire_on_commit=False)
