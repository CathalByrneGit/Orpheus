# Questions the corpus raises

The network says which pages are joined and how. This asks the next thing: is
any of that worth someone looking at?

Five shapes, none of them about procurement — a bundle from another domain gets
the same five questions about its own types:

| | |
|---|---|
| **`person_bridges`** | one person connected to two organisations that deal with each other |
| **`shared_detail`** | two pages given the same stated address, or the same registration number |
| **`shared_counterparty`** | two pages whose *only* connection runs through one shared third party |
| **`two_parts_in_one_document`** | one entity recorded under two different roles in a single document |
| **`circular_relation`** | a chain of one relation type that leads back to where it started |

`person_bridges` is the one the relation graph was worth building to reach.
`shared_detail` splits deliberately: a shared **address** is extremely common for
dull reasons — a serviced office, a formation agent, an accountant with a hundred
clients at their own door — while a shared **registration number** identifies one
legal entity and means either a merge that never happened or an extraction error.
The two get different questions because they are different questions.

---

## Never a finding

A shared subcontractor is not wrongdoing. It is usually a small market, a
specialist everybody uses, or a parent company. In the setting Orpheus is built
for — public contracts, named companies, real people — an accusation drawn from
a graph join does real damage, and a graph join is all this has.

So what the corpus says is: *these two are closer than they look, here is the
chain, and here is how much of it anybody has checked.* Every question carries
an `asks` field that offers the innocent explanation first, and none of them
says "conflict".

```
[unreviewed] Ardmore Digital Ltd and Meridian Systems Ltd are connected
             only through Kestrel Medical Group
      Ardmore  subcontracts_to  Kestrel   2 doc(s), nobody has checked this
      Kestrel  subcontracts_to  Meridian  1 doc(s), nobody has checked this
   -> Is the shared party a specialist everybody in this market uses, or is it
      the reason these two are closer than they appear?
```

That is a real result from the test corpus.

## The individual decides, and the store remembers

A question is *computed* — derived from the graph on every run. So without
somewhere for a judgement to live, the same questions come back every time and
*"I looked; Kestrel is the only supplier of this specialism in the state"* is
something a person has to remember rather than something the store knows.

| | |
|---|---|
| **`standing`** | *This is real and it stays on the list.* A finished piece of review, not an outstanding task. |
| `explained` | There is an innocent account, and here it is. Not a dismissal — it records what somebody established so nobody establishes it again. |
| `dismissed` | Not a real question: an extraction error, a duplicate. |

`standing` is load-bearing. It is the difference between a feature that informs
a decision and one that only ever redisplays the same list, and it is why the
ranking puts standing questions above open ones rather than below them.

**A reason is required for every state**, including `standing` — the reason is
the part worth anything later. A new judgement supersedes rather than
overwrites, so what somebody decided last time stays readable.

### A judgement does not outlive its evidence

The chain is digested when a decision is recorded. If the evidence changes — a
new document, a link confirmed, a hop that was not there before — the question
in front of you is not the one that was ruled on. It reopens, says so, and keeps
the old judgement visible rather than dropping it:

> 1 carry a judgement made against different evidence, and are open again.

That is `stale`, the same mechanism concept evaluations use.

## Checked chains come first

The ordering is the opposite of a severity sort, deliberately.

A question every hop of which somebody has confirmed is worth a person's time.
One assembled from unreviewed machine guesses is a reason to go and check the
extraction — and the worst outcome available here is somebody acting on a chain
that turned out to be two extraction errors. `confirmed_throughout` says which,
and the sort puts the checked ones at the top.

## Why `shared_counterparty` is restricted to cut vertices

The shared party has to be the **only** route between the two — an articulation
point in the relation graph. Without that restriction every well-connected page
raises a question about every pair around it, and the list becomes noise that
buries the two cases that matter.

## Coverage first, again

Two of the three checks read the relation graph, and
[the graph is only as complete as the wiki](network-and-corroboration.md). A
question never raised because its evidence never reached the graph is the one
nobody will think to look for, so `coverage` is the first key in the report and
the first line on the page.

## Surfaces

| | |
|---|---|
| `/-/orpheus/questions` | the page |
| `GET /questions` | **administrator** — it spans the whole corpus |
| `POST /questions/review` | **administrator** — `fingerprint`, `status`, `rationale` |
| `GET /questions/reviews` | **administrator** — every live judgement |
| `orpheus questions [--open-only]` | the same, from a terminal |
| `orpheus questions --review <fp> --status standing --note "…"` | record a decision |

## What this is not

It is not conflict-of-interest detection, and calling it that would be the first
step toward treating its output as findings. It has no notion of ownership,
directorships, shareholdings, political donations or family relationships —
those live in registers Orpheus does not read. What it has is the shape of one
corpus of documents, and the honesty to say that is all it has.
