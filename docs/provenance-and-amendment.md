← [Back to index](index.md)

# Provenance and amendment

This is the part of Orpheus that everything else rests on. Extraction quality is
the whole point of Phase 1, and extraction quality is unmeasurable unless the
system remembers what the machine said, what the person changed it to, and who
that person was.

---

## The problem

An extraction pipeline that overwrites is a pipeline you cannot improve. If a
reviewer corrects "Meridian Systems Limited" to "Meridian Systems Ltd." and the
old value is gone, three things are lost at once:

- **The audit answer.** In a public-sector setting, "who changed this, when, and
  from what" is a question that will be asked.
- **The training signal.** Every correction is a labelled example of where
  extraction went wrong. Overwriting throws away the only dataset that could
  tell you whether the pipeline is getting better.
- **The trust signal.** A row that reads the same whether a model guessed it or a
  person verified it is a row nobody can rely on.

So nothing is destructively overwritten, and every fact says where it came from.

---

## The four states

```mermaid
stateDiagram-v2
  [*] --> unconfirmed: extraction writes<br/>source = ai_local / ai_cloud
  unconfirmed --> confirmed: orph_confirm_instance()<br/><i>values unchanged</i>
  unconfirmed --> amended: orph_amend_instance()<br/><i>source becomes human</i>
  unconfirmed --> rejected: orph_reject_instance()<br/><i>excluded, not deleted</i>
  confirmed --> amended: a later correction
  amended --> amended: a further correction
  rejected --> [*]: stays queryable as evidence

  note right of amended
    previous value preserved
    in edit_history
    confidence becomes 1.0
  end note
```

Three deliberate choices in that diagram:

**Confirming does not change `source`.** The row stays `ai_local`; a person
agreeing with a machine does not make it a human observation. Only `status` and
`amended_by` change.

**Amending does change `source`, to `human`, at confidence `1.0`.** A human
correction is ground truth. Leaving the model's `0.7` in place would mean a
corrected row still read as a machine guess.

**Rejecting never deletes.** The row is excluded from `orph_document_instances()`,
from concept evaluation and from corpus matching, but it stays in the table. A
rejected extraction is evidence about extraction quality.

---

## The audit trail

Every mutation writes to `edit_history` **inside the same transaction as the
change it describes**, so a change and its audit row commit or roll back
together.

| Column | Meaning |
|---|---|
| `seq` | Monotonic. This, not `edited_at`, defines order |
| `table_name`, `row_id` | What changed |
| `document_id` | For document-scoped audit views |
| `action` | `ingest`, `classify`, `extract`, `confirm`, `amend`, `reject`, `superseded`, `evaluate`, `share_granted`, … |
| `previous_value`, `new_value` | JSON |
| `edited_by`, `edited_at`, `note` | Who, when, why |

`orph_row_history()` gives one row's story; `orph_document_history()` gives
everything that has happened to a document and to every instance extracted from
it — the view an auditor asks for.

> **Why `seq` and not a timestamp.** Timestamps are stored to the second, and
> several changes inside one transaction share one. Ordering an audit trail by a
> random identifier as a tiebreak means it reports events in an arbitrary order —
> precisely the question it exists to answer.

### The honest limitation

