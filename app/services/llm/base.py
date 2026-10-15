from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class LLMRequest:
    system_prompt: str
    user_prompt: str
    temperature: float = 0.8
    max_tokens: int = 16
    # Structured output: when set, the provider asks for JSON matching this schema (strict mode)
    # and `generate_json` returns the parsed object.
    json_schema: dict | None = None
    schema_name: str = "out"
    timeout_s: float = 20
    # None keeps the provider's default for the model (no reasoning on gpt-5.1/5.2).
    reasoning_effort: str | None = None
    verbosity: str = "low"


class LLMProviderError(RuntimeError):
    pass


class LLMProvider:
    name: str

    async def generate(self, request: LLMRequest) -> str:
        raise NotImplementedError

    async def generate_json(self, request: LLMRequest) -> dict:
        """Run the request and parse the reply as one JSON object. Any reply that is not a JSON
        object raises LLMProviderError, so callers handle one error type for every failure."""
        raw = await self.generate(request)
        try:
            data = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise LLMProviderError("Model reply was not valid JSON.") from exc
        if not isinstance(data, dict):
            raise LLMProviderError("Model reply was not a JSON object.")
        return data
