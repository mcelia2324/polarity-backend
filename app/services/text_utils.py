from __future__ import annotations


def normalize_dashes(text: str | None) -> str | None:
    """Replace em/en dashes and double hyphens with plain punctuation.

    Long dashes are a giveaway that text was machine-written, so we strip them from anything
    user-facing (definitions, quotes, the daily contemplation). Applied at the API boundary so it
    also cleans content that was generated and stored before this rule existed.
    """
    if not text:
        return text
    for sep in (" — ", " – ", " -- ", "—", "--"):
        text = text.replace(sep, ", ")
    return text.replace(" ,", ",").replace(",,", ",")
