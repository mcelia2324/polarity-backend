"""Shared test setup: in-memory SQLite, a scripted stub LLM, and an HTTP client for the app.

The client goes through httpx.ASGITransport, which does not run the app's startup event, so the
real database is never touched, and `build_provider` is replaced so nothing calls OpenAI.
"""
from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.main as main
from app.db import Base
from app.models import DailyContent, Setting, WordDefinition, WordPair
from app.services import content as content_module
from app.services import definition_service
from app.services.llm.base import LLMProvider, LLMProviderError, LLMRequest

# The date the API treats as today in client tests.
TODAY = dt.date(2026, 10, 15)

# Replies that pass every check for the pair courage (higher) / fear (lower).
CONTEMPLATION_OK = (
    "Fear often shows up as a quiet no, like the call you put off or the hand you keep down in a "
    "meeting. You do not have to make it leave. Choose one small act of courage today, and let it "
    "be enough."
)
QUOTE_OK = {
    "quote": "We suffer more often in imagination than in reality.",
    "author": "Seneca",
    "work": "Letters to Lucilius",
}
QUOTE_NONE = {"quote": None, "author": None, "work": None}
DEFINITIONS_OK = {
    "courage": (
        "Acting on what matters while part of you is still afraid. In a normal day it looks like "
        "asking the hard question, owning a mistake, or speaking up when silence would be easier."
    ),
    "fear": (
        "The alarm you feel when something seems to threaten you, real or imagined. In a normal day "
        "it shows up as putting off a call, checking things twice, or saying yes when you mean no."
    ),
}


class StubLLM(LLMProvider):
    """Scripted replies, routed by which prompt is asking: "pair", "definition", "quote",
    "contemplation" (anything else is "other"). Each script is a list used in order: a dict is
    returned as JSON, an exception is raised. A call with nothing scripted raises
    LLMProviderError, the same as an outage. Every call is recorded in `calls`."""

    name = "stub"

    def __init__(self, **scripts):
        self.scripts: dict[str, list] = {kind: list(replies) for kind, replies in scripts.items()}
        self.calls: list[tuple[str, LLMRequest]] = []

    @staticmethod
    def kind_of(request: LLMRequest) -> str:
        prompt = request.system_prompt
        if "daily contemplation" in prompt:
            return "contemplation"
        if "real quotation" in prompt:
            return "quote"
        if "explain one word" in prompt:
            return "definition"
        if "contrasting single English words" in prompt:
            return "pair"
        return "other"

    def calls_of(self, kind: str) -> list[LLMRequest]:
        return [request for k, request in self.calls if k == kind]

    async def generate(self, request: LLMRequest) -> str:
        kind = self.kind_of(request)
        self.calls.append((kind, request))
        replies = self.scripts.get(kind)
        if not replies:
            raise LLMProviderError(f"stub: no scripted {kind} reply")
        reply = replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, (dict, list)):
            return json.dumps(reply)
        return reply


class Seeder:
    """Writes rows straight into the test database."""

    def __init__(self, factory: async_sessionmaker):
        self.factory = factory

    async def add(self, *rows) -> None:
        async with self.factory() as session:
            session.add_all(rows)
            await session.commit()

    async def pair(self, date: dt.date, word_a: str, word_b: str) -> None:
        await self.add(WordPair(date=date, word_a=word_a, word_b=word_b))

    async def definition(self, word: str, text: str, source: str = "openai-v3") -> None:
        await self.add(WordDefinition(word=word, definition=text, source=source))

    async def definitions_ok(self) -> None:
        for word, text in DEFINITIONS_OK.items():
            await self.definition(word, text)

    async def content(self, date: dt.date, **fields) -> None:
        await self.add(DailyContent(date=date, **fields))

    async def setting(self, key: str, value: str) -> None:
        await self.add(Setting(key=key, value=value))

    async def get_content(self, date: dt.date) -> DailyContent | None:
        async with self.factory() as session:
            return (await session.execute(select(DailyContent).where(DailyContent.date == date))).scalar_one_or_none()

    async def get_definition(self, word: str) -> WordDefinition | None:
        async with self.factory() as session:
            return await session.get(WordDefinition, word)

    async def get_setting(self, key: str) -> str | None:
        async with self.factory() as session:
            row = await session.get(Setting, key)
            return None if row is None else row.value


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    await engine.dispose()


@pytest.fixture
def seed(session_factory) -> Seeder:
    return Seeder(session_factory)


@pytest.fixture
def stub() -> StubLLM:
    return StubLLM()


@pytest.fixture(autouse=True)
def _isolate_process_state(monkeypatch):
    """Reset process-wide state between tests, and keep the dictionary fallback off the network."""

    async def _no_dictionary(self, word):
        return None

    monkeypatch.setattr(definition_service.DefinitionService, "_fetch_dictionary_definition", _no_dictionary)
    monkeypatch.setattr(main, "_rate_limiter", main._RateLimitStore())
    definition_service._recent_failures.clear()
    content_module._orientation_sources.cache_clear()
    main._word_of_day_cache.clear()
    yield
    definition_service._recent_failures.clear()
    main._word_of_day_cache.clear()


@pytest.fixture
def known_pairs(monkeypatch):
    """Replace the orientation sources (pair bank, overrides) with in-memory ones:
    known_pairs(bank=[KnownPair(...)], overrides=[KnownPair(...)])."""

    def _set(bank=(), overrides=()):
        sources = (
            {content_module.pair_key(p.higher, p.lower): p for p in bank},
            {content_module.pair_key(p.higher, p.lower): p for p in overrides},
        )
        monkeypatch.setattr(content_module, "_orientation_sources", lambda: sources)

    return _set


@pytest_asyncio.fixture
async def client(monkeypatch, session_factory, stub):
    """An HTTP client for the app, wired to the test database and the stub LLM, with today
    fixed to TODAY and the cron secret set to "test-secret"."""

    async def _provider(store, transport=None):
        return stub

    monkeypatch.setattr(main, "SessionLocal", session_factory)
    monkeypatch.setattr(main, "build_provider", _provider)
    monkeypatch.setattr(main, "_today", lambda timezone: TODAY)
    monkeypatch.setattr(main.env_settings, "cron_secret", "test-secret")
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
