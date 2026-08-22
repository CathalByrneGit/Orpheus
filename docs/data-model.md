← [Back to index](index.md)

# Data model

Everything Orpheus knows lives in one SQLite file. This page is the reference
for what is in it, why each table exists, and the two vocabularies —
the confidence rubric and the review statuses — that every AI-sourced row
carries.

---

## The ontology bundle is the schema

Instance tables are not hand-written. They are generated from an **ontology
bundle**: a JSON document listing object types, their properties, link types,
and seed concept definitions. The shipped bundle is
`inst/bundles/contract-core-0.1.0.json`, reproducible from
`data-raw/make_bundle.R`, and replaceable wholesale by `dis_to_bundle()` output
from a real `ontologyDiscoverR` discovery run.

`orph_apply_bundle_schema()` turns the bundle into DDL: one table per managed
object type, one column per declared property. It is idempotent, and it adds
columns the bundle has gained since — which is what makes accepting a schema
amendment a live operation rather than a redeployment.

### One bundle, three readers

The bundle is read by three packages that spell the same ideas differently. It
carries every spelling, so each package works unmodified:

| Concept | `ontologyDiscoverR` | `conceptR` | `objectSetsR` |
|---|---|---|---|
| Object type list | `object_types` | `object_types` | `objects` / `object_types` |
| Backing table | — | `table_name` | `source$table` |
| Primary key | `primary_key` | `primary_key` | `primaryKey` / `primary_key` |
| Link endpoints | `from_type_id` / `to_type_id` | — | `from` / `to` |
| Join keys | — | — | `join$fromKeys` / `join$toKeys` |

`orph_validate_bundle()` checks all of these up front, including that the
duplicated pairs agree. Without that check a mismatch surfaces much later, deep
inside whichever package needed the spelling that was missing.

### Object types

| Type | Table | What it holds |
|---|---|---|
| `Contract` | `instances_Contract` | The agreement: name, reference, value, dates, procedure, governing law |
| `Company` | `instances_Company` | A legal entity party to or named in the agreement, including public bodies |
| `Person` | `instances_Person` | A named individual |
| `Clause` | `instances_Clause` | A numbered or titled provision |
| `Obligation` | `instances_Obligation` | A duty a clause places on a party |
| `Flag` | `instances_Flag` | An issue raised against a document or clause |
| `KeyDate` | `instances_KeyDate` | A date found in the text, with the phrase that introduced it |
| `MonetaryAmount` | `instances_MonetaryAmount` | A monetary value found in the text |
| `Relationship` | `edges` | An extracted link between two instances |

`KeyDate` and `MonetaryAmount` exist so the deterministic pass has somewhere to
write a finding with its own page-level provenance, rather than overwriting a
`Contract` property a model also populated. Two independent readings of the same
fact stay visible as two rows.

`Relationship` is backed by the hand-written `edges` table and marked
`x_orpheus.managed = false`, so schema generation leaves it alone. It exists as
an object type because `objectSetsR` traverses links by joining object tables on
key columns — a many-to-many link cannot be traversed in one hop, but it can be
reached in two through the edge itself.

### Interfaces: asking one question across several types

An interface is a contract several object types share, so a question spans them
without the list of types being written at the call site — which is how a new
object type silently stops being included in an answer.

| Interface | Implemented by | The question it answers |
|---|---|---|
| `Reviewable` | every extracted type | "What has not been checked yet?" |
| `Named` | `Company`, `Person` | "Does this name appear anywhere else?" |
| `PageAnchored` | `Clause`, `KeyDate`, `MonetaryAmount` | "What can I point at on this page?" |

```r
orph_object_set_by_interface(con, "Named")
#>   instance_id  document_id  name                  naive_key         status       type_id
#> 1 inst_8e00…   doc_45ee…    Meridian Systems Ltd  meridian systems  unconfirmed  Company
#> 2 inst_0463…   doc_45ee…    Aoife Nolan           aoife nolan       unconfirmed  Person
```

Rows are projected to the interface's properties only, so every row has the
same shape whichever type it came from; `type_id` says which that was.
Type-specific properties (`role`, `job_title`) are deliberately not projected —
if a caller needs those it is asking a type question, not an interface one.

`orph_validate_bundle()` refuses a bundle where a type declares an interface it
cannot satisfy. An unchecked interface is worse than none: the cross-type query
would fail at runtime, or quietly return fewer rows than it should.

`Relationship` implements nothing — it is an edge, not an extracted instance,
and its primary key is `edge_id`.

This is `ontologySpecR`'s `interface_type` and `objectSetsR`'s
`object_set_by_interface()`, carried across. The corpus escalation uses it: a
name is looked up across every `Named` type, so a name that is a company in one
document and a person in another is reported as a `cross_type_match` rather than
filtered out before anyone sees it.

### Every instance table has the same tail

| Column | Type | Meaning |
|---|---|---|
| `instance_id` | TEXT | Primary key |
| `document_id` | TEXT | Document this was extracted from |
| `source` | TEXT | `ai_local`, `ai_cloud`, or `human` |
| `confidence` | REAL | A rubric level — see below |
| `status` | TEXT | `unconfirmed`, `confirmed`, `amended`, `rejected` |
| `amended_by` | TEXT | Actor who last changed it |
| `amended_at` | TEXT | When |
| `created_at` | TEXT | Store bookkeeping, not a bundle property |

