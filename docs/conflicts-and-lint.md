# Conflicts, the lint, and the markdown export

Three changes that arrived together, because they are one idea seen from three
angles: **a knowledge base that smooths over disagreement is worse than one that
admits it.**

The prompt was a sibling project, [DocIt][docit] — the same problem approached
from the opposite end. It builds a living understanding of codebases as a
directory of markdown maintained by an agent; Orpheus builds one of documents as
a SQLite store maintained through review. Reading its instructions turned up
three conventions Orpheus had no equivalent for, and one warning worth quoting:

> LLMs have a gravitational pull toward consensus … Genuine tensions get
> resolved into readable prose that hides the decision point entirely. The most
> dangerous output is not the obviously wrong claim — it is the smoothly written
> paragraph that has erased a real trade-off.

[docit]: https://github.com/CathalByrneGit/DocIt

---

## The fourth verb

Orpheus had three review verbs and all of them end with one answer standing.
`confirm` says the machine was right, `amend` says it was nearly right, `reject`
says it was wrong. That is the correct shape for grading an extraction.

It is the wrong shape for a corpus. Two documents that name the same company at
two different registered addresses are not an extraction error — the company
moved, and both filings were right when they were written. Under three verbs, a
reviewer who checks both can only confirm both, and the entity page then renders
them in the same voice, one under the other, reading as though they agree.

The reader who most needed to know they disagree is the reader who will not
notice.

### What a tension is, and is not

A tension is **not uncertainty**. `confidence` is already the uncertainty axis
and has five levels of it; a low-confidence extraction means *the machine is not
sure*. A tension means *somebody checked, and the conflict is real*.

DocIt draws the same line between its two markers, and Orpheus now uses both
names in the export for exactly that reason:

| Marker | Means |
|---|---|
| `> **Inferred**:` | uncertainty about something that could not be verified |
| `> **Tension**:` | a conflict that *was* verified — it is really there |

### `accepted` is a terminal state

The important part. Four states:

| Status | Means |
|---|---|
| `open` | raised, nobody has ruled |
| `accepted` | a person looked; the conflict is real and it stands |
| `resolved` | settled — `resolution` is required and says how |
| `withdrawn` | not a real conflict; kept anyway, as evidence about detection |

Most review vocabularies have no way to stop at "this conflict is real", so a
reviewer's only exits are to pick a side or to leave the item looking
unreviewed. Both bury the finding. `accepted` is a *finished piece of review
work*, and the wiki renders an accepted tension as an assertion rather than as a
question.

`resolved` demands a reason for a reason: a settled conflict with no account of
the reasoning looks decided and cannot be checked, which is worse than an open
one. `withdrawn` keeps the row for the same reason a rejected instance is kept —
it measures how well conflict detection works, and deleting it throws away the
measurement along with the mistake.

### Cited on both sides

The rule that stops this becoming a notes field:

> A tension cites **at least two sides**, and every side is an instance carrying
> provenance.

The same rule as the entity page, for the same reason. One claim on its own is
an assertion — if it is wrong, reject it; if it is unclear, that is what
confidence is for. Without the floor, a tension is an unfalsifiable opinion, and
one of those in a store built on citations is the single row nobody can check.

### Finding them

`detect_conflicts()` compares the mentions linked to one entity, property by
property, and reports where the values differ. It writes nothing.

Two deliberate exclusions:

- **Unreviewed mentions, by default.** Two *unconfirmed* extractions disagreeing
  is far more likely to be one bad extraction than a real conflict. That is what
  the review queue is for, and raising tensions over it would drown the real
  ones. `--include-unreviewed` overrides it.
- **Rejected mentions, always.** A rejected extraction is a known error. Arguing
  with it manufactures conflicts out of mistakes review already caught.

`propose_tensions()` raises what it finds at `open`, sourced `lint` — never
`accepted`. The machine can see that two values differ; it cannot see whether
the difference matters, and that judgement is the entire content of the state.

```bash
orpheus tension find                     # what disagrees, recorded or not
orpheus tension propose --actor-id act_… # raise the ones not yet recorded
orpheus tension accept  tns_… --actor-id act_… --note "moved offices in 2024"
orpheus tension resolve tns_… --actor-id act_… --note "the later filing governs"
```

---

## The antagonistic lint

`quality.py` asks whether the confidence rubric ranks reliability — a
calibration question, answered with rates. `lint.py` asks a different one:
**where is this store lying to a reader?**

It is not a health check, and DocIt's rule is the one that shapes it:

> Report findings as specific, located problems — not general observations.
> "Component X and component Y both describe the aggregation step differently"
> is useful. "Some docs may be inconsistent" is not.

Every finding names a row you can open. The checks, ordered by what they cost a
reader who trusts the output:

