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
_SUFFIXES = re.compile(
    r"\b(limited|ltd|plc|llp|llc|inc|incorporated|company|co|group|holdings|the)\b"
)
_SPACE = re.compile(r"\s+")


def naive_key(name: Any) -> str:
    """Normalise a name for naive cross-document matching.

    Deliberately crude: lowercase, strip punctuation, drop common company
    suffixes, collapse whitespace. This is a stepping stone to entity
    resolution, not entity resolution, and anything built on it must be
    labelled unresolved -- see `rubric.NAIVE_RESOLUTION`. Its known failure is
    tested rather than hidden: "Ernst & Young" and "Ernst and Young" produce
    different keys, because the ampersand becomes a space and the word does not.
    """
    text = str(name or "").lower()
    text = _PUNCT.sub(" ", text)
    text = _SUFFIXES.sub(" ", text)
    return _SPACE.sub(" ", text).strip()
