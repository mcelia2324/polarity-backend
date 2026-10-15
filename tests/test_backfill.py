import datetime as dt

import pytest
from sqlalchemy import select

from app.models import WordPair
from conftest import CONTEMPLATION_OK, DEFINITIONS_OK, QUOTE_OK, TODAY

SECRET = {"X-Cron-Secret": "test-secret"}
PAST = TODAY - dt.timedelta(days=3)
OLD_CONTEMPLATION = "Old contemplation written for the old pair, hope and despair."


async def _pair_for(session_factory, date):
    async with session_factory() as session:
        return (await session.execute(select(WordPair).where(WordPair.date == date))).scalar_one_or_none()


@pytest.mark.asyncio
async def test_force_replaces_the_pair_and_its_content(client, seed, stub, session_factory):
    await seed.pair(PAST, "hope", "despair")
    await seed.content(PAST, quote="An old quote for an old pair.", quote_author="Seneca",
                       quote_resolved=True, contemplation=OLD_CONTEMPLATION, attempts=1)
    stub.scripts.update(
        pair=["courage, fear"],
        definition=[DEFINITIONS_OK["courage"], DEFINITIONS_OK["fear"]],
        quote=[QUOTE_OK],
        contemplation=[CONTEMPLATION_OK],
    )

    resp = await client.post(f"/cron/backfill?date={PAST.isoformat()}&force=true", headers=SECRET)
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok", "date": PAST.isoformat(), "word_a": "courage", "word_b": "fear", "complete": True,
    }

    pair = await _pair_for(session_factory, PAST)
    assert (pair.word_a, pair.word_b) == ("courage", "fear")
    row = await seed.get_content(PAST)
    assert row.contemplation == CONTEMPLATION_OK  # regenerated for the new pair
    assert row.quote == QUOTE_OK["quote"]
    assert row.attempts == 1
    # The quote and contemplation were written for the new words, in the right order.
    assert stub.calls_of("contemplation")[0].user_prompt.startswith("Higher word: courage\nLower word: fear")
    assert int(await seed.get_setting("content_epoch")) >= 1


@pytest.mark.asyncio
async def test_force_backfill_of_today_refreshes_the_cached_word_of_day(client, seed, stub):
    await seed.pair(TODAY, "hope", "despair")
    await seed.content(TODAY, quote=None, quote_resolved=True, contemplation=OLD_CONTEMPLATION, attempts=1)
    await seed.definition("hope", "Hope is the quiet belief that tomorrow can be better than today, even when it does not look that way yet.")
    await seed.definition("despair", "Despair is the heavy sense that nothing will change, which makes small efforts feel pointless before you even try.")
    assert (await client.get("/api/word-of-day")).json()["word_a"] == "hope"  # now cached

    stub.scripts.update(
        pair=["courage, fear"],
        definition=[DEFINITIONS_OK["courage"], DEFINITIONS_OK["fear"]],
        quote=[QUOTE_OK],
        contemplation=[CONTEMPLATION_OK],
    )
    resp = await client.post(f"/cron/backfill?date={TODAY.isoformat()}&force=true", headers=SECRET)
    assert resp.status_code == 200

    body = (await client.get("/api/word-of-day")).json()
    assert (body["word_a"], body["word_b"]) == ("courage", "fear")
    assert body["contemplation"] == CONTEMPLATION_OK


@pytest.mark.asyncio
async def test_backfill_without_force_keeps_the_pair_and_fills_missing_content(client, seed, stub, session_factory):
    await seed.pair(PAST, "courage", "fear")
    await seed.definitions_ok()
    # Capped: the read path would never try again, but a deliberate backfill does.
    await seed.content(PAST, quote=None, quote_resolved=True, contemplation=None, attempts=6,
                       last_attempt_at=dt.datetime.now(dt.timezone.utc))
    stub.scripts["contemplation"] = [CONTEMPLATION_OK]

    resp = await client.post(f"/cron/backfill?date={PAST.isoformat()}", headers=SECRET)
    assert resp.status_code == 200
    assert resp.json()["complete"] is True
    pair = await _pair_for(session_factory, PAST)
    assert (pair.word_a, pair.word_b) == ("courage", "fear")
    assert (await seed.get_content(PAST)).contemplation == CONTEMPLATION_OK
    assert stub.calls_of("pair") == [] and stub.calls_of("quote") == []
    assert await seed.get_setting("content_epoch") is None  # nothing was replaced


@pytest.mark.asyncio
async def test_backfill_requires_the_cron_secret(client):
    assert (await client.post(f"/cron/backfill?date={PAST.isoformat()}")).status_code == 403
    assert (await client.post(f"/cron/backfill?date={PAST.isoformat()}", headers={"X-Cron-Secret": "nope"})).status_code == 403
