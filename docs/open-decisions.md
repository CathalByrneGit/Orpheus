← [Back to index](index.md)

# Open decisions

What Phase 1 deliberately did not decide, and what the build corrected. Each
entry says what is open, what was built so the decision stays cheap, and what
would settle it.

---

## R stack vs. a Python rebuild

**Status: decided. The platform is Python; the R implementation is deleted.**

Phase 1 was specified as building on the existing R ontology stack rather than
re-implementing its ideas. That was the right default, and the stack's thinking
— discover → populate, the confidence rubric, schema amendment candidates,
versioned concepts — is genuinely reusable and was reused. What was decided
against is the *stack*, not the ideas.

The R implementation was built first and ran end to end: 709 tests, a live
Plumber API, a Datasette plugin, a real PDF through the whole pipeline. It was
then ported rather than abandoned, and the port is what settled the question.

### What the R build actually found

These came out of building against the packages' source, and they are the
substantive input to the decision:

1. **Two incompatible bundle shapes shared one class.**
   `ontologySpecR::bundle()` emits `bundleId` / `objects` / `links`;
   `ontologyDiscoverR::dis_to_bundle()` emits `bundle_id` / `object_types` /
   `link_types`. Both are classed `ontology_bundle`, so a consumer cannot tell
   them apart, and each of three consumers read a different subset of the field
   names. The R bundle carried **every spelling** and validation asserted the
   duplicated pairs agreed — a workaround for an inconsistency inside the stack,
   and the reason the file was 114 KB.

2. **`pop_add_file()` would silently discard OCR text.** It re-parses the file
   from disk with `pdftools`, so a scanned document would have thrown away the
   OCR pass and extracted from nothing — on exactly the documents where quality
   matters most. Orpheus bypassed it by injecting a `DiscoverySource` built from
   stored page text into `pop_sess$sources`, relying on an internal shape rather
   than the public API.

3. **Vocabulary mismatches at the boundary.** The stack used
   `pending` / `approved` against this platform's four states, and documented a
   confidence rubric while `pop_extract()` defaulted to `0.8` and passed
   arbitrary floats through.

4. **`cpt_add_score_component(version = NULL)` could never work.** Its own
   default is documented as "use whichever version is active at evaluation
   time", but `composite_score_components.version` is `NOT NULL` *and* part of
   the primary key, so the default always failed on insert.

5. Three of the four packages **could not be installed** in a clean environment
   at all: `ontologySpecR` needs V8 via `jsonvalidate`, `objectSetsR` imports it,
   `ontologyDiscoverR` needs `ellmer`. Only `conceptR` installed cleanly and it
   worked exactly as documented — 37 tests exercised it for real.

### What the port changed

