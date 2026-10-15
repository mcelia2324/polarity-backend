from __future__ import annotations

import datetime as dt
import logging
import re
import unicodedata
from dataclasses import dataclass, replace

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DailyContent
from app.services.backoff import retry_due
from app.services.llm import LLMProvider, LLMRequest
from app.services.voice import (
    DASH_RE,
    DOUBLE_QUOTES,
    PROMPT_BANNED_WORDS,
    clean_generated_text,
    has_word,
    sentences,
    voice_errors,
    word_count,
)

logger = logging.getLogger(__name__)

# Rows written by this code. 1 = the original prompts; 3 = the v3 prompts below.
PROMPT_VERSION = 3

# One attempt = the first draft plus one retry with the errors fed back.
DRAFTS_PER_ATTEMPT = 2

CONTEMPLATION_MIN_WORDS = 25
CONTEMPLATION_MAX_WORDS = 60  # the shipped 2.1 app shows it in a fixed card; never longer
QUOTE_MIN_WORDS = 4
QUOTE_MAX_WORDS = 30  # 2.1 shows the quote in three lines at most
QUOTE_WORK_MAX_CHARS = 80

# Public-domain authors whose words are well documented. Canonical name -> other spellings the
# model may use. The canonical name is what gets stored and shown.
QUOTE_AUTHORS: dict[str, tuple[str, ...]] = {
    "Marcus Aurelius": ("Marcus Aurelius Antoninus", "Emperor Marcus Aurelius"),
    "Seneca": ("Seneca the Younger", "Lucius Annaeus Seneca"),
    "Epictetus": (),
    "Aristotle": (),
    "Confucius": (),
    "Michel de Montaigne": ("Montaigne",),
    "Blaise Pascal": ("Pascal",),
    "Baruch Spinoza": ("Spinoza", "Benedict de Spinoza", "Benedictus de Spinoza"),
    "Henry David Thoreau": ("Thoreau",),
    "Ralph Waldo Emerson": ("Emerson",),
    "William James": (),
    "George Eliot": (),
    "Leo Tolstoy": ("Tolstoy", "Lev Tolstoy"),
    "Fyodor Dostoevsky": ("Dostoevsky", "Fyodor Dostoyevsky", "Dostoyevsky"),
    "Søren Kierkegaard": ("Kierkegaard", "Soren Kierkegaard"),
    "Frederick Douglass": (),
    "Samuel Johnson": ("Dr. Samuel Johnson", "Dr. Johnson"),
    "Simone Weil": (),
}
# Left off the list on purpose: the names that collect the most invented quotes.
EXCLUDED_QUOTE_AUTHORS: tuple[str, ...] = (
    "Rumi", "Lao Tzu", "Buddha", "Albert Einstein", "Abraham Lincoln", "Mark Twain",
)

CONTEMPLATION_SYSTEM_PROMPT = f"""You write the daily contemplation for Polarity, a journaling app.

You get two words. The lower word is a state people drop into. The higher word is the state that answers it. You also get a short phrase that says what the pair is about.

Write two or three short sentences, 30 to 60 words in all, to one reader. Use "you". First help them notice where the lower word shows up in a normal day, without blame. Then point them to one small, real step toward the higher word. The last sentence must use the higher word and must not use the lower word.

Write the way a calm person talks, not like a coach or a guide. Plain words. Be concrete. No questions, lists, headings, quotation marks, emoji, or exclamation marks. No em dashes, en dashes, double hyphens, or semicolons. Use periods and commas. Never mention numbers, scores, levels, books, or teachers. Never use these words or any form of them: {PROMPT_BANNED_WORDS}.

Return only the text."""

QUOTE_SYSTEM_PROMPT = """You choose one real quotation for Polarity, a journaling app. Getting it right matters more than having one.

You get a higher word and a lower word. Look for a short quotation that speaks to the higher word, or to the move from the lower word to the higher one.

Only return a quotation if all of these are true:
- You know its exact wording from a published book, essay, letter, or speech.
- You can name the author and the specific work.
- The author is on this list: {authors}.
- It has fewer than 30 words and no dashes.
- It is not one of the many lines that get credited to famous people with no real source.

If any of these is not true, return null for every field. No quote is fine. A wrong quote is not.

Give the quotation text without quotation marks, the author's name in its usual form, and the title of the work."""

QUOTE_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["quote", "author", "work"],
    "properties": {
        "quote": {"type": ["string", "null"], "description": "The exact quotation, without quotation marks."},
        "author": {"type": ["string", "null"], "description": "The author's name in its usual form."},
        "work": {"type": ["string", "null"], "description": "The title of the work it comes from."},
    },
}

RETRY_TEXT = """Your last draft was:
{draft}

It broke these rules:
- {errors}
Write it again. Fix those points and keep the rest of the meaning."""

