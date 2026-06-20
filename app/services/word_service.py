from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Iterable

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from wordfreq import zipf_frequency

from app.models import UsedWord, WordPair
from app.services.llm import LLMProvider, LLMRequest

WORD_RE = re.compile(r"^[A-Za-z]+$")
logger = logging.getLogger(__name__)

# Minimum word-frequency (wordfreq "zipf" scale) for a generated word to be accepted. Real
# reflection words score ~1.75+ (e.g. deceitfulness 1.75, magnanimity 2.2); concatenated
# non-words the model sometimes emits to satisfy the single-word format (e.g. "innerstillness",
# "selfrighteousness") score ~0, so this cleanly rejects them.
MIN_WORD_ZIPF = 1.5

# How long a word stays "used" before it may appear again. Word-level uniqueness within this
# window keeps the daily pair fresh, while allowing long-term reuse so the finite pool of common
# virtue/vice words never truly runs out (which previously forced the curated fallback).
WORD_REUSE_AFTER_DAYS = 180

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

# Curated word pools for the last-resort fallback (Power vs Force style). The generator pairs the
# first still-unused "higher" (virtue) word with the first still-unused "lower" (vice) word, so a
# valid contrasting pair stays available even as the used-word set grows, far more robust than
# fixed pairs. Only reached if the LLM can't produce a novel pair, so the daily prompt never 500s.
FALLBACK_HIGHER: list[str] = [
    "courage", "acceptance", "serenity", "willingness", "compassion", "gratitude", "humility",
    "forgiveness", "devotion", "temperance", "clarity", "trust", "generosity", "authenticity",
    "equanimity", "discernment", "magnanimity", "fortitude", "reverence", "patience", "kindness",
    "honesty", "wisdom", "empathy", "resilience", "optimism", "sincerity", "benevolence",
    "graciousness", "tranquility", "humor", "tenderness", "loyalty", "respect", "hope",
]
FALLBACK_LOWER: list[str] = [
    "intimidation", "denial", "agitation", "resistance", "indifference", "entitlement", "arrogance",
    "resentment", "apathy", "indulgence", "confusion", "suspicion", "greed", "pretense", "reactivity",
    "gullibility", "pettiness", "timidity", "contempt", "impatience", "cruelty", "folly", "callousness",
    "fragility", "pessimism", "insincerity", "malice", "hostility", "envy", "vanity", "blame",
    "complacency", "scorn", "deception", "despair",
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


def _is_common_word(word: str) -> bool:
    """Reject nonsense/concatenated tokens (e.g. 'innerstillness') the model sometimes emits to
    satisfy the single-word format. Real words score well above MIN_WORD_ZIPF; glued tokens ~0."""
    return zipf_frequency(word, "en") >= MIN_WORD_ZIPF


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
            "Each must be a single, common, real English dictionary word (for example: courage, "
            "apathy, humility, resentment). Never use a compound, concatenation, hyphenated word, or "
            "multi-word phrase. "
            "Return exactly two lowercase words separated by a comma, and nothing else."
        )
        return LLMRequest(system_prompt=system_prompt, user_prompt=user_prompt, temperature=0.9, max_tokens=16)

    async def _words_available(self, word_a: str, word_b: str, on_date: dt.date) -> bool:
        """A word is available unless it appeared in a pair within the last
        WORD_REUSE_AFTER_DAYS days (relative to the date being generated)."""
        cutoff = on_date - dt.timedelta(days=WORD_REUSE_AFTER_DAYS)
        result = await self._session.execute(
            select(WordPair.id).where(
                WordPair.date >= cutoff,
                or_(
                    WordPair.word_a.in_([word_a, word_b]),
                    WordPair.word_b.in_([word_a, word_b]),
                ),
            )
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
            if not _is_common_word(word_a) or not _is_common_word(word_b):
                logger.info("Filtered non-dictionary word(s) on attempt %d: %s, %s", attempt, word_a, word_b)
                _reject(word_a, word_b)
                continue
            if not await self._words_available(word_a, word_b, date):
                logger.info("Recently used word(s) on attempt %d: %s, %s", attempt, word_a, word_b)
                _reject(word_a, word_b)
                continue

            pair = WordPair(date=date, word_a=word_a, word_b=word_b)
            try:
                self._session.add(pair)
                await self._session.flush()
                await self._mark_used(word_a, pair.id)
                await self._mark_used(word_b, pair.id)
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

    async def _mark_used(self, word: str, pair_id: int) -> None:
        """Record (or refresh) a word's most-recent use. Upsert keeps the single-row-per-word
        UsedWord schema while allowing a word to be chosen again once its previous use ages out."""
        now = dt.datetime.now(dt.timezone.utc)
        result = await self._session.execute(select(UsedWord).where(UsedWord.word == word))
        existing = result.scalar_one_or_none()
        if existing is not None:
            existing.created_at = now
            existing.pair_id = pair_id
        else:
            self._session.add(UsedWord(word=word, pair_id=pair_id, created_at=now))

    async def _first_available(self, candidates: list[str], on_date: dt.date) -> str | None:
        """First candidate not in BANNED_TRIVIAL and not used within WORD_REUSE_AFTER_DAYS days."""
        cutoff = on_date - dt.timedelta(days=WORD_REUSE_AFTER_DAYS)
        result = await self._session.execute(
            select(WordPair.word_a, WordPair.word_b).where(WordPair.date >= cutoff)
        )
        recent: set[str] = set()
        for row in result.all():
            recent.add(row[0])
            recent.add(row[1])
        for word in candidates:
            if word not in recent and word not in BANNED_TRIVIAL:
                return word
        return None

    async def _fallback_pair(self, date: dt.date) -> WordPair | None:
        """Pair the first not-recently-used 'higher' word with the first 'lower' one."""
        higher = await self._first_available(FALLBACK_HIGHER, date)
        lower = await self._first_available(FALLBACK_LOWER, date)
        if higher is None or lower is None or higher == lower:
            return None
        pair = WordPair(date=date, word_a=higher, word_b=lower)
        try:
            self._session.add(pair)
            await self._session.flush()
            await self._mark_used(higher, pair.id)
            await self._mark_used(lower, pair.id)
            await self._session.commit()
            return pair
        except IntegrityError:
            await self._session.rollback()
            existing = await self.get_pair_for_date(date)
            if existing:
                return existing
            return None
