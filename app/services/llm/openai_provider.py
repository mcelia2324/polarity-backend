from __future__ import annotations

import httpx

from app.services.llm.base import LLMProvider, LLMProviderError, LLMRequest

CHAT_URL = "https://api.openai.com/v1/chat/completions"
RESPONSES_URL = "https://api.openai.com/v1/responses"


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, api_key: str, model: str, transport: httpx.AsyncBaseTransport | None = None):
        self._api_key = api_key
        self._model = model
        self._transport = transport  # None = real network; tests pass an httpx.MockTransport

    def _use_responses_api(self) -> bool:
        model = (self._model or "").lower()
        return model.startswith("gpt-5") or model.startswith("o")

    def _reasoning_effort(self, request: LLMRequest) -> str:
        if request.reasoning_effort:
            return request.reasoning_effort
        model = (self._model or "").lower()
        if model.startswith("gpt-5.2") or model.startswith("gpt-5.1"):
            return "none"
        return "minimal"

    async def generate(self, request: LLMRequest) -> str:
        if not self._api_key:
            raise LLMProviderError("OpenAI API key is not configured.")
        if self._use_responses_api():
            return await self._generate_responses(request)
        return await self._generate_chat(request)

    async def _post(self, url: str, payload: dict, timeout: float) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            async with httpx.AsyncClient(timeout=timeout, transport=self._transport) as client:
                return await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise LLMProviderError(f"OpenAI request failed: {type(exc).__name__}") from exc

    async def _generate_chat(self, request: LLMRequest) -> str:
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
            "max_completion_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        if request.json_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": request.schema_name, "schema": request.json_schema, "strict": True},
            }
        resp = await self._post(CHAT_URL, payload, request.timeout_s)
        if resp.status_code >= 400:
            if "temperature" in resp.text:
                payload.pop("temperature", None)
            if "max_completion_tokens" in resp.text:
                payload.pop("max_completion_tokens", None)
                payload["max_tokens"] = request.max_tokens
            resp = await self._post(CHAT_URL, payload, request.timeout_s)
        if resp.status_code >= 400:
            raise LLMProviderError(f"OpenAI API error: {resp.status_code} {resp.text}")
        data = resp.json()
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMProviderError("Unexpected OpenAI response format.") from exc
        if message.get("refusal"):
            raise LLMProviderError(f"Model refused: {message['refusal']}")
        content = message.get("content")
        if not isinstance(content, str):
            raise LLMProviderError("Unexpected OpenAI response format.")
        return content

    async def _generate_responses(self, request: LLMRequest) -> str:
        text: dict = {"verbosity": request.verbosity}
        if request.json_schema is not None:
            text["format"] = {
                "type": "json_schema",
                "name": request.schema_name,
                "schema": request.json_schema,
                "strict": True,
            }
        payload = {
            "model": self._model,
            "input": request.user_prompt,
            "instructions": request.system_prompt,
            "max_output_tokens": max(96, request.max_tokens),
            "reasoning": {"effort": self._reasoning_effort(request)},
            "text": text,
        }
        resp = await self._post(RESPONSES_URL, payload, request.timeout_s)
        if resp.status_code >= 400:
            if "temperature" in resp.text:
                payload.pop("temperature", None)
            resp = await self._post(RESPONSES_URL, payload, request.timeout_s)
        if resp.status_code >= 400:
            raise LLMProviderError(f"OpenAI API error: {resp.status_code} {resp.text}")
        data = resp.json()
        if not isinstance(data, dict):
            raise LLMProviderError("Unexpected OpenAI response format.")

        output_items = data.get("output") or []
        # A refusal comes back as a content item of its own; it is never usable output.
        for item in output_items:
            if isinstance(item, dict) and item.get("type") == "message":
                for content in item.get("content") or []:
                    if isinstance(content, dict) and content.get("type") == "refusal":
                        raise LLMProviderError(f"Model refused: {content.get('refusal') or 'no reason given'}")
        # A structured reply cut off by max_output_tokens is not valid JSON; say so plainly.
        if request.json_schema is not None and data.get("status") == "incomplete":
            reason = (data.get("incomplete_details") or {}).get("reason") or "unknown"
            raise LLMProviderError(f"OpenAI response was incomplete: {reason}")

        try:
            # Some Responses payloads include output_text directly.
            output_text = data.get("output_text")
            if isinstance(output_text, str) and output_text.strip():
                return output_text

            # Preferred structure: output -> message -> content -> output_text.
            texts: list[str] = []
            for item in output_items:
                if item.get("type") != "message":
                    continue
                for content in item.get("content", []):
                    content_type = content.get("type")
                    if content_type in {"output_text", "text"}:
                        text_value = content.get("text", "")
                        if text_value:
                            texts.append(text_value)
            if texts:
                return "".join(texts)

            # Fallback for models returning a simple choices/message structure.
            if "choices" in data:
                return data["choices"][0]["message"]["content"]
        except Exception as exc:  # noqa: BLE001
            raise LLMProviderError("Unexpected OpenAI response format.") from exc

        raise LLMProviderError(f"OpenAI response did not include output text. Raw: {data}")
