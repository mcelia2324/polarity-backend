import pytest

from app.services import definition_service
from app.services.definition_service import (
    DEFINITION_SOURCE,
    DEFINITION_UNAVAILABLE,
    DefinitionService,
    definition_errors,
)
from conftest import DEFINITIONS_OK, StubLLM

COURAGE = DEFINITIONS_OK["courage"]
OLD_TEXT = "Old courage text from the first prompt, which runs on and on and on... and ends with dots."


async def _get(session_factory, stub, word="courage", **kwargs) -> str:
    async with session_factory() as session:
        return await DefinitionService(session, stub).get_definition(word, **kwargs)


@pytest.mark.asyncio
async def test_new_definition_is_saved_with_the_v3_source(session_factory, seed):
    stub = StubLLM(definition=[COURAGE])
    assert await _get(session_factory, stub, word="  Courage ") == COURAGE
    row = await seed.get_definition("courage")
    assert row.definition == COURAGE
    assert row.source == DEFINITION_SOURCE == "openai-v3"
    assert stub.calls_of("definition")[0].user_prompt.startswith("Word: courage")


@pytest.mark.asyncio
async def test_v3_rows_are_served_without_the_llm(session_factory, seed):
    await seed.definition("courage", COURAGE, source="openai-v3")
    stub = StubLLM()
    assert await _get(session_factory, stub) == COURAGE
    assert stub.calls == []


@pytest.mark.asyncio
async def test_unavailable_fallback_is_returned_but_never_saved(session_factory, seed):
    stub = StubLLM()  # the LLM fails, and the dictionary fallback finds nothing (conftest)
    assert await _get(session_factory, stub) == DEFINITION_UNAVAILABLE
    assert await seed.get_definition("courage") is None


@pytest.mark.asyncio
async def test_dictionary_text_is_saved_as_stale(session_factory, seed, monkeypatch):
    async def dictionary(self, word):
        return "The ability to do something that frightens one."

    monkeypatch.setattr(DefinitionService, "_fetch_dictionary_definition", dictionary)
    assert await _get(session_factory, StubLLM()) == "The ability to do something that frightens one."
    row = await seed.get_definition("courage")
    assert row.source == "dictionaryapi"
    assert row.source in definition_service.STALE_SOURCES


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["openai", "fallback", "dictionaryapi"])
async def test_stale_rows_are_upgraded(session_factory, seed, source):
    await seed.definition("courage", OLD_TEXT if source != "fallback" else DEFINITION_UNAVAILABLE, source=source)
    stub = StubLLM(definition=[COURAGE])
    assert await _get(session_factory, stub) == COURAGE
    row = await seed.get_definition("courage")
    assert row.definition == COURAGE and row.source == "openai-v3"


@pytest.mark.asyncio
async def test_failed_upgrade_keeps_serving_the_stale_text(session_factory, seed):
    await seed.definition("courage", OLD_TEXT, source="openai")
    assert await _get(session_factory, StubLLM()) == OLD_TEXT
    row = await seed.get_definition("courage")
    assert row.definition == OLD_TEXT and row.source == "openai"


@pytest.mark.asyncio
async def test_failed_upgrade_of_a_placeholder_row_is_still_unavailable(session_factory, seed):
    await seed.definition("courage", DEFINITION_UNAVAILABLE, source="fallback")
    assert await _get(session_factory, StubLLM()) == DEFINITION_UNAVAILABLE


@pytest.mark.asyncio
async def test_no_generation_when_not_allowed(session_factory, seed):
    stub = StubLLM(definition=[COURAGE])
    assert await _get(session_factory, stub, allow_generate=False) == DEFINITION_UNAVAILABLE
    await seed.definition("courage", OLD_TEXT, source="openai")
    assert await _get(session_factory, stub, allow_generate=False) == OLD_TEXT
    assert stub.calls == []


@pytest.mark.asyncio
async def test_invalid_draft_is_retried_with_feedback(session_factory, seed):
    too_long = " ".join(["Courage is acting while afraid."] * 14)  # 70 words
    stub = StubLLM(definition=[too_long, COURAGE])
    assert await _get(session_factory, stub) == COURAGE
    retry = stub.calls_of("definition")[1].user_prompt
    assert "This draft has 70." in retry and too_long in retry


@pytest.mark.asyncio
async def test_long_definitions_are_not_truncated(session_factory, seed):
    long_text = " ".join(["understanding"] * 39) + " remains."
    assert len(long_text) > 500 and definition_errors(long_text, "courage") == []
    stub = StubLLM(definition=[long_text])
    assert await _get(session_factory, stub) == long_text
    stored = (await seed.get_definition("courage")).definition
    assert stored == long_text and not stored.endswith("...")


@pytest.mark.asyncio
async def test_failures_back_off_within_the_process(session_factory, seed, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(definition_service, "_clock", lambda: now[0])
    stub = StubLLM()
    assert await _get(session_factory, stub) == DEFINITION_UNAVAILABLE
    calls = len(stub.calls)
    assert calls == 1  # an outage is not retried within the attempt

    assert await _get(session_factory, stub) == DEFINITION_UNAVAILABLE
    assert len(stub.calls) == calls  # backing off

    now[0] += 5 * 60
    stub.scripts["definition"] = [COURAGE]
    assert await _get(session_factory, stub) == COURAGE


@pytest.mark.parametrize(
    "text,fragment",
    [
        ("Too short to count as a definition.", "20 to 45 words"),
        ("Acting on what matters while you are afraid — like asking the hard question at work or "
         "owning a mistake you made in front of others.", "dash"),
        ("Acting on what matters while you are afraid; like asking the hard question at work or owning "
         "a mistake you made in front of others.", "semicolon"),
        ("The deep resonance of acting on what matters while you are afraid, like asking the hard "
         "question at work or owning a mistake.", "resonance"),
    ],
)
def test_definition_rules(text, fragment):
    assert any(fragment in error for error in definition_errors(text, "courage"))


def test_definition_word_itself_is_exempt():
    text = (
        "A place or thing held as sacred, set apart from daily use. In a normal day it shows up as "
        "the quiet way people treat a family photo or a worn book."
    )
    assert definition_errors(text, "sacred") == []
    assert definition_errors(text, "courage") != []