| | R | Python |
|---|---|---|
| Bundle | Every list twice, 114 KB, validated for self-agreement | One spelling, 58 KB, [ontologySpecR](https://github.com/CathalByrneGit/ontologySpecR)'s format validated against that project's own schema |
| Extraction | One engine behind an adapter | A registry: `gliner2`, `langextract`, `llm`, `chat` — see [Extraction engines](extraction-engines.md) |
| Grounding | Whatever the engine claimed | Computed: every excerpt located in the source, the rubric level following from match quality |
| Writer | A Plumber API; Datasette read-only beside it | Datasette itself, with the core as a library it imports |
| Processes | Two, plus a token between them | One |
| Dependencies | `DBI`, `RSQLite`, `jsonlite`, `digest`, `rlang`, `cli`, `plumber`, plus four GitHub packages | None. The core is standard library only |
| Tests | 709 | 300, plus a browser loop over real HTTP |

The test count fell because the R suite tested things the Python core does not
have to: the bundle's self-agreement, the adapter's vocabulary translation, and
the interop workarounds above. Removing the need for a test is better than
passing it.

### What the port kept

Everything that was a decision rather than a mechanism. The four-state review
vocabulary, the confidence rubric and its downward-biased snapping, provenance
as the immutable record beside a mutable instance row, `seq`-ordered
append-only history, the two-condition cloud gate, dependency-tracked staleness,
and the schema-amendment queue. Those were the stack's good ideas, and they are
all here — several of them, like the rubric and the amendment queue, were
already re-implemented in Orpheus rather than borrowed.

The R implementation is preserved in git history, and
`tests/fixtures/contract-core-0.1.0.json` keeps its bundle so a store built by
it still opens. A test proves Python opens and migrates an R-built store in
place.

**What is still not settled** is the question this was all supposed to answer,
and no language choice touches it: see *Extraction quality is unmeasured* below.

---

## Scope: a document platform, with contracts as the worked example

**Settled: the engine is domain-neutral and the bundle carries the domain.**

Phase 1 was specified around contracts and the code drifted into assuming them —
`compare_primary_values()` once selected from `instances_Contract` by name,
deterministic findings were linked by a hardcoded `contract_instance_id`, the
classifier's vocabulary was a constant, and a Datasette canned query listed the
instance tables of one domain.

None of that was necessary. The pipeline needs to know *which type plays which
role*, not what the types mean, so a `x_orpheus` block on the bundle now says
so and the four sites read it. See
[the domain block](data-model.md#the-domain-block).

Contracts remain the shipped bundle and the example throughout the docs: it is
the driving use case, and a general platform documented only in the abstract is
harder to understand than one with a worked example. But
`orpheus/bundles/contract-core-0.2.0.json` is data, and replacing it replaces the
domain.

**Kept honest by a test.** `tests/test_domain_neutrality.py` defines a planning-
application bundle — Application, Applicant, Condition, its own decision
codelist, its own rule concept, its own comparable value (floor area in square
metres) — and runs ingest, extraction, deterministic linking, review, concept
evaluation and codelist reporting against it with no code changes. A claim of
generality that nothing exercises tends to stop being true quietly, and this one
had: porting the test found `raise_flag()` hardcoding `instances_Flag`, so any
bundle without a `Flag` type crashed concept evaluation.

**What is still contract-flavoured, deliberately:** the seed concepts
(`high_value`, `direct_award`, `uncapped_liability`), the OCDS codelists, and
the CUAD benchmark mapping. Those are bundle content and benchmark
configuration, which is exactly where domain knowledge belongs.

---

## Datasette as the primary surface

**Status: taken, and now further than "primary surface" — Datasette *is* the
writer.** The decision was to lean into the Datasette ecosystem rather than
build a bespoke UI, and to make the central interaction a person and a model
working through a document together.

**Built:** `plugins/orpheus_datasette.py` — upload a file in the browser, watch
it ingest, classify and extract, then confirm, amend or reject each fact. The
API is mounted in the same process at `/-/orpheus/api/`.

The R arrangement had Datasette as a read-only client of a Plumber API that
owned the only write connection. The Python one inverts that: Datasette holds
the database, the core is a library it imports, and every write is queued
through Datasette's own write thread. The invariant that replaces "never opens a
connection" is stricter and easier to check — *nothing writes except through
core functions* — and the constraints, plus what running it for real exposed,
are in [Deployment](deployment.md#the-datasette-ui-plugin).

**Built: the reading companion.** All three problems are answered, and the last
of them turned out to be the design rather than an obstacle.

- **The unit of provenance.** Settled, and it is the whole thing. A batch
  extraction is something somebody asked for; a companion firing as a person
  reads produces proposals nobody asked for. Landing those as `unconfirmed`
  instances would have poured them into `extraction_quality()` — the number
  Phase 1 turns on — along with flooding the review queue and letting the wiki
  build pages from them. So **a suggestion is not an extraction**: it lives in
  its own table until a person accepts it, and accepting writes through the same
  `insert_instance()` and `write_provenance()` path a batch pass uses.
- **Latency.** Settled. The passage is the page, which is already how text is
  stored, and the default engine is the deterministic pass: microseconds, no
  model, no network, and unable to offer something the page does not contain,
  which is why it needs no opt-in. A model per passage goes through the same
  gate, budget and audit as any other call.
- **Identity.** Settled earlier. The plugin provisions an Orpheus actor from
  whatever Datasette's `actor_from_request` produced. Which provider a
  deployment should use is still open.

This is what agents.md scopes as Phase 3. The surface question is settled the
same way it was for review: agents.md imagines a bespoke split-view reading
pane, and this puts the same interaction inside Datasette. See
[Reading with the machine](reading-companion.md).

**What is still missing** is context. Offers come from one page in isolation, so
a clause that only makes sense in the light of an earlier definition is read
without it — and nothing streams, a passage is read when asked for rather than
as somebody scrolls. Both were deliberately left until the write path and the
provenance were right.

**`datasette-agent` is the most likely vehicle for it** — a chat surface with a
`register_agent_tools` hook, which is the right seam for handing a model typed
Orpheus operations rather than raw SQL. It does not remove the need for the
core; it needs it more than the current UI does, because its alternative write
path is arbitrary SQL approved in a chat window. Now that the API is mounted
in-process, registering those tools is a small piece of work: each one is an
`api.handle()` call. The conditions for adopting it are in
[Datasette ecosystem](datasette-ecosystem.md#datasette-agent).

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
[`quality_report()`](provenance-and-amendment.md#measuring-extraction-quality),
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

**Two findings that fed the R-versus-Python decision above:**

- `ontologyAPI` already exposes ontology queries over plumber, but its auth is a
  **single shared API key with no actor concept**. It could not have backed
  Orpheus's per-document permissions, which need `created_by`, share grants and
  `amended_by` tied to a real person.
- `sqlglotR` wraps Python SQLGlot through reticulate. The stack was **already
  not pure R**, so "R or Python" was never the clean choice it looked like.

`ontologySpecR` is the one that survived the port, and not as a dependency: its
**bundle format** is what Orpheus now writes, validated against its schema
unmodified.

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

### Where it landed

Forty contracts from CUAD, 1,045 extractions, both engines:

```
reviewed 198 of 1045 · confirmed 197 · amended 0 · rejected 1
ai_cloud: 1041/1045 quotations located in the document
calibration: flat
  explicit  1.0  150/997 reviewed, 100% correct
  named     0.9   25/25  reviewed, 100% correct
  implied   0.7   19/19  reviewed, 100% correct
```

**Grounding is answered.** Four extractions in 1,045 quoted text their document
does not contain, and each was recorded at `inferred` rather than as fact. One
of the four was a genuine fabrication — a name stitched to an address, with a
suite number that appears nowhere — and it is the single rejection above. The
design's central claim, that a model's assertion is checked against the
document rather than believed, holds on real filings.

**The rubric ranks, on evidence just below the bar.** The `inferred` level is
excluded from the verdict for having four reviewed instances against a
threshold of five. Include it and the picture is unambiguous:

| level | reviewed | correct |
|---|---|---|
| explicit 1.0 | 150 | 100% |
| named 0.9 | 25 | 100% |
| implied 0.7 | 19 | 100% |
| **inferred 0.5** | **4** | **75%** |

`min_reviewed=4` returns `monotonic`; the default of five returns `flat`. The
threshold was not lowered to make it pass, and should not be: the number would
then rest on four rows, which is the kind of evidence this file exists to
refuse.

**What it would take, and why it may not be worth taking.** The bottom level is
rare *because extraction is good* — 4 unlocated in 1,045, and ten further
documents through the `llm` engine added none at all. Reaching five is not a
matter of a bigger corpus so much as a worse one. The criterion below asks for
a monotonic verdict; on a pipeline this accurate it is close to asking for
enough failures to measure failure. Rewriting it around the grounding rate,
which is answerable and answered, is the more honest fix.

**Reviewed by a machine.** All 198 were reviewed under an actor named
`Claude (machine test reviewer - not human review)`, at the owner's direction
and recorded as such in `decided_by`. It found the fabrication and four real
defects in the surrounding code. It is not a domain expert's judgement, and the
store does not claim it is.

**What would settle it:** whether an individual user may opt a sensitive document
into cloud processing on a shared server is a question for the organisation's
information-governance people, not a technical one.

### OCR approach

**Deliberately not settled. A provider registry was built instead.**

`textract.set_ocr_provider()` takes any `f(image_path) -> text`. Built-in
fallbacks are `pytesseract` then a `tesseract` binary. With no backend, pages are marked
`needs_ocr` rather than passed off as empty.

A third candidate arrived with the port: **Docling** does layout-aware parsing
and OCR in one pass, giving reading order and table structure rather than a flat
string. It is wired as an optional text backend and is **untested** — it would
not install in the build environment (`antlr4-python3-runtime` conflicts) — so
it is a path, not a recommendation.

**What would settle it:** evaluating candidates against real scanned
contracts — the accuracy difference on poor-quality government scans is the whole
question, and it cannot be answered from documentation.

### Identity provider

**Still deliberately not settled — but the bridge is now connected, not just
built.**

`auth.upsert_actor(store, idp, external_id, ...)` is where any provider lands,
and the Datasette plugin calls it on every request: an identity Datasette
authenticates becomes an Orpheus actor the first time it is seen, keyed on
`(idp, external_id)`. Nothing has to be listed anywhere for that to work, which
is the point — a `actor_map` written by hand works for three people and not for
thirty, and a typo in it silently attributes one person's corrections to
another. It survives as an optional *pin*, for actors that existed before the
auth plugin did.

`auth.permission_sql()` emits the row-level rule for whichever plugin is chosen,
and the `actors` row is the authority for `is_admin` for the same reason: that
SQL can only read the row.

**What would settle it:** confirming what the deployment target actually runs —
Entra ID, Okta, a government SSO, or nothing yet. Any of them plugs into the
same seam, so this is a deployment question rather than a code one.

**The leading candidate, now tested.** `datasette-accounts` stores accounts in
Datasette's internal database with PBKDF2 hashing, brute-force lockout,
revocable sessions and audit logging. Run against 1.0a38 it starts clean,
registers `permission_resources_sql`, and drives the whole upload-to-review loop
with correct attribution; promoting and demoting an account upstream moves
`actors.is_admin` in step. It would delete token minting, revocation and
authentication from `auth.py`. It is marked experimental and it puts a second
writer on a second database. See
[Datasette ecosystem](datasette-ecosystem.md#datasette-accounts).

Tokens (`orpheus token`) stay regardless: they are the script path, and no
browser-session plugin covers it.

### Knowledge with no source behind it

**Deliberately not resolved. Recorded here rather than smoothed over.**

The invariant that makes an entity page worth reusing is that a claim with no
mention behind it cannot be written. It is also why there is nowhere to put
"this clause is unusual because the 2023 framework changed" — domain knowledge
with no excerpt anywhere in the corpus, which is often the most valuable thing a
person knows about a document.

[DocIt](prior-art.md#markdown-as-the-agent-maintained-knowledge-format) resolves
this differently: a `notes/` directory the agent never writes to, and a
`## Human Context` section on every page it never overwrites. It works because
DocIt's pages are not answerable to anything the way Orpheus's are.

`entities.description` is the nearest thing here — a person's own words, kept
visually apart in the UI and marked `> **Context**:` in the export. But it still
carries a `source`, a `confidence` and a review `status`, because everything in
this store does. It is a claim in the same shape as a machine's, not a separate
tier.

**What would settle it:** whether a public servant's own knowledge should be
reviewable in the same vocabulary as an extraction, or is a different kind of
thing that the store should hold without grading. That is a question about how
the work actually happens, and it cannot be answered from here.

The honest position is that this is a tension in Orpheus's own design, and the
module that exists to record those would have it as `unexplained`.

### Permission boundaries

**Mechanism settled, rules deliberately not.**

Owner + visibility + share-table, following `datasette-paper`. Actors carry
`departments_json` so a `datasette-acl` dynamic group can key off it the moment
department rules exist — but no department rule is invented, because guessing
would bake a wrong rule into the schema.

**What would settle it:** scoping with real stakeholders whether the boundary is
department, sensitivity tag, document owner, or a combination.

### `bundle_diff()` and staging concept versions

**Partly addressed.** `bundle.register(stage="staging")` stores a bundle without
activating it, and a staging bundle cannot be activated by accident. Concepts
are versioned and deprecated rather than edited, so an evaluation always points
at a version that still exists.

A true `bundle_diff()` — showing what registering a bundle would change before
it changes it — is not built. It is now ordinary work in this repository rather
than upstream work in another project, which is one thing the port bought.

---

## Extraction quality is measured

**Status: measured, on 40 real contracts. Grounding is settled. The rubric shows
every sign of ranking and is four instances short of proving it — see the
threshold note at the end of this section.**

Phase 1's definition of done is extraction *good enough to trust as a
foundation*. Everything above is machinery for producing that number and for
making it honest — provenance preserved beside corrections, grounding computed
rather than believed, unreviewed rows never counted as correct, rule flags kept
out of the extraction figures. The machinery works. The number does not exist.

What has not happened, plainly:

| | Status |
|---|---|
| A real model has run against a real document | **Yes.** Anthropic, six EX-10 exhibits filed with the SEC, 92,551 characters |
| A real corpus has been ingested | **Six documents.** Real, and far short of the few hundred this needs |
| A person has reviewed extractions they did not write | **No.** All 165 were reviewed, but by a machine actor labelled as such. It exercised the review path and found real defects; it is not the human calibration this asks for |
| `gliner2` has run | **No.** Its weights could not be downloaded |
| `docling` has run | **No.** It would not install |

### What the first run produced

```
reviewed 165 of 165 · confirmed 160 · amended 1 · rejected 4
calibration: insufficient_evidence
  explicit  1.0  158/158 reviewed, 99% correct
  named     0.9  3/3    not enough reviewed
  implied   0.7  1/1    not enough reviewed
  inferred  0.5  3/3    not enough reviewed
ai_cloud: 161/162 quotations located in the document
```

Grounding is answered: essentially nothing was invented, and the one
non-exact quotation is an *elided* one the alignment caught and downgraded
rather than a fabrication.

Calibration is not answered, and looking into why turned up something that
changes what the question means.

**Confidence is not the model's opinion. It is grounding, restated.** Across
all 164 cloud extractions the stored confidence is exactly
`confidence_for_alignment(alignment)`, with no exceptions:

| alignment | confidence | n |
|---|---|---|
| `match_exact` | 1.0 `explicit` | 157 |
| `match_greater` | 0.9 `named` | 3 |
| `match_fuzzy` | 0.7 `implied` | 1 |
| unlocated | 0.5 `inferred` | 3 |

That is deliberate, and `population.py` says so: *"a model's opinion of its own
certainty is exactly what the rubric was invented to avoid storing."* The
prompt never asks for a confidence. `ALIGNMENT_CONFIDENCE` supplies it.

So the exit criterion below — *if `calibration.verdict` is not `monotonic` the
rubric is not ranking reliability and everything built on it rests on nothing*
— was written against a rubric that has since been given a different meaning.
Nothing is built on a model's self-assessed certainty, because there is no such
number in the store. What calibration actually asks is **does an exactly
located quotation get confirmed more often than a fuzzy or an unlocatable
one?** That is a fair question, arguably a better one, and it is not the
question the docstring states.

The lopsided distribution is a sample-size problem after all, and a small one.
At the rates this corpus shows, a second level clears the five-reviewed floor
at roughly **ten to thirty documents** — not the few hundred assumed above.
Six was simply too few.

The deterministic pass *is* exercised for real — it finds four dates and an
amount in the fixture, grounded to the right pages, through a real browser
upload. That is the part that needs no model. It is also the smaller part.

**What would settle it:** ingest a few hundred real contracts with a model
configured, have someone who knows the domain review a sample, and read
`orpheus report`. The three things to look at, in order:

1. **`calibration.verdict`.** If it is not `monotonic`, the confidence rubric is
   not ranking reliability and everything built on it — which triage queue a
   reviewer sees first, which findings are trusted without checking — rests on
   nothing. That is a finding about the design, not about the corpus.
2. **`accuracy` at `explicit` and `named`.** These are the levels a person would
   plausibly skim rather than check. If they are not high, the review burden is
   the whole cost of the system and the automation is not paying for itself.
3. **`property_corrections`.** The fields people keep fixing are the prompt's
   defects, and they are cheap to act on.

Nothing else on this list is worth deciding before that number exists. A
language, a storage engine and an identity provider are all answerable later; an
extraction pipeline nobody has measured is answerable only by measuring it.

---

## What leaves the building on a cloud call

**Status: open, and it was quietly answered wrongly until this cleanup.**

The cloud gate decides *whether* a call happens. It says nothing about *how
much* goes with it, and the two are separate questions a deployment handling
sensitive contracts needs both answers to.

The R implementation scored pages against clause-related terms and sent the best
few. That was not ported. `populate()` sends `document_text()` — the whole
document — to whichever engine, and all three engines record `excerpt_only=False`
on the `llm_calls` row.

**The audit was telling the truth and `/capabilities` was not.** It read a
`cloud_send_mode` setting that defaulted to `"excerpt"` and that nothing
implemented, so a deployment checking what it sends would have been told its
contracts left in fragments when they left whole. That claim is now removed:
`send_mode` reports `full_document`, with a note saying classification is the
only pass that truncates.

**What would settle it:** whether excerpt selection is wanted at all. It is not
obviously right — sending a model four pages of a forty-page agreement is how
you miss the clause on page thirty-one, and the extraction quality cost is
unmeasured. The honest options are to implement it and measure both, or to
decide that the cloud tier is for documents a deployment is willing to send
whole and let the gate carry the whole decision. Either is defensible; claiming
one and doing the other is not.

---

## Reconciliation: how this gets reused elsewhere

**Status: open, and the clearest answer available.**

The point of a wiki whose every line is cited is that the next project can use
it. The
[Reconciliation Service API](https://reconciliation-api.github.io/specs/latest/)
is the standard way that happens: a small JSON protocol for matching messy names
against a canonical list, spoken by OpenRefine and a W3C community group's worth
of tooling. Someone with a spreadsheet of supplier names could point OpenRefine
at an Orpheus store and reconcile against it.

`entities` maps onto it with no code — `entity_id`, `canonical_name`,
`description`, `type_id` are exactly its four fields. `datasette-reconcile`
implements the protocol as a plugin and would need only config, but it is
unmaintained (last commit February 2024) and still calls `permission_allowed`,
removed in Datasette 1.0. See
[Datasette ecosystem](datasette-ecosystem.md#datasette-reconcile--right-idea-unmaintained).

**What would settle it:** implementing the manifest and `queries` endpoints
directly. They are small, the spec is stable, and the candidate scoring already
exists in `entities.similar_names()`. The reason to wait is that the scoring
threshold is calibrated on ten hand-picked pairs, and exposing a matching
service on an uncalibrated threshold publishes a guess.

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

**A second candidate: [LatticeDB](https://github.com/jeffhajewski/latticedb).**
An embedded single-file property-graph database in Zig with HNSW vectors and
BM25 full-text in the same engine and query layer. MIT, v0.12.0, one author.
Installed from the published wheel and exercised against the graph shape this
corpus actually produced.

It is the strongest thing on this list for the vector question specifically:
0.83 ms 10-NN at 1M vectors with 100% recall, against `sqlite-vec`'s 17 ms
brute force. That is the one dimension where Orpheus has nothing rather than
something adequate. It also handles concurrent readers while another process
holds the file — checked, because the "single-writer" framing reads as though
it might not, and Datasette is all readers.

**Two findings that have to be recorded, because the benchmark table does not
show them and a later reader would otherwise re-evaluate it from the README.**

*Variable-length traversal cannot report what it traversed.* Both forms are
refused outright:

```
MATCH p = (a)-[:REL*1..5]->(b) RETURN p
  → LatticeQueryError: Expected '(' to start node pattern
MATCH (a)-[r:REL*1..5]->(b) RETURN r
  → LatticeQueryError: Variable-length relationships do not support edge variables
```

Endpoints only. Fixed-length chains work and expose edge properties correctly,
but that is the part anyone would hand-roll. For the question Orpheus most wants
a graph engine to answer — *how* are these two connected, through which hops,
resting on which documents, and has anyone checked them — it currently does less
than the breadth-first walk in `graph.py`. `OPTIONAL MATCH` and `CALL` are also
not implemented.

*The coupling is the real obstacle regardless.* Datasette is not a viewer bolted
onto SQLite here: it is the browsing surface, the permission enforcement
(`auth.permission_sql()` feeding `permission_resources_sql`) and the writer
(`Store.adopt()` borrows its connection and joins its transaction). Changing
storage engine is not a storage decision, it is rebuilding all three.

And the scale argument does not bite. This graph is hundreds of entity pages,
built from one query into Python adjacency dicts. 39 μs against SQLite's 548 μs
at 100K nodes is not a number that changes anything at this size.

**So: recorded, not adopted.** Revisit if semantic search over excerpts becomes a
real requirement — and re-check path binding then rather than assuming, since it
is the kind of gap a later version could close.

One idea worth taking regardless of the engine: LatticeDB puts durable named
streams and a graph changefeed on the same transaction and WAL path as graph
writes. That is `edit_history` — an append-only log written inside the same
transaction as the change it describes — and it is the third project surveyed
here to arrive at it independently.

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

Orpheus serves without the flag, and checkpoints after writes. See
[Deployment](deployment.md#the-wal-and-immutable-mode-trap).

### Concept evaluation does not carry lineage

The architecture refers to `ont_evaluate()` carrying lineage automatically.
Nothing in the stack did: `conceptR::cpt_evaluate()` evaluates one SQL
expression against one table and returns a data frame.

Lineage is therefore built here: `concept_evaluation_dependencies` records which
instances each evaluation read, which is what makes `stale` automatic rather
than something a person has to notice. Worth knowing when reading the
architecture alongside the code.

### A model failure must not cost the deterministic findings

Not in the specification at all, and it only shows up once one transaction
spans a whole request. The deterministic pass needs no model; raising through a
failed model call rolled its findings back, so the half of the pipeline that
works offline only ever survived when the half that doesn't also worked. A model
failure now leaves the run `partial`, keeps what was found, and says so.

---

## Still out of scope for Phase 1

Procurement-specific views, and conflict-of-interest *detection*.

[Questions the corpus raises](questions.md) is the honest part of the latter:
shared counterparties, one party in two roles, relations that come back round.
What it deliberately is not is detection — it has no notion of ownership,
directorships or donations, those live in registers Orpheus does not read, and
treating graph shape as a finding is the failure this whole design guards
against.

**Three things that were on this list are now in.** Entity resolution and alias
merging arrived with [the wiki](entities.md); the cross-document relation graph
arrived with [the network](network-and-corroboration.md); the reading companion
arrived with [reading a passage at a time](reading-companion.md), though what it
still lacks is context across passages rather than the interaction itself.
Neither of the first two was pulled
forward for its own sake. The wiki needed resolution to have pages at all, and
the graph turned out to be the only way to say something the store could not
otherwise express — that four contracts asserting one relation are one claim
with four sources rather than four unrelated rows.

Building the graph also exposed why it had looked out of scope: **no shipped
engine had ever returned a relationship**, so `edges` was unreachable and no
corpus Orpheus had processed could contain one. The table, the normaliser and
the writer were all correct and all dead. That is fixed.

What remains genuinely deferred is the *analysis* on top: conflict-of-interest
rules, risk propagation, procurement-specific views. The structure is there and
the questions are not asked yet.

The one deliberate exception is the step 9 corpus escalation, which is
best-effort naive name matching, labelled `naive_unresolved` on every result. It
is a stepping stone to Phase 4 resolution, and it should be **replaced** by real
resolution rather than patched indefinitely. `naive_key()` has a test asserting
its known failure — `"Ernst & Young"` and `"Ernst and Young"` produce different
keys — so the limitation cannot quietly disappear.

It had a second, worse one that was not documented because it was not known:
`group` and `holdings` were treated as legal-form suffixes and stripped anywhere
in a name, so `"Kestrel Medical Group"` and `"Kestrel Medical Ltd"` shared a
key, as did `"Ardmore Holdings plc"` and `"Ardmore Ltd"`. A false *merge* is
strictly worse than a false split: a split leaves two rows a person can join,
while a merge combines two organisations and leaves nothing to notice — and a
holding company is not its subsidiary, which is the distinction that matters
most in exactly the conflict-of-interest work this is a stepping stone to. Only
trailing legal forms are stripped now, and migration 5 recomputes stored keys.

**Two better tools for the same job now exist.** `entities.candidates_for_mention()`
offers pages an extracted mention might belong to, ranked by the kind of
evidence rather than by how close the spelling looks. And
`search.unextracted_mentions()` asks what neither can: which documents *name*
something the extractor never picked up at all. Key matching can only join
instances that were already extracted, so those misses are invisible to it by
construction.

---

[← Back to index](index.md)
