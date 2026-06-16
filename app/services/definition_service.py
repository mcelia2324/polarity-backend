from __future__ import annotations

import re

import httpx
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import WordDefinition
from app.services.llm import LLMProvider, LLMRequest


class DefinitionService:
    def __init__(self, session: AsyncSession, llm_provider: LLMProvider):
        self._session = session
        self._llm = llm_provider

    async def get_definition(self, word: str) -> str:
        normalized = word.strip().lower()
        existing = await self._session.get(WordDefinition, normalized)
        if existing:
            return existing.definition

        # LLM-first: richer, reflection-oriented definitions
        definition = await self._generate_definition_with_llm(normalized)
        source = "openai"

        if not definition:
            definition = await self._fetch_dictionary_definition(normalized)
            source = "dictionaryapi"

        if not definition:
            definition = "Definition unavailable."
            source = "fallback"

        row = WordDefinition(word=normalized, definition=definition, source=source)
        self._session.add(row)
        try:
            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            existing = await self._session.get(WordDefinition, normalized)
            if existing:
                return existing.definition
        return definition

    async def _fetch_dictionary_definition(self, word: str) -> str | None:
        url = f"https://api.dictionaryapi.dev/api/v2/entries/en/{word}"
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                response = await client.get(url)
        except Exception:  # noqa: BLE001
            return None

        if response.status_code != 200:
            return None

        try:
            payload = response.json()
            if not payload or not isinstance(payload, list):
                return None
            meanings = payload[0].get("meanings") or []
            for meaning in meanings:
                definitions = meaning.get("definitions") or []
                for item in definitions:
                    raw = (item.get("definition") or "").strip()
                    cleaned = self._normalize_definition(raw)
                    if cleaned:
                        return cleaned
        except Exception:  # noqa: BLE001
            return None
        return None

    async def _generate_definition_with_llm(self, word: str) -> str | None:
        prompt = LLMRequest(
            system_prompt=(
                "You are writing definitions for a reflective journaling app called Polarity. "
                "Each day, users receive two contrasting words and journal about them. "
                "Write a clear, thoughtful definition in 2-3 sentences that helps someone "
                "understand the word deeply — not just its dictionary meaning, but how it "
                "shows up in human experience, emotions, and behavior. "
                "Write in plain English. Do not include the word itself at the start. "
                "Return only the definition text, no quotes or labels."
            ),
            user_prompt=f"Define '{word}' for someone reflecting on it in a journaling context.",
            temperature=0.4,
            max_tokens=150,
        )
        try:
            raw = await self._llm.generate(prompt)
        except Exception:  # noqa: BLE001
            return None
        return self._normalize_definition(raw)

    def _normalize_definition(self, raw: str) -> str | None:
        if not raw:
            return None
        text = raw.strip().strip("\"'")
        cleaned = re.sub(r"\s+", " ", text)
        if not cleaned:
            return None
        if len(cleaned) > 500:
            cleaned = cleaned[:497].rstrip() + "..."
        return cleaned
