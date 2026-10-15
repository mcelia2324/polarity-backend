from __future__ import annotations

import logging
import re
import time

import httpx
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import WordDefinition
from app.services.backoff import backoff_delay
from app.services.llm import LLMProvider, LLMRequest
from app.services.voice import PROMPT_BANNED_WORDS, clean_generated_text, voice_errors, word_count

logger = logging.getLogger(__name__)

# Rows written by the current prompt. Older rows ("openai" from the first prompt, "fallback" for
# the "Definition unavailable." placeholder older code saved, "dictionaryapi" for plain dictionary
# text that ignores the voice rules) are stale: still served, and replaced when the word comes up
# on the today path (at most its two words) or in a prewarm.
DEFINITION_SOURCE = "openai-v3"
STALE_SOURCES = frozenset({"openai", "fallback", "dictionaryapi"})

# Returned when there is no definition yet. Never saved, so the next request tries again.
DEFINITION_UNAVAILABLE = "Definition unavailable."

MIN_WORDS = 15
MAX_WORDS = 60
DRAFTS_PER_ATTEMPT = 2

DEFINITION_SYSTEM_PROMPT = f"""You explain one word for Polarity, a daily journaling app.

Write two short sentences, 20 to 45 words in all. The first says what the word means in plain terms. The second shows how it tends to look in a normal day, in what people do or say.

Write the way a calm person explains a word to a friend. Plain words. Do not start with the word itself, or with "It is" or "It's". No em dashes, en dashes, double hyphens, or semicolons. No numbers. Never use these words or any form of them: {PROMPT_BANNED_WORDS}.

Return only the text."""

RETRY_TEXT = """Your last draft was:
{draft}

It broke these rules:
- {errors}
Write it again. Fix those points and keep the rest of the meaning."""

# Per-process memory of words whose generation just failed: word -> (failures in a row, when the
# last one happened). Requests for an incomplete day repeat every few minutes, so without this a
# word the model keeps getting wrong would be retried (and paid for) all day.
_recent_failures: dict[str, tuple[int, float]] = {}
_clock = time.monotonic


def _generation_due(word: str) -> bool:
    entry = _recent_failures.get(word)
    if entry is None:
        return True
    failures, last = entry
    return _clock() - last >= backoff_delay(failures).total_seconds()


def _record_failure(word: str) -> None:
    failures = _recent_failures.get(word, (0, 0.0))[0]
    _recent_failures[word] = (failures + 1, _clock())


def definition_errors(text: str, word: str) -> list[str]:
    """Rules a definition draft breaks (empty list = accepted)."""
    if not text:
        return ["The reply was empty."]
    errors: list[str] = []
    count = word_count(text)
    if not MIN_WORDS <= count <= MAX_WORDS:
        errors.append(f"Use 20 to 45 words in all. This draft has {count}.")
    errors.extend(voice_errors(text, exempt=(word,)))
    return errors


class DefinitionService:
    def __init__(self, session: AsyncSession, llm_provider: LLMProvider):
        self._session = session
        self._llm = llm_provider

    async def get_definition(self, word: str, *, allow_generate: bool = True) -> str:
        """The stored definition, generating (or upgrading a stale one) when allowed.

        Never raises for a generation failure: it falls back to the stale text, then to a plain
        dictionary definition, then to DEFINITION_UNAVAILABLE, which is returned but not saved.
        """
        normalized = word.strip().lower()
        existing = await self._session.get(WordDefinition, normalized)
        if existing is not None and existing.source not in STALE_SOURCES:
            return existing.definition
        # What to serve if a new definition cannot be made right now.
        current = existing.definition if existing is not None and existing.source != "fallback" else None

        if not allow_generate or not _generation_due(normalized):
            return current or DEFINITION_UNAVAILABLE

        definition = await self._generate_definition_with_llm(normalized)
        if definition:
            _recent_failures.pop(normalized, None)
            return await self._save(normalized, definition, DEFINITION_SOURCE, existing)
        _record_failure(normalized)
        if current:
            return current

        definition = await self._fetch_dictionary_definition(normalized)
        if definition:
            return await self._save(normalized, definition, "dictionaryapi", existing)
        return DEFINITION_UNAVAILABLE

    async def _save(self, word: str, definition: str, source: str, existing: WordDefinition | None) -> str:
        if existing is not None:
            existing.definition = definition
            existing.source = source
        else:
            self._session.add(WordDefinition(word=word, definition=definition, source=source))
        try:
            await self._session.commit()
        except IntegrityError:
            # Another instance saved it first; serve theirs.
            await self._session.rollback()
            stored = await self._session.get(WordDefinition, word)
            if stored is not None:
                return stored.definition
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
        base_user = f"Word: {word}\nExplain this word."
        user = base_user
        for _ in range(DRAFTS_PER_ATTEMPT):
            try:
                raw = await self._llm.generate(LLMRequest(
                    system_prompt=DEFINITION_SYSTEM_PROMPT,
                    user_prompt=user,
                    temperature=0.4,
                    max_tokens=150,
                ))
            except Exception:  # noqa: BLE001
                logger.warning("Failed to generate a definition for %s", word, exc_info=True)
                return None
            text = clean_generated_text(raw)
            errors = definition_errors(text, word)
            if not errors:
                return text
            logger.info("Rejected definition draft for %s (%s): %r", word, "; ".join(errors), text)
            user = f"{base_user}\n\n" + RETRY_TEXT.format(draft=text, errors="\n- ".join(errors))
        return None

    def _normalize_definition(self, raw: str) -> str | None:
        if not raw:
            return None
        text = raw.strip().strip("\"'")
        cleaned = re.sub(r"\s+", " ", text)
        return cleaned or None
