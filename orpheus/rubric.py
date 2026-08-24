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

# The other axis. `confidence` says how sure the machine is; `STATUSES` says
# whether a person has checked. Neither can express "two people checked, they
# disagree, and both are right" -- every value in both vocabularies resolves
# toward a single answer. TENSION_STATUSES is that missing axis.
#
# `accepted` is a terminal state on purpose. Most review vocabularies have no
# way to stop at "this conflict is real", so a reviewer's only exits are to
# pick a side or leave it looking unreviewed; both bury the finding.
TENSION_STATUSES = ("open", "accepted", "resolved", "withdrawn")
SETTLED_TENSIONS = ("resolved", "withdrawn")

# Where a tension came from. `SOURCES` plus `lint`, which is neither a model nor
# a person: the detector compares values that are already in the store and
# invents nothing, so calling it `ai_local` would overstate what it did.
TENSION_SOURCES = SOURCES + ("lint",)

# What kind of conflict. Deliberately about the *shape* of the disagreement
# rather than about contracts, so a bundle from another domain describes its
# own conflicts in the same four words.
TENSION_KINDS = (
    # Two sources give different values for the same property of one thing.
    "conflicting_value",
    # Both sources claim to govern the same question.
    "competing_authority",
    # One source can be read two ways and the readings differ materially.
    "ambiguous_wording",
    # Something is verifiably so and nothing explains why. The residual, and
    # the honest place for "this surprised me and I could not resolve it".
    "unexplained",
)

ACTIONS = ("view", "edit", "share", "delete")
SHARE_ROLES = ("viewer", "editor")
# Matches datasette-paper's three levels, which is not a coincidence: the same
# shape solves the same problem, and it is the plugin that would enforce these
# row by row in Datasette.
VISIBILITY = ("private", "link-view", "link-edit")
CLOUD_POLICIES = ("disabled", "per_user", "org_allow")

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
