# Reading with the machine

The plugin reviews a document *after* extraction has run over the whole of it.
That is the right shape for grading a batch and the wrong shape for the thing
this project was started to do: a person and a machine going through a file
together, the machine offering what seems worth recording and the person
deciding.

Three problems stood between the two, and they are now all answered.
[Identity](datasette-ecosystem.md#datasette-accounts) was settled when the
plugin started provisioning actors from whatever Datasette authenticated. The
other two are this.

---

## A suggestion is not an extraction

This is the whole design, and it is not a performance question.

A batch extraction is a deliberate act. Somebody asked for it, over a whole
document, and the review queue it produces is the thing they wanted. A companion
firing as a person reads produces proposals **nobody asked for**, most of which
will be ignored.

If those landed as `unconfirmed` instances — which is what every other machine
finding in this store does — three things would happen, each quietly:

| | |
|---|---|
| The review queue fills with work nobody requested | and the queue stops meaning "what needs doing" |
| `propose_entities()` builds wiki pages out of them | so the wiki asserts groupings drawn from guesses nobody looked at |
| `extraction_quality()` counts them | and **that is the number Phase 1 turns on** |

The third is the serious one. Extraction accuracy is measured from corrections:
what a person confirmed, amended or rejected. Pour in a stream of proposals
nobody ever looked at and the denominator is wrong in a way nothing reports.

So suggestions live in their own table until a person accepts one. Accepting
writes the instance through the same `insert_instance()` and `write_provenance()`
path a batch pass uses — there is still exactly one way an instance comes into
being, and it still carries its page, its excerpt and its span.

```
accept  →  instances_KeyDate  source=human  status=confirmed
           provenance         source_label=companion:deterministic
                              page_no=1  alignment=match_exact  char_start=…
```

`source` is `human` because a person vouched for the value. What the machine
actually offered stays on the suggestion row, so *what did the machine say
before a person fixed it* remains answerable — the same property amendments
give the batch path.

**Dismissals are kept**, like rejected instances. They are the only evidence
there is about whether the suggestions are worth reading, and
`suggestion_quality()` reports the acceptance rate per engine. That measure is
deliberately separate from `extraction_quality()`: one measures extraction
against review, the other measures offers against a person's attention, and
mixing them would answer neither.

## Latency: the passage is the page

A companion reacting to somebody scrolling cannot take seconds. The unit is the
page, which is already how text is stored, so nothing has to be chunked. The
default engine is the deterministic pass — patterns over one page, microseconds,
no model, no network, no opt-in.

It also **cannot offer something the page does not contain**, which is what
separates it from a model and why it needs no gate. A model engine can be asked
for per passage and goes through the same cloud gate, budget and audit as any
other model call: a companion is not a reason to send text somewhere a batch
could not.

## Reading twice

Without care, scrolling back re-offers everything already dismissed and the
companion becomes something to close. Every offer carries a fingerprint over its
type, page and values, so:

- what was **accepted** is an instance now, and is not offered again
- what was **dismissed** stays dismissed
- what is still **offered** is the same row, not a duplicate

## Reading is recorded separately from finding

`reading_passages` records that a page was read, by whom, with which engine —
whether or not anything was found.

That matters because *nobody opened this page* and *this page holds nothing* are
different facts, and no count of findings can tell them apart. It is also what
makes "we went through this together" checkable rather than a feeling:
`reading_progress()` reports pages read against pages total, per person, and
never reports pages-with-findings as though it were pages-read.

## Correcting on the way in

The common case is the machine spotting the right thing and getting one field
wrong. Accept-then-amend would be two steps for one act, so the reading surface
makes every property editable and `accept_suggestion(properties=…)` applies the
correction as the row is written. The offer is preserved either way.

## Surfaces

| | |
|---|---|
| `/-/orpheus/read/<document_id>?page=N` | the page beside what it offers |
| `POST /documents/<id>/passages/<n>/read` | offer, and record the read |
| `POST /suggestions/<id>/accept` | record it, `properties` corrects it |
| `POST /suggestions/<id>/dismiss` | not worth it, and kept |
| `GET /documents/<id>/reading` | how far through this document somebody is |
| `GET /suggestions/quality` | acceptance rate, per engine |
| `orpheus read <id> --page N` | the same, from a terminal |

`POST …/read` is guarded by `view` rather than `edit`, which looks odd and is
right: it writes, but what it writes is the reader's own progress and a set of
proposals, not a change to the document. Requiring `edit` would stop a viewer
using the companion at all.

## The rest of the document, behind the passage

A model asked about page 7 did not know what page 3 said, so a clause that only
made sense in the light of an earlier definition was read without it.
`context_chars` now lets it see the rest of the document as background — pages
before the passage first, because a definition comes before the thing it
defines, then forwards with whatever budget is left, and whole pages only, since
half a definition read as a whole one is worse than not seeing it.

**It is off by default, and it does not widen scope.** Those are separate
guarantees and both matter.

Off by default because the context is charged to the same budget as everything
else: `llm_calls.prompt_chars` counts it, a page averages 1,908 characters
against 20,707 for a document, and a five-fold prompt is a deployment's decision
rather than this function's.

Not widening scope because the offers are still *about this page*. The model is
told the background is background, and then the boundary is **enforced anyway**:
an offer whose excerpt is not in the page is discarded, and the count comes back
as `n_outside_the_page`. Computed rather than trusted, for the same reason
alignment is. Without it, a fact from page 3 lands under page 7 with a
page-scoped fingerprint, a page-relative offset, and a reviewer sent to the
wrong passage to check it.

### What it was worth, measured

Run against one page of a real SEC filing, three times with three wordings of
the background instruction:

| instruction ends… | page alone | with 11,365 chars of context |
|---|---|---|
| "do not report anything from it" | 11 offers | 11 |
| "report only things the passage itself contains" | 10 | **2** |
| "to help you read the passage that follows" | 9 | 12 |

The identical page-alone prompt gave 9, 10 and 11, so run-to-run variance is
about ±2. Against that, **context neither clearly helped nor hurt the count** —
but a badly worded instruction is well outside the noise and *suppresses*
findings, which is why the shipped wording asks for comprehension and says
nothing about scope. Policing the boundary in the prompt cost eight of ten
offers; `_on_this_page` does that job for free.

So this is available and honest rather than recommended. It is the mechanism the
gap called for; on this corpus it did not pay for itself.

## What this does not do yet

**Offers can arrive with no properties at all.** Asked to read page 4 of a
contract, the model returned `{"type": "Company", "excerpt": "YEC",
"properties": {}}` — because from that page alone, "YEC" is all there is; the
name behind the abbreviation is defined on page 1. Whole-document context did
**not** fix it: the properties came back empty with the background attached too.
`Company.name` is NOT NULL, so accepting such an offer without correcting it is
refused rather than silently filed — and `accept_suggestion` takes the
correction, which is the designed path. But an offer a reviewer must complete
before it can be accepted should say so, and it does not yet.

Nor is there any streaming: a passage is read when somebody asks for it, not as
they scroll. That is a deliberate ordering — the write path and the provenance
had to be right before the interaction was made continuous.
