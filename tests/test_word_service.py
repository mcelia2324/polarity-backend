import datetime as dt

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import Base
from app.services.llm.base import LLMProvider, LLMRequest
from app.services.word_service import FALLBACK_HIGHER, FALLBACK_LOWER, format_pair_display, parse_two_words, WordService


class StubLLM(LLMProvider):
    name = "stub"

    def __init__(self, outputs: list[str]):
        self._outputs = iter(outputs)

    async def generate(self, request: LLMRequest) -> str:
        return next(self._outputs)


async def _setup_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    return engine, session_factory


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Light, Dark", ("light", "dark")),
        ("Hope vs Fear", ("hope", "fear")),
        ("Love vs. Fear", ("love", "fear")),
        ("courage bravery", ("courage", "bravery")),
        ("light-dark", ("light", "dark")),
    ],
)
def test_parse_two_words_valid(raw, expected):
    assert parse_two_words(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "Same Same",
        "JustOne",
        "123 456",
        "Love, Love",
    ],
)
def test_parse_two_words_invalid(raw):
    assert parse_two_words(raw) is None


def test_format_pair_display():
    assert format_pair_display("light", "dark") == "Light vs Dark"


@pytest.mark.asyncio
async def test_ensure_pair_unique_across_days():
    engine, session_factory = await _setup_db()

    async with session_factory() as session:
        llm = StubLLM(["hope, fear"])
        service = WordService(session, llm)
        pair = await service.ensure_pair_for_date(dt.date(2026, 2, 4))
        assert {pair.word_a, pair.word_b} == {"hope", "fear"}

    # Day 2: the model first repeats the already-used pair, then offers a fresh one.
    async with session_factory() as session:
        llm = StubLLM(["hope, fear", "humility, pride"])
        service = WordService(session, llm)
        pair = await service.ensure_pair_for_date(dt.date(2026, 2, 5))
        assert {pair.word_a, pair.word_b} == {"humility", "pride"}

    await engine.dispose()


@pytest.mark.asyncio
async def test_keeps_going_past_fixated_used_word():
    """Regression for the 'integrity' fixation: the model repeats one already-used word
    several times before offering a novel pair; the loop must keep going and succeed."""
    engine, session_factory = await _setup_db()

    async with session_factory() as session:
        llm = StubLLM(["integrity, apathy"])
        service = WordService(session, llm)
        await service.ensure_pair_for_date(dt.date(2026, 2, 4))

    async with session_factory() as session:
        # "integrity" is taken; the model keeps proposing it, then finally varies.
        llm = StubLLM([
            "integrity, malice",
            "integrity, sloth",
            "integrity, vanity",
            "courage, despair",
        ])
        service = WordService(session, llm)
        pair = await service.ensure_pair_for_date(dt.date(2026, 2, 5))
        assert {pair.word_a, pair.word_b} == {"courage", "despair"}

    await engine.dispose()


@pytest.mark.asyncio
async def test_falls_back_to_curated_pair_when_llm_exhausted():
    """If the model never produces a novel pair, a curated unused pair is used instead of 500-ing."""
    engine, session_factory = await _setup_db()

    async with session_factory() as session:
        llm = StubLLM(["hope, fear"])
        service = WordService(session, llm)
        await service.ensure_pair_for_date(dt.date(2026, 2, 4))

    async with session_factory() as session:
        # The model only ever returns the already-used pair.
        llm = StubLLM(["hope, fear", "hope, fear", "hope, fear"])
        service = WordService(session, llm)
        pair = await service.ensure_pair_for_date(dt.date(2026, 2, 5), max_attempts=3)
        assert pair.word_a in FALLBACK_HIGHER and pair.word_b in FALLBACK_LOWER

    await engine.dispose()


@pytest.mark.asyncio
async def test_rejects_concatenated_non_words():
    """Regression: glued non-words like 'innerstillness' are rejected; the next clean pair wins."""
    engine, session_factory = await _setup_db()

    async with session_factory() as session:
        llm = StubLLM(["innerstillness, selfrighteousness", "humility, arrogance"])
        service = WordService(session, llm)
        pair = await service.ensure_pair_for_date(dt.date(2026, 2, 4))
        assert {pair.word_a, pair.word_b} == {"humility", "arrogance"}

    await engine.dispose()


@pytest.mark.asyncio
async def test_word_reusable_after_window():
    """A word used long ago (beyond WORD_REUSE_AFTER_DAYS) becomes available again."""
    engine, session_factory = await _setup_db()

    async with session_factory() as session:
        llm = StubLLM(["hope, despair"])
        service = WordService(session, llm)
        await service.ensure_pair_for_date(dt.date(2025, 1, 1))

    # ~535 days later, "hope" may be paired again.
    async with session_factory() as session:
        llm = StubLLM(["hope, fear"])
        service = WordService(session, llm)
        pair = await service.ensure_pair_for_date(dt.date(2026, 6, 19))
        assert {pair.word_a, pair.word_b} == {"hope", "fear"}

    await engine.dispose()


@pytest.mark.asyncio
async def test_word_not_reused_within_window():
    """A word used recently (within the window) is NOT reused even days later."""
    engine, session_factory = await _setup_db()

    async with session_factory() as session:
        llm = StubLLM(["hope, despair"])
        service = WordService(session, llm)
        await service.ensure_pair_for_date(dt.date(2026, 6, 1))

    async with session_factory() as session:
        # "hope" was used 18 days ago (< 180): the repeat is rejected, the fresh pair wins.
        llm = StubLLM(["hope, fear", "courage, doubt"])
        service = WordService(session, llm)
        pair = await service.ensure_pair_for_date(dt.date(2026, 6, 19))
        assert {pair.word_a, pair.word_b} == {"courage", "doubt"}

    await engine.dispose()
