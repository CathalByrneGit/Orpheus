"""Locating an extracted span in the document it came from.

This is what makes the extraction engines interchangeable. LangExtract computes
alignment itself; GLiNER2 is extractive and returns offsets by construction; a
general LLM returns neither and will happily quote text the document does not
contain. Rather than trust each engine's own account of itself, Orpheus locates
every span in the source and scores what it finds.

**Grounding is computed, not trusted.** That is the whole of the confidence
rubric's top level -- "stated verbatim in the document" is a fact about the
text, checkable, and not a number a model chose for itself.
"""

from __future__ import annotations

import re
import unicodedata

# The vocabulary is LangExtract's, so one mapping table serves every engine.
MATCH_EXACT = "match_exact"
MATCH_GREATER = "match_greater"
MATCH_LESSER = "match_lesser"
MATCH_FUZZY = "match_fuzzy"
UNGROUNDED = None

_WS = re.compile(r"\s+")


def _normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    # Word processors turn quotes and dashes into characters a model often
    # reproduces in its plain-ASCII form, which is a difference of typography
    # rather than of fact.
    for fancy, plain in (("’", "'"), ("‘", "'"), ("“", '"'),
                         ("”", '"'), ("–", "-"), ("—", "-"),
                         (" ", " ")):
        text = text.replace(fancy, plain)
    return _WS.sub(" ", text).strip().lower()


def align(text: str, span: str, hint: int | None = None) -> tuple[int | None, int | None, str | None]:
    """Locate `span` in `text`, returning `(start, end, status)`.

    `hint` is a character offset to prefer when a span occurs more than once —
    an engine that reported an approximate position gets to keep it.
    """
    if not span or not text:
        return None, None, UNGROUNDED

    # 1. Verbatim.
    for start in _all_occurrences(text, span):
        if hint is None or abs(start - hint) < 200:
            return start, start + len(span), MATCH_EXACT
    first = text.find(span)
    if first != -1:
        return first, first + len(span), MATCH_EXACT

    # 2. Same text, different typography or spacing. The document says it; the
    #    engine merely retyped it.
    located = _find_normalised(text, span)
    if located is not None:
        start, end = located
        # Narrower or wider than what was asked for, but the same words.
        status = MATCH_LESSER if (end - start) < len(span) else MATCH_GREATER
        if (end - start) == len(span):
            status = MATCH_EXACT
        return start, end, status

    # 3. A long enough run of the span's own words, in order. Weak, and scored
    #    as weak.
    located = _find_partial(text, span)
    if located is not None:
        return located[0], located[1], MATCH_FUZZY

    # 4. Not in the document. The engine asserted it.
    return None, None, UNGROUNDED


def _all_occurrences(text: str, span: str):
    start = text.find(span)
    while start != -1:
        yield start
        start = text.find(span, start + 1)


def _find_normalised(text: str, span: str) -> tuple[int, int] | None:
    """Match on normalised text, then map the result back to real offsets."""
    target = _normalise(span)
    if not target:
        return None
    flat, offsets = _flatten(text)
    position = flat.find(target)
    if position == -1:
        return None
    start = offsets[position]
    end_index = min(position + len(target) - 1, len(offsets) - 1)
    return start, offsets[end_index] + 1


def _flatten(text: str) -> tuple[str, list[int]]:
    """Normalised text alongside, for each character, its offset in the original."""
    out: list[str] = []
    offsets: list[int] = []
    previous_space = True
    for index, char in enumerate(text):
        normalised = _normalise(char)
        if normalised == "":
            if not previous_space and out:
                out.append(" ")
                offsets.append(index)
                previous_space = True
            continue
        out.append(normalised)
        offsets.append(index)
        previous_space = False
    return "".join(out), offsets


def _find_partial(text: str, span: str, minimum_words: int = 3) -> tuple[int, int] | None:
    """Longest leading run of the span's words that does appear."""
    words = _normalise(span).split()
    if len(words) < minimum_words:
        return None
    for length in range(len(words), minimum_words - 1, -1):
        located = _find_normalised(text, " ".join(words[:length]))
        if located is not None:
            return located
    return None
