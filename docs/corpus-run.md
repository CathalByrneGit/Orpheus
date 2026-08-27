# The corpus run

The one thing Phase 1 has been waiting on. Everything in this store is built to
answer a single question — **is extraction good enough to build on?** — and that
question has never been asked of a real model, so it is still open.

This is the runbook. It exists because the run has to happen in a session that
was *created* with an API key in its environment, which means whoever does it
starts without the conversation that built this.

---

## What is already true

- Every surface works end to end, driven through a browser against a live
  Datasette: ingest, extract, review, the wiki, tensions, the network,
  corroboration, the reading companion, questions, the markdown export.
- Two real defects have already been found by running real documents rather
  than tests — `edges` was unreachable because no engine emitted relationships,
  and `propose` fragmented every page on a second run. Both are fixed. Expect
  the corpus run to find more of that class; that is what it is for.
- `orpheus report` is written and has nothing to compute over.

## What the run has to produce

Not a demo. A number:

```bash
orpheus --db store.sqlite report
```

- **accuracy by confidence level** — does the rubric rank reliability at all?
  If `explicit` is not more often confirmed than `inferred`, the rubric is
  decoration and should be said so out loud.
- **which fields people keep correcting** — where extraction is weakest
- **which rule concepts over-fire**
- **grounding by engine** — how often a model quoted text its document does not
  contain. `alignment` is computed, never taken from the model, so this
  distinguishes a cautious extraction from an invented one.

## Two thresholds it unblocks

Both are module settings and arguments precisely because they need this:

| | |
|---|---|
| `entities.SIMILARITY_THRESHOLD` = 80.0 | rests on ten name pairs chosen by hand |
| `corroboration.COPY_THRESHOLD` = 0.92 | rests on nothing but a stated bias toward calling near-identical text a copy |

Neither is calibrated. After a real corpus, measure and move them — or record
that the hand-picked value held, which is also a result.

## Prerequisites

**A session whose environment has `ANTHROPIC_API_KEY` set.** Environment
variables are read when the container starts, so it has to be set at creation.
Orpheus already reads that variable and resolves the provider as `anthropic` —
no code change.

**A network policy that allows `api.anthropic.com`.** Worth checking: at the
time of writing, `api.anthropic.com` and `generativelanguage.googleapis.com`
were reachable through the agent proxy while `openrouter.ai`, `api.openai.com`
and `huggingface.co` were not. Confirm before assuming.

**The branch.** `claude/python-port`.

## The run

```bash
pip install -e '.[dev,pdf]' && pip install llm-anthropic

orpheus --db store.sqlite init --admin "Your Name"
orpheus --db store.sqlite budget --set-limit 2000000 --window total

python3 -c "
from orpheus.store import Store
s = Store('store.sqlite', mode='write')
s.set_setting('cloud_ai_policy', 'org_allow', '<actor id from init>')
s.close()"

orpheus --db store.sqlite ingest ./corpus \
  --actor-id act_... --extract --tier cloud --engine llm --cloud-opt-in
```

**Use `--engine llm`, not `chat`.** The `chat` engine posts OpenAI-shaped
requests and defaults its base URL to OpenRouter; `llm-anthropic` speaks
Anthropic's native shape. Both go through the same gate, budget and audit.

Then build the wiki and look:

```bash
orpheus --db store.sqlite wiki propose --actor-id act_...
orpheus --db store.sqlite report
orpheus --db store.sqlite lint
orpheus --db store.sqlite graph topology
orpheus --db store.sqlite questions
```

## Three controls, and they are not decoration

1. **The cloud gate** refuses unless the org policy allows it *and* the request
   opts in *and* the budget has room — checked before any text is prepared, so a
   refused call sends nothing.
2. **The budget** is denominated in characters, not currency, because Orpheus
   knows no provider's price list and a cap that silently stops matching the
   invoice is worse than none. `orpheus budget` exits non-zero when spent.
3. **`llm_calls`** records every call, successes and failures alike — a call
   that errored sent its payload just the same. Afterwards, `orpheus --db …
   config` and that table answer *what left this deployment*.

Set a spend limit on the API key in the Anthropic Console too. That one is
enforced by the provider regardless of anything here.

## What a corpus needs to look like

Enough documents to overlap. The interesting findings — corroboration,
conflicts, shared counterparties, the network having any shape at all — all need
the same entity to appear in more than one document. Twenty contracts from one
buyer will say far more than two hundred unrelated ones.

`dev` does not carry a PDF text backend, which is why the install line above
asks for `pdf` too — without it every PDF in the corpus fails at ingest with no
text extracted.

Scanned PDFs are the harder case and the more realistic one for public bodies.
`--ocr` needs Tesseract installed; without it a scan ingests with no text and
the deterministic pass finds nothing, which the run should record rather than
work around.

## Read this before reporting the result

`report` will not say "extraction is good enough". It reports rates, and whether
enough has been reviewed to say anything at all — under a handful of reviews it
says so rather than producing a number. The judgement is a person's.

And the answer being *no* is a real answer. It would mean the local tier and the
deterministic pass carry Phase 1 while extraction improves, and that is worth
knowing before anything is built on top.
