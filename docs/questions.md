# Questions the corpus raises

The network says which pages are joined and how. This asks the next thing: is
any of that worth someone looking at?

Three shapes, none of them about procurement — a bundle from another domain gets
the same three questions about its own types:

| | |
|---|---|
| **`shared_counterparty`** | two pages whose *only* connection runs through one shared third party |
| **`two_parts_in_one_document`** | one entity recorded under two different roles in a single document |
| **`circular_relation`** | a chain of one relation type that leads back to where it started |

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
| `orpheus questions [--confirmed-only]` | the same, from a terminal |

## What this is not

It is not conflict-of-interest detection, and calling it that would be the first
step toward treating its output as findings. It has no notion of ownership,
directorships, shareholdings, political donations or family relationships —
those live in registers Orpheus does not read. What it has is the shape of one
corpus of documents, and the honesty to say that is all it has.