QUOTE_RETRY_TEXT = """Your last answer broke these rules:
- {errors}
If you are not sure of a quotation that meets every rule, return null for every field."""


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def fallback_contemplation(higher: str, lower: str, meaning: dict | None = None) -> str:
    """Served (never saved) when today's contemplation is still missing, so the shipped 2.1 app,
    which keeps whatever it fetched for the whole day, never gets an empty card. Ends facing the
    higher word."""
    if meaning and meaning.get("disguise") and meaning.get("practice"):
        return f"{meaning['disguise']} {meaning['practice']}"
    return (
        f"Notice where {lower} showed up today. You do not have to fight it. "
        f"Ask what {higher} would do next, and try one small piece of that."
    )


def contemplation_errors(text: str, higher: str, lower: str) -> list[str]:
    """Rules a contemplation draft breaks (empty list = accepted)."""
    if not text:
        return ["The reply was empty."]
    errors: list[str] = []
    count = word_count(text)
    if not CONTEMPLATION_MIN_WORDS <= count <= CONTEMPLATION_MAX_WORDS:
        errors.append(f"Use 30 to 60 words in all. This draft has {count}.")
    parts = sentences(text)
    if not 2 <= len(parts) <= 3:
        errors.append("Write two or three sentences.")
    if "?" in text:
        errors.append("Do not ask a question.")
    if not text.endswith("."):
        errors.append("End the last sentence with a period.")
    last = parts[-1] if parts else ""
    if not has_word(last, higher):
        errors.append(f"The last sentence must use the word {higher}.")
    if has_word(last, lower):
        errors.append(f"The last sentence must not use the word {lower}.")
    errors.extend(voice_errors(text, exempt=(higher, lower)))
    return errors


def _author_key(name: str) -> str:
    folded = unicodedata.normalize("NFKD", name.lower().replace("ø", "o"))
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^a-z ]+", " ", folded).split())


_AUTHOR_LOOKUP: dict[str, str] = {}
for _canonical, _aliases in QUOTE_AUTHORS.items():
    for _name in (_canonical, *_aliases):
        _AUTHOR_LOOKUP[_author_key(_name)] = _canonical


def canonical_author(name: str | None) -> str | None:
    """The allowlisted canonical name for an author, or None if they are not on the list."""
    if not name:
        return None
    return _AUTHOR_LOOKUP.get(_author_key(name))


@dataclass(frozen=True)
class QuoteResult:
    status: str  # "ok" (a quote passed), "none" (no good quote exists: final), "failed" (retry later)
    text: str | None = None
    author: str | None = None
    work: str | None = None


def _clean_field(value: object, strip_chars: str = "") -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split()).strip("".join(DOUBLE_QUOTES) + strip_chars).strip()
    return cleaned or None


def validate_quote(data: dict) -> tuple[QuoteResult, list[str]]:
    """Check a structured quote reply. All fields empty means the model found no quote it could
    stand behind, which is a final answer. Anything else must pass every acceptance rule."""
    text = _clean_field(data.get("quote"))
    author = _clean_field(data.get("author"))
    work = _clean_field(data.get("work"), strip_chars="*_ ")
    if not text and not author and not work:
        return QuoteResult("none"), []

    errors: list[str] = []
    if not text:
        errors.append("The quotation text is missing.")
    else:
        count = word_count(text)
        if not QUOTE_MIN_WORDS <= count <= QUOTE_MAX_WORDS:
            errors.append(f"The quotation must have {QUOTE_MIN_WORDS} to {QUOTE_MAX_WORDS} words.")
        if DASH_RE.search(text):
            errors.append("The quotation contains a dash.")
        if any(mark in text for mark in DOUBLE_QUOTES):
            errors.append("Give the quotation without quotation marks.")
    canonical = canonical_author(author)
    if canonical is None:
        errors.append("The author must be one of the names on the list.")
    if not work:
        errors.append("Name the specific work the quotation comes from.")
    elif len(work) > QUOTE_WORK_MAX_CHARS:
        errors.append(f"The title of the work must be {QUOTE_WORK_MAX_CHARS} characters or fewer.")
    if errors:
        return QuoteResult("failed"), errors
    return QuoteResult("ok", text=text, author=canonical, work=work), []


def bank_quote(higher: str, lower: str) -> QuoteResult | None:
    """A reviewed quote from the pair bank always wins over a generated one. The bank arrives in
    a later change; until then there is never one."""
    return None


def _quote_settled(row: DailyContent) -> bool:
    # A stored quote is final even if the row predates quote_resolved (or an older revision wrote it).
    return bool(row.quote_resolved) or row.quote is not None


def is_complete(row: DailyContent | None) -> bool:
    """Nothing left to generate for this day."""
    return row is not None and row.contemplation is not None and _quote_settled(row)


