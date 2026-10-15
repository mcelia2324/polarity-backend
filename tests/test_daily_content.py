import datetime as dt

import pytest

from app.services import daily_content_service as dcs
from app.services.backoff import BACKOFF_MINUTES, MAX_ATTEMPTS, backoff_delay, retry_due
from app.services.daily_content_service import (
    EXCLUDED_QUOTE_AUTHORS,
    QUOTE_AUTHORS,
    DailyContentService,
    QuoteResult,
    canonical_author,
    contemplation_errors,
    fallback_contemplation,
    validate_quote,
)
from app.services.llm.base import LLMProviderError
from app.services.voice import has_word, sentences, word_count
from conftest import CONTEMPLATION_OK, QUOTE_NONE, QUOTE_OK, StubLLM

DAY = dt.date(2026, 10, 15)
START = dt.datetime(2026, 10, 15, 5, 0, tzinfo=dt.timezone.utc)
BAD_CONTEMPLATION = (
    "Fear keeps you small — it hides in every pause you take before you speak. Notice it "
    "without any blame at all. Then let courage answer the fear with one small step today."
)


class Clock:
    def __init__(self, start: dt.datetime):
        self.now = start

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, **delta) -> None:
        self.now += dt.timedelta(**delta)


@pytest.fixture
def clock(monkeypatch) -> Clock:
    clock = Clock(START)
    monkeypatch.setattr(dcs, "_utcnow", clock)
    return clock


async def _run(session_factory, stub, **kwargs):
    async with session_factory() as session:
        return await DailyContentService(session, stub).get_or_create(DAY, "courage", "fear", **kwargs)


# --- generation and storage -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generates_and_saves_both_pieces(session_factory, seed, clock):
    stub = StubLLM(quote=[QUOTE_OK], contemplation=[CONTEMPLATION_OK])
    await _run(session_factory, stub, axis="how you meet the unknown")

    row = await seed.get_content(DAY)
    assert row.quote == QUOTE_OK["quote"]
    assert row.quote_author == "Seneca"
    assert row.quote_source == "Letters to Lucilius"
    assert row.quote_resolved is True
    assert row.contemplation == CONTEMPLATION_OK
    assert row.attempts == 1
    assert row.prompt_version == 3
    assert row.last_attempt_at is not None

    quote_request = stub.calls_of("quote")[0]
    assert quote_request.json_schema["required"] == ["quote", "author", "work"]
    assert quote_request.json_schema["additionalProperties"] is False
    assert all(name in quote_request.system_prompt for name in QUOTE_AUTHORS)
    contemplation_request = stub.calls_of("contemplation")[0]
    assert contemplation_request.user_prompt.startswith("Higher word: courage\nLower word: fear\n")
    assert "What the pair is about: how you meet the unknown" in contemplation_request.user_prompt

    # Complete: later calls never reach the LLM.
    calls = len(stub.calls)
    clock.advance(days=1)
    await _run(session_factory, stub)
    assert len(stub.calls) == calls


@pytest.mark.asyncio
async def test_failure_saves_a_pending_row_not_a_final_null(session_factory, seed, clock):
    stub = StubLLM()  # every call fails
    returned = await _run(session_factory, stub)
    assert returned.contemplation is None

    row = await seed.get_content(DAY)
    assert row.contemplation is None
    assert row.quote is None and row.quote_resolved is False
    assert row.attempts == 1 and row.last_attempt_at is not None

    # Inside the backoff window nothing is retried.
    calls = len(stub.calls)
    clock.advance(minutes=4)
    await _run(session_factory, stub)
    assert len(stub.calls) == calls

    # After it, only the missing pieces are generated.
    clock.advance(minutes=1)
    stub.scripts.update(quote=[QUOTE_OK], contemplation=[CONTEMPLATION_OK])
    await _run(session_factory, stub)
    row = await seed.get_content(DAY)
    assert row.contemplation == CONTEMPLATION_OK
    assert row.quote_resolved is True
    assert row.attempts == 2


