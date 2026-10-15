"""Idempotent schema changes for databases created before a column existed.

`Base.metadata.create_all` creates missing tables but never adds columns to existing ones, and
there is no migration framework. `db.init_db` runs these right after create_all, on Postgres only
(SQLite tests get the full schema from create_all).

Every statement must be safe to run on every startup, and on two instances starting at once:
only `IF NOT EXISTS` forms and backfills that are no-ops the second time. Postgres adds a column
with a constant default without rewriting the table, so these are instant. If one fails, startup
fails, the new revision never becomes ready, and Cloud Run keeps serving the old one.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

# ALTER TABLE needs a brief exclusive lock. If a long transaction holds the table, give up after
# this long (startup fails and the deploy can be retried) instead of queueing every request behind it.
LOCK_TIMEOUT = "15s"

POSTGRES_STATEMENTS: tuple[str, ...] = (
    # daily_content status columns (see DailyContentService.get_or_create).
    "ALTER TABLE daily_content ADD COLUMN IF NOT EXISTS quote_source TEXT",
    "ALTER TABLE daily_content ADD COLUMN IF NOT EXISTS quote_resolved BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE daily_content ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE daily_content ADD COLUMN IF NOT EXISTS last_attempt_at TIMESTAMPTZ",
    "ALTER TABLE daily_content ADD COLUMN IF NOT EXISTS prompt_version INTEGER NOT NULL DEFAULT 1",
    # Rows written before quote_resolved existed: a stored quote is final.
    "UPDATE daily_content SET quote_resolved = TRUE WHERE quote IS NOT NULL AND quote_resolved = FALSE",
)


async def run_migrations(conn: AsyncConnection) -> None:
    """Apply POSTGRES_STATEMENTS inside the caller's transaction (all or nothing)."""
    await conn.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
    for statement in POSTGRES_STATEMENTS:
        await conn.execute(text(statement))
