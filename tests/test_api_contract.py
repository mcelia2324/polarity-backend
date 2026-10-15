import datetime as dt
import re

import pytest

import app.main as main
from app.schemas import WordPairResponse
from app.services import content as content_module
from app.services import daily_content_service as dcs
from app.services.content import KnownPair
from app.services.daily_content_service import fallback_contemplation
from app.services.definition_service import DefinitionService
from conftest import CONTEMPLATION_OK, DEFINITIONS_OK, QUOTE_OK, TODAY

# What the shipped 2.1 app decodes (Models/WordPair.swift): date and both words are required
# strings, everything else an optional string.
LEGACY_REQUIRED = ("date", "word_a", "word_b")
LEGACY_OPTIONAL = ("word_a_definition", "word_b_definition", "quote", "quote_author", "contemplation")
V3_KEYS = (
    "higher", "lower", "pair_key", "axis", "lower_level", "higher_level", "quote_source", "meaning",
    "previous_dates",
)


def assert_decodes_like_21(item: dict) -> None:
    for key in LEGACY_REQUIRED:
        assert isinstance(item[key], str), key
    for key in LEGACY_OPTIONAL:
        assert key in item, key
        assert item[key] is None or isinstance(item[key], str), key
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", item["date"])


def numeric_values(value) -> list:
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        return [value]
    if isinstance(value, dict):
        return [n for v in value.values() for n in numeric_values(v)]
    if isinstance(value, list):
        return [n for v in value for n in numeric_values(v)]
    return []


async def seed_complete_day(seed, date=TODAY, word_a="courage", word_b="fear"):
    await seed.pair(date, word_a, word_b)
    await seed.content(
        date,
        quote=QUOTE_OK["quote"],
        quote_author="Seneca",
        quote_source="Letters to Lucilius",
        quote_resolved=True,
        contemplation=CONTEMPLATION_OK,
        attempts=1,
    )


# --- /api/word-of-day -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_word_of_day_keeps_the_21_contract(client, seed, stub):
    await seed_complete_day(seed)
    await seed.definitions_ok()

    resp = await client.get("/api/word-of-day")
    assert resp.status_code == 200
    body = resp.json()
    assert_decodes_like_21(body)
    assert body["date"] == TODAY.isoformat()
    assert (body["word_a"], body["word_b"]) == ("courage", "fear")
    assert body["word_a_definition"] == DEFINITIONS_OK["courage"]
    assert body["word_b_definition"] == DEFINITIONS_OK["fear"]
    assert body["quote"] == QUOTE_OK["quote"] and body["quote_author"] == "Seneca"
    assert body["contemplation"] == CONTEMPLATION_OK

    # Additive v3 keys, and nothing the model does not declare.
    assert set(body) == set(WordPairResponse.model_fields)
    assert (body["higher"], body["lower"], body["pair_key"]) == ("courage", "fear", "courage|fear")
    assert body["quote_source"] == "Letters to Lucilius"
    assert body["meaning"] is None and body["previous_dates"] == []
    assert numeric_values(body) == []

    assert resp.headers["cache-control"] == "public, max-age=1800"
    assert stub.calls == []


@pytest.mark.asyncio
async def test_word_of_day_generates_a_new_day_from_scratch(client, seed, stub):
    stub.scripts.update(
        pair=["courage, fear"],
        definition=[DEFINITIONS_OK["courage"], DEFINITIONS_OK["fear"]],
        quote=[QUOTE_OK],
        contemplation=[CONTEMPLATION_OK],
    )
    resp = await client.get("/api/word-of-day")
    assert resp.status_code == 200
    body = resp.json()
    assert_decodes_like_21(body)
    assert (body["word_a"], body["word_b"]) == ("courage", "fear")
    assert body["word_a_definition"] == DEFINITIONS_OK["courage"]
    assert body["quote_author"] == "Seneca" and body["quote_source"] == "Letters to Lucilius"
    assert body["contemplation"] == CONTEMPLATION_OK
    assert resp.headers["cache-control"] == "public, max-age=1800"
    assert [kind for kind, _ in stub.calls] == ["pair", "definition", "definition", "quote", "contemplation"]
    assert (await seed.get_definition("fear")).source == "openai-v3"


