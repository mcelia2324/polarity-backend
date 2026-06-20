from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DailyContent
from app.services.llm import LLMProvider, LLMRequest

logger = logging.getLogger(__name__)


class DailyContentService:
    """Generates and persists the daily quote + guided contemplation once per day for everyone.

    Cost stays flat (one generation per day, globally) no matter how many users or instances there
    are — the same cheap "generate once, serve from storage" pattern as the word pair itself.
    """

    def __init__(self, session: AsyncSession, provider: LLMProvider):
        self._session = session
        self._provider = provider

    async def get_or_create(self, date: dt.date, word_a: str, word_b: str) -> DailyContent:
        result = await self._session.execute(select(DailyContent).where(DailyContent.date == date))
        existing = result.scalar_one_or_none()
        if existing is not None:
            return existing

        quote_text, quote_author = await self._generate_quote(word_a, word_b)
        contemplation = await self._generate_contemplation(word_a, word_b)

        content = DailyContent(
            date=date, quote=quote_text, quote_author=quote_author, contemplation=contemplation
        )
        try:
            self._session.add(content)
            await self._session.commit()
            return content
        except IntegrityError:
            # Another instance generated it concurrently; return the stored row.
            await self._session.rollback()
            result = await self._session.execute(select(DailyContent).where(DailyContent.date == date))
            existing = result.scalar_one_or_none()
            if existing is not None:
                return existing
            raise

    async def _generate_quote(self, word_a: str, word_b: str) -> tuple[str | None, str | None]:
        try:
            raw = await self._provider.generate(LLMRequest(
                system_prompt=(
                    "You provide a single inspiring quote related to one or both of the given words. "
                    "The quote should be from a real, well-known person (philosopher, author, leader, thinker). "
                    "Format: the quote text on the first line, then a newline, then just the author's name. "
                    "No quotation marks. No extra commentary."
                ),
                user_prompt=f"Give an inspiring quote related to '{word_a}' or '{word_b}'.",
                temperature=0.7,
                max_tokens=100,
            ))
            lines = [l.strip() for l in raw.strip().splitlines() if l.strip()]
            if len(lines) >= 2:
                return lines[0].strip('"').strip("'"), lines[-1].lstrip("—–- ").strip()
            if lines:
                return lines[0].strip('"').strip("'"), None
        except Exception:
            logger.warning("Failed to generate daily quote", exc_info=True)
        return None, None

    async def _generate_contemplation(self, word_a: str, word_b: str) -> str | None:
        try:
            raw = await self._provider.generate(LLMRequest(
                system_prompt=(
                    "You are a calm, wise contemplative guide. Given two contrasting words (a higher and a "
                    "lower expression of consciousness), write a short guided reflection of 2-3 sentences that "
                    "helps the reader notice where each shows up in their life today and gently invites them "
                    "to lean toward the higher one. Warm, grounded, and non-preachy, in the second person "
                    "('you'). No headings, no lists, no quotation marks — just the reflection itself."
                ),
                user_prompt=f"Higher word: '{word_a}'. Lower word: '{word_b}'. Write today's guided contemplation.",
                temperature=0.8,
                max_tokens=180,
            ))
            text = raw.strip().strip('"').strip()
            return text or None
        except Exception:
            logger.warning("Failed to generate daily contemplation", exc_info=True)
            return None
