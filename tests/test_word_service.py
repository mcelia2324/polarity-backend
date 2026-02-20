import datetime as dt

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import Base
from app.services.llm.base import LLMProvider, LLMRequest
from app.services.word_service import format_pair_display, parse_two_words, WordService


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
        llm = StubLLM(["light, dark"])
        service = WordService(session, llm)
        pair = await service.ensure_pair_for_date(dt.date(2026, 2, 4))
        assert {pair.word_a, pair.word_b} == {"light", "dark"}

    async with session_factory() as session:
        llm = StubLLM(["light, dark", "hope, fear"])
        service = WordService(session, llm)
        pair = await service.ensure_pair_for_date(dt.date(2026, 2, 5))
        assert {pair.word_a, pair.word_b} == {"hope", "fear"}

    await engine.dispose()
