from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LLMRequest:
    system_prompt: str
    user_prompt: str
    temperature: float = 0.8
    max_tokens: int = 16


class LLMProviderError(RuntimeError):
    pass


class LLMProvider:
    name: str

    async def generate(self, request: LLMRequest) -> str:
        raise NotImplementedError