> **`provenance` is the immutable record; the instance row is the current one.**
> Amending an instance overwrites its `confidence` and `source` with the human's
> values, because after a correction the row is ground truth. The provenance row
> keeps what the machine originally claimed, which is what makes extraction
> quality measurable after people start correcting things.

`source`, `confidence` and `status` are declared as **bundle properties**, not
hidden metadata. `objectSetsR` projects an object set down to declared
properties only, so a query that cannot see `status` cannot exclude rejected
rows — which would make every corpus-wide answer quietly wrong.

---

## The confidence rubric

Confidence is never an arbitrary float. It is one of five levels, so that "0.7"
means the same thing to every reviewer:

| Value | Label | Meaning |
|---|---|---|
| `1.0` | `explicit` | Stated verbatim and unambiguously |
| `0.9` | `named` | Clearly named with its attributes present |
| `0.7` | `implied` | Mentioned, with structure implied |
| `0.5` | `inferred` | Inferred from surrounding context |
| `0.2` | `speculative` | Speculative |

Extraction backends return arbitrary scores, so `orph_snap_confidence()` snaps
every score to the nearest level **at the persistence boundary**, biased
downward: a score is promoted to a level only if it is at least that level. A
model's `0.83` is stored as `0.7`, never as `0.9`. A missing confidence becomes
`0.5` — inferred — rather than being treated as certain.

---

## Review statuses

| Status | Set by | Meaning |
|---|---|---|
| `unconfirmed` | extraction | No person has looked at this |
| `confirmed` | `orph_confirm_instance()` | A person agrees with it as it stands |
| `amended` | `orph_amend_instance()` | A person changed it; the previous value is in `edit_history` |
| `rejected` | `orph_reject_instance()` | Excluded from downstream use, never deleted |

`ontologyDiscoverR` uses its own `pending`/`approved` vocabulary. It is
translated in the adapter (`R/ontology_stack.R`) so the store has one vocabulary
rather than two.

---

## Tables

### Documents and text

| Table | Purpose |
|---|---|
| `documents` | One row per ingested file: hash, storage path, classification, visibility, document-level `review_status` |
| `document_pages` | One row per page: `text`, `text_source` (`native` / `ocr` / `needs_ocr`), `image_path`, `char_count` |
| `document_shares` | Per-actor share grants: `role` is `viewer` or `editor` |

`documents.file_hash` carries a unique index: dedup is on content, so the same
contract mailed round twice under different names is one document.

### Instances and links

| Table | Purpose |
|---|---|
| `instances_*` | One table per object type, generated from the bundle |
| `instance_index` | `instance_id → type_id, table_name, document_id`. The type-agnostic handle: makes "find this instance" one lookup instead of a scan across every type table |
| `edges` | Extracted relationships, with the same provenance tail as instances |
| `provenance` | `instance_id, document_id, source_label, page_no, excerpt, confidence, source` — where each fact came from, and what the machine claimed at the time |

### Interpretation

| Table | Purpose |
|---|---|
| `concept_evaluations` | Rule results, narrative analyses and corpus comparisons, distinguished by `kind` |
| `concept_evaluation_dependencies` | Which instances an evaluation read — this is what makes `stale` automatic |
| `concept_definitions`, `concept_versions` | Created and owned by `conceptR` |

`concept_evaluations` carries `scope` (`document` or `database`),
`resolution_quality` (set to `naive_unresolved` for corpus results),
`corpus_context_used`, and `stale` with a `stale_reason`.

### Audit

| Table | Purpose |
|---|---|
| `edit_history` | Append-only. Every change, with `previous_value` and `new_value` as JSON |
| `llm_calls` | Every model call: tier, model, purpose, prompt size, payload digest, and any error |
| `schema_amendments` | Properties and types seen during population but not in the bundle |
| `extraction_runs` | One row per extraction attempt, including failures |

Both `edit_history` and `llm_calls` carry a `seq INTEGER PRIMARY KEY
AUTOINCREMENT`. Ordering is by `seq`, never by timestamp: several changes inside
one transaction share a timestamp to the second, and an audit trail that
reports them in an arbitrary order cannot answer the question it exists for.

### Identity and configuration

| Table | Purpose |
|---|---|
| `actors` | Authenticated users and API agents, with `departments_json` and `is_admin` |
| `actor_tokens` | Bearer tokens, stored as SHA-256 hashes only |
| `bundles` | Every registered bundle version, with `stage` and `is_active` |
| `org_settings` | Deployment settings, notably `cloud_ai_policy` and `cloud_send_mode` |

---

## How it fits together

```mermaid
erDiagram
  documents ||--o{ document_pages : "has pages"
  documents ||--o{ document_shares : "shared with"
  documents ||--o{ instance_index : "yields instances"
  documents ||--o{ edges : "yields links"
  documents ||--o{ concept_evaluations : "analysed by"
  documents ||--o{ extraction_runs : "extracted by"

  instance_index ||--|| instances_Contract : "one per type table"
  instance_index ||--o{ provenance : "evidenced by"
  instance_index ||--o{ concept_evaluation_dependencies : "depended on by"

  concept_evaluations ||--o{ concept_evaluation_dependencies : "reads"

  actors ||--o{ actor_tokens : "authenticates with"
  actors ||--o{ edit_history : "makes changes"
  actors ||--o{ llm_calls : "triggers"

  bundles ||--o{ schema_amendments : "amended by"
```

---

[← Back to index](index.md) | [Next: Pipeline walkthrough →](pipeline-walkthrough.md)