@pytest.mark.asyncio
async def test_no_good_quote_is_final(session_factory, seed, clock):
    stub = StubLLM(quote=[QUOTE_NONE], contemplation=[CONTEMPLATION_OK])
    await _run(session_factory, stub)

    row = await seed.get_content(DAY)
    assert row.quote is None and row.quote_author is None and row.quote_source is None
    assert row.quote_resolved is True

    clock.advance(hours=3)
    await _run(session_factory, stub)
    assert len(stub.calls_of("quote")) == 1  # never asked again


@pytest.mark.asyncio
async def test_quote_failure_is_retried_and_the_contemplation_kept(session_factory, seed, clock):
    stub = StubLLM(quote=[LLMProviderError("timeout")], contemplation=[CONTEMPLATION_OK])
    await _run(session_factory, stub)
    row = await seed.get_content(DAY)
    assert row.quote_resolved is False
    assert row.contemplation == CONTEMPLATION_OK

    clock.advance(minutes=5)
    stub.scripts["quote"] = [QUOTE_OK]
    await _run(session_factory, stub)
    row = await seed.get_content(DAY)
    assert row.quote_resolved is True and row.quote == QUOTE_OK["quote"]
    assert len(stub.calls_of("contemplation")) == 1  # not regenerated


@pytest.mark.asyncio
async def test_unlisted_author_settles_on_no_quote_after_one_retry(session_factory, seed, clock):
    stub = StubLLM(
        quote=[
            {"quote": "What you seek is seeking you, in every quiet moment.", "author": "Rumi", "work": "Masnavi"},
            {"quote": "Life is like riding a bicycle, you must keep moving.", "author": "Albert Einstein", "work": "A letter"},
            QUOTE_OK,  # never reached
        ],
        contemplation=[CONTEMPLATION_OK],
    )
    await _run(session_factory, stub)

    quote_calls = stub.calls_of("quote")
    assert len(quote_calls) == 2
    assert "The author must be one of the names on the list." in quote_calls[1].user_prompt
    assert "return null for every field" in quote_calls[1].user_prompt
    row = await seed.get_content(DAY)
    assert row.quote is None and row.quote_author is None
    assert row.quote_resolved is True


@pytest.mark.asyncio
async def test_invalid_contemplation_is_retried_with_the_errors(session_factory, seed, clock):
    stub = StubLLM(quote=[QUOTE_NONE], contemplation=[BAD_CONTEMPLATION, CONTEMPLATION_OK])
    await _run(session_factory, stub)

    retry = stub.calls_of("contemplation")[1].user_prompt
    assert BAD_CONTEMPLATION in retry
    assert "It uses a dash." in retry
    assert "The last sentence must not use the word fear." in retry
    assert (await seed.get_content(DAY)).contemplation == CONTEMPLATION_OK


@pytest.mark.asyncio
async def test_contemplation_that_never_passes_is_not_saved(session_factory, seed, clock):
    stub = StubLLM(quote=[QUOTE_NONE], contemplation=[BAD_CONTEMPLATION, BAD_CONTEMPLATION])
    await _run(session_factory, stub)
    row = await seed.get_content(DAY)
    assert row.contemplation is None
    assert row.attempts == 1


@pytest.mark.asyncio
async def test_allow_generate_false_never_calls_the_llm(session_factory, seed, clock):
    stub = StubLLM(quote=[QUOTE_OK], contemplation=[CONTEMPLATION_OK])
    assert await _run(session_factory, stub, allow_generate=False) is None
    assert await seed.get_content(DAY) is None

    await seed.content(DAY, contemplation=None, attempts=0)
    row = await _run(session_factory, stub, allow_generate=False)
    assert row is not None and row.contemplation is None
    assert stub.calls == []


