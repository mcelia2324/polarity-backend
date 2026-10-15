import contextlib
import re

import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

import app.db as db
from app.migrations import POSTGRES_STATEMENTS, run_migrations
from app.models import DailyContent


class _RecordingConnection:
    def __init__(self, dialect_name: str = "postgresql"):
        self.dialect = type("Dialect", (), {"name": dialect_name})()
        self.statements: list[str] = []

    async def execute(self, statement):
        self.statements.append(str(statement))

    async def run_sync(self, fn):
        self.statements.append("create_all")


class _FakeEngine:
    def __init__(self, conn):
        self._conn = conn

    @contextlib.asynccontextmanager
    async def begin(self):
        yield self._conn


@pytest.mark.asyncio
async def test_migrations_run_in_order_with_a_lock_timeout():
    conn = _RecordingConnection()
    await run_migrations(conn)
    assert conn.statements[0].startswith("SET LOCAL lock_timeout")
    assert conn.statements[1:] == list(POSTGRES_STATEMENTS)


def test_every_alter_is_idempotent():
    for statement in POSTGRES_STATEMENTS:
        if statement.startswith("ALTER TABLE"):
            assert "ADD COLUMN IF NOT EXISTS" in statement, statement
    backfill = [s for s in POSTGRES_STATEMENTS if s.startswith("UPDATE")]
    assert backfill == [
        "UPDATE daily_content SET quote_resolved = TRUE WHERE quote IS NOT NULL AND quote_resolved = FALSE"
    ]


def test_migrations_cover_every_new_daily_content_column():
    added = {
        re.search(r"ADD COLUMN IF NOT EXISTS (\w+)", s).group(1)
        for s in POSTGRES_STATEMENTS
        if s.startswith("ALTER TABLE daily_content")
    }
    original = {"date", "quote", "quote_author", "contemplation", "created_at"}
    model_columns = {column.name for column in DailyContent.__table__.columns}
    assert added == model_columns - original
    # Non-null model columns get a server default, so rows inserted by an older revision during
    # a deploy (which does not know the columns) are still valid.
    for column in DailyContent.__table__.columns:
        if column.name in added and not column.nullable:
            assert column.server_default is not None, column.name
            statement = next(s for s in POSTGRES_STATEMENTS if f" {column.name} " in s)
            assert "NOT NULL DEFAULT" in statement, statement


@pytest.mark.asyncio
async def test_init_db_runs_migrations_on_postgres(monkeypatch):
    conn = _RecordingConnection("postgresql")
    monkeypatch.setattr(db, "engine", _FakeEngine(conn))
    await db.init_db()
    assert conn.statements[0] == "create_all"
    assert conn.statements[1].startswith("SET LOCAL lock_timeout")
    assert len(conn.statements) == 2 + len(POSTGRES_STATEMENTS)


@pytest.mark.asyncio
async def test_init_db_skips_migrations_on_sqlite(monkeypatch):
    calls = []

    async def recording(conn):
        calls.append(conn)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "run_migrations", recording)
    await db.init_db()
    assert calls == []
    async with engine.connect() as conn:
        columns = await conn.run_sync(lambda sync: {c["name"] for c in inspect(sync).get_columns("daily_content")})
    assert {"quote_source", "quote_resolved", "attempts", "last_attempt_at", "prompt_version"} <= columns
    await engine.dispose()
