"""Shared voice rules for every piece of generated text Polarity shows.

The prompts ask for the voice; these checks enforce it. Generated text that breaks a rule is
rejected and retried (with the errors fed back), never patched. `normalize_dashes` in text_utils
stays as the last safety net at the API boundary for older stored text.

Voice: a calm person talking to a friend. Plain words, short sentences, no dashes, no therapy or
AI-tell words, no numbers or scores.
"""
from __future__ import annotations

import re
from typing import Iterable

# Figure dash, en dash, em dash, horizontal bar (U+2012 to U+2015), a double hyphen, or a hyphen
# with spaces around it (a hyphen used as a dash). Hyphenated words like "self-respect" pass.
DASH_RE = re.compile(r"[‒-―]|--|\s-\s")

# Banned stems, matched case-insensitively on word boundaries, so inflections are caught
# (journeys, embracing, navigated, empowerment...).
BANNED_HARD: tuple[str, ...] = (
    r"journey\w*",
    r"embrac\w*",
    r"delv\w*",
    r"tapestr\w*",
    r"navigat\w*",
    r"resonat\w*",
    r"resonance",
    r"unlock\w*",
    r"empower\w*",
    r"profound\w*",
    r"sacred",
    r"transformat\w*",
)
# Proposed additions, still waiting on the owner's confirmation. Enforced for now; drop a stem
# here to allow it again.
BANNED_PROPOSED: tuple[str, ...] = (
    r"realms?",
    r"foster\w*",
    r"nurtur\w*",
    r"holistic\w*",
    r"testaments?",
    r"beacons?",
    r"vibrant\w*",
    r"myriad",
    r"unleash\w*",
    r"lean(?:s|ed|ing)? into",
    r"hold(?:s|ing)? space",
    r"calibrat\w*",
    r"hawkins",
    r"consciousness",
)
BANNED_RE = re.compile(r"\b(?:" + "|".join(BANNED_HARD + BANNED_PROPOSED) + r")\b", re.IGNORECASE)

# The banned list as plain words, for prompts ("Never use these words or any form of them: ...").
PROMPT_BANNED_WORDS = (
    "journey, embrace, delve, tapestry, navigate, resonate, unlock, empower, profound, sacred, transformative"
)

DIGIT_RE = re.compile(r"\d")
EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U0001F1E6-\U0001F1FF☀-➿⬀-⯿️‍]"
)
DOUBLE_QUOTES = ('"', "“", "”", "„")

_WORD_RE = re.compile(r"\w+(?:['’-]\w+)*")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def word_count(text: str | None) -> int:
    """Words in the text. A contraction (don't) or hyphenated word (self-respect) counts once."""
    return len(_WORD_RE.findall(text or ""))


def sentences(text: str | None) -> list[str]:
    """Split on sentence-ending punctuation followed by a space. Good enough for the short,
    plain prose we generate (no abbreviations)."""
    return [part for part in _SENTENCE_SPLIT_RE.split((text or "").strip()) if part]


def has_word(text: str | None, word: str) -> bool:
    """True when `word` appears as a whole word, allowing a simple plural (fear, fears; loss,
    losses). Case-insensitive. Longer words that merely contain it (fearful) do not count."""
    if not text or not word:
        return False
    return re.search(rf"\b{re.escape(word.strip())}(?:s|es)?\b", text, re.IGNORECASE) is not None


def banned_words(text: str | None, exempt: Iterable[str] = ()) -> list[str]:
    """Banned words found in the text, lowercased, in order of appearance. The pair's own words
    are exempt (with a simple plural), so a pair like sacred/profane can still name itself."""
    allowed: set[str] = set()
    for word in exempt:
        w = (word or "").strip().lower()
        if w:
            allowed.update({w, f"{w}s", f"{w}es"})
    found: list[str] = []
    for match in BANNED_RE.finditer(text or ""):
        hit = match.group(0).lower()
        if hit not in allowed and hit not in found:
            found.append(hit)
    return found


def voice_errors(text: str | None, exempt: Iterable[str] = ()) -> list[str]:
    """Every common voice rule the text breaks, as short plain sentences that can be fed back to
    the model in a retry. An empty list means the text passes."""
    text = text or ""
    errors: list[str] = []
    if DASH_RE.search(text):
        errors.append("It uses a dash. Use a period or a comma instead.")
    for word in banned_words(text, exempt):
        errors.append(f"It uses the word {word}. Do not use it or any form of it.")
    if DIGIT_RE.search(text):
        errors.append("It contains a digit. Leave numbers out.")
    if ";" in text:
        errors.append("It contains a semicolon. Use a period or a comma instead.")
    if "!" in text:
        errors.append("It contains an exclamation mark.")
    if EMOJI_RE.search(text):
        errors.append("It contains an emoji.")
    if any(mark in text for mark in DOUBLE_QUOTES):
        errors.append("It contains quotation marks.")
    return errors


def clean_generated_text(raw: str | None) -> str:
    """Trim a plain-text model reply: collapse whitespace and drop quotation marks wrapped around
    the whole reply. Nothing else is changed; anything wrong is left for the validators."""
    text = re.sub(r"\s+", " ", (raw or "")).strip()
    if len(text) >= 2 and text[0] in DOUBLE_QUOTES and text[-1] in DOUBLE_QUOTES:
        text = text[1:-1].strip()
    return text
