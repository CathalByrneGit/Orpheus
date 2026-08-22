← [Back to index](index.md)

# Open decisions

What Phase 1 deliberately did not decide, and what the build corrected. Each
entry says what is open, what was built so the decision stays cheap, and what
would settle it.

---

## R stack vs. a Python rebuild

**Status: open, and the largest decision on this list.**

Phase 1 was specified as building on the existing R ontology stack rather than
re-implementing its ideas. That was the right default — the stack already has the
discover → populate pattern, the confidence rubric, schema amendment candidates
and versioned concepts, and all of that is genuinely reusable thinking. But the
stack has not been exercised much in anger, and rebuilding the same ideas in
Python is a live alternative.

This build produced concrete evidence rather than opinion, so it is recorded
here.

### What was verified to work

| Package | Result |
|---|---|
| `conceptR` | **Installed cleanly and worked as documented.** Versioned SQL concepts, activation, deprecation and evaluation all behaved. 37 tests exercise it for real, not against a double. Its dependencies are only `DBI`, `jsonlite`, `cli`. |

### What could not be exercised

| Package | Blocker |
|---|---|
| `ontologySpecR` | Imports `jsonvalidate`, which needs V8. Not installable in this environment. |
| `objectSetsR` | Imports `ontologySpecR`, so blocked behind it. |
| `ontologyDiscoverR` | Imports `ellmer`, plus `pdftools` for `parse_pdf()`. |

None of these are fatal in a real deployment — they are ordinary dependencies.
But three of the four packages could not be run at all here, which is itself
information about how much friction the stack carries.

### Interop findings

These came out of building against the packages' actual source, and they are the
substantive input to the decision:

