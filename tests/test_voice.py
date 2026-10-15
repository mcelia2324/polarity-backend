import pytest

from app.services.text_utils import normalize_dashes
from app.services.voice import (
    DASH_RE,
    banned_words,
    clean_generated_text,
    has_word,
    sentences,
    voice_errors,
    word_count,
)


@pytest.mark.parametrize(
    "text",
    [
        "Fear—courage",  # em dash, bare
        "fear — courage",  # em dash, spaced
        "fear–courage",  # en dash, bare
        "fear – courage",  # en dash, spaced
        "fear‒courage",  # figure dash
        "fear―courage",  # horizontal bar
        "fear--courage",
        "fear -- courage",
        "fear - courage",  # a hyphen used as a dash
    ],
)
def test_dash_variants_are_caught(text):
    assert DASH_RE.search(text)
    assert voice_errors(text)


@pytest.mark.parametrize("text", ["self-respect", "a well-worn habit", "two-sided"])
def test_hyphenated_words_are_not_dashes(text):
    assert not DASH_RE.search(text)
    assert voice_errors(text) == []


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Every journey starts here.", "journey"),
        ("Your journeys matter.", "journeys"),
        ("Try embracing the quiet.", "embracing"),
        ("Let us delve into it.", "delve"),
        ("A rich tapestry of days.", "tapestry"),
        ("You navigated it well.", "navigated"),
        ("That resonates with you.", "resonates"),
        ("A deep resonance.", "resonance"),
        ("Unlocking your best self.", "unlocking"),
        ("An empowering choice.", "empowering"),
        ("A profoundly simple act.", "profoundly"),
        ("This is sacred ground.", "sacred"),
        ("A Transformative week.", "transformative"),
        # Proposed list, enforced for now.
        ("In the realm of habits.", "realm"),
        ("Fostering patience at work.", "fostering"),
        ("Nurture the small things.", "nurture"),
        ("A holistic view.", "holistic"),
        ("It is a testament to you.", "testament"),
        ("A beacon for others.", "beacon"),
        ("A vibrant morning.", "vibrant"),
        ("A myriad of small choices.", "myriad"),
        ("Unleash your patience.", "unleash"),
        ("Try leaning into it.", "leaning into"),
        ("She holds space for you.", "holds space"),
        ("Calibrate your day.", "calibrate"),
        ("As Hawkins wrote.", "hawkins"),
        ("Your consciousness shifts.", "consciousness"),
    ],
)
def test_banned_stems_with_inflections(text, expected):
    assert banned_words(text) == [expected]
    assert any(expected in error for error in voice_errors(text))


def test_banned_matching_respects_word_boundaries():
    # "lean" and "space" alone are fine; only the phrases are banned.
    assert banned_words("Lean on a friend and give it some space.") == []


def test_pair_words_are_exempt_from_the_banned_list():
    text = "What is sacred to you is worth a small act of reverence."
    assert banned_words(text) == ["sacred"]
    assert banned_words(text, exempt=("reverence", "sacred")) == []
    # A simple plural of a pair word is exempt too.
    assert banned_words("Two journeys.", exempt=("journey",)) == []
    assert banned_words("Two journeys.") == ["journeys"]
    # The exemption covers the word itself, not other banned words.
    assert banned_words("A sacred journey.", exempt=("sacred",)) == ["journey"]


@pytest.mark.parametrize(
    "text,fragment",
    [
        ("Take 3 slow breaths.", "digit"),
        ("Pause; then breathe.", "semicolon"),
        ("Try it today!", "exclamation"),
        ("A calm day \U0001F60A.", "emoji"),
        ("A calm day ✨.", "emoji"),
        ('He said "enough" and left.', "quotation"),
        ("He said “enough” and left.", "quotation"),
    ],
)
def test_other_rejections(text, fragment):
    errors = voice_errors(text)
    assert any(fragment in error for error in errors), errors


def test_clean_text_passes():
    text = "Notice where fear showed up today. Ask what courage would do next, and try one small piece of that."
    assert voice_errors(text, exempt=("courage", "fear")) == []


def test_word_count():
    assert word_count("") == 0
    assert word_count(None) == 0
    assert word_count("Don't let self-respect slip, ever.") == 5
    assert word_count("  spaced   out  words ") == 3
    assert word_count("Søren Kierkegaard wrote it") == 4


def test_sentences():
    assert sentences("One. Two? Three!") == ["One.", "Two?", "Three!"]
    assert sentences("Only one sentence") == ["Only one sentence"]
    assert sentences("  ") == []
    assert sentences("First one.  Second one.") == ["First one.", "Second one."]


@pytest.mark.parametrize(
    "text,word,expected",
    [
        ("Fear shows up.", "fear", True),
        ("Old fears return.", "fear", True),
        ("A fear's grip.", "fear", True),
        ("Old losses.", "loss", True),
        ("A fearful day.", "fear", False),
        ("Distrust grows.", "trust", False),
        ("TRUST it.", "trust", True),
        ("", "trust", False),
    ],
)
def test_has_word(text, word, expected):
    assert has_word(text, word) is expected


def test_clean_generated_text():
    assert clean_generated_text('  "Wrapped in quotes."  ') == "Wrapped in quotes."
    assert clean_generated_text("“Curly.”") == "Curly."
    assert clean_generated_text("Line one.\n\nLine two.") == "Line one. Line two."
    assert clean_generated_text(None) == ""


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("fear — courage", "fear, courage"),
        ("fear—courage", "fear, courage"),
        ("fear–courage", "fear, courage"),  # the bare en dash the old version missed
        ("fear – courage", "fear, courage"),
        ("fear‒courage", "fear, courage"),
        ("fear―courage", "fear, courage"),
        ("fear--courage", "fear, courage"),
        ("fear -- courage", "fear, courage"),
        ("fear - courage", "fear, courage"),
        ("fear——courage", "fear, courage"),
        ("fear, — courage", "fear, courage"),
        ("the end —.", "the end."),
        ("— Seneca", "Seneca"),
        ("Seneca —", "Seneca"),
        ("self-respect stays", "self-respect stays"),
        ("no dashes here.", "no dashes here."),
        ("", ""),
        (None, None),
    ],
)
def test_normalize_dashes(raw, expected):
    assert normalize_dashes(raw) == expected
