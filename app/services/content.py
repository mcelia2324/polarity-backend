"""Builds each day's API response in one place.

`/api/word-of-day` and `/api/history` both run every day through `assemble_day`, so a new field
is added once (Pydantic silently drops keys a model does not declare). `orient` decides which word
is the higher one, so `word_a` is the higher word in API output even for older rows that were
stored the other way round.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DailyContent
from app.schemas import WordPairResponse
from app.services.daily_content_service import DailyContentService, fallback_contemplation, is_complete
from app.services.definition_service import DEFINITION_UNAVAILABLE, DefinitionService
from app.services.llm import LLMProvider
from app.services.settings_store import SettingsStore
from app.services.text_utils import normalize_dashes
from app.services.word_service import WordService

logger = logging.getLogger(__name__)

# Optional data files (shipped in the image under app/data/). Until they exist, every pair keeps
# its stored order.
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
BANK_PATH = DATA_DIR / "pair_bank.json"
OVERRIDES_PATH = DATA_DIR / "orientation_overrides.json"

# Settings key read with app_timezone on every word-of-day request. Bumping it (backfill, and
# later the order audit) invalidates the in-memory response cache on every instance.
CONTENT_EPOCH_KEY = "content_epoch"


def pair_key(word_a: str, word_b: str) -> str:
    """Order-free key for a pair: both words lowercased, sorted, joined ("courage|fear")."""
    return "|".join(sorted((word_a.strip().lower(), word_b.strip().lower())))


@dataclass(frozen=True)
class KnownPair:
    higher: str
    lower: str
    axis: str | None = None
    lower_level: str | None = None
    higher_level: str | None = None


@dataclass(frozen=True)
class Orientation:
    higher: str
    lower: str
    source: str  # "bank" | "override" | "stored"
    known: KnownPair | None = None


def _optional_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _load_pairs(path: Path, *, details: bool) -> dict[str, KnownPair]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.exception("Could not read %s; ignoring it", path)
        return {}
    entries = data.get("pairs") if isinstance(data, dict) else None
    pairs: dict[str, KnownPair] = {}
    for entry in entries if isinstance(entries, list) else []:
        higher = _optional_str(entry.get("higher")) if isinstance(entry, dict) else None
        lower = _optional_str(entry.get("lower")) if isinstance(entry, dict) else None
        if not higher or not lower or higher.lower() == lower.lower():
            logger.warning("Skipping malformed pair in %s: %r", path.name, entry)
            continue
        pairs[pair_key(higher, lower)] = KnownPair(
            higher=higher.lower(),
            lower=lower.lower(),
            axis=_optional_str(entry.get("axis")) if details else None,
            lower_level=_optional_str(entry.get("lower_level")) if details else None,
            higher_level=_optional_str(entry.get("higher_level")) if details else None,
        )
    return pairs


@lru_cache(maxsize=1)
def _orientation_sources() -> tuple[dict[str, KnownPair], dict[str, KnownPair]]:
    """(bank, overrides), keyed by pair_key, read once per process."""
    return _load_pairs(BANK_PATH, details=True), _load_pairs(OVERRIDES_PATH, details=False)


def orient(word_a: str, word_b: str) -> Orientation:
    """Which word is the higher one: the pair bank first, then the orientation overrides, then the
    stored order (word_a was always meant to be the higher word)."""
    bank, overrides = _orientation_sources()
    key = pair_key(word_a, word_b)
    if key in bank:
        known = bank[key]
        return Orientation(known.higher, known.lower, "bank", known)
    if key in overrides:
        known = overrides[key]
        return Orientation(known.higher, known.lower, "override", known)
    return Orientation(word_a, word_b, "stored")


def _definition_usable(text: str | None) -> bool:
    return bool(text) and text != DEFINITION_UNAVAILABLE


def assemble_day(
    date: dt.date,
    word_a: str,
    word_b: str,
    *,
    content: DailyContent | None,
    definitions: Mapping[str, str | None],
    serve_fallback: bool,
) -> tuple[WordPairResponse, bool]:
    """Build one day's response from stored rows. No database or LLM calls.

    `definitions` maps lowercased words to their text. With `serve_fallback`, a missing
    contemplation is filled with the deterministic fallback (never saved). Returns the response
    and whether it is complete: real contemplation, settled quote, both definitions.
    """
    orientation = orient(word_a, word_b)
    higher, lower, known = orientation.higher, orientation.lower, orientation.known
    higher_definition = definitions.get(higher.strip().lower())
    lower_definition = definitions.get(lower.strip().lower())

    contemplation = content.contemplation if content is not None else None
    if contemplation is None and serve_fallback:
        contemplation = fallback_contemplation(higher, lower)

    complete = (
        is_complete(content)
        and _definition_usable(higher_definition)
        and _definition_usable(lower_definition)
    )
    response = WordPairResponse(
        date=date,
        word_a=higher,
        word_b=lower,
        word_a_definition=normalize_dashes(higher_definition),
        word_b_definition=normalize_dashes(lower_definition),
        quote=normalize_dashes(content.quote if content is not None else None),
        quote_author=normalize_dashes(content.quote_author if content is not None else None),
        contemplation=normalize_dashes(contemplation),
        higher=higher,
        lower=lower,
        pair_key=pair_key(higher, lower),
        axis=known.axis if known is not None else None,
        lower_level=known.lower_level if known is not None else None,
        higher_level=known.higher_level if known is not None else None,
        quote_source=normalize_dashes(content.quote_source if content is not None else None),
        meaning=None,
        previous_dates=[],
    )
    return response, complete


async def _reset_session(session: AsyncSession) -> None:
    try:
        await session.rollback()
    except Exception:  # noqa: BLE001
        logger.warning("Session rollback failed", exc_info=True)


async def build_day(
    session: AsyncSession,
    provider: LLMProvider,
    date: dt.date,
    *,
    allow_generate: bool = True,
    force: bool = False,
) -> tuple[WordPairResponse, bool]:
    """The day's full response, generating what is missing when `allow_generate` is set.

    Only the pair itself is required. Definitions and daily content are extras: if one fails,
    the response still carries the pair and every legacy field it can, marked incomplete.
    `force` resets the daily-content retry backoff (used by backfill).
    """
    word_service = WordService(session, provider)
    if allow_generate:
        pair = await word_service.ensure_pair_for_date(date)
    else:
        pair = await word_service.get_pair_for_date(date)
    if pair is None:
        raise LookupError(f"No word pair stored for {date.isoformat()}")
    # Read these now: a rollback below would expire the row.
    word_a, word_b = pair.word_a, pair.word_b

    try:
        orientation = orient(word_a, word_b)
        higher, lower = orientation.higher, orientation.lower
        axis = orientation.known.axis if orientation.known is not None else None
    except Exception:  # noqa: BLE001
        logger.exception("Could not orient %s/%s; using the stored order", word_a, word_b)
        higher, lower, axis = word_a, word_b, None

    definitions: dict[str, str | None] = {}
    definition_service = DefinitionService(session, provider)
    for word in (higher, lower):
        try:
            definitions[word.strip().lower()] = await definition_service.get_definition(
                word, allow_generate=allow_generate
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to load the definition of %s for %s", word, date)
            await _reset_session(session)

    content: DailyContent | None = None
    try:
        content = await DailyContentService(session, provider).get_or_create(
            date, higher, lower, axis=axis, allow_generate=allow_generate, force=force
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to load daily content for %s", date)
        await _reset_session(session)

    try:
        return assemble_day(date, word_a, word_b, content=content, definitions=definitions, serve_fallback=True)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to assemble %s; serving the pair alone", date)
        return WordPairResponse(
            date=date,
            word_a=higher,
            word_b=lower,
            word_a_definition=normalize_dashes(definitions.get(higher.strip().lower())),
            word_b_definition=normalize_dashes(definitions.get(lower.strip().lower())),
        ), False


async def bump_content_epoch(session: AsyncSession) -> str:
    """Change the content epoch so every instance drops its cached word-of-day response. The
    caller commits."""
    store = SettingsStore(session)
    current = await store.get_int(CONTENT_EPOCH_KEY, 0) or 0
    value = str(current + 1)
    await store.set_value(CONTENT_EPOCH_KEY, value)
    return value
