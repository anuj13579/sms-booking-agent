"""The eval database: separate from development and tests, rebuilt from the migrations at the
start of every run, so a run starts from nothing and its rows can be inspected afterwards."""

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_URL = "postgresql+asyncpg://booking:booking@localhost:5432/booking_agent_eval"


class _EvalSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    eval_database_url: str = DEFAULT_URL


def eval_database_url() -> str:
    """``EVAL_DATABASE_URL`` from the environment or ``.env``, like the app's own settings."""
    return _EvalSettings().eval_database_url


class UnsafeDatabaseError(RuntimeError):
    pass


async def _create_if_missing(url: str) -> None:
    target = make_url(url)
    admin = create_async_engine(
        target.set(database="postgres"), poolclass=NullPool, isolation_level="AUTOCOMMIT"
    )
    try:
        async with admin.connect() as conn:
            exists = await conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": target.database},
            )
            if not exists:
                await conn.execute(text(f'CREATE DATABASE "{target.database}"'))
    finally:
        await admin.dispose()


async def _drop_schema(url: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
    finally:
        await engine.dispose()


def reset(url: str) -> None:
    """Create the database if needed, wipe it, and migrate it to head. Refuses any database
    whose name doesn't end in ``_eval``, so a typo can't wipe development data."""
    name = make_url(url).database or ""
    if not name.endswith("_eval"):
        raise UnsafeDatabaseError(f"refusing to wipe {name!r}: eval databases end in _eval")
    asyncio.run(_create_if_missing(url))
    asyncio.run(_drop_schema(url))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
