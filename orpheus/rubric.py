"""The controlled vocabularies, and the rubric every confidence is snapped onto.

Kept in its own module with no imports of its own, because everything else
depends on it and nothing it says depends on anything else.
"""

from __future__ import annotations

# The platform deliberately does not store arbitrary floats. Every confidence
# is one of five rubric levels, so "0.7" means the same thing to every reviewer
# rather than being an opaque model score.
CONFIDENCE = {
    "explicit": 1.0,     # stated verbatim in the document
    "named": 0.9,        # clearly named with its attributes listed
    "implied": 0.7,      # mentioned as a concept with structure implied
    "inferred": 0.5,     # inferred from surrounding context
    "speculative": 0.2,  # speculative
}

_LEVELS = sorted(CONFIDENCE.values())

SOURCES = ("ai_local", "ai_cloud", "human")
STATUSES = ("unconfirmed", "confirmed", "amended", "rejected")
REVIEWED_STATUSES = ("confirmed", "amended", "rejected")
EXCLUDED_STATUSES = ("rejected",)

ACTIONS = ("view", "edit", "share", "delete")
SHARE_ROLES = ("viewer", "editor")
VISIBILITY = ("private", "department", "organisation")
CLOUD_POLICIES = ("disabled", "opt_in", "enabled")

# Every naive-matched corpus result carries this, so nothing downstream can
# mistake name matching for entity resolution.
NAIVE_RESOLUTION = "naive_unresolved"

# Columns the platform owns on every instance table. A bundle may not declare
# a property with one of these names -- it would collide with the review state.
RESERVED_PROPS = (
    "instance_id", "document_id", "source", "confidence", "status",
    "amended_by", "amended_at", "created_at",
)


def snap_confidence(score: float | None) -> float:
    """Snap an arbitrary score onto the rubric.

    Extraction backends return arbitrary floats, and storing those directly
    would quietly abandon the rubric. Snapping is **downward-biased**: a score
    is promoted to a level only when it is at least that level, so the pipeline
    never reports more certainty than the backend claimed. A missing score is
    treated as `inferred` rather than as the top of the scale.
    """
    if score is None:
        return CONFIDENCE["inferred"]
    try:
        value = float(score)
    except (TypeError, ValueError):
        return CONFIDENCE["inferred"]
    if value != value:  # NaN
        return CONFIDENCE["inferred"]
    value = max(0.0, min(1.0, value))
    eligible = [lvl for lvl in _LEVELS if lvl <= value + 1e-9]
    return max(eligible) if eligible else min(_LEVELS)


def confidence_label(value: float) -> str:
    """Name the rubric level, or `"unknown"` for anything off-rubric."""
    for name, level in CONFIDENCE.items():
        if abs(level - float(value)) < 1e-9:
            return name
    return "unknown"
