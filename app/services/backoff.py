"""Retry schedule for generated content that failed (daily content now, meanings later).

A failed generation is retried on a later request, not in a tight loop: wait 5, 10, 20, 40, 80,
then 160 minutes between attempts, and stop after MAX_ATTEMPTS until someone forces a reset.
"""
from __future__ import annotations

import datetime as dt

BACKOFF_MINUTES: tuple[int, ...] = (5, 10, 20, 40, 80, 160)
MAX_ATTEMPTS = 6


def as_utc(value: dt.datetime | None) -> dt.datetime | None:
    """Make a datetime timezone-aware. SQLite hands back naive datetimes (we always store UTC),
    while Postgres TIMESTAMPTZ returns aware ones, and mixing the two raises TypeError."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.timezone.utc)


def backoff_delay(attempts: int) -> dt.timedelta:
    """How long to wait after the given number of attempts (1 -> 5 minutes ... 6+ -> 160)."""
    index = min(max(attempts, 1), len(BACKOFF_MINUTES)) - 1
    return dt.timedelta(minutes=BACKOFF_MINUTES[index])


def retry_due(attempts: int | None, last_attempt_at: dt.datetime | None, now: dt.datetime) -> bool:
    """True when another attempt may run now: never tried, or the backoff after the last attempt
    has passed, and the attempt cap is not reached."""
    attempts = attempts or 0
    if attempts >= MAX_ATTEMPTS:
        return False
    last = as_utc(last_attempt_at)
    if attempts == 0 or last is None:
        return True
    return as_utc(now) >= last + backoff_delay(attempts)
