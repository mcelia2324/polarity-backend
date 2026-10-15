from __future__ import annotations

import re

# A run of long dashes (U+2012 to U+2015: figure, en, em, horizontal bar) or double hyphens, with
# or without spaces around it, or a hyphen with spaces on both sides (a hyphen used as a dash).
# Hyphenated words like "self-respect" are left alone.
_DASH_RUN_RE = re.compile(r"\s*(?:[‒-―]|-{2,})+\s*|\s+-\s+")


def normalize_dashes(text: str | None) -> str | None:
    """Replace dashes with plain punctuation.

    Long dashes are a giveaway that text was machine-written, so we strip them from anything
    user-facing (definitions, quotes, the daily contemplation). Generated text is validated so it
    never contains one; this is the last safety net, applied at the API boundary so it also
    cleans content that was generated and stored before the rule existed.
    """
    if not text:
        return text
    out = _DASH_RUN_RE.sub(", ", text)
    out = re.sub(r"\s+,", ",", out)  # "word ," -> "word,"
    out = re.sub(r",\s*(?=[,.;:!?])", "", out)  # ",," -> ","  and ", ." -> "."
    out = re.sub(r"^\s*,\s*|\s*,\s*$", "", out)  # a dash at either end leaves a stray comma
    return out
