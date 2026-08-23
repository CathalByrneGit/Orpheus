"""Dates and money, found by pattern rather than asked of a model.

A date printed in a document is a fact about the text, not a judgement. Finding
it with a regex is exact, free, and repeatable, so it is recorded at the top of
the confidence rubric and the model is left to do the work only it can do —
deciding what the date *means*.

The one judgement made here is the role: is this date the start, the end, a
signature date? That is inferred from the **nearest** cue phrase, and the cues
are stems (`"terminat"`, not `"terminate on"`) because a cue written as an exact
phrase misses the ordinary inflection and the next-nearest cue then answers in
its place — which is how a real contract's termination date came to be stored as
its start date.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from .rubric import CONFIDENCE

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Python 3 strings are unicode and the source file is UTF-8, so these can be
# written literally. In R they could not: R source must be ASCII to be portable,
# and a "€" escape was marked UTF-8 while document text read off disk was
# not, so in a C-locale container the two would not match and no euro amount was
# ever found. That failure is worth remembering even though it cannot recur
# here, because "no euro amounts" looks exactly like "a document with no money
# in it".
CURRENCY_SYMBOLS = {"€": "EUR", "$": "USD", "£": "GBP"}

_MONTH_ALT = "|".join(sorted(MONTHS, key=len, reverse=True))
_NUM = r"[0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?"

_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DMY = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_ALT})\.?,?\s+(\d{{4}})\b", re.I)
_MDY = re.compile(
    rf"\b({_MONTH_ALT})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", re.I)
_SLASH = re.compile(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{4})\b")


def _valid(year: int, month: int, day: int) -> bool:
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        return False
    from calendar import monthrange
    return day <= monthrange(year, month)[1]


def find_dates(text: str | None) -> list[dict]:
    """Every date in the text, with the rubric level it earned.

    Slash dates are read day-first, which is right for Irish, UK and EU
    documents and wrong for US ones. Rather than pick a side silently, a date
    that could be read either way (both fields ≤ 12) is recorded at `inferred`
    with its raw text kept, so a reviewer can see what it came from.
    """
    text = text or ""
    found: list[dict] = []
    seen: set[str] = set()

    def add(raw: str, value: str, confidence: float, ambiguous: bool = False,
            position: int = -1) -> None:
        if raw in seen:
            return
        seen.add(raw)
        found.append({"raw_text": raw, "value": value, "confidence": confidence,
                      "ambiguous": ambiguous, "position": position})

    for m in _ISO.finditer(text):
        year, month, day = (int(g) for g in m.groups())
        if _valid(year, month, day):
            add(m.group(0), m.group(0), CONFIDENCE["explicit"], position=m.start())

    for m in _DMY.finditer(text):
        day, month_name, year = m.group(1), m.group(2).lower(), m.group(3)
        month = MONTHS.get(month_name)
        if month and _valid(int(year), month, int(day)):
            add(m.group(0), f"{year}-{month:02d}-{int(day):02d}",
                CONFIDENCE["explicit"], position=m.start())

    for m in _MDY.finditer(text):
        month_name, day, year = m.group(1).lower(), m.group(2), m.group(3)
        month = MONTHS.get(month_name)
        if month and _valid(int(year), month, int(day)):
            add(m.group(0), f"{year}-{month:02d}-{int(day):02d}",
                CONFIDENCE["explicit"], position=m.start())

    for m in _SLASH.finditer(text):
        first, second, year = (int(g) for g in m.groups())
        ambiguous = first <= 12 and second <= 12
        day, month = first, second
        if _valid(year, month, day):
            add(m.group(0), f"{year}-{month:02d}-{day:02d}",
                CONFIDENCE["inferred"] if ambiguous else CONFIDENCE["explicit"],
                ambiguous=ambiguous, position=m.start())

    return found


def _to_number(raw: str) -> float | None:
    cleaned = re.sub(r"[^\d.]", "", raw.replace(",", ""))
    try:
        return float(cleaned)
    except ValueError:
        return None


def find_amounts(text: str | None) -> list[dict]:
    """Every monetary amount, with its currency and how confidently it was read.

    A currency read from a code (`EUR 500,000`) is explicit. One read from a
    word next to the number (`500,000 euro`) is a level lower, because the word
    is beside the number rather than attached to it.
    """
    text = text or ""
    found: list[dict] = []
    seen: set[str] = set()

    def add(raw: str, amount: float | None, currency: str, confidence: float,
            position: int) -> None:
        if amount is None or raw in seen:
            return
        seen.add(raw)
        found.append({"raw_text": raw, "amount": amount, "currency": currency,
                      "confidence": confidence, "position": position})

    for symbol, code in CURRENCY_SYMBOLS.items():
        for m in re.finditer(rf"{re.escape(symbol)}\s?(?:{_NUM})", text):
            add(m.group(0), _to_number(m.group(0)), code,
                CONFIDENCE["explicit"], m.start())

    for m in re.finditer(rf"\b(EUR|USD|GBP)\s?(?:{_NUM})", text):
        add(m.group(0), _to_number(m.group(0)), m.group(1).upper(),
            CONFIDENCE["explicit"], m.start())

    for m in re.finditer(rf"(?:{_NUM})\s?(EUR|USD|GBP)\b", text):
        add(m.group(0), _to_number(m.group(0)), m.group(1).upper(),
            CONFIDENCE["explicit"], m.start())

    for m in re.finditer(rf"(?:{_NUM})\s?(euros?|dollars?|pounds?)\b", text, re.I):
        word = m.group(1).lower()
        code = "EUR" if word.startswith("euro") else "USD" if word.startswith("dollar") else "GBP"
        add(m.group(0), _to_number(m.group(0)), code, CONFIDENCE["named"], m.start())

    return found


# ---------------------------------------------------------------------------
# What a finding is for
# ---------------------------------------------------------------------------

# Matched as literal substrings, so each cue is written as a **stem** to survive
# ordinary inflection: "commenc" catches commences and commencing, where
# "terminate on" caught only the bare infinitive and missed "terminates on" --
# the commonest phrasing there is. A date matching no cue is left `unknown`
# rather than guessed at; one matching the wrong cue is worse than either,
# because it reads as a fact.
DATE_ROLE_CUES = {
    "start": ("commenc", "start date", "effective from", "effective date",
              "with effect from"),
    "end": ("expir", "terminat", "end date", "until", "ceases"),
    "signature": ("signed", "executed", "dated this", "in witness whereof"),
    "milestone": ("milestone", "delivery date", "by no later than", "deadline"),
}

AMOUNT_ROLE_CUES = {
    "contract_value": ("total value", "contract value", "consideration",
                       "contract sum", "total price"),
    "cap": ("aggregate liability", "shall not exceed", "capped at",
            "maximum liability"),
    "penalty": ("liquidated damages", "penalty", "service credit"),
    "rate": ("per day", "per hour", "per annum", "day rate", "hourly rate"),
}


def infer_role(text: str, position: int, cues: dict[str, Iterable[str]],
               window: int = 160) -> str:
    """Label a finding from the nearest cue phrase, not the first one listed.

    In *"Commencing on 1 January 2024 and shall expire on 2026-12-31"* both cues
    are in range of both dates. Nearest-cue is what makes the second one `end`
    rather than `start`.
    """
    if position is None or position < 0:
        return "unknown"
    start = max(0, position - window)
    around = text[start:position + window].lower()
    target = position - start

    best_role, best_distance = "unknown", None
    for role, phrases in cues.items():
        for phrase in phrases:
            offset = around.find(phrase)
            while offset != -1:
                # A cue introduces the value that follows it, so one before the
                # value governs it. Cues after are still considered, at a
                # penalty, for trailing forms ("2026-12-31, the expiry date").
                distance = (target - offset) if offset <= target else (offset - target) * 2
                if best_distance is None or distance < best_distance:
                    best_role, best_distance = role, distance
                offset = around.find(phrase, offset + 1)
    return best_role


def find_all(text: str | None, page_no: int | None = None) -> list[dict]:
    """Both passes over one page, each finding labelled with its role."""
    text = text or ""
    findings = []
    for date in find_dates(text):
        findings.append({
            "kind": "date", "page_no": page_no,
            "role": infer_role(text, date["position"], DATE_ROLE_CUES),
            **date,
        })
    for amount in find_amounts(text):
        findings.append({
            "kind": "amount", "page_no": page_no,
            "role": infer_role(text, amount["position"], AMOUNT_ROLE_CUES),
            **amount,
        })
    return findings