@pytest.mark.asyncio
async def test_word_a_is_the_higher_word_when_the_order_is_known(client, seed, known_pairs):
    known_pairs(bank=[KnownPair("courage", "fear", "how you meet the unknown", "Fear", "Courage")])
    await seed_complete_day(seed, word_a="fear", word_b="courage")  # stored lower-first
    await seed.definitions_ok()

    body = (await client.get("/api/word-of-day")).json()
    assert (body["word_a"], body["word_b"]) == ("courage", "fear")
    assert (body["higher"], body["lower"]) == ("courage", "fear")
    assert body["word_a_definition"] == DEFINITIONS_OK["courage"]
    assert body["word_b_definition"] == DEFINITIONS_OK["fear"]
    assert body["axis"] == "how you meet the unknown"
    assert (body["lower_level"], body["higher_level"]) == ("Fear", "Courage")
    assert numeric_values(body) == []


@pytest.mark.asyncio
async def test_stored_order_is_kept_for_unknown_pairs(client, seed):
    await seed_complete_day(seed, word_a="hope", word_b="despair")
    body = (await client.get("/api/word-of-day")).json()
    assert (body["word_a"], body["word_b"]) == ("hope", "despair")
    assert body["pair_key"] == "despair|hope"
    assert body["axis"] is None and body["lower_level"] is None


def test_orientation_files_are_read_when_present(tmp_path, monkeypatch):
    bank = tmp_path / "pair_bank.json"
    bank.write_text(
        '{"version": 1, "pairs": [{"higher": "hope", "lower": "despair", "axis": "whether tomorrow can be '
        'different", "higher_level": "Willingness", "lower_level": "Apathy", "active": true}]}'
    )
    overrides = tmp_path / "orientation_overrides.json"
    overrides.write_text('{"version": 1, "pairs": [{"higher": "joy", "lower": "hatred", "note": ""}, {"bad": 1}]}')
    monkeypatch.setattr(content_module, "BANK_PATH", bank)
    monkeypatch.setattr(content_module, "OVERRIDES_PATH", overrides)
    content_module._orientation_sources.cache_clear()

    hope = content_module.orient("despair", "hope")
    assert (hope.higher, hope.lower, hope.source) == ("hope", "despair", "bank")
    assert hope.known.axis == "whether tomorrow can be different"
    joy = content_module.orient("hatred", "joy")
    assert (joy.higher, joy.lower, joy.source) == ("joy", "hatred", "override")
    other = content_module.orient("fear", "courage")
    assert (other.higher, other.lower, other.source) == ("fear", "courage", "stored")


def test_missing_orientation_files_keep_the_stored_order(tmp_path, monkeypatch):
    monkeypatch.setattr(content_module, "BANK_PATH", tmp_path / "missing.json")
    monkeypatch.setattr(content_module, "OVERRIDES_PATH", tmp_path / "missing-too.json")
    content_module._orientation_sources.cache_clear()
    assert content_module.orient("despair", "hope").source == "stored"


