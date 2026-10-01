import datetime as dt
import json

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.main as main
from app.db import Base
from app.models import Delivery, DeviceToken, WordPair
from app.services.llm.base import LLMProvider, LLMRequest
from app.services.push.apns import APNSClient, APNSConfig, is_invalid_token_response

# Canned APNs replies: (HTTP status, JSON body or None for an empty 200).
OK = (200, None)
UNREGISTERED = (410, {"reason": "Unregistered", "timestamp": 1790000000000})
BAD_DEVICE_TOKEN = (400, {"reason": "BadDeviceToken"})
UNAVAILABLE = (503, {"reason": "ServiceUnavailable"})


async def _setup_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    return engine, session_factory


async def _seed_tokens(session_factory, tokens: list[str], disabled: list[str] = ()):
    async with session_factory() as session:
        for token in tokens:
            session.add(DeviceToken(token=token, enabled=True))
        for token in disabled:
            session.add(DeviceToken(token=token, enabled=False))
        await session.commit()


async def _tokens_by_name(session_factory) -> dict[str, DeviceToken]:
    async with session_factory() as session:
        rows = (await session.execute(select(DeviceToken))).scalars().all()
        return {row.token: row for row in rows}


def _client(replies: dict[str, tuple[int, dict | None]], calls: list[str]) -> APNSClient:
    """An APNs client wired to a mock transport: `replies` maps device token -> canned reply,
    and every token actually pushed to is appended to `calls`."""

    def handler(request: httpx.Request) -> httpx.Response:
        token = request.url.path.rsplit("/", 1)[-1]
        calls.append(token)
        status, body = replies[token]
        return httpx.Response(status) if body is None else httpx.Response(status, json=body)

    auth_key = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    config = APNSConfig(
        key_id="KEYID00000",
        team_id="TEAMID0000",
        bundle_id="mcelia.PolarityApp",
        auth_key=auth_key,
        use_sandbox=False,
    )
    return APNSClient(config, transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (410, '{"reason":"Unregistered","timestamp":1790000000000}', True),
        (410, '{"reason":"ExpiredToken"}', True),
        (410, "", True),
        (400, '{"reason":"BadDeviceToken"}', True),
        (400, '{"reason":"BadTopic"}', False),
        (403, '{"reason":"InvalidProviderToken"}', False),
        (429, '{"reason":"TooManyRequests"}', False),
        (503, "not json", False),
        (500, "[]", False),
    ],
)
def test_is_invalid_token_response(status, body, expected):
    assert is_invalid_token_response(status, body) is expected


@pytest.mark.asyncio
async def test_send_daily_disables_dead_tokens_and_keeps_rows():
    engine, session_factory = await _setup_db()
    await _seed_tokens(session_factory, ["ok1", "ok2", "gone", "bad", "flaky"], disabled=["off"])
    replies = {"ok1": OK, "ok2": OK, "gone": UNREGISTERED, "bad": BAD_DEVICE_TOKEN, "flaky": UNAVAILABLE}
    calls: list[str] = []

    async with session_factory() as session:
        result = await _client(replies, calls).send_daily(
            WordPair(word_a="hope", word_b="fear"), dt.date(2026, 10, 2), "msg", session
        )

    # Dead tokens count as disabled, not failed; only the transient 503 is a failure.
    assert result == (2, 1, 2)
    assert "off" not in calls

    tokens = await _tokens_by_name(session_factory)
    assert len(tokens) == 6  # nothing deleted
    assert tokens["gone"].enabled is False
    assert json.loads(tokens["gone"].last_error)["reason"] == "Unregistered"
    assert tokens["bad"].enabled is False
    assert json.loads(tokens["bad"].last_error)["reason"] == "BadDeviceToken"
    # A transient error is recorded but the token stays active for tomorrow.
    assert tokens["flaky"].enabled is True
    assert json.loads(tokens["flaky"].last_error)["reason"] == "ServiceUnavailable"
    assert tokens["ok1"].enabled is True and tokens["ok1"].last_notified_at is not None

    await engine.dispose()


@pytest.mark.asyncio
async def test_disabled_tokens_are_skipped_on_next_send():
    engine, session_factory = await _setup_db()
    await _seed_tokens(session_factory, ["ok", "gone", "bad"])
    replies = {"ok": OK, "gone": UNREGISTERED, "bad": BAD_DEVICE_TOKEN}
    pair = WordPair(word_a="hope", word_b="fear")

    first_calls: list[str] = []
    async with session_factory() as session:
        assert await _client(replies, first_calls).send_daily(pair, dt.date(2026, 10, 2), "msg", session) == (1, 0, 2)
    assert sorted(first_calls) == ["bad", "gone", "ok"]

    second_calls: list[str] = []
    async with session_factory() as session:
        assert await _client(replies, second_calls).send_daily(pair, dt.date(2026, 10, 3), "msg", session) == (1, 0, 0)
    assert second_calls == ["ok"]

    await engine.dispose()


class _StubLLM(LLMProvider):
    name = "stub"

    async def generate(self, request: LLMRequest) -> str:
        return "hope, fear"


class _OfflineExtras:
    """Stands in for DefinitionService / DailyContentService so the cron run stays offline."""

    def __init__(self, *args):
        pass

    async def get_definition(self, word):
        return ""

    async def get_or_create(self, *args):
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "replies,expected_status,expected_error,expected_counts",
    [
        # Dead tokens alone must not make the day "partial".
        ({"ok": OK, "gone": UNREGISTERED, "bad": BAD_DEVICE_TOKEN}, "sent", None, (1, 0, 2)),
        # A real failure on an active token still does.
        ({"ok": OK, "gone": UNREGISTERED, "flaky": UNAVAILABLE}, "partial", "1 failed", (1, 1, 1)),
    ],
)
async def test_daily_delivery_status_counts_only_active_tokens(
    monkeypatch, replies, expected_status, expected_error, expected_counts
):
    engine, session_factory = await _setup_db()
    await _seed_tokens(session_factory, list(replies))
    client = _client(replies, [])

    async def _provider(store):
        return _StubLLM()

    async def _from_settings(store):
        return client

    monkeypatch.setattr(main, "SessionLocal", session_factory)
    monkeypatch.setattr(main, "build_provider", _provider)
    monkeypatch.setattr(main, "DefinitionService", _OfflineExtras)
    monkeypatch.setattr(main, "DailyContentService", _OfflineExtras)
    monkeypatch.setattr(APNSClient, "from_settings", _from_settings)

    result = await main._run_daily()

    assert result["push"] == expected_status
    assert (result["sent"], result["failed"], result["disabled"]) == expected_counts
    async with session_factory() as session:
        delivery = (await session.execute(select(Delivery))).scalar_one()
        assert delivery.status == expected_status
        assert delivery.error == expected_error
        assert await session.scalar(select(func.count()).select_from(DeviceToken)) == len(replies)

    await engine.dispose()
