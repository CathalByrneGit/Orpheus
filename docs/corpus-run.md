# The corpus run

The one thing Phase 1 has been waiting on. Everything in this store is built to
answer a single question — **is extraction good enough to build on?**

**The run has now happened**, against Anthropic's API, on six real contracts
filed with the SEC. What it found is in [What the first run found](#what-the-first-run-found)
below. The runbook that follows is still the way to do it again.

---

## What the first run found

Six EX-10 exhibits, 92,551 characters, taken from the [Contract Understanding
Atticus Dataset](https://github.com/TheAtticusProject/cuad) (CC BY 4.0), which
publishes real contracts pulled from EDGAR filings. Two clusters, chosen so the
corpus-level features had something to work on rather than one document each:

- **NETGEAR** — a distributor agreement and its two amendments, one naming
  Ingram Micro. The same parties restated across three documents.
- **ScanSource** — three distributor agreements with different counterparties.

The headline number, from `orpheus report`:

```
ai_cloud: 154/154 quotations located in the document (0% not found)
```

**No quotation was invented.** Every excerpt the model claimed to have taken
from a contract was found in that contract, at a located offset. This is the
one result the whole grounding design exists to produce, and it is the first
time it has been measured against a real model on real text rather than a
stand-in.

Calibration is still open, and honestly so: `insufficient_evidence`, 0 of 154
reviewed. The rubric cannot be checked until a person reviews instances. That
is the next number, and it needs a human, not a bigger corpus.

What worked, on real text:

- **Corroboration told agreement from copying.** NETGEAR as supplier and
  Ingram Micro as buyer were each asserted in three documents in three
  *different* wordings, and counted. Their postal addresses appear in just as
  many documents in one identical wording, and were refused — that is boilerplate
  travelling down an amendment chain, not three sources agreeing.
- **Conflicts surfaced that a reader would want.** Cisco Systems is `supplier`
  in one document and `payer` in another.
- **Relations reached the graph at all**, which they did not before the
  `edges` fix: 172 edges across six documents.

Four defects it found, all now fixed, none of which the test suite could have
caught on its own:

1. The `anthropic` engine asked Anthropic for `gemini-2.5-flash` — the cloud
   tier's default model — and all six documents failed with a 404 that read
   like an outage.
2. Bridge questions rendered each hop in the order the walk reached it, so a
   chain read `NETGEAR employed_by Lloyd Cainey`. The store held the opposite,
   and correctly; the *display* asserted something false, in the one feature
   whose whole rule is not to.
3. `coverage` reported 11% and told the reader to work the wiki queue. Every
   relation between two named things was already drawn; the other 153 joined a
   contract to a clause or a date, which never gets a page. The advice pointed
   at a queue that could not move the number.
4. Report columns cut names to width with no ellipsis, printing `Zebra
   Technologies International, LLC` as `Zebra Technologies Inter`.

A fifth, found by the install rather than the run: `tests/test_ingest.py`
pinned PDF page lengths to 684 and 544 characters, which are `pdftotext`'s
numbers. Installing the documented `[pdf]` extra puts `pdfminer` ahead of it
and moves both by one. The test was green *because* the extra was missing.

---

## What is already true

- Every surface works end to end, driven through a browser against a live
  Datasette: ingest, extract, review, the wiki, tensions, the network,
  corroboration, the reading companion, questions, the markdown export.
- Six real defects have now been found by running real documents rather than
  tests — `edges` unreachable because no engine emitted relationships, `propose`
  fragmenting every page on a second run, and the four above. All are fixed.
  Expect any further corpus to find more of that class; that is what it is for.
- `orpheus report` has something to compute over, and 154 located quotations to
  compute it from.

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

**Documents.** `sec.gov` is blocked by the agent proxy, so EDGAR cannot be
read directly, but CUAD carries real filing exhibits and clones from GitHub:

```bash
GIT_LFS_SKIP_SMUDGE=1 git clone --depth 1 \
  https://github.com/TheAtticusProject/cuad /tmp/cuad
unzip -o /tmp/cuad/data.zip CUADv1.json -d /tmp/cuad
```

`CUADv1.json` holds 510 contracts as `data[].paragraphs[0].context`, titled
`FILER_DATE-EXHIBIT-NAME`. Pick documents that *share parties* — a corpus of six
unrelated contracts leaves corroboration, the network and the tension checks
with nothing to find. The filer prefix is the cheap way to spot clusters, and
an amendment chain is the best single thing to include: the same relationship
restated, which is exactly what corroboration is built to count.

**A network policy that allows `api.anthropic.com`.** Worth checking: at the
time of writing, `api.anthropic.com` and `generativelanguage.googleapis.com`
were reachable through the agent proxy while `openrouter.ai`, `api.openai.com`
and `huggingface.co` were not. Confirm before assuming.

**The branch.** `claude/python-port`.

## The run

```bash
pip install -e '.[dev,pdf,anthropic]'

orpheus --db store.sqlite init --admin "Your Name"
orpheus --db store.sqlite budget --set-limit 2000000 --window total

python3 -c "
from orpheus.store import Store
s = Store('store.sqlite', mode='write')
s.set_setting('cloud_ai_policy', 'org_allow', '<actor id from init>')
s.close()"

orpheus --db store.sqlite ingest ./corpus \
  --actor-id act_... --extract --tier cloud --engine anthropic --cloud-opt-in
```

**Use `--engine llm`.** One dependency reaches every provider the `llm`
plugin ecosystem covers, it is the library underneath `datasette-llm`, and
swapping provider is `pip install llm-<provider>` and a model id:

```bash
pip install llm-anthropic          # or llm-gemini, llm-openrouter, llm-ollama
orpheus --db store.sqlite config --set cloud_llm_model=anthropic/claude-sonnet-5
```

`chat` posts OpenAI-shaped requests and defaults its base URL to OpenRouter,
which is wrong for Anthropic. The `anthropic` engine is for one case `llm`
cannot cover: a key that spans workspaces must name the one it acts in, in an
`anthropic-workspace-id` header on every request, and `llm-anthropic` has no
way to send one. A workspace-scoped key needs no header, so it does not need
that engine.

If the key is identity-linked, the API says so plainly on the first call:

```
400 anthropic-workspace-id is required when authenticating with an
    identity-linked API key
```

There are two ways past it, and either is enough.

**Scope a key to one workspace.** Every key created now is backed by an
identity — a person or a service account — so there is no longer a key type
that avoids this. What decides it is the *workspace* field, set once when the
key is made: Console → Settings → API keys → Create key, then set the
workspace instead of leaving it across all of them. A key created for a
specific workspace only works in that workspace, and requests using it omit
the id entirely. Nothing needs configuring here afterwards.

**Or supply the workspace id.** The id is configuration, not something to
discover from here — the endpoint that lists workspaces needs an admin key,
and the Console's Settings → Workspaces list does not show the *Default*
workspace's id at all. Set it before the run:

```bash
orpheus --db store.sqlite config --set anthropic_workspace_id=wrkspc_...
# or: export ANTHROPIC_WORKSPACE_ID=wrkspc_...
```

All engines go through the same gate, budget and audit.

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

**Why this was not caught by the tests.** `textract` falls back to the
`pdftotext` binary when `pdfminer` is missing, and the container this was
developed in happens to have poppler installed. So the browser e2e has been
ingesting a real PDF successfully all along *through an undeclared system
dependency*, and the missing Python extra was invisible. A container without
poppler fails on the first document.

The lesson generalises past this one line: a green suite in one environment says
nothing about what is declared. If a run fails at ingest, check both — the
extra, and `which pdftotext`.

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