@pytest.mark.asyncio
async def test_incomplete_day_serves_the_fallback_and_is_cached_briefly(client, seed, stub, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(main, "_clock", lambda: now[0])
    content_now = [dt.datetime(2026, 10, 15, 6, 0, tzinfo=dt.timezone.utc)]
    monkeypatch.setattr(dcs, "_utcnow", lambda: content_now[0])
    await seed.pair(TODAY, "courage", "fear")
    await seed.definitions_ok()

    # The quote and contemplation calls fail (nothing scripted).
    resp = await client.get("/api/word-of-day")
    assert resp.status_code == 200
    body = resp.json()
    assert_decodes_like_21(body)
    assert body["contemplation"] == fallback_contemplation("courage", "fear")
    assert resp.headers["cache-control"] == "public, max-age=60"
    assert (await seed.get_content(TODAY)).contemplation is None  # the fallback is never saved

    # Served from the cache for a few minutes, without touching the LLM.
    calls = len(stub.calls)
    now[0] += 299
    assert (await client.get("/api/word-of-day")).json() == body
    assert len(stub.calls) == calls

    # After the TTL (and the content backoff) the day is rebuilt and completed.
    now[0] += 2
    content_now[0] += dt.timedelta(minutes=5)
    stub.scripts.update(quote=[QUOTE_OK], contemplation=[CONTEMPLATION_OK])
    resp = await client.get("/api/word-of-day")
    assert resp.json()["contemplation"] == CONTEMPLATION_OK
    assert resp.headers["cache-control"] == "public, max-age=1800"

    # Complete responses stay cached for the rest of the day.
    calls = len(stub.calls)
    now[0] += 12 * 3600
    assert (await client.get("/api/word-of-day")).json()["contemplation"] == CONTEMPLATION_OK
    assert len(stub.calls) == calls


@pytest.mark.asyncio
async def test_content_epoch_invalidates_the_cache(client, seed, session_factory):
    await seed_complete_day(seed)
    await seed.definitions_ok()
    assert (await client.get("/api/word-of-day")).json()["contemplation"] == CONTEMPLATION_OK

    # Another instance rewrites the day and bumps the epoch.
    async with session_factory() as session:
        row = await session.get(main.DailyContent, TODAY)
        row.contemplation = "Fear can wait at the door today. Let courage pick the first small step you take."
        await content_module.bump_content_epoch(session)
        await session.commit()

    body = (await client.get("/api/word-of-day")).json()
    assert body["contemplation"].startswith("Fear can wait at the door today.")
    assert await seed.get_setting("content_epoch") == "1"


@pytest.mark.asyncio
async def test_failing_extras_still_return_the_pair(client, seed, monkeypatch):
    await seed.pair(TODAY, "courage", "fear")

    async def broken(*args, **kwargs):
        raise RuntimeError("database hiccup")

    monkeypatch.setattr(content_module.DailyContentService, "get_or_create", broken)
    monkeypatch.setattr(DefinitionService, "get_definition", broken)

    resp = await client.get("/api/word-of-day")
    assert resp.status_code == 200
    body = resp.json()
    assert_decodes_like_21(body)
    assert (body["word_a"], body["word_b"]) == ("courage", "fear")
    assert body["word_a_definition"] is None and body["word_b_definition"] is None
    assert body["contemplation"] == fallback_contemplation("courage", "fear")
    assert resp.headers["cache-control"] == "public, max-age=60"


# --- /api/history -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_joins_content_filters_the_future_and_never_generates(client, seed, stub, known_pairs):
    known_pairs(overrides=[KnownPair("hope", "despair")])
    yesterday, two_days_ago = TODAY - dt.timedelta(days=1), TODAY - dt.timedelta(days=2)
    await seed_complete_day(seed, date=yesterday)
    await seed.pair(two_days_ago, "despair", "hope")  # lower-first, no daily content row
    await seed.pair(TODAY, "patience", "irritation")
    await seed.content(TODAY, contemplation=None, attempts=1)  # still pending
    await seed.pair(TODAY + dt.timedelta(days=1), "trust", "suspicion")  # backfilled ahead of time
    await seed.definitions_ok()

    resp = await client.get("/api/history?days=30")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [item["date"] for item in items] == [d.isoformat() for d in (TODAY, yesterday, two_days_ago)]
    for item in items:
        assert_decodes_like_21(item)
        assert set(item) == set(WordPairResponse.model_fields)
        assert numeric_values(item) == []

    today_item, yesterday_item, older_item = items
    assert today_item["contemplation"] is None  # no fallback in history
    assert yesterday_item["contemplation"] == CONTEMPLATION_OK  # joined from daily_content
    assert yesterday_item["quote"] == QUOTE_OK["quote"] and yesterday_item["quote_author"] == "Seneca"
    assert yesterday_item["word_a_definition"] == DEFINITIONS_OK["courage"]
    assert (older_item["word_a"], older_item["word_b"]) == ("hope", "despair")  # oriented
    assert older_item["quote"] is None and older_item["contemplation"] is None
    assert older_item["word_a_definition"] is None

    assert stub.calls == []  # history never calls the LLM

    limited = (await client.get("/api/history?days=2")).json()["items"]
    assert [item["date"] for item in limited] == [TODAY.isoformat(), yesterday.isoformat()]


@pytest.mark.asyncio
async def test_large_responses_are_gzipped(client, seed):
    for offset in range(10):
        await seed_complete_day(seed, date=TODAY - dt.timedelta(days=offset))
    await seed.definitions_ok()

    resp = await client.get("/api/history?days=10", headers={"Accept-Encoding": "gzip"})
    assert resp.headers["content-encoding"] == "gzip"
    assert len(resp.json()["items"]) == 10

    small = await client.get("/health", headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in small.headers