1. **Two incompatible bundle shapes share one class.**
   `ontologySpecR::bundle()` emits `bundleId` / `objects` / `links`;
   `ontologyDiscoverR::dis_to_bundle()` emits `bundle_id` / `object_types` /
   `link_types`. Both are classed `ontology_bundle`. A consumer cannot tell them
   apart by class, and each of the three consumers reads a different subset of
   the field names. The shipped bundle carries **every spelling** and
   `orph_validate_bundle()` asserts the duplicated pairs agree — see
   [Data model](data-model.md#one-bundle-three-readers). That works, but it is a
   workaround for an inconsistency inside the stack.

2. **`pop_add_file()` would silently discard OCR text.** It re-parses the file
   from disk with `pdftools`, so feeding it a scanned document would throw away
   the OCR pass and extract from nothing — on exactly the documents where
   extraction quality matters most. Orpheus bypasses it by constructing the
   `DiscoverySource` from stored page text and injecting it into
   `pop_sess$sources`, which relies on an internal shape rather than the public
   API.

3. **Vocabulary mismatches at the boundary.** The stack uses
   `pending` / `approved`; this platform's amendment model needs
   `unconfirmed` / `confirmed` / `amended` / `rejected`. The stack documents a
   confidence rubric but `pop_extract()` defaults to `0.8` and passes arbitrary
   floats through. Both are translated in the adapter.

4. **`objectSetsR` traverses links by joining object tables on key columns**, so
   a many-to-many link cannot be traversed in one hop. Modelling the edge table
   as its own object type works, but it is a shape the ontology has to adopt to
   suit the query library.

5. **`conceptR` interpolates `sql_expr` directly into SQL.** Concept authoring is
   therefore a privileged operation; the API restricts it to administrators.

### Why the decision is cheap to reverse

The extraction engine is reached through exactly one file,
`R/ontology_stack.R`, behind `orph_populate()`, with `orph_set_populator()` as
the injection point. Everything downstream — persistence, provenance, the
amendment model, concepts, permissions, the API — works on a normalised shape
and never sees the stack. The test suite proves this: all 543 tests pass with
`ontologyDiscoverR` absent, driving a substitute engine through the same
interface.

So the realistic options are not "R" or "Python" wholesale:

| Option | What it costs |
|---|---|
| Keep the R stack | Nothing further. It is wired up and the adapter absorbs its inconsistencies. |
| Rebuild extraction in Python, keep this store and API | A new implementation behind `orph_populate()`, reached over HTTP or a subprocess. Nothing else changes. |
| Rebuild the whole platform in Python | Everything here is rewritten. The design carries over; the code does not. |

The middle option is available precisely because the adapter exists, and it is
worth noting that the two things the stack contributes most — the rubric and the
schema-amendment pattern — are **ideas already re-implemented here**, in
`orph_snap_confidence()` and the `schema_amendments` queue. What the stack
uniquely provides today is `ontologyDiscoverR`'s population prompt-and-validate
loop and `conceptR`'s version lifecycle.

**What would settle it:** run a real discovery and population pass over a genuine
contract sample with `ontologyDiscoverR` installed and a live model, and judge
extraction quality and the friction of `dis_review()`. That is the test the stack
has not had, and it is the only evidence that matters. Until then, note that
`conceptR` — the one package that could be exercised — did not cause a single
problem.

---

## Direction: Datasette as the primary surface

**Status: a direction, not a decision. Nothing here is built, and Phase 1 does
not depend on it.**

The intended shape is to lean into the Datasette ecosystem rather than build a
bespoke UI, keep the R stack reachable over HTTP, and make the central
interaction a **person and a model classifying a document together as they read
it** — annotation as a conversation in the Datasette UI, not a batch job whose
output is inspected afterwards.

### What already points this way

More of this exists than it might look:

| Already true | Why it matters here |
|---|---|
| The Plumber API is the single writer, over HTTP | "R reached via an API" is the current architecture, not a change to it |
| Datasette already reads the store, with permissions emitted from one place | The read surface is in place and cannot drift from the API's rules |
| Every AI-sourced row lands `unconfirmed` with provenance | This *is* the co-classification loop: the model proposes, the person disposes |
| `orph_classify()` already writes `classification_status = 'unconfirmed'` | Classification is already a proposal awaiting a human, not a verdict |
| `datasette-paper` is already the model for per-document sharing | The same project is prior art for the UI |

So the gap is a write path from the Datasette UI, and incremental
rather than whole-document classification.

### The one thing that must not be got wrong

**A Datasette plugin must call the Plumber API, never write SQLite directly and
never call a model itself.**

Both shortcuts are tempting and both silently dismantle guarantees Phase 1
enforces:

- Writing SQLite directly from a plugin makes Datasette a second writer. The
  advisory lock refuses a second *Orpheus* writer, but it cannot stop a plugin
  opening its own connection — and the WAL/single-writer reasoning stops holding
  the moment it does.
- Calling a model directly from a plugin bypasses the cloud gate, the org
  policy, the per-request opt-in and the `llm_calls` audit log in one step. The
  gate is enforced in the API. A plugin that reaches around it means a document
  can go to a cloud model with no record that it did, which is the specific
  failure the opt-in exists to prevent.

Routed through the API, both problems disappear and the plugin stays thin.

### What would need designing

- **Incremental classification.** `orph_classify()` is one-shot over the whole
  document. Reading-companion behaviour wants per-page or per-passage proposals
  as the reader moves, which is a different call shape and a different unit of
  provenance.
- **Write-back from a read-only surface.** Datasette is deliberately read-only
  here. The plugin becomes the only writing client, and its auth has to resolve
  to the same actor the API knows — see the identity-provider decision above,
  which this makes more pressing rather than less.
- **Latency.** A batch pass can take seconds; a companion reacting to scrolling
  cannot. That is what the local tier is for, with the cloud tier reserved for
  on-demand questions.

This overlaps heavily with what agents.md scopes as Phase 3, and with
`ontologyMCP`. The difference worth noting is the surface: agents.md imagines a
bespoke split-view reading pane, and this direction puts the same interaction
inside Datasette instead. Deciding between them is the actual open question, and
it does not need answering yet.

---

## The rest of the ontology stack: what was taken, what was left

The account holds fifteen ontology-family packages. They were surveyed once so
that this does not have to happen again. `ontologyR` is out of scope by
instruction.

**Taken: one idea, from `auditR`.** It samples a concept's output, records human
judgments and computes disagreement rates. Orpheus does not need its sampling —
review here is exhaustive, so every reviewed row is already a labelled example —
but the underlying point was a real gap: Phase 1 turns on extraction being good
enough, and nothing measured it. That gap is now closed by
[`orph_quality_report()`](provenance-and-amendment.md#measuring-extraction-quality),
built on data the store already held. The package itself is not a dependency.

**Left, as later phases:**

| Package | Belongs to |
|---|---|
| `ontologyMCP` — MCP server, 10 tools | Phase 3, the reading companion. agents.md already names it |
| `vertexR` — graph, alert rules, risk propagation | Phase 5. Substantially more than the graph work Phase 5 anticipates |
| `ontologyConnectR` — REST/FHIR/JDBC/GraphQL, plus `live_icij` | Phase 5. An ICIJ Offshore Leaks connector is squarely conflict-of-interest territory |
| `actionTypesR`, `machineryR` — action lifecycle, state machines | Could back the review workflow later; Phase 1's four statuses do not need them |
| `lineageR` — source/transform/column lineage | Overlaps staleness tracking, but at dataset level rather than fact level |
| `datapond` — DuckLake | The storage migration above |

**A caveat on all of it.** Commit counts suggest how much has actually been
exercised: `datapond` 172, `explicaR` 38, `actionTypesR` 13, most others 4–6 —
and `auditR`, `sqlglotR`, `ontologyAPI`, `ontologyMCP` and `objectExploreR` have
**one or two commits each**. Those read as generated once and never run. Treat
the list as available ideas rather than as working machinery until something has
been exercised.

**Two findings that bear on the R-versus-Python question above:**

- `ontologyAPI` already exposes ontology queries over plumber, but its auth is a
  **single shared API key with no actor concept**. It could not have backed
  Orpheus's per-document permissions, which need `created_by`, share grants and
  `amended_by` tied to a real person. Building a separate API was right; mounting
  ontologyAPI alongside for generic querying is still an option.
- `sqlglotR` wraps Python SQLGlot through reticulate. The stack is **already not
  pure R**, so "R or Python" was never the real choice.

---

## Decisions the build made, and why

These were open in the architecture and are now settled in code. They can still
be changed; they are recorded so the reasoning is not lost.

### Document-level review state

**Settled: both, and they are kept separate.**

Per-instance status answers "has this fact been checked"; the document flag
answers "has anyone been through the whole thing". Marking a document reviewed
deliberately does **not** confirm its instances — conflating them would let one
click promote every unchecked machine guess to `confirmed`. The result reports
what is still outstanding instead. See
[Provenance and amendment](provenance-and-amendment.md#two-levels-of-review).

### Cloud AI opt-in policy

**Partly settled: the mechanism is built, the policy is a runtime setting that
defaults to off.**

Both candidate policies exist (`per_user`, `org_allow`) and neither is assumed.
The default is `disabled`, so a deployment handling sensitive contract data has
to enable cloud processing deliberately rather than discover it was on. Policy
alone never authorises a call — an explicit per-request `opt_in` is always
required as well.

**What would settle it:** whether an individual user may opt a sensitive document
into cloud processing on a shared server is a question for the organisation's
information-governance people, not a technical one.

### OCR approach

**Deliberately not settled. A provider registry was built instead.**

`orph_set_ocr_provider()` takes any `f(image_path) -> text`. Built-in fallbacks
are the `tesseract` R package then a `tesseract` binary. With no provider, pages
are marked `needs_ocr` rather than passed off as empty.

**What would settle it:** evaluating candidates against real scanned
contracts — the accuracy difference on poor-quality government scans is the whole
question, and it cannot be answered from documentation.

### Identity provider

**Deliberately not settled. The bridge was built instead.**

`orph_upsert_actor(con, idp, external_id, ...)` is where any provider lands.
Tokens work today. `orph_permission_sql()` emits the row-level rule for whichever
Datasette plugin is chosen.

**What would settle it:** confirming what the deployment target actually runs —
Entra ID, Okta, a government SSO, or nothing yet.

### Permission boundaries

**Mechanism settled, rules deliberately not.**

Owner + visibility + share-table, following `datasette-paper`. Actors carry
`departments_json` so a `datasette-acl` dynamic group can key off it the moment
department rules exist — but no department rule is invented, because guessing
would bake a wrong rule into the schema.

**What would settle it:** scoping with real stakeholders whether the boundary is
department, sensitivity tag, document owner, or a combination.

### `bundle_diff()` and staging concept versions

**Partly addressed.** `orph_register_bundle(stage = "staging")` stores a bundle
without activating it, and a staging bundle cannot be activated by accident.
`conceptR` already versions and deprecates concepts, so an evaluation always
points at a version that still exists.

A true `bundle_diff()` is upstream work in the R stack — and whether it lands is
entangled with the R-vs-Python decision above.

---

## Revisit when triggered

Deferred with an explicit trigger rather than a date.

### Storage migration to DuckLake

**Trigger: the application-level audit trail proves insufficient, or write load
outgrows the single-writer pattern.**

SQLite was chosen for Phase 1 because it is Datasette's first-class format and
needs no glue code. Two gaps are being accepted knowingly:

| Gap | What carries it instead | Weaker how |
|---|---|---|
| No storage-level time travel | The `edit_history` table | A code path that forgot to log would be a hole a storage guarantee would not have |
| No bundle versioning tied to data state | `concept_id` / `concept_version` columns | Application-level rather than a snapshot |

The mitigation is that every mutation routes through `record_edit()` inside the
same transaction as the change, and nothing writes to an instance table
directly. That is a convention enforced by code review, not by the storage
engine.

Migrating a working SQLite schema into DuckLake later is a much smaller job than
building the plumbing speculatively now. Note that a Datasette↔DuckLake access
plugin would also be needed, and was previously scoped as custom work.

### Vector and similarity search

**Trigger: a concrete, requested need — "find clauses like this one across
contracts".**

Not built, and not a reason to introduce LanceDB now. If it happens, DuckDB's
vector extension is the more consistent path, and it is entangled with the
storage decision above.

---

## Corrections this build made to the architecture

Two places where the specification, followed literally, would not have worked.

### `--immutable` is wrong for a live WAL store

The architecture asks for WAL mode *and* for Datasette in `--immutable` mode.
**These are incompatible.** `immutable=1` makes SQLite skip the WAL, so an
immutable reader sees the database as of the last checkpoint — nothing at all on
a freshly written store, with no error raised.

Measured: 0 documents visible immutable-without-checkpoint, 1 with a read-only
connection, 1 immutable after `wal_checkpoint(TRUNCATE)`.

Orpheus serves read-only without the flag, and checkpoints after every write. See
[Deployment](deployment.md#the-wal-and-immutable-mode-trap).

### Function names in the architecture do not match the packages

The architecture refers to `ont_evaluate()` carrying lineage automatically. The
function is `conceptR::cpt_evaluate()`, and it does **not** carry lineage — it
evaluates one SQL expression against one table and returns a data frame.

Lineage is therefore built here: `concept_evaluation_dependencies` records which
instances each evaluation read, which is what makes `stale` automatic rather than
something a person has to notice. Worth knowing when reading the architecture
alongside the code.

---

## Still out of scope for Phase 1

Unchanged from the architecture: entity resolution and alias merging, the
cross-document relationship graph, conflict-of-interest and procurement views,
and the live reading-pane companion.

The one deliberate exception is the step 9 corpus escalation, which is
best-effort naive name matching, labelled `naive_unresolved` on every result. It
is a stepping stone to Phase 4 resolution, and it should be **replaced** by real
resolution rather than patched indefinitely. `orph_naive_key()` has a test
asserting its known failure — `"Ernst & Young"` and `"Ernst and Young"` produce
different keys — so the limitation cannot quietly disappear.

---

[← Back to index](index.md)
