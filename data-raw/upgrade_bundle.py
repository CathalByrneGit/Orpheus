"""One-shot: rewrite the R-era bundle in the ontologySpecR format.

    python3 data-raw/upgrade_bundle.py inst/bundles/contract-core-0.1.0.json \
        orpheus/bundles/contract-core-0.2.0.json

The heavy lifting is `orpheus.bundle.normalise()`, which is the same code that
loads a legacy bundle at runtime -- so this script cannot produce something the
loader would not accept. What it adds on top are the two parts of the spec the
R bundle had no way to express: `queries` and `actions`.

Superseded once make_bundle.py exists; kept because it documents the migration.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orpheus.bundle import normalise, validate  # noqa: E402

# ---------------------------------------------------------------------------
# Queries
#
# These were hardcoded in the Datasette config generator, which meant a
# domain-neutral engine shipped a fixed set of contract-flavoured questions. In
# the bundle they are domain content, and a planning-application bundle asks its
# own questions instead.
#
# `{{instanceUnion}}` is expanded by the config generator into a UNION ALL over
# the managed instance tables. It exists because instance_index deliberately
# does not carry review status: status lives on the instance row, and copying it
# into the index would mean two places to keep in step.
# ---------------------------------------------------------------------------

QUERIES = [
    {
        "id": "needs_review",
        "display": {"name": "Documents still needing review"},
        "returns": {"kind": "table"},
        "definition": {
            "kind": "sql",
            "body": (
                "SELECT d.document_id, d.filename, d.doc_type, d.review_status,\n"
                "       COUNT(ii.instance_id) AS instances\n"
                "FROM documents d\n"
                "LEFT JOIN instance_index ii ON ii.document_id = d.document_id\n"
                "WHERE d.review_status = 'unreviewed'\n"
                "GROUP BY d.document_id\n"
                "ORDER BY d.date_added DESC"
            ),
        },
    },
    {
        "id": "low_confidence_unconfirmed",
        "display": {"name": "Unconfirmed findings at or below the inferred rubric level"},
        "returns": {"kind": "table"},
        "definition": {
            "kind": "sql",
            "body": (
                "SELECT i.type_id, i.instance_id, i.document_id, p.excerpt, p.confidence\n"
                "FROM instance_index i\n"
                "JOIN provenance p ON p.instance_id = i.instance_id\n"
                "WHERE p.confidence <= 0.5\n"
                "ORDER BY p.confidence ASC, i.created_at DESC"
            ),
        },
    },
    {
        "id": "stale_evaluations",
        "display": {"name": "Analyses invalidated by a later amendment"},
        "returns": {"kind": "table"},
        "definition": {
            "kind": "sql",
            "body": (
                "SELECT evaluation_id, concept_id, kind, target_document_id,\n"
                "       stale_reason, generated_at\n"
                "FROM concept_evaluations\n"
                "WHERE stale = 1\n"
                "ORDER BY generated_at DESC"
            ),
        },
    },
    {
        "id": "cloud_calls",
        "display": {"name": "What has been sent to the cloud model"},
        "returns": {"kind": "table"},
        "definition": {
            "kind": "sql",
            "body": (
                "SELECT c.created_at, c.purpose, c.document_id, d.filename,\n"
                "       c.actor_id, c.prompt_chars, c.excerpt_only, c.model\n"
                "FROM llm_calls c\n"
                "LEFT JOIN documents d ON d.document_id = c.document_id\n"
                "WHERE c.tier = 'cloud'\n"
                "ORDER BY c.created_at DESC"
            ),
        },
        "extensions": {"orpheus": {"allow": {"is_admin": 1}}},
    },
    {
        "id": "extraction_accuracy_by_confidence",
        "display": {"name": "Does the confidence rubric actually rank reliability?"},
        "returns": {"kind": "table"},
        "definition": {
            "kind": "sql",
            "body": (
                "-- Joins through provenance, which is what keeps rule-raised flags out:\n"
                "-- a concept flag has no provenance row, because it is not an extraction.\n"
                "-- Give concept flags provenance and this starts reporting rule\n"
                "-- precision as extraction accuracy.\n"
                "SELECT p.confidence,\n"
                "       COUNT(*) AS reviewed,\n"
                "       SUM(CASE WHEN x.status = 'confirmed' THEN 1 ELSE 0 END) AS confirmed,\n"
                "       SUM(CASE WHEN x.status = 'amended'   THEN 1 ELSE 0 END) AS amended,\n"
                "       SUM(CASE WHEN x.status = 'rejected'  THEN 1 ELSE 0 END) AS rejected,\n"
                "       ROUND(1.0 * SUM(CASE WHEN x.status = 'confirmed' THEN 1 ELSE 0 END)\n"
                "             / COUNT(*), 3) AS accuracy\n"
                "FROM provenance p\n"
                "JOIN (\n"
                "{{instanceUnion}}\n"
                ") x ON x.instance_id = p.instance_id\n"
                "WHERE x.status IN ('confirmed', 'amended', 'rejected')\n"
                "GROUP BY p.confidence\n"
                "ORDER BY p.confidence DESC"
            ),
        },
        "extensions": {"orpheus": {"expand": ["instanceUnion"]}},
    },
    {
        "id": "amendment_trail",
        "display": {"name": "Every human correction, newest first"},
        "returns": {"kind": "table"},
        "definition": {
            "kind": "sql",
            "body": (
                "SELECT seq, edited_at, edited_by, table_name, row_id, action,\n"
                "       previous_value, new_value, note\n"
                "FROM edit_history\n"
                "WHERE action IN ('amend', 'confirm', 'reject')\n"
                "ORDER BY seq DESC"
            ),
        },
    },
    {
        "id": "rule_concept_precision",
        "display": {"name": "How often does each rule concept point at something real?"},
        "returns": {"kind": "table"},
        "definition": {
            "kind": "sql",
            "body": (
                "SELECT flag_type AS concept_id,\n"
                "       COUNT(*) AS raised,\n"
                "       SUM(CASE WHEN status IN ('confirmed', 'amended') THEN 1 ELSE 0 END) AS upheld,\n"
                "       SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END) AS dismissed,\n"
                "       SUM(CASE WHEN status = 'unconfirmed' THEN 1 ELSE 0 END) AS unreviewed\n"
                "FROM instances_Flag\n"
                "WHERE raised_by_pass = 'concept'\n"
                "GROUP BY flag_type\n"
                "ORDER BY dismissed DESC"
            ),
        },
    },
]

# ---------------------------------------------------------------------------
# Actions
#
# ontologySpecR models an action as targets + parameters + effects, which is
# very nearly a tool definition. That is the point of declaring them: the review
# verbs become the tools an agent may call, generated from the bundle rather
# than written twice. `implementation.kind = "http"` says they are reached
# through the API, never as raw SQL -- which is the whole argument in
# docs/datasette-ecosystem.md, expressed where a machine can read it.
# ---------------------------------------------------------------------------

REVIEWABLE = ["Contract", "Company", "Person", "Clause", "KeyDate",
              "MonetaryAmount", "Obligation", "Flag"]

ACTIONS = [
    {
        "id": "confirm_instance",
        "display": {"name": "Confirm",
                    "description": "Keep the machine's value and record that a person checked it."},
        "targets": REVIEWABLE,
        "parameters": [
            {"id": "instance_id", "type": "string", "required": True},
            {"id": "note", "type": "string"},
        ],
        "effects": [{"kind": "update", "notes": "status becomes 'confirmed'"}],
        "implementation": {"kind": "http", "entrypoint": "POST /instances/{instance_id}/confirm"},
    },
    {
        "id": "amend_instance",
        "display": {"name": "Amend",
                    "description": "Replace one or more property values, keeping the original in the history."},
        "targets": REVIEWABLE,
        "parameters": [
            {"id": "instance_id", "type": "string", "required": True},
            {"id": "changes", "type": "json", "required": True},
            {"id": "note", "type": "string"},
        ],
        "effects": [
            {"kind": "update", "notes": "status becomes 'amended', source becomes 'human'"},
            {"kind": "emit", "notes": "an edit_history row carrying the previous values"},
        ],
        "implementation": {"kind": "http", "entrypoint": "POST /instances/{instance_id}/amend"},
    },
    {
        "id": "reject_instance",
        "display": {"name": "Reject",
                    "description": "Exclude a finding without deleting it."},
        "targets": REVIEWABLE,
        "parameters": [
            {"id": "instance_id", "type": "string", "required": True},
            {"id": "note", "type": "string"},
        ],
        "effects": [{"kind": "update", "notes": "status becomes 'rejected'; the row is excluded, not removed"}],
        "implementation": {"kind": "http", "entrypoint": "POST /instances/{instance_id}/reject"},
    },
]


def main(src: str, dest: str) -> None:
    bundle = normalise(json.loads(Path(src).read_text()))
    bundle["bundleVersion"] = "0.2.0"
    bundle["queries"] = QUERIES
    bundle["actions"] = ACTIONS
    bundle.setdefault("metadata", {})["description"] = (
        "Public-sector contracts: the worked example. The engine is domain-"
        "neutral; replacing this file replaces the domain."
    )
    validate(bundle)
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    Path(dest).write_text(json.dumps(bundle, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {dest}")
    print(f"  objects {len(bundle['objects'])}  links {len(bundle['links'])}"
          f"  interfaces {len(bundle['interfaces'])}"
          f"  concepts {len(bundle.get('concepts', []))}"
          f"  queries {len(bundle['queries'])}  actions {len(bundle['actions'])}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