@pytest.mark.asyncio
async def test_quote_stored_by_an_older_revision_is_kept(session_factory, seed, clock):
    # Written before quote_resolved existed (or by the old revision during a deploy).
    await seed.content(DAY, quote="Older quote text stays put.", quote_author="Seneca", contemplation=None)
    stub = StubLLM(contemplation=[CONTEMPLATION_OK])
    await _run(session_factory, stub)
    row = await seed.get_content(DAY)
    assert row.quote == "Older quote text stays put."
    assert row.contemplation == CONTEMPLATION_OK
    assert stub.calls_of("quote") == []


@pytest.mark.asyncio
async def test_naive_datetimes_from_sqlite_are_compared_safely(session_factory, seed, clock):
    await seed.content(DAY, attempts=1, last_attempt_at=dt.datetime(2026, 10, 15, 4, 0))
    stored = await seed.get_content(DAY)
    assert stored.last_attempt_at.tzinfo is None  # what SQLite hands back

    stub = StubLLM(quote=[QUOTE_OK], contemplation=[CONTEMPLATION_OK])
    await _run(session_factory, stub)  # 05:00 UTC vs a naive 04:00: due, and no TypeError
    assert (await seed.get_content(DAY)).contemplation == CONTEMPLATION_OK


@pytest.mark.asyncio
async def test_concurrent_insert_returns_the_stored_row(session_factory, seed, clock):
    stub = StubLLM(contemplation=[CONTEMPLATION_OK])

    async def racing_quote(higher, lower):
        # Another instance saves the day while this one is still generating.
        await seed.content(DAY, quote=None, quote_resolved=True, contemplation="Theirs.", attempts=1)
        return QuoteResult("none")

    async with session_factory() as session:
        service = DailyContentService(session, stub)
        service._generate_quote = racing_quote
        row = await service.get_or_create(DAY, "courage", "fear")
    assert row.contemplation == "Theirs."


# --- backoff ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backoff_schedule_cap_and_force(session_factory, seed, clock):
    stub = StubLLM()  # always fails: one quote call and one contemplation call per attempt

    async def attempts_after(**kwargs) -> int:
        await _run(session_factory, stub, **kwargs)
        return (await seed.get_content(DAY)).attempts

    assert await attempts_after() == 1
    for attempt, wait in enumerate(BACKOFF_MINUTES[: MAX_ATTEMPTS - 1], start=1):
        clock.advance(minutes=wait - 1)
        assert await attempts_after() == attempt  # not yet
        clock.advance(minutes=1)
        assert await attempts_after() == attempt + 1
    assert len(stub.calls) == 2 * MAX_ATTEMPTS

    clock.advance(days=1)
    assert await attempts_after() == MAX_ATTEMPTS  # capped
    assert len(stub.calls) == 2 * MAX_ATTEMPTS

    assert await attempts_after(force=True) == 1  # force resets the count and runs now
    assert len(stub.calls) == 2 * MAX_ATTEMPTS + 2


def test_backoff_helpers():
    assert [backoff_delay(n).total_seconds() / 60 for n in range(1, 8)] == [5, 10, 20, 40, 80, 160, 160]
    now = START
    assert retry_due(0, None, now)
    assert not retry_due(1, now - dt.timedelta(minutes=4), now)
    assert retry_due(1, now - dt.timedelta(minutes=5), now)
    assert retry_due(1, (now - dt.timedelta(minutes=5)).replace(tzinfo=None), now)  # naive, as from SQLite
    assert not retry_due(MAX_ATTEMPTS, now - dt.timedelta(days=30), now)


