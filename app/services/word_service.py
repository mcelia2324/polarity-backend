from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import UsedWord, WordPair
from app.services.llm import LLMProvider, LLMRequest

WORD_RE = re.compile(r"^[A-Za-z]+$")
logger = logging.getLogger(__name__)
BANNED_TRIVIAL = {
    "up",
    "down",
    "left",
    "right",
    "hot",
    "cold",
    "big",
    "small",
    "yes",
    "no",
    "light",
    "dark",
    "day",
    "night",
    "good",
    "bad",
    "true",
    "false",
    "black",
    "white",
    "high",
    "low",
    "old",
    "new",
    "fast",
    "slow",
    "open",
    "closed",
    "in",
    "out",
}

# Curated contrasting pairs (Power vs Force style), used ONLY as a last-resort fallback if the
# LLM somehow cannot produce a novel pair. Each candidate is still checked for word-level
# uniqueness before use, so the daily prompt can never hard-fail with a 500.
FALLBACK_PAIRS: list[tuple[str, str]] = [
    ("courage", "intimidation"),
    ("acceptance", "denial"),
    ("serenity", "agitation"),
    ("willingness", "resistance"),
    ("compassion", "indifference"),
    ("gratitude", "entitlement"),
    ("humility", "arrogance"),
    ("forgiveness", "resentment"),
    ("devotion", "apathy"),
    ("temperance", "indulgence"),
    ("clarity", "confusion"),
    ("trust", "suspicion"),
    ("generosity", "greed"),
    ("authenticity", "pretense"),
    ("equanimity", "reactivity"),
    ("discernment", "gullibility"),
    ("magnanimity", "pettiness"),
    ("fortitude", "timidity"),
    ("reverence", "contempt"),
    ("surrender", "control"),
]


class WordGenerationError(RuntimeError):
    pass


def _normalize_word(raw: str) -> str | None:
    cleaned = re.sub(r"[^A-Za-z]", "", raw).strip()
    if not cleaned:
        return None
    if not WORD_RE.match(cleaned):
        return None
    return cleaned.lower()


