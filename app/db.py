import asyncio

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings
from app.migrations import run_migrations


class Base(DeclarativeBase):
    pass


def _create_engine():
    return create_async_engine(
        settings.get_database_url(),
        pool_pre_ping=True,
    )


engine = _create_engine()
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    # Register every table before create_all; the migrations below fail if a table is missing.
    import app.models  # noqa: F401  (imported here: app.models imports Base from this module)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # create_all never adds columns to existing tables; app/migrations.py does (Postgres only).
        if conn.dialect.name == "postgresql":
            await run_migrations(conn)


async def wait_for_db(retries: int = 10, delay: float = 1.0) -> None:
    for attempt in range(retries):
        try:
            async with engine.connect():
                return
        except Exception:  # noqa: BLE001
            if attempt >= retries - 1:
                raise
            await asyncio.sleep(delay)


async def get_session():
    async with SessionLocal() as session:
        yield session
