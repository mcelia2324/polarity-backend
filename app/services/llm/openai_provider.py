from __future__ import annotations

import httpx

from app.services.llm.base import LLMProvider, LLMProviderError, LLMRequest


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, api_key: str, model: str):
        self._api_key = api_key
        self._model = model

    def _use_responses_api(self) -> bool:
        model = (self._model or "").lower()
        return model.startswith("gpt-5") or model.startswith("o")

    def _reasoning_effort(self) -> str:
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
        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post("https://api.openai.com/v1/chat/completions", json=payload, headers=headers)
        if resp.status_code >= 400:
            if "temperature" in resp.text:
                payload.pop("temperature", None)
            if "max_completion_tokens" in resp.text:
                payload.pop("max_completion_tokens", None)
                payload["max_tokens"] = request.max_tokens
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post("https://api.openai.com/v1/chat/completions", json=payload, headers=headers)
        if resp.status_code >= 400:
            raise LLMProviderError(f"OpenAI API error: {resp.status_code} {resp.text}")
        data = resp.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise LLMProviderError("Unexpected OpenAI response format.") from exc

    async def _generate_responses(self, request: LLMRequest) -> str:
        payload = {
            "model": self._model,
            "input": request.user_prompt,
            "instructions": request.system_prompt,
            "max_output_tokens": max(96, request.max_tokens),
            "reasoning": {"effort": self._reasoning_effort()},
            "text": {"verbosity": "low"},
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post("https://api.openai.com/v1/responses", json=payload, headers=headers)
        if resp.status_code >= 400:
            if "temperature" in resp.text:
                payload.pop("temperature", None)
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post("https://api.openai.com/v1/responses", json=payload, headers=headers)
        if resp.status_code >= 400:
            raise LLMProviderError(f"OpenAI API error: {resp.status_code} {resp.text}")
        data = resp.json()
        try:
            # Some Responses payloads include output_text directly.
            output_text = data.get("output_text")
            if isinstance(output_text, str) and output_text.strip():
                return output_text

            # Preferred structure: output -> message -> content -> output_text.
            output_items = data.get("output", [])
            texts: list[str] = []
            for item in output_items:
                if item.get("type") != "message":
                    continue
                for content in item.get("content", []):
                    content_type = content.get("type")
                    if content_type in {"output_text", "text"}:
                        text = content.get("text", "")
                        if text:
                            texts.append(text)
            if texts:
                return "".join(texts)

            # Fallback for models returning a simple choices/message structure.
            if "choices" in data:
                return data["choices"][0]["message"]["content"]
        except Exception as exc:  # noqa: BLE001
            raise LLMProviderError("Unexpected OpenAI response format.") from exc

        raise LLMProviderError(f"OpenAI response did not include output text. Raw: {data}")