| Check | Severity | What it means |
|---|---|---|
| `uncited_page` | high | a page asserting something no document says |
| `ungrounded_quotation` | high | an excerpt the cited document does not contain |
| `smoothed_conflict` | high | confirmed values that disagree, with nothing recorded saying so |
| `unchecked_conflict` | medium | a tension raised and never ruled on |
| `orphan_mention` | medium | a confirmed mention no page includes |
| `split_page` | medium | two pages that are probably one thing |
| `unnamed_page` | medium | a page filed under a role word, or under something with no letters in it |
| `unextracted_document` | medium | ingested, and nothing read from it |
| `unavailable_original` | high / medium | the file the document was read from is not where the store says it is |
| `stale_evaluation` | medium | an analysis whose evidence has since been amended |
| `unreviewed_grouping` | low | a page joining 3+ documents on machine evidence alone |

`--shallow` skips the two that compare every mention against every other.
`deep` is the default, which is the opposite of the usual arrangement on
purpose: those two are the ones that find conflict smoothed into consensus.

### It will not give you an all-clear it has not earned

The most dangerous thing this could return is a clean bill of health, so it is
the one answer it refuses to give plainly:

```
Nothing found, but only 4 link(s) have been reviewed. Most of these checks
compare things a person has confirmed, so this says more about how little has
been checked than about whether the store is sound.
```

And with enough reviewed:

```
Nothing found across 9 check(s), over 40 reviewed link(s). That is evidence,
not proof: these checks find contradictions, uncited claims and gaps, and they
cannot find a claim that is wrong in a way every source agrees on.
```

### The one check that reads a disk

`unavailable_original` asks whether the file each document was read from is
still there. It is not an assertion this store makes falsely — it is one it can
no longer be checked on. Every excerpt from such a document still renders, with
its page number and its character span, and there is nothing left to hold them
against. The store looks exactly as sound as it did yesterday.

It is `high` when the recorded path is not where content-addressed storage puts
a document, for the same reason `uncited_page` is: nothing but `ingest` writes
`storage_path`, so a value that is not that path did not come from this
codebase. A file that is simply gone is `medium`.

It is one `stat` per document, which is why it runs on every lint. It cannot
see a file whose *bytes* changed:

```bash
orpheus verify          # re-read every original, check it against its digest
orpheus verify --quick  # only that they exist -- what the lint already does
```

That is a separate command rather than a `deep` lint check because `deep` means
a few seconds of SQL and this means the size of the corpus in disk reads. It
exits non-zero when anything is unavailable, so it can gate a restore — which
is the question it exists for. A database and a `storage/` from two different
moments looks perfectly healthy from the inside: every row present, every
excerpt rendering, every offset pointing into bytes nobody has compared to
anything. Nothing else in Orpheus would notice.

`GET /storage/audit` is the same audit over HTTP, administrator-only. It runs
the cheap pass corpus-wide; `?verify=1` is bounded to a single `document_id`,
because hashing the whole corpus would hold the connection Datasette answers
pages on.

`orpheus lint` exits non-zero on a `high` finding, so it can gate a corpus run.
`/-/orpheus/lint` renders the same report with every finding linked to the thing
it is about.

---

## The markdown export

The entity layer exists so the knowledge is reusable by the next project.
Reusable *through this API* means reusable by one process with one schema, which
is not the same claim.

So the wiki projects out into the format four independent projects converged on
— [Karpathy's LLM-wiki, MemPalace, Google's Open Knowledge Format v0.1, and
DocIt](prior-art.md#markdown-as-the-agent-maintained-knowledge-format): a
directory of markdown, one concept per file,
cross-linked, with an index for progressive disclosure and no database in the
middle.

```
bundle/
  index.md              what this is, where the sources disagree, every page
  log.md                edit_history, newest first, ordered by seq
  entities/<name>.md    one page per entity
  documents/<name>.md   one per source document
```

OKF's shape where it says anything — YAML frontmatter, `type` as the only
required field, `index.md`, `log.md`, file path as concept identity. DocIt's
conventions where OKF is silent, because those carry what a plain wiki has no
word for: `> **Tension**:`, `> **Inferred**:`, and `> **Context**:` for the one
part of a page a person wrote rather than read.

**The invariant crosses the boundary.** Every claim exported carries the
document, page and excerpt it came from, and a page with no mention behind it is
not written — it is listed under "Not exported" in the index instead. An export
that quietly dropped the citations would read beautifully and be worthless, and
nothing downstream could tell.

The index says the quoted excerpts are immutable, because they are what the
bundle can be checked against. That is DocIt's source-doc rule and Orpheus's
provenance rule, which turn out to be the same rule.

```bash
orpheus export ./bundle                   # everything, proposals marked apart
orpheus export ./bundle --confirmed-only  # only what a person has checked
```

---

## What was deliberately not copied

DocIt's `notes/` directory is a free-form space the agent never writes to, and
every component doc has a `## Human Context` section it never overwrites. That
is a good idea and Orpheus has no equivalent, because its invariant forbids one:
a claim with no mention behind it cannot be written.

`entities.description` is the nearest thing — a person's own words about a page,
kept visually apart in the UI and marked `> **Context**:` in the export. But it
still carries a source and a review status, because everything in the store
does. Somewhere to record "the reason this clause is unusual is that the 2023
framework changed" — knowledge with no excerpt behind it — is a genuine gap.

It is recorded as a tension in the design rather than smoothed over, which is
the point of the whole exercise.

---

[← Back to index](index.md) | [Next: Provenance and amendment →](provenance-and-amendment.md)