class DailyContentService:
    """Generates and persists the daily quote + guided contemplation once per day for everyone.

    Cost stays flat (one generation per day, globally) no matter how many users or instances there
    are. This is the same cheap "generate once, serve from storage" pattern as the word pair itself.

    Only missing pieces are generated: a contemplation that is still NULL, or a quote that is not
    resolved. A failure leaves the piece missing and is retried on a later call after a backoff
    (5, 10, 20, 40, 80 minutes), up to six attempts; `force` resets the count.
    """

    def __init__(self, session: AsyncSession, provider: LLMProvider):
        self._session = session
        self._provider = provider

    async def _load(self, date: dt.date) -> DailyContent | None:
        result = await self._session.execute(select(DailyContent).where(DailyContent.date == date))
        return result.scalar_one_or_none()

    async def get_or_create(
        self,
        date: dt.date,
        higher: str,
        lower: str,
        axis: str | None = None,
        teaching: str | None = None,
        allow_generate: bool = True,
        force: bool = False,
    ) -> DailyContent | None:
        """Return the day's row, generating whatever is missing when allowed and due.

        Returns None only when there is no row and generation is not allowed. The contemplation
        may still be NULL; the caller serves `fallback_contemplation` in that case (never saved).
        """
        row = await self._load(date)
        if not allow_generate:
            return row
        if row is not None:
            if is_complete(row):
                return row
            if force:
                row.attempts = 0
            elif not retry_due(row.attempts, row.last_attempt_at, _utcnow()):
                return row
        return await self._generate_missing(date, row, higher, lower, axis, teaching)

    async def _generate_missing(
        self,
        date: dt.date,
        row: DailyContent | None,
        higher: str,
        lower: str,
        axis: str | None,
        teaching: str | None,
    ) -> DailyContent:
        quote: QuoteResult | None = None
        if row is None or not _quote_settled(row):
            quote = bank_quote(higher, lower) or await self._generate_quote(higher, lower)
        contemplation: str | None = None
        if row is None or row.contemplation is None:
            contemplation = await self._generate_contemplation(higher, lower, axis, teaching)

        if row is None:
            row = DailyContent(date=date, quote_resolved=False, attempts=0, prompt_version=PROMPT_VERSION)
            self._session.add(row)
        if quote is not None and quote.status != "failed":
            row.quote = quote.text
            row.quote_author = quote.author
            row.quote_source = quote.work
            row.quote_resolved = True
            row.prompt_version = PROMPT_VERSION
        if contemplation is not None:
            row.contemplation = contemplation
            row.prompt_version = PROMPT_VERSION
        row.attempts = (row.attempts or 0) + 1
        row.last_attempt_at = _utcnow()
        if not is_complete(row):
            logger.warning(
                "Daily content for %s still incomplete after attempt %d (quote resolved: %s, contemplation: %s)",
                date, row.attempts, _quote_settled(row), row.contemplation is not None,
            )
        try:
            await self._session.commit()
            return row
        except IntegrityError:
            # Another instance created the row concurrently; return the stored one.
            await self._session.rollback()
            existing = await self._load(date)
            if existing is not None:
                return existing
            raise

    async def _generate_quote(self, higher: str, lower: str) -> QuoteResult:
        authors = ", ".join(QUOTE_AUTHORS)
        base_user = f"Higher word: {higher}\nLower word: {lower}\nReturn the JSON object."
        request = LLMRequest(
            system_prompt=QUOTE_SYSTEM_PROMPT.format(authors=authors),
            user_prompt=base_user,
            temperature=0.7,
            max_tokens=200,
            json_schema=QUOTE_SCHEMA,
            schema_name="quote",
        )
        for _ in range(DRAFTS_PER_ATTEMPT):
            try:
                data = await self._provider.generate_json(request)
            except Exception:  # noqa: BLE001
                logger.warning("Failed to generate daily quote", exc_info=True)
                return QuoteResult("failed")
            result, errors = validate_quote(data)
            if not errors:
                return result
            logger.info("Rejected quote draft (%s): %r", "; ".join(errors), data)
            request = replace(request, user_prompt=f"{base_user}\n\n" + QUOTE_RETRY_TEXT.format(errors="\n- ".join(errors)))
        # Still invalid after one retry. Asking again only invites a different made-up quote, so
        # settle on no quote for the day.
        return QuoteResult("none")

    async def _generate_contemplation(
        self, higher: str, lower: str, axis: str | None, teaching: str | None
    ) -> str | None:
        lines = [f"Higher word: {higher}", f"Lower word: {lower}"]
        if axis:
            lines.append(f"What the pair is about: {axis}")
        if teaching:
            lines.append(f"Context, do not repeat it: {teaching}")
        lines.append("Write today's contemplation.")
        base_user = "\n".join(lines)

        user = base_user
        for _ in range(DRAFTS_PER_ATTEMPT):
            try:
                raw = await self._provider.generate(LLMRequest(
                    system_prompt=CONTEMPLATION_SYSTEM_PROMPT,
                    user_prompt=user,
                    temperature=0.8,
                    max_tokens=180,
                ))
            except Exception:  # noqa: BLE001
                logger.warning("Failed to generate daily contemplation", exc_info=True)
                return None
            text = clean_generated_text(raw)
            errors = contemplation_errors(text, higher, lower)
            if not errors:
                return text
            logger.info("Rejected contemplation draft (%s): %r", "; ".join(errors), text)
            user = f"{base_user}\n\n" + RETRY_TEXT.format(draft=text, errors="\n- ".join(errors))
        return None
