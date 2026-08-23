← [Back to index](index.md)

# Choosing an extraction engine

There is no right answer, which is why the engine is a setting rather than an
architecture. A 205M-parameter encoder running on a laptop CPU and a frontier
model reached over the internet are good at different things, cost different
amounts, and carry different risks — and in a public-sector deployment the risk
difference is frequently the one that decides it.

```
extraction_engine: auto | gliner2 | langextract | chat
```

---

## The floor they all stand on

Every engine's output goes through the same door.

**Grounding is computed, not trusted.** Whatever an engine claims, Orpheus
locates each extracted span in the source document itself
(`orpheus/align.py`) and assigns the confidence rubric from how well it
matched:

| Located as | Rubric level | What it means |
|---|---|---|
| verbatim | `explicit` 1.0 | stated in the document, character for character |
| same words, different typography or spacing | `named` 0.9 | the document says it; the engine retyped it |
| a leading run of the words | `implied` 0.7 | began by quoting, finished by inventing |
| not found | `inferred` 0.5 | the engine asserted something the document does not say |

That last row is the important one. A general model will quote text a document
does not contain, and it will do so fluently. Such a finding is **kept** — a
reviewer should see what was claimed — but it is recorded at `inferred`, not as
something the document says. An engine's own confidence score never sets the
rubric level, because a model's opinion of its own certainty is precisely what
the rubric exists to avoid storing.

The consequence worth stating plainly: **swapping engines changes recall and
cost, not the meaning of the data.** A `0.9` means the same thing whichever
engine produced it.

---

## The three

### `gliner2` — local, extractive, cannot invent

[GLiNER2](https://github.com/fastino-ai/GLiNER2), Apache 2.0. A 205M-parameter
encoder (340M large) that labels spans in text against a schema. It is not
generative: it selects from what is in front of it.

**For:** it runs on a CPU with no API key and no network, which makes it the
only option here that survives an air-gapped deployment intact. Fast and free
per document, so re-running extraction over a whole corpus is a decision about
time rather than budget. Being extractive, **every finding is grounded by
construction** — it cannot produce the `inferred` row above, because it cannot
produce a span that is not in the text. It also does classification and
structured field extraction, so it could serve step 3 as well as step 4.

**Against:** little reasoning. A value stated as a total elsewhere in the
document, an obligation implied by two clauses read together, a date that is
"thirty days after signature" — these are out of reach. Flat schemas suit it;
nesting does not. And a threshold has to be chosen, which trades recall against
noise in a way a reviewer will feel.

**Best when** the documents are formulaic, the properties are named things that
appear on the page, and the deployment cannot or should not send text anywhere.

### `langextract` — a generative model, handled properly

[LangExtract](https://github.com/google/langextract), Google, Apache 2.0.
A library around a generative model that does the work Orpheus would otherwise
have to: chunking long documents, parallel workers, multi-pass extraction for
recall, and its own span alignment.

**For:** it handles a 200-page contract without anyone writing a chunker, and
it reports `alignment_status` per extraction, which lines up with the rubric
without being bent. Runs against Ollama locally or a cloud provider.

**Against:** it needs a model behind it, and the quality is that model's
quality. Locally that means a model large enough to be useful and small enough
to run, which is a real constraint on modest hardware.

**Best when** documents are long or irregular and there is a usable model
available, local or otherwise.

### `chat` — any OpenAI-compatible endpoint

One HTTP shape reaches a lot of places. **OpenRouter** fronts Anthropic, Google
and OpenAI models behind a single endpoint; **Ollama** serves the same shape
locally; **OpenAI** serves it directly. Which one is in use is a base URL and a
model id, both configuration:

```
local_base_url    http://localhost:11434/v1        # Ollama
cloud_base_url    https://openrouter.ai/api/v1     # anything OpenRouter fronts
cloud_model       <the provider's model id>
```

**For:** the capability ceiling. Reasoning about what a clause *means*, reading
a table, resolving "the Supplier" back to a named company — a frontier model is
simply better at these, and switching provider is a settings change rather than
a code change. No dependency beyond the standard library.

**Against:** it will invent quotations, so its output is worth exactly as much
as the grounding check applied to it. No chunking — a long document has to fit
the context window. Per-document cost, and text leaves the building, which is
what the cloud gate is for.

**Best when** the extraction needs judgement, or when you want to compare a
frontier model against a local one on the same corpus.

---

## The gate is never delegated

Every engine that can reach the network is called **through** `orpheus.llm`:

- **Two independent conditions** before any text leaves — the org's
  `cloud_ai_policy`, and this request's `opt_in`. An organisation enabling cloud
  processing is not a person consenting to send *this* document, and collapsing
  the two into one setting silently removes one of the two protections.
- **An `llm_calls` row either way.** The question that log answers is *what left
  this deployment*, and a call that failed sent the payload just the same.
- **A local engine writes no row at all**, because nothing left.

This is not incidental. A library that resolves its own API key and calls its
own provider routes around all three in one step — the same failure identified
for `datasette-llm` in [Datasette ecosystem](datasette-ecosystem.md). The
libraries do the calling; Orpheus decides whether they may.

---

## Choosing

| If the deciding factor is… | Use |
|---|---|
| No network, or data that must not leave | `gliner2` |
| Long or irregular documents | `langextract` |
| Extraction that needs judgement | `chat` against a frontier model |
| Cost per document at corpus scale | `gliner2`, then re-run the uncertain ones with `chat` |
| Not knowing yet | `auto`, then measure |

That last row is the honest one, and Orpheus is built to answer it: run a
corpus through one engine, review it, and read
[`quality_report()`](provenance-and-amendment.md#measuring-extraction-quality)
— accuracy by rubric level, whether the rubric ranks reliability at all, which
fields people keep correcting. Then change one setting and do it again. The
comparison is the point; the engine is a variable in it.

**A two-pass shape is available and not yet built:** run `gliner2` over
everything, then send only the documents whose findings came back thin or
ungrounded to a stronger engine. Cheap where cheap suffices, expensive only
where it buys something. Recorded here rather than built because it should be
decided by measurement, not by argument.

---

## What has actually been run

Being straight about this, because two of the three are written against
documentation rather than against a running library:

| Engine | Status |
|---|---|
| `chat` | **Exercised end to end** against a real HTTP server speaking the OpenAI-compatible shape, including a fenced reply, a hallucinated quotation, and a failed endpoint |
| `langextract` | Installed and exercised for translation and rubric mapping; **no model has been called** — no API key and no Ollama in this environment |
| `gliner2` | Adapter exercised against a fake model; **the real model has never run** — its weights cannot be fetched here (the proxy refuses `huggingface.co`) |
| Docling (parsing, not extraction) | **Never run** — will not build here (`antlr4-python3-runtime`) |

Treat the unexercised paths as untested until someone has run them.

---

[← Back to index](index.md) | [Prior art →](prior-art.md)