SQLite has no storage-level time travel. `edit_history` does that job at the
application level, which is a **weaker guarantee** than a snapshotting store
would give: a code path that forgot to log would be a hole a storage-layer
guarantee would not have. This is mitigated by routing every mutation through
`record_edit()` and letting nothing write to an instance table directly, but the
gap is real and understood rather than accidental. See
[Open decisions](open-decisions.md#storage-migration-to-ducklake).

---

## Two levels of review

The architecture left open whether review should be per-instance only or also
document-level. **Both are implemented**, because they answer different
questions.

| Level | Function | Question it answers |
|---|---|---|
| Per-instance | `orph_confirm_instance()` etc. | Has *this fact* been checked? |
| Per-document | `orph_mark_document_reviewed()` | Has anyone been through *the whole thing*? |

Marking a document reviewed **does not** confirm its instances. Conflating the
two would let one click silently promote every unchecked machine guess in the
document to `confirmed` — turning the review flag from a signal into noise.
Instead the result reports what is still outstanding:

```r
orph_mark_document_reviewed(con, doc, actor_id = "act_nuala")
#> $review_status         "reviewed"
#> $unconfirmed_instances 6
#> $note                  "Marked reviewed with 6 instance(s) still unconfirmed."
```

`orph_review_progress()` gives the counts by status at any time.

---

## Schema amendments

When population produces a property the bundle does not declare, it is **not
dropped**. Silently discarding it would lose exactly the signal that tells you
the schema is wrong. It becomes a candidate in `schema_amendments`.

This happens in two places, deliberately: the engine flags what it noticed, and
`insert_instance()` flags anything undeclared regardless of what the engine
said. A property cannot reach an instance table without either being declared or
being queued.

| Amendment type | Raised when | Accepting it |
|---|---|---|
| `new_property` | A property is not declared on a known type | Adds the property to the bundle, bumps the patch version, adds the column live |
| `new_type` | An object type is not in the bundle | Recorded only |
| `new_link_type` | A link type is not in the bundle | Recorded only |

Repeat sightings **increment `occurrences`** rather than queueing duplicates: a
property appearing in forty contracts is one decision, and its frequency is the
strongest argument for accepting it. The queue is sorted by `occurrences`
descending.

`new_type` and `new_link_type` are not auto-applied. A new object type needs
properties, keys and a place in the model that only a person can decide;
accepting one records the decision and leaves the bundle edit deliberate.

**Deciding an amendment is an administrator action**, not an ordinary review:

```r
orph_review_schema_amendment(con, id, "accepted", actor_id = "act_admin")
#> $applied_to_bundle TRUE
#> $bundle_version    "0.1.1"
```

Accepting one changes the bundle for every document, not one row. The API
enforces this with a 403 for non-admin actors.

---

## Staleness

An interpretation built on a fact that has since changed is not wrong so much as
out of date — and the difference matters, because silently stale is worse than
visibly stale.

`concept_evaluation_dependencies` records which instances each evaluation
actually read. Amending or rejecting any of them calls
`mark_dependent_evaluations_stale()`, which sets `stale = 1` and a
`stale_reason` on every evaluation that depended on it.

```mermaid
flowchart LR
  I["instances_Contract<br/>signature_block_present = 'no'"]
  E["concept_evaluations<br/>narrative: risk = high"]
  D[("concept_evaluation_<br/>dependencies")]

  I -->|"read during analysis"| D
  D --> E

  A["orph_amend_instance()<br/>→ 'yes'"] --> I
  A -.->|"mark_dependent_<br/>evaluations_stale()"| E

  E --> S["stale = 1<br/>stale_reason = 'Instance … was amended.'"]

  style S fill:#8b3a3a,color:#fff
```

Without the dependency table, `stale` could only ever be set by hand. With it,
the Datasette `stale_evaluations` query is a live worklist of analyses that need
re-running.

---

## Measuring extraction quality

Everything above preserves the machine's value beside the human's. This is what
that is for.

Phase 1's definition of done is extraction *good enough to trust as a
foundation*. That is a claim about a number, and until the number exists the
claim cannot be made either way. `orph_quality_report()` computes it from data
the store already holds — every reviewed row is a labelled example, so there is
nothing to sample.

| Function | Question it answers |
|---|---|
| `orph_extraction_quality()` | How often did extracted facts survive review, by type, confidence and tier? |
| `orph_confidence_calibration()` | Does a higher rubric level actually mean a more reliable fact? |
| `orph_concept_precision()` | How often does each rule concept point at something real? |
| `orph_property_corrections()` | Which fields do people keep having to fix? |
| `orph_quality_report()` | All of the above, plus a readiness verdict |

### Three things it refuses to do

**It never counts an unreviewed row as correct.** An `unconfirmed` instance is
an unknown one. Every rate is computed over the reviewed subset only, and
reported next to the `coverage` it rests on. Below 20% coverage the report
declines to give a verdict at all:

```r
orph_quality_report(con)$readiness
#> $state "insufficient_review"
#> $note  "Only 8% of instances have been reviewed. Too little to judge extraction on."
```

**It attributes a correction to the confidence the machine originally gave it.**
Amending a row sets its `confidence` to `1.0` and `source` to `human` — correct,
because the row is ground truth afterwards. But grouping by those values would
report every correction as a full-confidence success, inverting the very thing
being measured. The original values come from `provenance`, which is written
once at extraction and never amended.

**It keeps rule flags out of the extraction figures.** A concept-raised `Flag`
carries confidence `explicit` because a SQL expression evaluated true, which
says nothing about extraction. Mixing them in makes a coarse rule look like an
extraction failure — and makes the rubric look inverted when it is fine. Rules
are measured separately, by precision.

### What it looks like when it works

```r
orph_quality_report(con)$by_confidence
#>   confidence_label n_reviewed accuracy amend_rate reject_rate
#> 1         explicit         12    0.917      0.083       0.000
#> 2            named         16    0.812      0.188       0.000
#> 3          implied          8    0.500      0.250       0.250
#> 4         inferred          8    0.250      0.250       0.500
```

That is the rubric earning its place: accuracy falls as confidence falls. When
it does not, the report says so plainly rather than leaving it to be noticed:

```r
orph_confidence_calibration(con)$note
#> "A higher rubric level scored worse than a lower one. The rubric is not
#>  ranking reliability here -- treat the levels as labels, not as a ranking,
#>  until this resolves."
```

A rubric that does not rank correctly is worse than no rubric, because people
trust it.

### Rule precision

```r
orph_concept_precision(con)
#>          concept_id n_raised n_reviewed n_upheld n_dismissed precision
#> 1   open_ended_term        8          8        1           7     0.125
#> 2  missing_signature        8          8        6           2     0.750
#> 3 uncapped_liability        8          8        7           1     0.875
```

`open_ended_term` firing on eight documents and being dismissed seven times is
the signal to tighten its expression — or to accept that a missing `end_date`
usually means failed extraction rather than an open-ended contract, which is
exactly what its seed rationale predicted.

This measures precision only. **Nothing in the store knows about the issues a
concept failed to raise**, so recall is unmeasurable here, and reporting a
number for it would be worse than the gap.

### Where to read it

| Route | Access |
|---|---|
| `GET /quality` | Administrator — corpus figures span documents an actor may not be able to read |
| `GET /documents/<id>/quality` | Anyone who can view that document |

In Datasette, the `extraction_accuracy_by_confidence` and
`rule_concept_precision` canned queries cover the same ground.

---

## The cloud gate

Cloud processing is opt-in, and an opt-in nobody can audit afterwards is a
formality. Two independent conditions must both hold before any text leaves the
building:

| Condition | Where it lives | Values |
|---|---|---|
| Org policy allows it at all | `org_settings.cloud_ai_policy` | `disabled` (default), `per_user`, `org_allow` |
| This request explicitly asked | `opt_in` / `cloud_opt_in` | must be `TRUE` |

**The default is `disabled`.** A deployment handling sensitive contract data
should have to turn cloud processing on deliberately, not discover it was on.
Whether the toggle belongs to each user or to the organisation is an open
question, so both policies exist and neither is assumed — see
[Open decisions](open-decisions.md#cloud-ai-opt-in-policy).

**Policy alone never authorises a call.** Even under `org_allow`, a request
without `opt_in = TRUE` is refused. That is what stops cloud becoming a silent
default the moment an administrator relaxes the policy.

The gate is checked *before* any document text is prepared, so a blocked request
sends nothing and logs nothing.

### The audit log

Every call — local and cloud, success and failure — is written to `llm_calls`:

| Recorded | Not recorded |
|---|---|
| tier, provider, model, purpose | the prompt text |
| `prompt_chars`, `excerpt_only` | the document content |
| SHA-256 `payload_digest` | |
| `actor_id`, `document_id`, `created_at`, `error` | |

The digest is enough to prove what left the building without storing the
contract text a second time. The `cloud_calls` canned query in Datasette is the
administrator's view of it.

---

[← Back to index](index.md) | [Next: API reference →](api-reference.md)