def parse_two_words(text: str) -> tuple[str, str] | None:
    if not text:
        return None
    cleaned = text.strip()
    if not cleaned:
        return None

    line = cleaned.splitlines()[0].strip().strip("\"'")

    patterns = [
        r"([A-Za-z]+)\s*,\s*([A-Za-z]+)",
        r"([A-Za-z]+)\s+vs\.?\s+([A-Za-z]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, line, flags=re.IGNORECASE)
        if match:
            word_a = _normalize_word(match.group(1))
            word_b = _normalize_word(match.group(2))
            if word_a and word_b and word_a != word_b:
                return word_a, word_b

    words = re.findall(r"[A-Za-z]+", line)
    if len(words) != 2:
        return None

    word_a = _normalize_word(words[0])
    word_b = _normalize_word(words[1])

    if not word_a or not word_b or word_a == word_b:
        return None

    return word_a, word_b


def format_pair_display(word_a: str, word_b: str) -> str:
    return f"{word_a.title()} vs {word_b.title()}"


class WordService:
    def __init__(self, session: AsyncSession, llm_provider: LLMProvider):
        self._session = session
        self._llm = llm_provider

    async def get_pair_for_date(self, date: dt.date) -> WordPair | None:
        result = await self._session.execute(select(WordPair).where(WordPair.date == date))
        return result.scalar_one_or_none()

    async def _get_recent_used_words(self, limit: int = 200) -> list[str]:
        result = await self._session.execute(
            select(UsedWord.word).order_by(UsedWord.created_at.desc()).limit(limit)
        )
        return [row[0] for row in result.all()]

    def _build_request(self, avoid_words: Iterable[str]) -> LLMRequest:
        avoid = ", ".join(avoid_words)
        user_prompt = "Provide one pair for today's reflection."
        if avoid:
            user_prompt += (
                " These words are already taken and must NOT be used (avoid close variants too): "
                f"{avoid}. Pick two genuinely different words that are not in that list."
            )
        system_prompt = (
            "You generate two contrasting single English words inspired by David R. Hawkins' "
            "Power vs Force framework. Choose words with deep inner meaning (states of consciousness, "
            "virtues vs vices, or power vs force dynamics). Avoid trivial physical or directional "
            "opposites (up/down, hot/cold, left/right, big/small, light/dark), colors, numbers, "
            "or generic yes/no. The words should feel substantial for journaling and reflection. "
            "Return exactly two lowercase words separated by a comma, and nothing else."
        )
        return LLMRequest(system_prompt=system_prompt, user_prompt=user_prompt, temperature=0.9, max_tokens=16)

    async def _words_available(self, word_a: str, word_b: str) -> bool:
        result = await self._session.execute(
            select(UsedWord.word).where(UsedWord.word.in_([word_a, word_b]))
        )
        return result.first() is None

    async def ensure_pair_for_date(self, date: dt.date, max_attempts: int = 40) -> WordPair:
        existing = await self.get_pair_for_date(date)
        if existing:
            return existing

        recent_words = await self._get_recent_used_words()
        # Words the model has already proposed this run that turned out to be taken or trivial.
        # We feed these back into every prompt so the model stops repeating the same word
        # (e.g. fixating on "integrity") and keeps moving toward a genuinely unused pair.
        session_rejects: list[str] = []
        session_seen: set[str] = set()

        def _reject(*words: str) -> None:
            for w in words:
                if w and w not in session_seen:
                    session_seen.add(w)
                    session_rejects.append(w)

        for attempt in range(1, max_attempts + 1):
            # Rejected words first (most important), then recent history, capped to keep the
            # prompt focused. The DB check below still enforces uniqueness against ALL used words.
            prompt_avoid = session_rejects + [w for w in recent_words[:40] if w not in session_seen]
            request = self._build_request(prompt_avoid)

            raw = await self._llm.generate(request)
            parsed = parse_two_words(raw)
            if not parsed:
                logger.warning("Invalid LLM output on attempt %d: %s", attempt, raw)
                continue
            word_a, word_b = parsed
            if word_a in BANNED_TRIVIAL or word_b in BANNED_TRIVIAL:
                logger.info("Filtered trivial pair on attempt %d: %s, %s", attempt, word_a, word_b)
                _reject(word_a, word_b)
                continue
            if not await self._words_available(word_a, word_b):
                logger.info("Duplicate word(s) on attempt %d: %s, %s", attempt, word_a, word_b)
                _reject(word_a, word_b)
                continue

            pair = WordPair(date=date, word_a=word_a, word_b=word_b)
            try:
                self._session.add(pair)
                self._session.add(UsedWord(word=word_a, pair=pair))
                self._session.add(UsedWord(word=word_b, pair=pair))
                await self._session.commit()
                logger.info("Generated pair for %s on attempt %d: %s, %s", date, attempt, word_a, word_b)
                return pair
            except IntegrityError:
                await self._session.rollback()
                existing = await self.get_pair_for_date(date)
                if existing:
                    return existing
                _reject(word_a, word_b)
                continue

        # Safety net: the model never produced a novel pair (extremely unlikely with feedback).
        # Use a curated contrasting pair whose words are both still unused so the daily prompt
        # never hard-fails with a 500.
        fallback = await self._fallback_pair(date)
        if fallback is not None:
            logger.warning(
                "LLM exhausted %d attempts for %s; used curated fallback: %s, %s",
                max_attempts, date, fallback.word_a, fallback.word_b,
            )
            return fallback

        raise WordGenerationError("Unable to generate a new unique word pair after several attempts.")

    async def _fallback_pair(self, date: dt.date) -> WordPair | None:
        """Return the first curated contrasting pair whose words are both still unused."""
        for word_a, word_b in FALLBACK_PAIRS:
            if word_a in BANNED_TRIVIAL or word_b in BANNED_TRIVIAL:
                continue
            if not await self._words_available(word_a, word_b):
                continue
            pair = WordPair(date=date, word_a=word_a, word_b=word_b)
            try:
                self._session.add(pair)
                self._session.add(UsedWord(word=word_a, pair=pair))
                self._session.add(UsedWord(word=word_b, pair=pair))
                await self._session.commit()
                return pair
            except IntegrityError:
                await self._session.rollback()
                existing = await self.get_pair_for_date(date)
                if existing:
                    return existing
                continue
        return None