# --- validation -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data,expected_author",
    [
        (QUOTE_OK, "Seneca"),
        ({**QUOTE_OK, "author": "Seneca the Younger"}, "Seneca"),
        ({**QUOTE_OK, "author": "MONTAIGNE"}, "Michel de Montaigne"),
        ({**QUOTE_OK, "author": "Soren Kierkegaard"}, "Søren Kierkegaard"),
        ({**QUOTE_OK, "author": "  Marcus   Aurelius "}, "Marcus Aurelius"),
        ({**QUOTE_OK, "quote": "“We suffer more often in imagination than in reality.”"}, "Seneca"),
    ],
)
def test_valid_quotes(data, expected_author):
    result, errors = validate_quote(data)
    assert errors == []
    assert result.status == "ok"
    assert result.author == expected_author
    assert result.text == "We suffer more often in imagination than in reality."
    assert result.work == "Letters to Lucilius"


@pytest.mark.parametrize(
    "data,fragment",
    [
        ({**QUOTE_OK, "quote": " ".join(["word"] * 31)}, "4 to 30 words"),
        ({**QUOTE_OK, "quote": "Too short here."}, "4 to 30 words"),
        ({**QUOTE_OK, "quote": "We suffer more — often in imagination than in reality."}, "dash"),
        ({**QUOTE_OK, "quote": 'We suffer more often in "imagination" than in reality.'}, "quotation marks"),
        ({**QUOTE_OK, "work": None}, "specific work"),
        ({**QUOTE_OK, "work": "x" * 81}, "80 characters"),
        ({**QUOTE_OK, "author": "Mark Twain"}, "names on the list"),
        ({**QUOTE_OK, "author": None}, "names on the list"),
        ({**QUOTE_OK, "quote": None}, "text is missing"),
    ],
)
def test_invalid_quotes(data, fragment):
    result, errors = validate_quote(data)
    assert result.status == "failed"
    assert any(fragment in error for error in errors), errors


@pytest.mark.parametrize("data", [QUOTE_NONE, {"quote": "", "author": " ", "work": None}, {}])
def test_empty_quote_means_none(data):
    result, errors = validate_quote(data)
    assert errors == [] and result.status == "none"


def test_quote_allowlist_leaves_out_misattributed_names():
    for name in EXCLUDED_QUOTE_AUTHORS + ("Lao-Tzu", "Gautama Buddha", "Einstein", "Lincoln", "Twain"):
        assert canonical_author(name) is None, name
    assert len(QUOTE_AUTHORS) == 18


@pytest.mark.parametrize(
    "text,fragment",
    [
        ("Where does fear show up? Choose one small act of courage today, and let that be enough for now.", "question"),
        ("Fear hides in small pauses during your day at work and at home, so choose one small act of "
         "courage today and let it be enough for you.", "two or three sentences"),
        ("Fear hides in the small pauses before you speak. It shapes more than you think. You can choose "
         "courage today in one small way, even with fear present.", "must not use the word fear"),
        ("Fear hides in the small pauses before you speak up at work. Notice it, and choose one small "
         "act that feels honest to you today.", "must use the word courage"),
        ("Fear hides in the small pauses before you speak up at work. Notice it gently. Your journey "
         "toward courage starts with one small act today.", "journey"),
        (" ".join(["Fear hides in small pauses."] * 12) + " Choose courage today.", "30 to 60 words"),
        ("Fear hides in the small pauses before you speak up at work. Then choose one small act of "
         "courage today", "period"),
    ],
)
def test_contemplation_rules(text, fragment):
    errors = contemplation_errors(text, "courage", "fear")
    assert any(fragment in error for error in errors), errors


def test_good_contemplation_passes():
    assert contemplation_errors(CONTEMPLATION_OK, "courage", "fear") == []


def test_fallback_contemplation_ends_facing_the_higher_word():
    text = fallback_contemplation("courage", "fear")
    assert contemplation_errors(text, "courage", "fear") == []  # same rules as generated text
    last = sentences(text)[-1]
    assert has_word(last, "courage") and not has_word(last, "fear")
    assert word_count(text) <= 60

    meaning = {"disguise": "Fear often passes for being careful.", "practice": "Make the call you keep putting off."}
    assert fallback_contemplation("courage", "fear", meaning) == (
        "Fear often passes for being careful. Make the call you keep putting off."
    )
