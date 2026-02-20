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

        definition = await self._fetch_dictionary_definition(normalized)
        source = "dictionaryapi"

        if not definition:
            definition = await self._generate_definition_with_llm(normalized)
            source = "openai"

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
                "You are a concise dictionary editor. "
                "Return one plain-English definition in under 22 words. "
                "Return only the definition text."
            ),
            user_prompt=f"Define the word '{word}' for reflective self-inquiry context.",
            temperature=0.2,
            max_tokens=48,
        )
        try:
            raw = await self._llm.generate(prompt)
        except Exception:  # noqa: BLE001
            return None
        return self._normalize_definition(raw)

    def _normalize_definition(self, raw: str) -> str | None:
        if not raw:
            return None
        first_line = raw.strip().splitlines()[0].strip().strip("\"'")
        # Keep a compact single-sentence definition.
        cleaned = re.sub(r"\s+", " ", first_line)
        if not cleaned:
            return None
        if len(cleaned) > 180:
            cleaned = cleaned[:177].rstrip() + "..."
        return cleaned
