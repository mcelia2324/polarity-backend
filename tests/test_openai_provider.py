import json

import httpx
import pytest

from app.services.llm.base import LLMProviderError, LLMRequest
from app.services.llm.openai_provider import OpenAIProvider

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["quote"],
    "properties": {"quote": {"type": ["string", "null"]}},
}


def _message(*content: dict) -> dict:
    return {"output": [{"type": "reasoning", "summary": []}, {"type": "message", "content": list(content)}]}


def _text_reply(text: str) -> dict:
    return _message({"type": "output_text", "text": text})


class Recorder:
    """A mock OpenAI endpoint: records each request and answers with a canned reply."""

    def __init__(self, reply: dict | None = None, status: int = 200, error: Exception | None = None):
        self.reply = reply if reply is not None else _text_reply("ok")
        self.status = status
        self.error = error
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return httpx.Response(self.status, json=self.reply)

    @property
    def payload(self) -> dict:
        return json.loads(self.requests[-1].content)


def _provider(recorder: Recorder, model: str = "gpt-5.2") -> OpenAIProvider:
    return OpenAIProvider(api_key="test-key", model=model, transport=httpx.MockTransport(recorder))


@pytest.mark.asyncio
async def test_schema_request_sends_strict_text_format_and_verbosity():
    recorder = Recorder(_text_reply('{"quote": null}'))
    request = LLMRequest(
        system_prompt="sys",
        user_prompt="user",
        max_tokens=2000,
        json_schema=SCHEMA,
        schema_name="quote",
        reasoning_effort="low",
        verbosity="medium",
    )
    assert await _provider(recorder).generate_json(request) == {"quote": None}

    sent = recorder.requests[-1]
    assert sent.url == httpx.URL("https://api.openai.com/v1/responses")
    assert sent.headers["Authorization"] == "Bearer test-key"
    payload = recorder.payload
    assert payload["text"] == {
        "verbosity": "medium",
        "format": {"type": "json_schema", "name": "quote", "schema": SCHEMA, "strict": True},
    }
    assert payload["reasoning"] == {"effort": "low"}
    assert payload["max_output_tokens"] == 2000
    assert payload["instructions"] == "sys" and payload["input"] == "user"


@pytest.mark.asyncio
async def test_plain_request_keeps_the_existing_payload():
    recorder = Recorder(_text_reply("hope, fear"))
    reply = await _provider(recorder).generate(LLMRequest(system_prompt="s", user_prompt="u", max_tokens=16))
    assert reply == "hope, fear"
    payload = recorder.payload
    assert payload["text"] == {"verbosity": "low"}
    assert payload["reasoning"] == {"effort": "none"}  # gpt-5.2 default
    assert payload["max_output_tokens"] == 96
    assert "temperature" not in payload


@pytest.mark.asyncio
async def test_default_reasoning_effort_for_older_gpt5_models():
    recorder = Recorder()
    await _provider(recorder, model="gpt-5-mini").generate(LLMRequest(system_prompt="s", user_prompt="u"))
    assert recorder.payload["reasoning"] == {"effort": "minimal"}


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout_s,expected", [(60, 60.0), (None, 20.0)])
async def test_timeout_is_set_per_request(timeout_s, expected):
    recorder = Recorder()
    kwargs = {} if timeout_s is None else {"timeout_s": timeout_s}
    await _provider(recorder).generate(LLMRequest(system_prompt="s", user_prompt="u", **kwargs))
    assert recorder.requests[-1].extensions["timeout"] == {
        "connect": expected, "read": expected, "write": expected, "pool": expected,
    }


@pytest.mark.asyncio
async def test_transport_timeout_raises_provider_error():
    recorder = Recorder(error=httpx.ReadTimeout("slow"))
    with pytest.raises(LLMProviderError):
        await _provider(recorder).generate(LLMRequest(system_prompt="s", user_prompt="u"))


@pytest.mark.asyncio
async def test_refusal_raises():
    recorder = Recorder(_message({"type": "refusal", "refusal": "I can't help with that."}))
    with pytest.raises(LLMProviderError, match="refused"):
        await _provider(recorder).generate(LLMRequest(system_prompt="s", user_prompt="u"))
    with pytest.raises(LLMProviderError, match="refused"):
        await _provider(recorder).generate_json(LLMRequest(system_prompt="s", user_prompt="u", json_schema=SCHEMA))


@pytest.mark.asyncio
async def test_generate_json_reads_output_text_shortcut():
    recorder = Recorder({"output_text": '{"quote": "Know thyself."}'})
    data = await _provider(recorder).generate_json(LLMRequest(system_prompt="s", user_prompt="u", json_schema=SCHEMA))
    assert data == {"quote": "Know thyself."}


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["not json", "[1, 2]", ""])
async def test_generate_json_rejects_anything_but_an_object(text):
    recorder = Recorder({"output_text": text} if text else _text_reply(text))
    with pytest.raises(LLMProviderError):
        await _provider(recorder).generate_json(LLMRequest(system_prompt="s", user_prompt="u", json_schema=SCHEMA))


@pytest.mark.asyncio
async def test_incomplete_structured_reply_raises():
    reply = _text_reply('{"quote": "cut o')
    reply.update({"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}})
    with pytest.raises(LLMProviderError, match="max_output_tokens"):
        await _provider(Recorder(reply)).generate_json(
            LLMRequest(system_prompt="s", user_prompt="u", json_schema=SCHEMA)
        )


@pytest.mark.asyncio
async def test_http_error_raises():
    recorder = Recorder({"error": {"message": "server error"}}, status=500)
    with pytest.raises(LLMProviderError, match="500"):
        await _provider(recorder).generate(LLMRequest(system_prompt="s", user_prompt="u"))


@pytest.mark.asyncio
async def test_missing_api_key_raises_without_a_request():
    recorder = Recorder()
    provider = OpenAIProvider(api_key="", model="gpt-5.2", transport=httpx.MockTransport(recorder))
    with pytest.raises(LLMProviderError):
        await provider.generate(LLMRequest(system_prompt="s", user_prompt="u"))
    assert recorder.requests == []


@pytest.mark.asyncio
async def test_chat_models_get_response_format_and_refusals():
    recorder = Recorder({"choices": [{"message": {"content": '{"quote": null}', "refusal": None}}]})
    provider = _provider(recorder, model="gpt-4o")
    data = await provider.generate_json(
        LLMRequest(system_prompt="s", user_prompt="u", json_schema=SCHEMA, schema_name="quote", timeout_s=7)
    )
    assert data == {"quote": None}
    assert recorder.requests[-1].url == httpx.URL("https://api.openai.com/v1/chat/completions")
    assert recorder.payload["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "quote", "schema": SCHEMA, "strict": True},
    }
    assert recorder.requests[-1].extensions["timeout"]["read"] == 7.0

    refusing = Recorder({"choices": [{"message": {"content": None, "refusal": "No."}}]})
    with pytest.raises(LLMProviderError, match="refused"):
        await _provider(refusing, model="gpt-4o").generate(LLMRequest(system_prompt="s", user_prompt="u"))
