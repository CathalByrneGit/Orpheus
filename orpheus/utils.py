"""Identifiers, time, JSON, and the naive name key.

Small enough to be boring, and deliberately so: everything here is called from
everywhere, and none of it should have an opinion.
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timezone
from typing import Any


def new_id(prefix: str = "obj") -> str:
    """A prefixed random hex identifier.

    Prefixes make ids self-describing in the audit trail, which matters when a
    row id turns up in `edit_history` detached from the table it came from.
    """
    return f"{prefix}_{secrets.token_hex(16)}"


def now() -> str:
    """Current UTC time, ISO-8601, second resolution.

    Second resolution is not an oversight: rows written in one transaction
    share a timestamp, which is exactly why `edit_history` is ordered by its
    monotonic `seq` and never by time.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_json(value: Any) -> str | None:
    """Serialise for storage in a TEXT column."""
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=False, default=str)


def from_json(text: Any) -> Any:
    """Deserialise a JSON TEXT column, tolerating NULL and empty string."""
    if text is None or text == "":
        return None
    if not isinstance(text, (str, bytes)):
        return text
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


class OrpheusError(Exception):
    """Base for every error this package raises deliberately.

    Carries a message written for a person, because the API surfaces it
    verbatim and the person reading it is usually not the one who wrote the
    call that failed.
    """


class PermissionDenied(OrpheusError):
    pass


class NotFound(OrpheusError):
    pass


def require_choice(value: Any, choices, arg: str):
    if value not in choices:
        raise OrpheusError(
            f"{arg} must be one of {', '.join(map(str, choices))}, not {value!r}."
        )
    return value


def require_string(value: Any, arg: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OrpheusError(f"{arg} must be a non-empty string.")
    return value


_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)

# Legal forms only, and only where they trail. These say how a company is
# incorporated, not who it is, so two renderings of one name differ only by
# which of them is present: "Halloran Instruments, Inc." and "Halloran
# Instruments Inc" are the same company written twice.
#
# `group` and `holdings` were on this list and are deliberately not any more.
# They are name components, and worse, they denote a *different legal entity*
# in a corporate structure -- a holding company is not its subsidiary. Stripping
# them merged "Kestrel Medical Group" with "Kestrel Medical Ltd", and "Ardmore
# Holdings plc" with "Ardmore Ltd". That is a false merge, which is strictly
# worse than the false split this function is documented as having: a split
# leaves two rows a person can join, while a merge combines two organisations
# and leaves nothing to notice. Conflating a parent with its subsidiary is also
# precisely the error that matters most in procurement and conflict-of-interest
# work, which is where this is heading.
LEGAL_FORMS = _LEGAL_FORMS = (
    "limited", "ltd", "plc", "llp", "llc", "lp", "inc", "incorporated",
    "corporation", "corp", "company", "co", "sa", "nv", "bv", "ag", "gmbh",
    "oy", "ab", "as", "aps", "pty", "dac", "cic", "ug", "kg", "srl", "spa",
    # Irish-registered companies file under the Irish forms too.
    "teoranta", "teo", "cpt", "ct",
)
_TRAILING_FORM = re.compile(
    r"(?:\s+(?:" + "|".join(_LEGAL_FORMS) + r"))+$"
)
_LEADING_THE = re.compile(r"^the\s+")
_SPACE = re.compile(r"\s+")

# A title a person is addressed by, which is not part of their name. The
# corpus run produced "Dr. Mitchell Felder", "Mitchell Felder" and "Mitchell S.
# Felder" as three pages for one man, splitting his three relations across
# them; the first of those differs from the second by nothing but this.
#
# Leading, and repeatedly, for the same reason legal forms are stripped
# trailing and repeatedly: "Prof. Dr. Meier" stacks two.
HONORIFICS = _HONORIFICS = (
    "mr", "mrs", "ms", "miss", "mx", "dr", "prof", "professor", "sir", "dame",
    "lord", "lady", "rev", "reverend", "fr", "hon", "capt", "col", "maj",
    "sgt", "lt", "gen",
)
_LEADING_HONORIFIC = re.compile(
    r"^(?:(?:" + "|".join(_HONORIFICS) + r")\s+)+"
)

#: How a type's names are normalised. `organisation` strips trailing legal
#: forms, `personal` strips leading honorifics. A bundle says which per object
#: type, because the engine has no business knowing that this domain calls its
#: people `Person` -- a planning-applications bundle normalises an applicant's
#: name the same way under a different type id.
NAME_STYLES = ("organisation", "personal")


def naive_key(name: Any, style: str | None = None) -> str:
    """Normalise a name for naive cross-document matching.

    Deliberately crude: lowercase, strip punctuation, drop a leading "the",
    collapse whitespace, and then apply whichever of the two style-specific
    strips the bundle asked for. This is a stepping stone to entity resolution,
    not entity resolution, and anything built on it must be labelled unresolved
    -- see `rubric.NAIVE_RESOLUTION`. Its known failure is tested rather than
    hidden: "Ernst & Young" and "Ernst and Young" produce different keys,
    because the ampersand becomes a space and the word does not.

    Legal forms are stripped trailing, and repeatedly, because "Foo Co Ltd" is
    one company under two stacked forms. Anchoring matters: an unanchored match
    took the "co" out of "Costa Coffee" and the "the" out of "The Boston
    Consulting Group".

    Honorifics are stripped leading, and only for a `personal` name. Doing it
    to every name would take the "Dr" out of "Dr Pepper" -- a false merge is
    strictly worse than a false split, and this function's output is matched on
    for *equality*, so a bad strip here merges silently.

    `style` defaults to `organisation`, which is what every caller did before
    there was a choice, so an older bundle that says nothing keeps its keys.
    """
    text = str(name or "").lower()
    text = _PUNCT.sub(" ", text)
    text = _SPACE.sub(" ", text).strip()
    text = _LEADING_THE.sub("", text)

    if style == "personal":
        stripped = _LEADING_HONORIFIC.sub("", text).strip()
    else:
        stripped = _TRAILING_FORM.sub("", text).strip()
    # A name that is *only* a legal form, or only a title, keeps it: an empty
    # key would match every other empty key.
    return stripped or text
